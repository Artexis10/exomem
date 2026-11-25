"""Answer `query_records` with `timeline`: the spans of one field over the rows that `filters` select.

A selection whose rows hold at most `DIRECT_BOUND` versions is folded from its versions. A larger one reads the
field's span index, and a field whose index is not ready is refused as warming while the store builds it. Both
paths judge a span by the (tag, key) of its value (`timeline_index.keyed`) and take the details of a page from
`timeline.fold`, so they return the same spans, totals and order.
"""

from __future__ import annotations

import datetime as dt
import json
import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import collection_profiles, collection_refs, query_data, record_formats
from .. import structured_collections as collections
from ..governance import egress
from ..query_compat import DEFAULT_OP, OPS, match
from ..query_engine import ir, legacy, scalars
from . import timeline, timeline_index

#: The keys of a `timeline` argument: closed names that the tool schema fixes.
KEYS = frozenset({"field", "value", "at", "from", "to", "changes"})  # nosemgrep: ep-word-set -- tool-schema keys
#: The query arguments that a timeline answer honours; the tool schema fixes these names.
# nosemgrep: ep-word-set -- closed query_records argument names, fixed by the tool schema
COMPANIONS = frozenset({"collection", "filters", "columns", "descending", "limit", "continuation", "timeline"})
#: The query flags whose default (False) a facade may send with any query.
_FLAGS = frozenset({"expand_children", "include_agent_history"})  # nosemgrep: ep-word-set -- tool-schema names
#: The selected rows' summed versions up to which a timeline is folded from the versions (design D7).
DIRECT_BOUND = 20_000
#: How long a warming query waits before it asks again; one build batch takes well under it.
RETRY_AFTER_MS = 2_000
#: Rows read in one statement while `filters` select rows by their current values.
_SELECT_BATCH = 500
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_DAY_US = 86_400 * 1_000_000
_PRESENT_TEXT = (scalars.STRING_TAG, scalars.LINK_TAG)
# Groups of `query_compat.OPS`, the filter grammar's closed operators, as a span predicate applies them.
# nosemgrep: ep-word-set -- closed filter operators fixed by the query grammar
_LISTS, _EQUAL, _UNEQUAL = frozenset({"in", "nin"}), frozenset({"eq", "in"}), frozenset({"ne", "nin"})
# nosemgrep: ep-word-dict -- closed filter operators fixed by the query grammar, with their SQL and Python forms
_ORDERED = {"gt": (">", operator.gt), "gte": (">=", operator.ge), "lt": ("<", operator.lt), "lte": ("<=", operator.le)}
# nosemgrep: ep-word-set -- the reply envelope's keys, fixed by the records spec
_FIELDS = ("collection_id", "snapshot", "timeline", "spans", "returned", "total_matched", "truncated", "continuation")
_SHAPE = ('Send timeline as {"field": a field, "value": {"op": ..., "value": ...}}, with "at" (a date or an RFC 3339 '
          'instant) or with "from" and "to", and "changes": true to list the changes that took effect in the window.')
egress.register_projector("record_timeline", _FIELDS)


def invalid(argument: str, reason: str, remediation: str = _SHAPE) -> collections.CollectionError:
    return collections.CollectionError("TIMELINE_ARGUMENT_INVALID", f"{argument} {reason}",
                                       {"argument": argument, "remediation": remediation})


def conflicts(arguments: Mapping[str, Any]) -> None:
    """Refuse each query argument that a timeline answer does not honour, naming it."""
    offending = sorted(name for name, value in arguments.items()
                       if value is not None and name not in COMPANIONS
                       and not (name == "output_format" and value == "json")
                       and not (name in _FLAGS and value is False))
    if offending:
        raise invalid(offending[0], "is not taken with timeline" + (
            f" (nor {', '.join(offending[1:])})" if offending[1:] else ""),
            "Remove " + ", ".join(offending) + ". A timeline lists spans by effective time and selects rows with "
            "filters only; descending reverses its order.")


@dataclass(frozen=True, slots=True)
class Request:
    field: str
    op: str | None
    literal: Any
    at: str | None
    start: str | None
    end: str | None
    changes: bool
    lo: int | None  # the window's first instant, UTC microseconds, or None when it has no start
    hi: int | None  # the instant after the window, or None when it has no end

    def identity(self) -> dict[str, Any]:
        """What a continuation binds: the request as it was sent."""
        return {"field": self.field, "op": self.op, "value": self.literal, "at": self.at, "from": self.start,
                "to": self.end, "changes": self.changes}

    def window(self) -> dict[str, str | None] | None:
        if self.lo is None and self.hi is None:
            return None
        return {"from": None if self.lo is None else _instant(self.lo),
                "to": None if self.hi is None else _instant(self.hi)}

    def holds(self, start: int, end: int | None) -> bool:
        """Whether a span from `start` to `end` answers: it starts in the window, or overlaps it."""
        if self.hi is not None and start >= self.hi:
            return False
        if self.changes:
            return self.lo is None or start >= self.lo
        return self.lo is None or end is None or end > self.lo or start >= self.lo


