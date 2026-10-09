"""Exact grouped reductions: a ready rollup under uniform release, else a bounded base scan.

OpenSpec add-collection-query-engine §12-§13 (CQ11). The planner reads a
declared rollup only when every row of the collection is released to this
caller (uniform release, or a summary collection whose owner-only release is
complete), no field is projected away and a ready rollup keeps exactly the
requested basis, bucket, groups and reductions, and the requested local-day
window covers whole buckets. Otherwise it reduces the admitted rows
themselves, exactly and within the session's profile bounds, or refuses. A
building rollup is never read, so no answer is stale-complete.

Groups come in pages ordered by (bucket, exact group identity), the rollup
primary-key order. A page holds whole groups only: it stops at the page limit
or the whole-result byte cap and then carries a cursor bound to the query,
caller, manifest version, frozen ``as_of`` and the collection's freshness
basis. A mixed release has no such basis, so an answer longer than one page
is refused there rather than continued unbound. Percentile and
distinct count are refused rather than assembled from scalar statistics. A
session runs one execution profile: interactive by default, or the explicit
synchronous analytics profile, whose whole-call deadline includes admission
and result assembly.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from contextlib import closing
from dataclasses import asdict, dataclass

from ..collection_store import query_freshness, rollups, typed_storage
from . import cursors, ir
from .buckets import EXTREMES, Accumulator, Basis, bucket_key, declared_kind, group_key
from .runtime import MAX_RESULT_BYTES, QueryError, wire_bytes
from .scalars import parse_instant

_UNSUPPORTED = frozenset({"percentile", "distinct_count"})
_MAX_DECODE_BYTES = 256 * 1024
#: Rows between deadline and cancellation checks in the Python reduction loop.
_CHECK_EVERY = 512
#: Retained bytes charged for each group's accumulators, beyond held extreme values.
_GROUP_BYTES = 256
#: Result bytes held back for the continuation token and the returned count.
_PAGE_RESERVE = cursors._MAX_TOKEN_BYTES + 8


@dataclass(frozen=True, slots=True)
class _Shape:
    """The requested reduction, bound to the collection's declared fields."""

    timed: ir.GroupKey | None
    basis: Basis | None
    window: tuple[dt.date | None, dt.date | None]
    others: tuple[str, ...]
    sort_kinds: tuple[str, ...]
    fields: tuple[str, ...]
    kinds: tuple[str, ...]
    extremes: tuple[bool, ...]
    latest: bool
    values: tuple[ir.NamedAggregate, ...]

    def reads(self) -> tuple[str, ...]:
        """Every declared field the reduction reads: aggregated values, group keys and the timing basis."""
        timing = () if self.basis is None else (self.basis.field, self.basis.offset)
        return tuple(dict.fromkeys(name for name in (*self.fields, *self.others, *timing) if name is not None))


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
                     "kind": shape.basis.kind, "from": shape.timed.window_from, "to": shape.timed.window_to}
        return {"strategy": self.strategy, "reason": self.reason, "release": self.release,
                "rollup": None if self.rollup is None else self.rollup[1].name, "basis": basis}


def _refuse_request(query, limits) -> None:
    if not isinstance(query, ir.Query) or query.source.domain != "collections" or query.aggregate is None:
        raise QueryError("QUERY_UNSUPPORTED", "only a grouped collection reduction runs here")
    if query.execution_profile != limits.profile:
        raise QueryError("QUERY_PROFILE_UNAVAILABLE", "the request names a profile this session does not run")
    if query.where is not None:
        raise QueryError("QUERY_UNSUPPORTED", "bound a reduction by its local-day window: group_by[n].from and .to")
    if (query.joins or query.having is not None or query.text is not None
            or query.graph is not None or sum(key.bucket is not None for key in query.aggregate.groups) > 1):
        raise QueryError("QUERY_UNSUPPORTED", "this reduction shape is not available")
    if any(value.op in _UNSUPPORTED for value in query.aggregate.values):
        raise QueryError("QUERY_UNSUPPORTED", "percentile and distinct count are not assembled from rollups")


