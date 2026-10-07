"""Bounded SQL execution of the admitted legacy collection dialect.

No public route accepts this module's SQL or admission handles. Typed v1
execution remains unavailable until its field and physical-plan gates pass.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager

from .. import query_compat as compat
from .. import query_data
from .legacy import LegacyQuery
from .runtime import AdmittedCollection, QueryError

# Capability admission is deliberately explicit: expanding the legacy dialect
# does not automatically enable a new SQL push-down without its parity proof.
LEGACY_SQL_OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "contains",
                                 "icontains", "startswith", "in", "nin", "exists", "missing"})


def execute_legacy(admitted: AdmittedCollection, query: LegacyQuery, *, path: str,
                   format: str) -> query_data.QueryDataResult:
    """Read one admitted source, never a bare IR or a caller release flag."""
    if not isinstance(admitted, AdmittedCollection):
        raise QueryError("QUERY_UNSUPPORTED")
    admitted.check()
    if not isinstance(query, LegacyQuery) or query.compatibility_version != compat.VERSION:
        raise QueryError("QUERY_UNSUPPORTED")
    session = admitted.session
    requested = set(query.columns) | {p["column"] for p in json.loads(query.filters_json)}
    requested.update(name for name in (query.sort_by, query.date_column) if name)
    if query.aggregate and ":" in query.aggregate:
        requested.add(query.aggregate.split(":", 1)[1])
    plan = session._field_plans[admitted.collection_id]
    if not plan.owner and any(not plan.admits_path(name) for name in requested):
        raise QueryError("QUERY_FIELD_UNAVAILABLE")
    # Account for base admission/filtering and the bounded reduction passes.
    passes = 1 + (len(query.available) if query.aggregate and query.aggregate.strip() == "profile" else 1)
    if admitted.visible_count * passes > session.limits.max_row_visits:
        raise QueryError("QUERY_COST_LIMIT")
    warnings = list(query.warnings)
    with _matched(admitted, query) as total:
        if query.aggregate:
            aggregate = _reduce(session, query, total, path, format, admitted.input_order)
            session.check()
            aggregate, truncated = query_data._bounded_aggregate(aggregate)
            if truncated:
                warnings.append("response size cap truncated aggregate")
            session.check()
            return query_data.QueryDataResult(path, format, admitted.visible_count, total, 0,
                                             list(query.available), [], aggregate, truncated, warnings)
        order = admitted.input_order
        if query.sort_by:
            direction = "DESC" if query.descending else "ASC"
            order = f"i.sort_key COLLATE exomem_legacy_order {direction}, {order}"
        cursor = session.connection.execute(
            f"SELECT i.values_json FROM temp.exomem_legacy_rows i ORDER BY {order} LIMIT ? OFFSET ?",
            (query.limit, query.offset),
        )
        rows, size = [], 2
        byte_truncated = False
        with closing(session.fetch(cursor)) as batches:
            for batch in batches:
                for (raw,) in batch:
                    session.check()
                    value = json.loads(raw)
                    row = {field: compat.get_field(value, field) for field in query.columns} if query.columns else value
                    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()
                    if size + len(encoded) + bool(rows) > query_data.MAX_RESPONSE_BYTES:
                        byte_truncated = True
                        break
                    size += len(encoded) + bool(rows)
                    rows.append(row)
                if byte_truncated:
                    break
        if byte_truncated:
            warnings.append("response size cap truncated returned rows")
        session.check()
        return query_data.QueryDataResult(path, format, admitted.visible_count, total, len(rows),
                                         list(query.columns or query.available), rows,
                                         truncated=byte_truncated or query.offset + len(rows) < total,
                                         warnings=warnings)


@contextmanager
def _matched(admitted, query):
    """A bounded disk relation; indexes exist before data, so no uncapped sorter."""
    session, conn = admitted.session, admitted.session.connection
    predicates = json.loads(query.filters_json)
    if any(predicate.get("op", "eq") not in LEGACY_SQL_OPERATORS for predicate in predicates):
        raise QueryError("QUERY_UNSUPPORTED")

    def matches(raw):
        session.check()
        if raw is None:
            return 0
        row = json.loads(raw)
        result = int(all(compat.match(row, predicate) for predicate in predicates))
        session.check()
        return result

    def order(left, right):
        session.check()
        a, b = tuple(json.loads(left)), tuple(json.loads(right))
        return (a > b) - (a < b)

    conn.create_function("exomem_legacy_matches", 1, matches, deterministic=True)
    conn.create_collation("exomem_legacy_order", order)
    try:
        conn.execute("CREATE TEMP TABLE exomem_legacy_rows(row_id INTEGER PRIMARY KEY, "
                     "created_txn INTEGER, view_path TEXT, values_json TEXT NOT NULL, sort_key TEXT)")
        input_order = admitted.input_order.replace("i.", "")
        conn.execute(f"CREATE INDEX temp.exomem_legacy_input ON exomem_legacy_rows({input_order})")
        if query.sort_by:
            direction = "DESC" if query.descending else "ASC"
            conn.execute("CREATE INDEX temp.exomem_legacy_sorted ON exomem_legacy_rows("
                         f"sort_key COLLATE exomem_legacy_order {direction}, {input_order})")
        cursor = conn.execute(
            f"SELECT {admitted.values_sql}, i.row_id,i.created_txn,i.view_path FROM items i "
            f"WHERE i.collection_id=? AND {admitted.membership_sql} "
            f"AND exomem_legacy_matches({admitted.values_sql})", (admitted.collection_id,),
        )
        total = 0
        with closing(session.fetch(cursor)) as batches:
            for batch in batches:
                for raw, row_id, created, view_path in batch:
                    session.check()
                    key = None
                    if query.sort_by:
                        key = json.dumps(compat.sort_key(compat.get_field(json.loads(raw), query.sort_by)),
                                         ensure_ascii=False)
                    conn.execute("INSERT INTO temp.exomem_legacy_rows VALUES(?,?,?,?,?)",
                                 (row_id, created, view_path, raw, key))
                    total += 1
                session.check_temp()
        yield total
    finally:
        # If interruption prevents DDL, the owning session closes its private
        # connection; neither membership nor result relations can be reused.
        try:
            conn.execute("DROP TABLE IF EXISTS temp.exomem_legacy_rows")
        except sqlite3.Error:
            pass
        conn.create_function("exomem_legacy_matches", 1, None)
        conn.create_collation("exomem_legacy_order", None)


def _rows(session, order):
    cursor = session.connection.execute(
        f"SELECT i.values_json FROM temp.exomem_legacy_rows i ORDER BY {order}",
    )
    with closing(session.fetch(cursor)) as batches:
        for batch in batches:
            for (raw,) in batch:
                session.check()
                yield json.loads(raw)


@contextmanager
def _keys(session):
    """Distinct/group state is disk-backed, never an unbounded Python set."""
    conn = session.connection
    try:
        conn.execute("CREATE TEMP TABLE exomem_legacy_keys(key TEXT PRIMARY KEY, "
                     "value TEXT NOT NULL, display_key TEXT NOT NULL, count INTEGER NOT NULL) WITHOUT ROWID")
        conn.execute("CREATE INDEX temp.exomem_legacy_groups ON exomem_legacy_keys(display_key)")
        yield conn
    finally:
        try:
            conn.execute("DROP TABLE IF EXISTS temp.exomem_legacy_keys")
        except sqlite3.Error:
            pass


def _reduce(session, query, total, path, format, order):
    spec = query.aggregate.strip()
    if spec == "count":
        return {"count": total}
    # The input index preserves first occurrence and floating-point reduction
    # order, independent of a filter's physical access path.
    if spec == "profile":
        columns = [_profile(session, field, order) for field in query.available]
        profile = {"path": path, "format": format, "total_rows": total, "columns": columns}
        return {"profile": profile, "dataset_card": query_data.build_dataset_card(profile)}
    if ":" not in spec:
        raise compat.QueryDataError("BAD_AGGREGATE", "aggregate must be 'count' or 'func:column' "
                                   "(func in min,max,sum,avg,latest,distinct)")
    func, field = (part.strip() for part in spec.split(":", 1))
    if func in {"min", "max", "sum", "avg"}:
        with closing(_rows(session, order)) as rows:
            return compat.numeric_aggregate(rows, func, field)
    if func == "latest":
        with closing(_rows(session, order)) as rows:
            return compat.latest_aggregate(rows, query.date_column or field)
    if func not in {"distinct", "group"}:
        raise compat.QueryDataError("BAD_AGGREGATE", f"unknown aggregate func {func!r}")
    out, distinct = [], 0
    with _keys(session) as conn, closing(_rows(session, order)) as rows:
        for row in rows:
            value = compat.get_field(row, field)
            key = compat.distinct_key(value) if func == "distinct" else compat.group_key(value)
            encoded = json.dumps(value, ensure_ascii=False)
            if func == "distinct":
                fresh = conn.execute("INSERT OR IGNORE INTO temp.exomem_legacy_keys VALUES(?,?,?,1)",
                                     (key, encoded, encoded)).rowcount
                if fresh:
                    distinct += 1
                    if len(out) < query_data.PROFILE_MAX_DISTINCT:
                        out.append(value)
            else:
                conn.execute("INSERT INTO temp.exomem_legacy_keys VALUES(?,?,?,1) "
                             "ON CONFLICT(key) DO UPDATE SET count=count+1,value=excluded.value,"
                             "display_key=excluded.display_key", (key, encoded, encoded))
            session.check_temp()
        if func == "distinct":
            return {"distinct": out, "n": distinct, "truncated": distinct > len(out)}
        count = conn.execute("SELECT count(*) FROM temp.exomem_legacy_keys").fetchone()[0]
        cursor = conn.execute("SELECT value,count FROM temp.exomem_legacy_keys ORDER BY display_key LIMIT ?",
                              (query_data.PROFILE_MAX_DISTINCT,))
        groups = [{"value": json.loads(raw), "count": n}
                  for batch in session.fetch(cursor) for raw, n in batch]
        return {"groups": groups, "n": count, "truncated": count > len(groups)}


def _profile(session, field, order):
    non_null = numeric = date_like = distinct = 0
    numeric_min = numeric_max = earliest = latest = None
    samples = []
    with _keys(session) as conn, closing(_rows(session, order)) as rows:
        def numbers():
            nonlocal non_null, numeric, date_like, distinct, numeric_min, numeric_max, earliest, latest
            for row in rows:
                value = compat.get_field(row, field)
                if value in (None, ""):
                    continue
                non_null += 1
                text = str(value)
                date_like += bool(compat.DATE_LIKE.search(text))
                earliest = text if earliest is None else min(earliest, text)
                latest = text if latest is None else max(latest, text)
                encoded = json.dumps(value, ensure_ascii=False)
                if conn.execute("INSERT OR IGNORE INTO temp.exomem_legacy_keys VALUES(?,?,?,1)",
                                (text, encoded, encoded)).rowcount:
                    distinct += 1
                    if len(samples) < query_data.PROFILE_MAX_DISTINCT:
                        samples.append(value)
                session.check_temp()
                number = compat.coerce_num(value)
                if number is not None:
                    numeric += 1
                    numeric_min = number if numeric_min is None else min(numeric_min, number)
                    numeric_max = number if numeric_max is None else max(numeric_max, number)
                    yield number
        total = sum(numbers())
    kind = compat.profile_kind(field, non_null, numeric, date_like, distinct, query_data.PROFILE_MAX_DISTINCT)
    kwargs = {"distinct_truncated": distinct > len(samples)}
    if kind == "date":
        kwargs.update(earliest=earliest, latest=latest)
    elif kind == "numeric":
        kwargs.update(min=numeric_min, max=numeric_max, sum=total, avg=total / numeric)
    else:
        kwargs["top_values"] = samples
    return query_data.ColumnProfile(field, kind, non_null, distinct, **kwargs).as_dict()