def _instant(micros: int) -> str:
    return timeline.instant(_EPOCH + dt.timedelta(microseconds=micros)).text


def _time(argument: str, value: Any) -> timeline.Effective:
    try:
        return timeline.parse(value)
    except ValueError as error:
        raise invalid(argument, "is not a calendar date or an RFC 3339 instant with an offset",
                      "Send a date such as 2026-03-01 or an instant such as 2026-03-01T14:00:00Z.") from error


def parse(value: Any) -> Request:
    """The `timeline` argument, checked for shape; the field and its values are checked against the collection."""
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise invalid("timeline", "must be an object")
    unknown = sorted(set(value) - KEYS)
    if unknown:
        raise invalid(f"timeline.{unknown[0]}", "is not a timeline key")
    field = value.get("field")
    if type(field) is not str or not field:
        raise invalid("timeline.field", "must name a field")
    op = literal = None
    if value.get("value") is not None:
        predicate = value["value"]
        # nosemgrep: ep-word-set -- the predicate's two tool-schema keys
        if not isinstance(predicate, Mapping) or set(predicate) - {"op", "value"}:
            raise invalid("timeline.value", 'must be {"op": ..., "value": ...}')
        op = predicate.get("op", DEFAULT_OP)
        if op not in OPS:
            raise invalid("timeline.value.op", "is not a filter operator",
                          "Use one of " + ", ".join(sorted(OPS)) + ".")
        if op not in ir.PRESENCE_OPS and "value" not in predicate:
            raise invalid("timeline.value.value", f"is required with {op}")
        literal = predicate.get("value")
    at, start, end = value.get("at"), value.get("from"), value.get("to")
    changes = value.get("changes")
    if changes is not None and type(changes) is not bool:
        raise invalid("timeline.changes", "must be true or false")
    changes = changes is True
    if at is not None and (start is not None or end is not None or changes):
        raise invalid("timeline.at", "excludes from, to and changes",
                      "Send at alone for one instant or day, or from and to for a window.")
    if changes and start is None and end is None:
        raise invalid("timeline.changes", "needs from, to or both", "Name the window the changes took effect in.")
    lo = hi = None
    if at is not None:
        moment = _time("timeline.at", at)
        lo, hi = moment.order, moment.order + (1 if moment.at is not None else _DAY_US)
    else:
        if start is not None:
            lo = _time("timeline.from", start).order
        if end is not None:
            moment = _time("timeline.to", end)
            hi = moment.order + (1 if moment.at is not None else _DAY_US)
        if lo is not None and hi is not None and hi <= lo:
            raise invalid("timeline.to", "is before from", "Send a window whose to is at or after its from.")
    for name, bound in (("at" if at is not None else "from", lo), ("at" if at is not None else "to", hi)):
        try:
            if bound is not None:
                _instant(bound)
        except (OverflowError, ValueError) as error:
            raise invalid(f"timeline.{name}", "is outside the calendar a window can name",
                          "Send a time between the years 1 and 9999.") from error
    return Request(field, op, literal, at, start, end, changes, lo, hi)


def field_kind(readable, field: str) -> str:
    """The declared scalar type of the field, refusing a field the caller cannot name or that is not scalar.

    `readable` is the manifest after the caller's field plan, so a withheld field is refused exactly as an
    undeclared one, and the refusal never names the field it was sent.
    """
    root = field.split(".", 1)[0]
    if root not in readable.schema.fields:
        raise collections.CollectionError(
            "QUERY_FIELD_UNKNOWN", "timeline.field names no declared field of this collection",
            {"argument": "timeline.field", "allowed": sorted(readable.schema.fields),
             "remediation": "Name one of the fields in allowed."})
    kind = readable.schema.fields[root].type
    if "." in field or kind not in scalars.SCALAR_TYPES:
        raise collections.CollectionError(
            "TIMELINE_FIELD_NOT_SCALAR", f"{field} is not a top-level scalar field; {root} is of type {kind}",
            {"argument": "timeline.field",
             "remediation": "Name a top-level field of type " + ", ".join(sorted(scalars.SCALAR_TYPES)) + "."})
    return kind


