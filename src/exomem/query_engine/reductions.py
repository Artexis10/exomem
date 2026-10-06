"""Exact grouped reductions: a ready rollup under uniform release, else a bounded base scan.

OpenSpec add-collection-query-engine §12-§13 (CQ11). The planner reads a
declared rollup only when every row of the collection is released to this
caller (uniform release, or a summary collection whose owner-only release is
complete), no field is projected away and a ready rollup keeps exactly the
requested basis, bucket, groups and reductions. Otherwise it reduces the
admitted rows themselves, exactly and within the session's profile bounds, or
refuses. A building rollup is never read, so no answer is stale-complete.

Percentile and distinct count are refused here rather than assembled from
scalar statistics. Every refusal happens before a result exists: there is no
partial aggregate and no continuation. A session runs one execution profile:
interactive by default, or the explicit synchronous analytics profile, whose
whole-call deadline includes admission and result assembly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..collection_store import rollups, typed_storage
from . import ir
from .buckets import EXTREMES, Accumulator, Basis, bucket_key, declared_kind, group_key
from .runtime import QueryError
from .scalars import ScalarValueError, scalar_key

_UNSUPPORTED = frozenset({"percentile", "distinct_count"})
_MAX_DECODE_BYTES = 256 * 1024
#: Rows between deadline and cancellation checks in the Python reduction loop.
_CHECK_EVERY = 512
#: Retained bytes charged for each group's accumulators, beyond held extreme values.
_GROUP_BYTES = 256


@dataclass(frozen=True, slots=True)
class _Shape:
    """The requested reduction, bound to the collection's declared fields."""

    timed: ir.GroupKey | None
    basis: Basis | None
    others: tuple[str, ...]
    sort_kinds: tuple[str, ...]
    fields: tuple[str, ...]
    kinds: tuple[str, ...]
    extremes: tuple[bool, ...]
    latest: bool
    values: tuple[ir.NamedAggregate, ...]


@dataclass(frozen=True, slots=True)
class _Plan:
    strategy: str
    reason: str
    release: str
    rollup: tuple | None = None

    def describe(self, shape: _Shape) -> dict:
        basis = None
        if shape.basis is not None:
            basis = {"field": shape.basis.field, "bucket": shape.timed.bucket, "offset": shape.basis.offset,
                     "kind": shape.basis.kind}
        return {"strategy": self.strategy, "reason": self.reason, "release": self.release,
                "rollup": None if self.rollup is None else self.rollup[1].name, "basis": basis}


def _refuse_request(query, limits) -> None:
    if not isinstance(query, ir.Query) or query.source.domain != "collections" or query.aggregate is None:
        raise QueryError("QUERY_UNSUPPORTED", "only a grouped collection reduction runs here")
    if query.execution_profile != limits.profile:
        raise QueryError("QUERY_PROFILE_UNAVAILABLE", "the request names a profile this session does not run")
    if (query.mode == "compose" or query.joins or query.where is not None or query.having is not None
            or query.text is not None or query.graph is not None or query.page.after is not None
            or sum(key.bucket is not None for key in query.aggregate.groups) > 1):
        raise QueryError("QUERY_UNSUPPORTED", "this reduction shape is not available")
    if any(value.op in _UNSUPPORTED for value in query.aggregate.values):
        raise QueryError("QUERY_UNSUPPORTED", "percentile and distinct count are not assembled from rollups")


def _shape(query, manifest) -> _Shape:
    declared = manifest.schema.fields
    aggregate = query.aggregate
    paths = [key.field.path for key in aggregate.groups]
    paths += [value.field.path for value in aggregate.values if value.field is not None]
    if any(path not in declared for path in paths):
        raise QueryError("QUERY_UNSUPPORTED", "reductions address declared top-level fields")
    timed = next((key for key in aggregate.groups if key.bucket is not None), None)
    basis = None if timed is None else rollups.basis(declared, timed.field.path)
    if timed is not None and basis is None:
        raise QueryError("QUERY_UNSUPPORTED", "a time bucket needs a declared date or datetime field")
    others = tuple(key.field.path for key in aggregate.groups if key.bucket is None)
    fields = tuple(dict.fromkeys(value.field.path for value in aggregate.values if value.field is not None))
    return _Shape(
        timed, basis, others, tuple(declared[name].type for name in others), fields,
        tuple(declared_kind(declared[name].type, declared[name].enum) for name in fields),
        tuple(any(value.op in EXTREMES and value.field.path == name for value in aggregate.values
                  if value.field is not None) for name in fields),
        any(value.op == "latest" for value in aggregate.values), aggregate.values)