def check_shape(query, manifest, limits) -> None:
    """Every refusal a reduction gets from its request and declaration; compose runs it too."""
    _refuse_request(query, limits)
    _shape(query, manifest)


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
    window = (None, None) if timed is None else tuple(
        None if day is None else dt.date.fromisoformat(day) for day in (timed.window_from, timed.window_to))
    others = tuple(key.field.path for key in aggregate.groups if key.bucket is None)
    fields = tuple(dict.fromkeys(value.field.path for value in aggregate.values if value.field is not None))
    return _Shape(
        timed, basis, window, others, tuple(declared[name].type for name in others), fields,
        tuple(declared_kind(declared[name].type, declared[name].enum) for name in fields),
        tuple(any(value.op in EXTREMES and value.field.path == name for value in aggregate.values
                  if value.field is not None) for name in fields),
        any(value.op == "latest" for value in aggregate.values), aggregate.values)


def _answers(rollup, shape: _Shape) -> bool:
    if shape.timed is None or rollup.basis != shape.basis or rollup.bucket != shape.timed.bucket:
        return False
    if rollup.groups != shape.others:
        return False
    return all((value.op == "count" and value.field is None) or rollup.reduces(value.field.path, value.op)
               for value in shape.values)


def _aligned(shape: _Shape) -> bool:
    """Whether the window covers whole buckets, so stored buckets answer it exactly."""
    start, end = shape.window
    bucket = shape.timed.bucket
    if start is not None and bucket_key(start, bucket) != start.isoformat():
        return False
    after = None if end is None else end + dt.timedelta(days=1)
    return after is None or bucket_key(after, bucket) == after.isoformat()


def _plan(session, collection_id: str, shape: _Shape, uniform: bool) -> _Plan:
    if not uniform:
        return _Plan("base", "mixed_release", "admitted")
    if session._projected_fields & set(shape.reads()):
        return _Plan("base", "fields_projected", "uniform")
    admitted = session._field_plans[collection_id].whole_fields

    def eligible(definition):
        rollup = definition[1]
        dependencies = (*rollup.groups, *(name for name, _ in rollup.values), rollup.basis.field, rollup.basis.offset)
        return all(name is None or name in admitted for name in dependencies)

    matching = [definition for definition in rollups.definitions(session.connection, collection_id)
                if eligible(definition) and _answers(definition[1], shape)]
    ready = [definition for definition in matching if definition[2] == "ready"]
    if ready and not _aligned(shape):
        return _Plan("base", "window_unaligned", "uniform")
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


def _boundary(bucket: str, identity: str) -> str:
    """A page's last group as a short cursor boundary: its bucket and a digest of its identity."""
    return f"{bucket}|{hashlib.sha256(identity.encode()).hexdigest()[:40]}"


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


def _from_rollup(session, shape: _Shape, plan: _Plan, after: str | None):
    """Stored groups of the window in primary-key order, from just past ``after``."""
    rollup_id, rollup, _, _ = plan.rollup
    names = [name for name, _ in rollup.values]
    start, end = (None if day is None else day.isoformat() for day in shape.window)
    resume = None if after is None else after.partition("|")[0]
    first = max(start or "", resume or "")
    with closing(rollups.bucket_states(session.connection, rollup_id, first=first, last=end)) as stored:
        for bucket, identity, tagged, rows, accumulators, _ in stored:
            session.check()
            if after is not None:
                if bucket != resume:
                    raise QueryError("QUERY_CURSOR_STALE")
                if _boundary(bucket, identity) == after:
                    after = None
                continue
            yield bucket, identity, tagged, rows, dict(zip(names, accumulators, strict=True))
    if after is not None:
        raise QueryError("QUERY_CURSOR_STALE")