def _predicate(request: Request, kind: str) -> Any:
    """The value predicate's literal, prepared for `_matches` and checked against the field's type."""
    op, literal = request.op, request.literal
    if op is None or op in ir.PRESENCE_OPS:
        return None
    if op in ir.TEXT_OPS:
        if type(literal) is not str:
            raise invalid("timeline.value.value", f"must be a string with {op}")
        return literal
    if op in _LISTS and not isinstance(literal, list):
        raise invalid("timeline.value.value", f"must be a list with {op}")
    keys = []
    for item in literal if op in _LISTS else [literal]:
        try:
            tag, key = scalars.scalar_key(item, kind)
        except scalars.ScalarValueError as error:
            raise invalid("timeline.value.value", f"holds {json.dumps(item)}, which is not a {kind} value",
                          f"Send values of the field's type, {kind}; exists and missing test for an absent "
                          "value.") from error
        if tag in (scalars.MISSING_TAG, scalars.NULL_TAG):
            raise invalid("timeline.value.value", "holds null, which compares with nothing",
                          "Use op exists or missing to test for an absent value.")
        keys.append((tag, key))
    return frozenset(keys) if op in _EQUAL | _UNEQUAL else keys[0]


def _matches(tag: int, key: str, op: str | None, prepared: Any) -> bool:
    """One span value against the value predicate, as `row_predicates` decides a current value."""
    if op is None:
        return True
    present = tag not in (scalars.MISSING_TAG, scalars.NULL_TAG)
    if op in ir.PRESENCE_OPS:
        exists = present and not (tag in _PRESENT_TEXT and key == "")
        return exists if op == "exists" else not exists
    if op in ir.TEXT_OPS:
        return tag in _PRESENT_TEXT and match({"value": bytes.fromhex(key).decode("utf-8")},
                                              {"column": "value", "op": op, "value": prepared})
    if not present:
        return False
    if tag == timeline_index.UNKEYED_TAG:
        return op in _UNEQUAL
    if op in _EQUAL:
        return (tag, key) in prepared
    if op in _UNEQUAL:
        return (tag, key) not in prepared
    literal_tag, literal_key = prepared
    return tag == literal_tag and _ORDERED[op][1](key, literal_key)


def _narrowed(op: str | None, prepared: Any) -> tuple[str, list] | None:
    """An index condition that keeps every span the predicate can keep: only values that are present match it."""
    if op in _EQUAL:
        if not prepared:
            return "0", []
        pairs = sorted(prepared)
        return ("(value_tag,value_key) IN (VALUES " + ",".join("(?,?)" for _ in pairs) + ")",
                [part for pair in pairs for part in pair])
    if op in _ORDERED:
        tag, key = prepared
        return f"value_tag=? AND value_key{_ORDERED[op][0]}?", [tag, key]
    return None


class _Shown:
    """A link field's values as the caller sees them: a link the caller may not follow reads as absent."""

    def __init__(self, field: str, links) -> None:
        self.field, self.links, self.keys = field, links, {}

    def key(self, tag: int, key: str) -> tuple[int, str]:
        if self.links is None or tag != scalars.LINK_TAG:
            return tag, key
        if (tag, key) not in self.keys:
            shown = self.value(bytes.fromhex(key).decode("utf-8"))
            self.keys[(tag, key)] = (tag, key) if shown is not timeline.MISSING else (scalars.MISSING_TAG, "")
        return self.keys[(tag, key)]

    def value(self, value: Any) -> Any:
        if self.links is None or type(value) is not str:
            return value
        return value if self.links({self.field: value}).get(self.field) is not None else timeline.MISSING


def _links(writer, manifest, field: str, filters, columns):
    """The caller's link projector, when the field, a filter or a column can read a link."""
    from .. import record_governance

    carrying = {name for name, spec in manifest.schema.fields.items()
                if record_governance._LinkProjector.carries_links(spec)}
    named = {field} | {str(predicate.get("column", "")).split(".", 1)[0] for predicate in filters or ()
                       if isinstance(predicate, Mapping)} | set(columns or ())
    if not carrying & named:
        return None
    return record_governance._LinkProjector.create(
        writer.root, manifest, policy=writer._operation.policy, allow_cold_index=True,
        authorize_path=writer._operation.allows_file, visible_path=None)