def _answers(rollup, shape: _Shape) -> bool:
    if shape.timed is None or rollup.basis != shape.basis or rollup.bucket != shape.timed.bucket:
        return False
    if sorted(rollup.groups) != sorted(shape.others):
        return False
    return all((value.op == "count" and value.field is None) or rollup.reduces(value.field.path, value.op)
               for value in shape.values)


def _plan(session, collection_id: str, shape: _Shape, uniform: bool) -> _Plan:
    if not uniform:
        return _Plan("base", "mixed_release", "admitted")
    if session._project_values is not None:
        return _Plan("base", "fields_projected", "uniform")
    matching = [definition for definition in rollups.definitions(session.connection, collection_id)
                if _answers(definition[1], shape)]
    ready = [definition for definition in matching if definition[2] == "ready"]
    if ready:
        return _Plan("rollup", "rollup_ready", "uniform", ready[0])
    return _Plan("base", "rollup_building" if matching else "no_matching_rollup", "uniform")


def _charge(session, visits: int) -> None:
    if session._estimated_visits + visits > session.limits.max_row_visits:
        raise QueryError("QUERY_COST_LIMIT")
    session._estimated_visits += visits


def _bounded_count(session, sql: str, parameters) -> int:
    return session.connection.execute(f"SELECT count(*) FROM ({sql} LIMIT ?)",  # noqa: S608 - fixed text
                                      (*parameters, session.limits.max_row_visits + 1)).fetchone()[0]


def _group_order(tagged, kinds):
    order = []
    for tag, kind in zip(tagged, kinds, strict=True):
        if tag[0] != "v":
            order.append((0 if tag[0] == "m" else 1,))
            continue
        try:
            typed = scalar_key(tag[1], kind)
        except ScalarValueError:
            typed = (99, "")
        order.append((2, *typed, json.dumps(tag[1])))
    return tuple(order)


def _row(shape: _Shape, bucket, tagged, rows: int, accumulators) -> dict:
    result = {} if shape.timed is None else {shape.timed.field.path: bucket}
    for name, tag in zip(shape.others, tagged, strict=True):
        if tag[0] != "m":
            result[name] = None if tag[0] == "n" else tag[1]
    for value in shape.values:
        if value.op == "count" and value.field is None:
            result[value.name] = rows
        else:
            result[value.name] = accumulators[value.field.path].result(value.op)
    return result


def _page(query, shape: _Shape, groups: list) -> list[dict]:
    """Every group in key order, or a refusal: a truncated group list would be a partial answer."""
    if len(groups) > query.page.limit:
        raise QueryError("QUERY_RESULT_TOO_LARGE", "narrow the window or coarsen the bucket")
    groups.sort(key=lambda group: (group[0] or "", _group_order(group[1], shape.sort_kinds)))
    return [_row(shape, *group) for group in groups]


def _from_rollup(session, limits, shape: _Shape, plan: _Plan) -> tuple[list, int]:
    rollup_id, rollup, _, flagged = plan.rollup
    positions = [rollup.groups.index(name) for name in shape.others]
    groups, retained = [], 0
    for bucket, tagged, rows, accumulators, size in rollups.bucket_states(
            session.connection, rollup_id, limit=limits.max_groups + 1):
        session.check()
        if len(groups) == limits.max_groups:
            raise QueryError("QUERY_GROUP_LIMIT")
        retained += size
        if retained > limits.max_state_bytes:
            raise QueryError("QUERY_COST_LIMIT")
        by_field = dict(zip((name for name, _ in rollup.values), accumulators, strict=True))
        groups.append((bucket, tuple(tagged[index] for index in positions), rows, by_field))
    return groups, flagged


def _scan_sql(collection_id: str, layout, predicate: str, fields: tuple[str, ...]):
    """One statement over admitted rows reading only the reduced fields, plus a row decoder."""
    if layout is None:
        def decode(row):
            if len(row[1]) > _MAX_DECODE_BYTES:
                raise QueryError("QUERY_COST_LIMIT")
            return json.loads(row[1])

        return (f"SELECT i.item_key,i.values_json FROM items i "  # noqa: S608 - internal predicate
                f"WHERE i.collection_id=? AND {predicate}"), decode
    columns = [(name, layout.fields.index(name)) for name in fields if name in layout.fields]
    residual = len(columns) != len(fields)
    selected = "".join(f",t.t{ordinal},t.v{ordinal},t.k{ordinal}" for _, ordinal in columns)
    missing = typed_storage.MISSING

    def decode(row):
        if row[1] != row[2]:
            raise QueryError("QUERY_UNAVAILABLE")
        values = {}
        for index, (name, _) in enumerate(columns, start=1):
            tag = row[3 * index]
            if tag != missing:
                values[name] = typed_storage.decode_value(tag, row[3 * index + 1], row[3 * index + 2])
        if residual and row[-1] is not None:
            values.update(json.loads(row[-1]))
        return values

    return (f"SELECT i.item_key,i.row_version,t.row_version{selected}{',t.r' if residual else ''} "  # noqa: S608
            f"FROM items i LEFT JOIN {layout.current_table} t ON t.row_id=i.row_id "
            f"WHERE i.collection_id=? AND {predicate}"), decode