def _scan_sql(session, collection_id: str, layout, predicate: str, fields: tuple[str, ...]):
    """Choose released dependencies before decoding any canonical value."""
    from ..collection_store.field_admission import WHOLE_SUBTREE

    selection = session._field_plans[collection_id].fields
    if layout is not None and all(name in layout.fields and (selection.get(name) is True
                                 or selection.get(name) is WHOLE_SUBTREE) for name in fields):
        # Dense admitted scalars share one scan; residuals still need the bounded field-tree reader.
        columns = [(name, layout.fields.index(name)) for name in fields]
        selected = "".join(f",t.t{ordinal},t.v{ordinal},t.k{ordinal}" for _, ordinal in columns)
        # Row positions of each field's tag; whole scalars must not hold a JSON tree.
        slots = tuple((name, 3 * index) for index, (name, _) in enumerate(columns, start=1))
        scalars_only = tuple(at for name, at in slots if selection[name] is True)
        missing, json_tag = typed_storage.MISSING, typed_storage.JSON
        string, int64, decode_value = typed_storage.STRING, typed_storage.INT64, typed_storage.decode_value

        def decode_typed(row):
            if row[1] != row[2]:
                raise QueryError("QUERY_UNAVAILABLE")
            for at in scalars_only:
                if row[at] == json_tag:
                    raise QueryError("QUERY_UNAVAILABLE")
            values = {}
            for name, at in slots:
                tag = row[at]
                if tag != missing:
                    value = row[at + 1]
                    # typed_storage.decode_value's STRING and INT64 rows, inline to skip a call per row.
                    if (tag == string and type(value) is str) or (tag == int64 and type(value) is int):
                        values[name] = value
                    else:
                        values[name] = decode_value(tag, value, row[at + 2])
            return values

        return (f"SELECT i.item_key,i.row_version,t.row_version{selected} FROM items i "
                f"LEFT JOIN {layout.current_table} t ON t.row_id=i.row_id "
                f"WHERE i.collection_id=? AND {predicate}"), decode_typed

    def decode(row):
        return session.selected_values(row[1], layout, fields, max_bytes=_MAX_DECODE_BYTES, check=session.check)

    return (f"SELECT i.item_key,i.row_id FROM items i "  # noqa: S608 - internal membership predicate
            f"WHERE i.collection_id=? AND {predicate}"), decode


def _from_rows(session, limits, collection_id: str, shape: _Shape, layout, predicate: str,
               after: str | None) -> tuple[list, int]:
    """Reduce the admitted rows of the window into groups ordered from just past ``after``.

    Flagged rows count whatever the window, since their local day is unknown.
    Rows of buckets before the cursor's bucket are skipped, not retained.
    """
    read = shape.reads()
    sql, decode = _scan_sql(session, collection_id, layout, predicate, read)
    project = session._project_values
    basis, bucket, timed = shape.basis, "", shape.timed
    start, end = shape.window
    floor = "" if after is None else after.partition("|")[0]
    reduced = tuple(zip(shape.fields, shape.kinds, shape.extremes, strict=True))
    groups, retained, flagged, visited = {}, 0, 0, 0
    empty_group = rollups.group_text({}, ())
    latest, bucket_keys = shape.latest, {}
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
                located = basis.locate(values, ordered=latest)
                if located is None:
                    flagged += 1
                    continue
                day, order = located
                if (start is not None and day < start) or (end is not None and day > end):
                    continue
                bucket = bucket_keys.get(day)
                if bucket is None:
                    bucket = bucket_key(day, timed.bucket)
                    if len(bucket_keys) < 4096:
                        bucket_keys[day] = bucket
                if bucket < floor:
                    continue
            identity = (bucket, rollups.group_text(values, shape.others) if shape.others else empty_group)
            group = groups.get(identity)
            if group is None:
                if len(groups) == limits.max_groups:
                    raise QueryError("QUERY_GROUP_LIMIT", "narrow the local-day window or coarsen the bucket")
                group = groups[identity] = [group_key(values, shape.others), 0,
                                            {name: Accumulator() for name in shape.fields}]
                retained += _GROUP_BYTES
            group[1] += 1
            accumulators = group[2]
            for name, kind, extremes in reduced:
                retained += accumulators[name].add(values.get(name), kind, row[0], order, extremes)
            if retained > limits.max_state_bytes:
                raise QueryError("QUERY_COST_LIMIT")
    finally:
        cursor.close()
    session.check()
    if shape.timed is None and not shape.others and not groups:
        # An ungrouped reduction of no rows is one row: zero counts, null values.
        groups["", "[]"] = [(), 0, {name: Accumulator() for name in shape.fields}]
    ordered = sorted(groups.items(), key=lambda item: item[0])
    if after is not None:
        resume = next((n for n, ((bucket, identity), _) in enumerate(ordered) if _boundary(bucket, identity) == after),
                      None)
        if resume is None:
            raise QueryError("QUERY_CURSOR_STALE")
        ordered = ordered[resume + 1:]
    return [(bucket, identity, *group) for (bucket, identity), group in ordered], flagged