def _current(writer, manifest, row_ids: list[int], snapshot: str, links) -> list[tuple[dict, dict]]:
    """Each row's `items` record and its current values as the caller reads them, with the system columns."""
    operation = writer._operation
    cursor = writer.connection.execute(
        "SELECT * FROM items WHERE row_id IN (SELECT value FROM json_each(?)) ORDER BY row_id", (json.dumps(row_ids),))
    names = [column[0] for column in cursor.description]
    items, _ = operation.field_rows(manifest.collection_id, [dict(zip(names, row, strict=True)) for row in cursor],
                                    snapshot)
    key_column = collection_profiles.profile_for(manifest.semantic_profile).item_id_property
    found = []
    for item in items:
        values = json.loads(item["values_json"])
        if links is not None:
            values = links(values)
        version = item.get("public_version") or writer._version(item)
        found.append((item, {**values, "collection_id": manifest.collection_id, key_column: item["item_key"],
                             "item_version": version}))
    return found


def _selection(writer, manifest, filters, released: Callable[[int, str], bool], snapshot: str,
               links) -> list[tuple[int, int]]:
    """(row_id, row_version) of each current row the caller may read that `filters` select, by row id."""
    rows = [(row_id, version) for row_id, key, version in writer.connection.execute(
        "SELECT row_id,item_key,row_version FROM items WHERE collection_id=? ORDER BY row_id",
        (manifest.collection_id,)) if released(row_id, key)]
    if not filters:
        return rows
    strings = {name for name, spec in manifest.schema.fields.items() if spec.type == "string"}
    checks = [(predicate, predicate["column"] in strings)
              for predicate in json.loads(legacy.normalize(columns_available=(), filters=filters).filters_json)]
    versions = dict(rows)
    kept = []
    for offset in range(0, len(rows), _SELECT_BATCH):
        for item, values in _current(writer, manifest, [row for row, _ in rows[offset:offset + _SELECT_BATCH]],
                                     snapshot, links):
            if all(match(values, predicate, as_string=as_string) for predicate, as_string in checks):
                kept.append((item["row_id"], versions[item["row_id"]]))
    return kept


def _warming(writer, manifest, field: str) -> collections.CollectionError:
    timeline_index.request(writer.handle.path, manifest.collection_id, field)
    return collections.CollectionError(
        "TIMELINE_WARMING", f"the {field} timeline index is building for this selection",
        {"retry_after_ms": RETRY_AFTER_MS,
         "progress": timeline_index.progress(writer.connection, manifest.collection_id, field),
         "remediation": "Retry the same query after retry_after_ms. A filter that selects fewer rows is answered "
                        f"now: a selection whose rows hold up to {DIRECT_BOUND} versions reads them directly."})


def _whole(writer, cid: str, row_ids: list[int]) -> set[int]:
    """The page rows that the caller reads whole, decided from one read of their subjects."""
    from . import governance

    operation = writer._operation
    if not row_ids or not operation.holds_every_field(operation.field_manifest(cid)):
        return set()
    subjects = {subject.row_id: subject for subject in governance.iter_subjects(
        writer.connection, cid, operation.logical_vault_id, row_ids=row_ids)}
    return {row_id for row_id in row_ids if operation.reads_whole_row(cid, subjects.get(row_id))}


def _provenance(writer, spans: list[tuple[int, timeline.Span]]) -> tuple[dict[int, str | None], dict]:
    """The `why` of each span's transaction and the sources of its version, one statement each."""
    conn = writer.connection
    whys = dict(conn.execute("SELECT txn_id,why FROM txns WHERE txn_id IN (SELECT value FROM json_each(?))",
                             (json.dumps(sorted({span.start.txn_id for _, span in spans})),)))
    sources: dict[tuple[int, int], list[str]] = {}
    pairs = json.dumps(sorted({(row_id, span.start.row_version) for row_id, span in spans}))
    for row_id, row_version, ref in conn.execute(
            "SELECT s.row_id,s.row_version,s.source_ref FROM json_each(?) p JOIN item_sources s "
            "ON s.row_id=json_extract(p.value,'$[0]') AND s.row_version=json_extract(p.value,'$[1]') "
            "ORDER BY s.row_id,s.row_version,s.ordinal", (pairs,)):
        sources.setdefault((row_id, row_version), []).append(ref)
    return whys, sources