def _from_rows(session, limits, collection_id: str, shape: _Shape, layout, predicate: str) -> tuple[list, int]:
    timing = () if shape.basis is None else (shape.basis.field, shape.basis.offset)
    read = tuple(dict.fromkeys(name for name in (*shape.fields, *shape.others, *timing) if name is not None))
    sql, decode = _scan_sql(collection_id, layout, predicate, read)
    project = session._project_values
    basis, bucket, timed = shape.basis, None, shape.timed
    reduced = tuple(zip(shape.fields, shape.kinds, shape.extremes, strict=True))
    groups, retained, flagged, visited = {}, 0, 0, 0
    cursor = session.connection.execute(sql, (collection_id,))
    try:
        for row in cursor:
            visited += 1
            if not visited % _CHECK_EVERY:
                session.check()
            try:
                values = decode(row)
            except (typed_storage.TypedStorageError, ValueError, TypeError) as error:
                raise QueryError("QUERY_UNAVAILABLE") from error
            if project is not None:
                values = project(values)
            order = None
            if basis is not None:
                located = basis.locate(values)
                if located is None:
                    flagged += 1
                    continue
                bucket = bucket_key(located[0], timed.bucket)
                order = located[1] if shape.latest else None
            identity = (bucket, rollups.group_text(values, shape.others)) if shape.others else bucket
            group = groups.get(identity)
            if group is None:
                if len(groups) == limits.max_groups:
                    raise QueryError("QUERY_GROUP_LIMIT")
                group = groups[identity] = [bucket, group_key(values, shape.others), 0,
                                            {name: Accumulator() for name in shape.fields}]
                retained += _GROUP_BYTES
            group[2] += 1
            accumulators = group[3]
            for name, kind, extremes in reduced:
                retained += accumulators[name].add(values.get(name), kind, row[0], order, extremes)
            if retained > limits.max_state_bytes:
                raise QueryError("QUERY_COST_LIMIT")
    finally:
        cursor.close()
    session.check()
    if shape.timed is None and not shape.others and not groups:
        # An ungrouped reduction of no rows is one row: zero counts, null values.
        groups[None] = [None, (), 0, {name: Accumulator() for name in shape.fields}]
    return [tuple(group) for group in groups.values()], flagged


def reduce(session, query, *, as_of: str) -> dict:
    """Explain, preview, dry-run or execute one grouped reduction inside ``session``."""
    session.check()
    limits = session.limits
    _refuse_request(query, limits)
    collection_id = query.source.ref
    # Admission proves uniform release, including a summary collection's complete owner release.
    with session._manifest(collection_id) as (manifest, _, _, uniform):
        shape = _shape(query, manifest)
    plan = _plan(session, collection_id, shape, uniform)
    result = {"collection": collection_id, "as_of": as_of, "execution_profile": limits.profile,
              "bounds": limits.bounds(), "plan": plan.describe(shape)}
    if query.mode == "explain":
        return result
    layout = predicate = None
    if plan.strategy == "rollup":
        visits = _bounded_count(session, "SELECT 1 FROM rollup_buckets WHERE rollup_id=?", (plan.rollup[0],))
        _charge(session, visits)
        if visits > limits.max_groups:
            raise QueryError("QUERY_GROUP_LIMIT")
    else:
        # Uniform admission counts rows; mixed admission streams released row ids to bounded temp.
        admitted = session.admit(collection_id)
        visits, layout, predicate = admitted.visible_count, admitted.layout, admitted.membership_sql
    if query.mode == "preview":
        return {**result, "estimated_row_visits": visits}
    if query.mode == "dry_run":
        return {**result, "admitted": True, "estimated_row_visits": visits}
    if plan.strategy == "rollup":
        groups, flagged = _from_rollup(session, limits, shape, plan)
    else:
        groups, flagged = _from_rows(session, limits, collection_id, shape, layout, predicate)
    rows = _page(query, shape, groups)
    session.check()
    return {**result, "groups": rows, "flagged_rows": flagged, "row_visits": visits, "complete": True}