def _binding(session, query, collection_id: str, manifest, shape: _Shape, uniform: bool) -> dict:
    """What a group page's continuation binds: caller, manifest version and the collection's freshness
    basis over the fields read, or a refusal where no such basis bounds the visible state."""
    read = {*shape.fields, *shape.others}
    if shape.basis is not None:
        read |= {name for name in (shape.basis.field, shape.basis.offset) if name is not None}
    field_plan = session._field_plans[collection_id]
    if uniform and read <= field_plan.whole_fields:
        basis = query_freshness.uniform_basis(session.connection, collection_id, read)
        if basis is None:
            raise QueryError("QUERY_UNAVAILABLE", "continuation needs the collection's freshness basis")
        visible = cursors._hash(asdict(basis))
    else:
        admitted = session.admit(collection_id)
        if admitted.visible_count + session._estimated_visits > session.limits.max_row_visits:
            raise QueryError("QUERY_COST_LIMIT")
        session._estimated_visits += admitted.visible_count
        digest = hashlib.sha256(b"exomem.group-visible-dependencies.v1\0")
        with closing(session.connection.execute(
                f"SELECT i.row_id,i.item_key FROM items i WHERE i.collection_id=? AND {admitted.membership_sql} "
                "ORDER BY i.row_id", (collection_id,))) as rows:
            for row_id, key in rows:
                values = session.selected_values(row_id, admitted.layout, read,
                                                 max_bytes=_MAX_DECODE_BYTES, check=session.check)
                digest.update(cursors._json([key, values]).encode() + b"\n")
        visible = digest.hexdigest()
    return {**cursors.caller_binding(session, query), "schema": cursors._hash([collection_id,
            manifest.manifest_version.hash]), "visible": visible}


def _assemble(session, query, shape: _Shape, ordered, envelope: dict, mint) -> dict:
    """Whole groups up to the page limit and the result byte cap; a stop with groups left mints a cursor."""
    empty = {**envelope, "groups": [], "returned": 0, "has_more": False, "truncated": False,
             "truncation_reason": "limit", "next_cursor": ""}
    rows, used, last, reason = [], wire_bytes(empty) + _PAGE_RESERVE, None, None
    for bucket, identity, tagged, count, accumulators in ordered:
        if len(rows) == query.page.limit:
            reason = "limit"
            break
        row = _row(shape, bucket, tagged, count, accumulators)
        size = wire_bytes(row) + 2
        if used + size > MAX_RESULT_BYTES:
            if not rows:
                raise QueryError("QUERY_RESULT_TOO_LARGE", "one group exceeds the result size cap")
            reason = "bytes"
            break
        rows.append(row)
        used += size
        last = (bucket, identity)
    token = None if reason is None else mint(_boundary(*last))
    result = {**envelope, "groups": rows, "returned": len(rows), "has_more": reason is not None,
              "truncated": reason is not None, "truncation_reason": reason, "next_cursor": token}
    session.check()
    if wire_bytes(result) > MAX_RESULT_BYTES:
        raise QueryError("QUERY_RESULT_TOO_LARGE")
    return result