def read(writer, readable, request: Request, *, filters=None, columns=None, descending=False, limit=None,
         continuation=None) -> dict[str, Any]:
    """The page of spans that answers `request`, bound to the caller's snapshot of the collection."""
    from .. import record_governance

    operation, conn, cid = writer._operation, writer.connection, readable.collection_id
    kind = field_kind(readable, request.field)
    prepared = _predicate(request, kind)
    try:
        record_formats._validate_query_projection_fields(readable, selected_child=None, filters=filters,
                                                         columns=columns, sort_by=None, aggregate=None,
                                                         date_column=None)
    except collections.CollectionError as error:
        if error.code != "INVALID_RECORD_QUERY_FIELD":
            raise
        raise record_governance._with_readable_columns(error, readable, {}) from error
    limit = query_data._bounded_limit(limit)
    released, snapshot = operation.row_release(cid)
    identity = {"timeline": request.identity(), "filters": filters, "columns": columns,
                "descending": bool(descending), "limit": limit}
    offset = record_formats._continuation_offset(continuation, readable, snapshot, identity)
    links = _links(writer, readable, request.field, filters, columns)
    shown = _Shown(request.field, links if kind == "link" else None)
    rows = _selection(writer, readable, filters, released, snapshot, links)
    details: dict[tuple[int, int, int], timeline.Span] = {}
    if sum(version for _, version in rows) <= DIRECT_BOUND:
        source, entries = "versions", []
        for row_id, span, tag, key in timeline_index.folded(conn, request.field, kind, [row for row, _ in rows]):
            start_at, start_seq = span.start.order
            details[(row_id, start_at, start_seq)] = span
            entries.append((start_at, start_seq, row_id, None if span.end is None else span.end.order[0], tag, key))
    else:
        field_key = timeline_index.ready(conn, readable, request.field)
        if field_key is None:
            raise _warming(writer, readable, request.field)
        source, selected = "index", {row for row, _ in rows}
        entries = [entry for entry in timeline_index.candidates(
            conn, field_key, lo=request.lo, hi=request.hi, changes=request.changes,
            narrowed=_narrowed(request.op, prepared)) if entry[2] in selected]
    kept = sorted((entry for entry in entries if request.holds(entry[0], entry[3])
                   and _matches(*shown.key(entry[4], entry[5]), request.op, prepared)),
                  key=lambda entry: entry[:3], reverse=bool(descending))
    page = kept[offset:offset + limit]
    page_rows = sorted({entry[2] for entry in page})
    if source == "index":
        for row_id, span, _, _ in timeline_index.folded(conn, request.field, kind, page_rows):
            details[(row_id, *span.start.order)] = span
    try:
        spans = [(entry[2], details[(entry[2], entry[0], entry[1])]) for entry in page]
    except KeyError as error:
        raise RuntimeError("the timeline index disagrees with the versions it was built from") from error
    current = {item["row_id"]: values for item, values in _current(writer, readable, page_rows, snapshot, links)}
    whole = _whole(writer, cid, page_rows)
    whys, sources = _provenance(writer, [(row_id, span) for row_id, span in spans if row_id in whole])
    rendered = []
    for row_id, span in spans:
        values = current[row_id]
        found: dict[str, Any] = {"id": collection_refs.row_id(readable.semantic_profile, values)}
        value = shown.value(span.value)
        found.update({"missing": True} if value is timeline.MISSING else {"value": value})
        if span.first:
            found["first"] = True
        else:
            previous = shown.value(span.previous)
            found.update({"previous_missing": True} if previous is timeline.MISSING else {"previous": previous})
        found.update({"effective_from": span.start.effective.text,
                      "effective_to": None if span.end is None else span.end.effective.text,
                      "effective_basis": span.start.basis, "written_at": span.start.written_at})
        if row_id in whole:
            refs = sources.get((row_id, span.start.row_version), [])
            readable_refs = [ref for ref in refs if operation.allows_history_path(ref)]
            found.update({"transition_id": span.start.transition_id, "why": whys.get(span.start.txn_id),
                          "sources": readable_refs,
                          **({"sources_withheld": len(refs) - len(readable_refs)} if len(refs) > len(readable_refs)
                             else {})})
        if columns is not None:
            found["columns"] = {name: values[name] for name in columns if name in values}
        rendered.append(found)
    rendered, _ = query_data._bounded_response_rows(rendered, overhead=1024)
    returned = len(rendered)
    truncated = offset + returned < len(kept)
    result = {
        "collection_id": cid, "snapshot": snapshot,
        "timeline": {"field": request.field, "window": request.window(), "source": source},
        "spans": rendered, "returned": returned, "total_matched": len(kept), "truncated": truncated,
        "continuation": record_formats._encode_continuation(
            {"collection_id": cid, "snapshot": snapshot, "query": identity, "offset": offset + returned})
        if truncated else None,
    }
    return egress.project(record_governance._RecordEnvelope(result), egress.LEVEL_FULL, kind="record_timeline") or {}
