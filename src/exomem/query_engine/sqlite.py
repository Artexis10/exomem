"""Bounded execution of the admitted legacy collection dialect.

The engine reads only the fields a query depends on, decides every filter, sort and aggregate
with the dialect's own primitives (`query_compat`), so its answers equal the file evaluator's by
construction, and returns which rows answer the query rather than their values: a caller
rebuilds only the rows it returns. No public route accepts this module's admission handles.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .. import query_compat as compat
from .. import query_data
from . import packed
from .legacy import LegacyQuery
from .runtime import _MAX_DECODE_BYTES, AdmittedCollection, QueryError

#: Rows a scan reads in its first statement; each next statement reads twice as many, up to
#: `_MAX_CHUNK`, until one packed result passes the session's length limit.
_FIRST_CHUNK = 256
_MAX_CHUNK = 8192


@dataclass(frozen=True, slots=True)
class LegacySelection:
    """Which admitted rows answer one legacy query."""

    total_rows: int
    total_matched: int
    row_ids: tuple[int, ...]  # the page, in answer order; empty for an aggregate
    aggregate: dict | None  # a `latest` answer carries "row": None; `latest_row_id` names its row
    latest_row_id: int | None
    leading_row_ids: tuple[int, ...]  # the first ``lead`` admitted rows in input order
    warnings: tuple[str, ...]


def execute_legacy(admitted: AdmittedCollection, query: LegacyQuery, *, path: str, format: str,
                   lead: int = 0) -> LegacySelection:
    """Select the answer of one admitted source, never a bare IR or a caller release flag."""
    if not isinstance(admitted, AdmittedCollection):
        raise QueryError("QUERY_UNSUPPORTED")
    admitted.check()
    if not isinstance(query, LegacyQuery) or query.compatibility_version != compat.VERSION:
        raise QueryError("QUERY_UNSUPPORTED")
    session = admitted.session
    predicates = json.loads(query.filters_json)
    spec = query.aggregate.strip() if query.aggregate else None
    function, column = (spec.split(":", 1) + [None])[:2] if spec else (None, None)
    function = function.strip() if function else None
    column = column.strip() if column else None
    requested = set(query.columns) | {predicate["column"] for predicate in predicates}
    requested.update(name for name in (query.sort_by, query.date_column, column) if name)
    plan = session._field_plans[admitted.collection_id]
    if not plan.owner and any(not plan.admits_path(name) for name in requested):
        raise QueryError("QUERY_FIELD_UNAVAILABLE")
    if spec == "profile" and admitted.visible_count * (1 + len(query.available)) > session.limits.max_row_visits:
        raise QueryError("QUERY_COST_LIMIT")
    # Only the values a decision reads: filter, sort and aggregate roots, never the page's columns.
    roots = {predicate["column"] for predicate in predicates}
    if spec == "profile":
        roots.update(query.available)
    elif function == "latest":
        roots.add(query.date_column or column)
    elif column:
        roots.add(column)
    if query.sort_by and not spec:
        roots.add(query.sort_by)
    fields = tuple(name for name in admitted.fields if name in {str(root).split(".", 1)[0] for root in roots})

    tests = [(predicate, predicate["column"] in query.string_columns) for predicate in predicates]
    match, get_field, sort_key = compat.match, compat.get_field, compat.sort_key
    leading = _Smallest(lead)
    page = _Smallest(0 if spec else query.offset + query.limit)
    matched_rows = [] if spec and spec != "count" else None
    total = matched = 0
    for row_id, key, values in scan(admitted, fields):
        total += 1
        if lead:
            leading.push((key, row_id))
        if not all(match(values, predicate, as_string=as_string) for predicate, as_string in tests):
            continue
        matched += 1
        if matched_rows is not None:
            matched_rows.append((key, row_id, values))
        elif not spec:
            if query.sort_by:
                order = sort_key(get_field(values, query.sort_by))
                page.push((_Descending(order) if query.descending else order, key, row_id))
            else:
                page.push((key, row_id))
        if total % 1024 == 0:
            session.check()

    aggregate, latest = None, None
    if spec == "count":
        aggregate = {"count": matched}
    elif spec:
        # The file evaluator reduces in input order: float sums, first occurrences and ties depend on it.
        matched_rows.sort(key=lambda entry: entry[0])
        rows = [values for _, _, values in matched_rows]
        if spec == "profile":
            aggregate = query_data._profile_payload(rows, list(query.available), format, path)
        else:
            aggregate = query_data._aggregate(rows, spec, query.date_column)
        session.check()
        if function == "latest":
            best = aggregate["row"]
            latest = next((row_id for _, row_id, values in matched_rows if values is best), None)
            aggregate = {**aggregate, "row": None}
    selected = page.result()[query.offset:]
    return LegacySelection(total, matched, tuple(entry[-1] for entry in selected), aggregate, latest,
                           tuple(entry[-1] for entry in leading.result()), tuple(query.warnings))


class _Descending:
    """A sort key in reverse; ties still fall to the input order that follows it."""

    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key

    def __eq__(self, other):
        return self.key == other.key

    def __lt__(self, other):
        return other.key < self.key


class _Smallest:
    """The ``size`` smallest entries pushed, which equals sorting all of them and taking the head."""

    __slots__ = ("size", "entries", "bound")

    def __init__(self, size: int):
        self.size, self.entries, self.bound = size, [], None

    def push(self, entry) -> None:
        if not self.size or (self.bound is not None and not entry < self.bound):
            return
        self.entries.append(entry)
        if len(self.entries) >= 2 * self.size:
            self._trim()

    def _trim(self) -> None:
        self.entries.sort()
        del self.entries[self.size:]
        if self.size and len(self.entries) == self.size:
            self.bound = self.entries[-1]

    def result(self) -> list:
        self._trim()
        return self.entries


def scan(admitted: AdmittedCollection, fields: tuple[str, ...]):
    """Yield ``(row_id, input key, values)`` of every admitted row, in no particular order.

    ``values`` holds only ``fields``, after the session's projection. A withheld row is excluded
    before any of its values is read.
    """
    from .reductions import scan_parts

    admitted.check()
    session, conn = admitted.session, admitted.session.connection
    project = session._project_values
    plan = session._field_plans[admitted.collection_id]
    if not fields:
        columns, source = [], f"{admitted.items} i"

        def decode(cells):
            return {}
    elif admitted.layout is None and plan.owner:
        # The owner reads its stored document whole: one JSON decode beats a field-tree walk per row.
        columns = [f"CASE WHEN length(CAST(i.values_json AS BLOB)) <= {_MAX_DECODE_BYTES} THEN i.values_json END"]
        source = f"{admitted.items} i"

        def decode(cells):
            if cells[0] is None:
                raise QueryError("QUERY_COST_LIMIT")
            stored = json.loads(cells[0])
            return {name: stored[name] for name in fields if name in stored}
    else:
        columns, source, decode = scan_parts(session, admitted.collection_id, admitted.layout, fields)
    key = admitted.input_key
    head = ("item_key", "view_path", "created_txn", "row_id")
    # Row id order walks items_by_collection_row and both tables in storage order; item key order
    # reads them at random, about three times slower once a collection outgrows the page cache.
    select = (f"SELECT {','.join([*(f'i.{name}' for name in head), *columns])} FROM {source} "  # noqa: S608
              f"WHERE i.collection_id=? AND {admitted.membership_sql} AND i.row_id {{}} ? "
              "ORDER BY i.row_id LIMIT ?")
    width, size, after, grow = len(head) + len(columns), _FIRST_CHUNK, None, True
    while True:
        session.check()
        try:
            found = packed.rows(conn, select.format(">=" if after is None else ">"),
                                (admitted.collection_id, -1 << 63 if after is None else after, size), width)
        except sqlite3.Error as error:
            if not packed.too_big(error) or size == 1:
                raise
            size, grow = size // 2, False
            continue
        for row in found:
            values = decode(row[len(head):])
            yield row[3], key({"item_key": row[0], "view_path": row[1], "created_txn": row[2], "row_id": row[3]}), \
                values if project is None else project(values)
        if len(found) < size:
            session.check()
            return
        after = max(row[3] for row in found)
        if grow:
            size = min(2 * size, _MAX_CHUNK)