def reduce(session, query, *, as_of: str | None = None) -> dict:
    """Explain, preview, dry-run or execute one page of a grouped reduction inside ``session``.

    A continuation (``page.after``) keeps its first page's ``as_of``; a different one is refused.
    """
    session.check()
    limits = session.limits
    if getattr(query, "mode", None) == "compose":
        raise QueryError("QUERY_UNSUPPORTED", "compose reads no values")
    _refuse_request(query, limits)
    collection_id = query.source.ref
    codec = payload = None
    if query.page.after is not None:
        codec = cursors._codec(session)
        payload = codec.open(query.page.after)
    try:
        frozen = parse_instant(payload["as_of"] if payload else as_of or dt.datetime.now(dt.UTC).isoformat())
        if payload is not None and as_of is not None and parse_instant(as_of).isoformat() != frozen.isoformat():
            raise QueryError("QUERY_CURSOR_INVALID")
    except ValueError as error:
        raise QueryError("QUERY_VALUE_INVALID", "as_of is an RFC 3339 instant") from error
    frozen = frozen.isoformat()
    # Admission proves uniform release, including a summary collection's complete owner release.
    with session._manifest(collection_id) as (manifest, _, _, uniform):
        shape = _shape(query, manifest)
    plan = _plan(session, collection_id, shape, uniform)
    result = {"source": asdict(query.source), "query_version": query.version,
              "schema_version": manifest.schema.version, "mode": query.mode, "as_of": frozen,
              "execution_profile": limits.profile, "bounds": limits.bounds(), "plan": plan.describe(shape)}
    binding = after = None
    if payload is not None:
        binding = _binding(session, query, collection_id, manifest, shape, uniform)
        codec.verify(payload, binding)
        after = payload["boundary"]
    if query.mode == "explain":
        return result
    layout = predicate = None
    if plan.strategy == "rollup":
        start, end = (None if day is None else day.isoformat() for day in shape.window)
        visits = _bounded_count(session, "SELECT 1 FROM rollup_buckets WHERE rollup_id=? AND bucket>=? AND bucket<=?",
                                (plan.rollup[0], start or "", end or "\uffff"))
        _charge(session, visits)
    else:
        # Uniform admission counts rows; mixed admission streams released row ids to bounded temp.
        admitted = session.admit(collection_id)
        visits, layout, predicate = admitted.visible_count, admitted.layout, admitted.membership_sql
    # What the plan reads, exactly: released rows on the base path, stored buckets on the rollup path.
    read = {"rollup_buckets" if plan.strategy == "rollup" else "admitted_rows": visits}
    if query.mode == "preview":
        return {**result, **read}
    if query.mode == "dry_run":
        return {**result, "admitted": True, **read}
    if plan.strategy == "rollup":
        ordered, flagged = _from_rollup(session, shape, plan, after), plan.rollup[3]
    else:
        ordered, flagged = _from_rows(session, limits, collection_id, shape, layout, predicate, after)

    def mint(boundary: str) -> str:
        nonlocal binding, codec
        codec = codec or cursors._codec(session)
        binding = binding or _binding(session, query, collection_id, manifest, shape, uniform)
        return codec.mint(binding, as_of=frozen, boundary=boundary)

    # Flagged rows have no local day, so they are counted over the released collection, not the window.
    envelope = {**result, "flagged_rows": flagged, "flagged_scope": "collection", **read}
    try:
        return _assemble(session, query, shape, ordered, envelope, mint)
    finally:
        if plan.strategy == "rollup":
            ordered.close()
