"""Rows and filtered reductions through declared references, on the Python base path.

A query that names a reference — ``related``, a dotted path, a filtered
reduction — cannot run through the typed projections or a rollup: its
predicates are three-valued and its group keys may live on another
collection's rows. This module scans the admitted base rows, resolves each
named reference under the caller's own view (one visit per distinct value,
through ``BoundReference``), evaluates the filter in Python, and reports
``combined``: the rows combined, the combined rows with an unresolved
reference, the rows the filter excluded only because a reference did not
resolve, and a bounded list of the values that did not resolve.
"""

from __future__ import annotations

import datetime as dt
import functools
from collections import Counter
from dataclasses import asdict, dataclass

from ..collection_store import native_identity, typed_storage
from . import cursors, ir, reductions, row_predicates
from .buckets import Accumulator
from .errors import QueryError
from .row_predicates import UNRESOLVED
from .runtime import MAX_RESULT_BYTES, wire_bytes
from .scalars import MISSING, ScalarValueError, parse_instant, scalar_key

_CHUNK = 256
_UNRESOLVED_VALUES = 20
_PAGE_RESERVE = cursors._MAX_TOKEN_BYTES + 8
_ROW_BYTES = 64


@dataclass(slots=True)
class _Row:
    """One scanned source row and what its named references resolved to."""

    row_id: int
    item_key: str
    values: dict
    states: dict  # chain name -> "resolved" | "unresolved" | "none"
    terminals: dict  # chain name -> Target | UNRESOLVED | MISSING
    pairs: set  # (reference, value) that did not resolve on this row


class _Execution:
    """The shared scan: dependencies, named chains, resolution and per-row reference state."""

    def __init__(self, session, query, bound: dict):
        self.session = session
        self.query = query
        self.bound = bound
        self.collection_id = query.source.ref
        self.chains: list[tuple[str, list]] = []
        self.related: dict[str, int] = {}
        self.fields = list(self._ir_fields())
        self._plan_chains()
        self.resolved: dict = {b: {} for _, hops in self.chains for b in hops}
        self.reads: dict = {}  # (bound, Target) -> values read
        self._needed = self._target_fields()
        self.pairs: Counter = Counter()
        self.count = 0
        self.unresolved_rows = 0
        self.excluded_unresolved = 0

    def _ir_fields(self):
        def leaves(node):
            if node is None:
                return
            for child in node.children:
                yield from leaves(child)
            if node.field is not None:
                yield node.field

        yield from self.query.select.fields
        yield from leaves(self.query.where)
        for key in self.query.order_by.keys:
            yield key.field
        if self.query.aggregate is not None:
            for group in self.query.aggregate.groups:
                yield group.field
            for value in self.query.aggregate.values:
                if value.field is not None:
                    yield value.field
                yield from leaves(value.where)
        yield from leaves(self.query.having)

    def _plan_chains(self) -> None:
        """Every reference chain the query names, hop by hop: related entries and path aliases."""
        chains: dict[str, list] = {}
        order: list[str] = []
        for entry in self.query.related:
            self.related[entry.path] = entry.limit
            if entry.path not in chains:
                order.append(entry.path)
                chains[entry.path] = [self.bound[(self.collection_id, entry.path)]]
        for field in self.fields:
            if field.alias is None:
                continue
            cid, hops = self.collection_id, []
            for index, segment in enumerate(field.alias.split(".")):
                hop = self.bound[(cid, segment)]
                hops.append(hop)
                cid = hop.collection_id
                name = ".".join(field.alias.split(".")[: index + 1])
                if name not in chains:
                    order.append(name)
                    chains[name] = list(hops)
        self.chains = [(name, chains[name]) for name in order]

    def _target_fields(self) -> dict:
        """The fields each bound reference's resolved targets must be read for."""
        needed: dict = {}
        for name, hops in self.chains:
            terminal = hops[-1]
            for field in self.fields:
                if field.alias == name:
                    needed.setdefault(terminal, set()).add(field.path)
            if name in self.related:
                needed.setdefault(hops[0], set()).update(hops[0].manifest.schema.fields)
            if len(hops) == 2:
                needed.setdefault(hops[0], set()).add(hops[1].declaration.path[0])
        return needed

    def dependencies(self) -> tuple[str, ...]:
        """The source fields the scan decodes: every base field used plus each hop's source field."""
        deps = {field.path for field in self.fields if field.alias is None and field.path != "item_key"}
        for _, hops in self.chains:
            deps.add(hops[0].declaration.path[0])
        return tuple(sorted(deps))

    def names(self) -> list[str]:
        return [name for name, _ in self.chains]

    def _item_key(self, layout, raw):
        if layout is not None and layout.encoding == typed_storage.TYPED_V2 and type(raw) is bytes:
            try:
                return native_identity.decode(raw, layout.identity)
            except (ValueError, UnicodeError) as error:
                raise QueryError("QUERY_UNAVAILABLE") from error
        if type(raw) is not str:
            raise QueryError("QUERY_UNAVAILABLE")
        return raw

    def _resolve_chunk(self, rows: list[tuple]) -> None:
        """Resolve one chunk's distinct values per hop, then read each new target's fields once."""
        first = {hops[0] for _, hops in self.chains}
        second = {hops[1] for _, hops in self.chains if len(hops) == 2}
        for bound in first:
            bound.resolve([value for *_, values in rows for value in bound.declaration.values(values)])
        for bound in first:
            fields = sorted(self._needed.get(bound, ()))
            if not fields:
                continue
            for *_, values in rows:
                for value in bound.declaration.values(values):
                    target = bound._found.get(value)
                    if target is not None and (bound, target) not in self.reads:
                        self.reads[bound, target] = bound.read(target, fields)
        for bound in second:
            hop1 = next(hops[0] for _, hops in self.chains if len(hops) == 2 and hops[1] is bound)
            wanted = []
            for *_, values in rows:
                for value in hop1.declaration.values(values):
                    target = hop1._found.get(value)
                    if target is None:
                        continue
                    wanted += list(bound.declaration.values(self.reads.get((hop1, target), {})))
            bound.resolve(wanted)
        for bound in second:
            fields = sorted(self._needed.get(bound, ()))
            if not fields:
                continue
            hop1 = next(hops[0] for _, hops in self.chains if len(hops) == 2 and hops[1] is bound)
            for *_, values in rows:
                for value in hop1.declaration.values(values):
                    target = hop1._found.get(value)
                    if target is None:
                        continue
                    for value2 in bound.declaration.values(self.reads.get((hop1, target), {})):
                        target2 = bound._found.get(value2)
                        if target2 is not None and (bound, target2) not in self.reads:
                            self.reads[bound, target2] = bound.read(target2, fields)

    def _row_state(self, values: dict) -> tuple[dict, dict, set]:
        """The state, terminal target and unresolved (reference, value) pairs of one row's chains."""
        states, terminals, pairs = {}, {}, set()
        for name, hops in self.chains:
            first = hops[0]
            values1 = first.declaration.values(values)
            if not values1:
                states[name], terminals[name] = "none", MISSING
                continue
            resolved = {value: first._found.get(value) for value in values1}
            failed = [value for value in values1 if resolved[value] is None]
            if len(hops) == 1:
                if failed:
                    states[name], terminals[name] = "unresolved", UNRESOLVED
                    pairs.update((name, value) for value in failed)
                else:
                    states[name] = "resolved"
                    terminals[name] = resolved[values1[0]]
                continue
            head = name.split(".", 1)[0]
            if failed:
                states[name], terminals[name] = "unresolved", UNRESOLVED
                pairs.update((head, value) for value in failed)
                continue
            second = hops[1]
            values2 = second.declaration.values(
                self.reads.get((first, resolved[values1[0]]), {}))
            if not values2:
                states[name], terminals[name] = "none", MISSING
                continue
            resolved2 = {value: second._found.get(value) for value in values2}
            if any(resolved2[value] is None for value in values2):
                states[name], terminals[name] = "unresolved", UNRESOLVED
                pairs.add((head, values1[0]))
            else:
                states[name] = "resolved"
                terminals[name] = resolved2[values2[0]]
        return states, terminals, pairs

    def value_of(self, row: _Row, field: ir.Field):
        """A base or path value of one classified row: the value, ``MISSING`` or ``UNRESOLVED``."""
        if field.alias is None:
            return row.item_key if field.path == "item_key" else row.values.get(field.path, MISSING)
        state = row.states.get(field.alias)
        if state != "resolved":
            return UNRESOLVED if state == "unresolved" else MISSING
        terminal = row.terminals[field.alias]
        chain = next(hops for name, hops in self.chains if name == field.alias)
        return self.reads.get((chain[-1], terminal), {}).get(field.path, MISSING)

    def scan(self, admitted) -> list[_Row]:
        """Scan the admitted rows and classify each against the filter and its reference chains.

        Returns the combined rows; the counters hold the rest of ``combined``.
        """
        sql, decode = reductions._scan_sql(
            self.session, self.collection_id, admitted.layout, admitted.membership_sql,
            self.dependencies(), with_row_id=True)
        project = self.session._project_values
        instant = self._instant
        combined, retained, visited = [], 0, 0
        cursor = self.session.connection.execute(sql, (self.collection_id,))
        try:
            buffer = []
            for raw in cursor:
                visited += 1
                if not visited % reductions._CHECK_EVERY:
                    self.session.check()
                try:
                    row_id, values = decode(raw)
                except (typed_storage.TypedStorageError, ValueError, TypeError) as error:
                    raise QueryError("QUERY_UNAVAILABLE") from error
                if project is not None:
                    values = project(values)
                buffer.append((row_id, self._item_key(admitted.layout, raw[1]), values))
                if len(buffer) == _CHUNK:
                    retained += self._classify(buffer, combined, instant)
                    if retained > self.session.limits.max_state_bytes:
                        raise QueryError("QUERY_COST_LIMIT")
                    buffer = []
            if buffer:
                retained += self._classify(buffer, combined, instant)
                if retained > self.session.limits.max_state_bytes:
                    raise QueryError("QUERY_COST_LIMIT")
        finally:
            cursor.close()
        self.session.check()
        return combined

    def _classify(self, buffer: list[tuple], combined: list[_Row], instant) -> int:
        self._resolve_chunk(buffer)
        retained = 0
        for row_id, item_key, values in buffer:
            states, terminals, pairs = self._row_state(values)
            row = _Row(row_id, item_key, values, states, terminals, pairs)
            outcome = row_predicates.evaluate(
                self.query.where, lambda field, row=row: self.value_of(row, field), instant=instant)
            if outcome is False:
                continue
            if outcome is None:
                self.excluded_unresolved += 1
            else:
                self.count += 1
                if "unresolved" in states.values():
                    self.unresolved_rows += 1
                combined.append(row)
                retained += wire_bytes(values) + _ROW_BYTES
            self.pairs.update(pairs)
        return retained

    def combined(self) -> dict:
        return {"count": self.count, "unresolved_rows": self.unresolved_rows,
                "excluded_unresolved": self.excluded_unresolved,
                "unresolved_values": _unresolved_values(self.pairs)}

    def related_out(self, row: _Row) -> dict:
        """The ``related`` object of one output row: state, rows and bounded many-valued items."""
        result = {}
        for name, limit in self.related.items():
            bound = self.bound[(self.collection_id, name)]
            state = row.states[name]
            if not bound.many:
                out = {"state": state}
                if state == "resolved":
                    target = row.terminals[name]
                    out["row"] = {**self.reads.get((bound, target), {}), "item_key": target.item_key}
                result[name] = out
                continue
            values = bound.declaration.values(row.values)
            items = []
            for value in values[:limit]:
                target = bound._found.get(value)
                item = {"value": value, "state": "resolved" if target is not None else "unresolved"}
                if target is not None:
                    item["row"] = {**self.reads.get((bound, target), {}), "item_key": target.item_key}
                items.append(item)
            result[name] = {"state": state, "count": len(values), "items": items,
                            "truncated": len(items) < len(values)}
        return result

    def digest(self) -> str:
        """The resolution digest a continuation binds: each distinct value's outcome and values read."""
        seen = []
        for name, hops in self.chains:
            for position, bound in enumerate(hops):
                entries = []
                for value, target in sorted(bound._found.items(), key=lambda item: item[0]):
                    entries.append([value, None if target is None else
                                    [target.item_key, self.reads.get((bound, target), {})]])
                seen.append([f"{name}#{position}", entries])
        return cursors._hash(seen)

    def binding(self, session, query, admitted, basis, uniform, dependencies) -> dict:
        targets = sorted({bound.collection_id: bound for _, hops in self.chains for bound in hops
                          }.items())
        schema = cursors._hash([
            [basis.manifest_hash, basis.type_name, basis.type_version, basis.declaration_hash],
            [[bound.collection_id, bound.manifest.manifest_version.hash] for _, bound in targets]])
        visible = cursors._hash([
            cursors.visible_basis(session, self.collection_id, uniform=uniform,
                                  layout=admitted.layout, dependencies=set(dependencies)),
            self.digest()])
        return {**cursors.caller_binding(session, query), "schema": schema,
                "visible": cursors.with_rows(session, self.collection_id, visible)}

    _instant = None


def _unresolved_values(pairs: Counter) -> dict:
    """The bounded, ordered list of distinct unresolved (reference, value) pairs."""
    entries = [{"reference": name, "value": value, "rows": rows}
               for (name, value), rows in pairs.items()]
    entries.sort(key=lambda item: (-item["rows"], item["reference"], item["value"]))
    return {"values": entries[:_UNRESOLVED_VALUES],
            "truncated": len(entries) > _UNRESOLVED_VALUES, "total": len(entries)}


def _refuse(query, session) -> None:
    if not isinstance(query, ir.Query) or query.source.domain != "collections":
        raise QueryError("QUERY_UNSUPPORTED")
    if (query.joins or query.having is not None or query.text is not None or query.graph is not None):
        raise QueryError("QUERY_UNSUPPORTED", "this query shape is not available")
    if query.aggregate is None:
        if query.execution_profile != "interactive":
            raise QueryError("QUERY_UNSUPPORTED", "row pages run under the interactive profile")
    else:
        if query.execution_profile != session.limits.profile:
            raise QueryError("QUERY_PROFILE_UNAVAILABLE", "the request names a profile this session does not run")
        if sum(key.bucket is not None for key in query.aggregate.groups) > 1:
            raise QueryError("QUERY_UNSUPPORTED", "this reduction shape is not available")
        if any(value.op in reductions._UNSUPPORTED for value in query.aggregate.values):
            raise QueryError("QUERY_UNSUPPORTED", "percentile and distinct count are not assembled from rollups")


def _freeze(session, query, as_of):
    codec = cursors._codec(session)
    payload = codec.open(query.page.after) if query.page.after is not None else None
    try:
        frozen = parse_instant(
            payload["as_of"] if payload else as_of or query.as_of or dt.datetime.now(dt.UTC).isoformat())
        if payload is not None and as_of is not None and parse_instant(as_of).isoformat() != frozen.isoformat():
            raise QueryError("QUERY_CURSOR_INVALID")
    except ValueError as error:
        raise QueryError("QUERY_VALUE_INVALID", "as_of is an RFC 3339 instant") from error
    return codec, payload, frozen


def _sort_key(execution, row: _Row, field: ir.Field):
    """(rank, tag, payload) of one order key: unresolved sorts below missing and null."""
    value = execution.value_of(row, field)
    if value is UNRESOLVED:
        return 3, 0, ""
    if value is MISSING:
        return 2, 0, ""
    if value is None:
        return 1, 0, ""
    try:
        tag, payload = scalar_key(value, field.value_type)
    except ScalarValueError:
        return 0, 0, ""
    return 0, tag, payload


def _sort_rows(execution, rows: list[_Row], keys) -> None:
    """Order combined rows like the SQL order: rank, then tag, then payload in the key's direction."""

    def compare(left: _Row, right: _Row) -> int:
        for key in keys:
            a = _sort_key(execution, left, key.field)
            b = _sort_key(execution, right, key.field)
            rank_a, rank_b = (a[0], b[0]) if key.nulls == "last" else (-a[0], -b[0])
            if rank_a != rank_b:
                return -1 if rank_a < rank_b else 1
            if a[1] != b[1]:
                return -1 if a[1] < b[1] else 1
            if a[2] != b[2]:
                ascending = key.direction == "asc"
                return -1 if (a[2] < b[2]) == ascending else 1
        return (left.item_key > right.item_key) - (left.item_key < right.item_key)

    rows.sort(key=functools.cmp_to_key(compare))


def _output_row(execution, admitted, row: _Row, select: tuple[ir.Field, ...]) -> dict:
    """One page row: its released values, path fields, reference states and related expansions."""
    session = execution.session
    base = [field.path for field in select if field.alias is None] if select else list(admitted.fields)
    stored = {name for name in base if name != "item_key"}
    selected = session.selected_values(
        row.row_id, admitted.layout, stored,
        max_bytes=min(reductions._MAX_DECODE_BYTES, session.limits.max_temp_bytes), check=session.check)
    if session._project_values is not None:
        selected = session._project_values(selected)
        selected = {name: value for name, value in selected.items() if name in stored}
    out = {}
    for name in base:
        if name == "item_key":
            out["item_key"] = row.item_key
        elif name in selected:
            out[name] = selected[name]
    for field in select:
        if field.alias is not None:
            value = execution.value_of(row, field)
            if value is not MISSING and value is not UNRESOLVED:
                out[f"{field.alias}.{field.path}"] = value
    out["references"] = dict(row.states)
    out["related"] = execution.related_out(row)
    return out


def _open_execution(session, query, bound, *, basis, uniform, as_of):
    _refuse(query, session)
    codec, payload, frozen = _freeze(session, query, as_of)
    execution = _Execution(session, query, bound)
    execution._instant = frozen
    return execution, codec, payload, frozen


def _explain_plan(session, query, execution, *, manifest, uniform, admitted, dependencies):
    result = {"source": asdict(query.source), "query_version": query.version,
              "schema_version": manifest.schema.version, "mode": query.mode,
              "execution_profile": session.limits.profile, "bounds": session.limits.bounds(),
              "plan": {"kind": "rows", "release": "uniform" if uniform else "admitted",
                       "access": "admitted_scan", "index": None,
                       "references": execution.names(),
                       "fields": sorted(dependencies)}}
    if query.mode == "explain":
        return result
    if query.mode == "preview":
        return {**result, "visit_charge": admitted.visible_count}
    return {**result, "admitted": True, "visit_charge": admitted.visible_count}


def _boundary_resume(rows: list[_Row], boundary: str) -> list[_Row]:
    position = next((n for n, row in enumerate(rows) if row.item_key == boundary), None)
    if position is None:
        raise QueryError("QUERY_CURSOR_STALE")
    return rows[position + 1:]


def run(session, query, bound: dict, *, basis, uniform, manifest, as_of=None) -> dict:
    """Execute, explain, preview or dry-run a row query that names a reference."""
    _refuse(query, session)
    collection_id = query.source.ref
    admitted = session.admit(collection_id)
    if query.mode in {"explain", "preview", "dry_run"}:
        execution = _Execution(session, query, bound)
        return _explain_plan(session, query, execution, manifest=manifest,
                             uniform=uniform, admitted=admitted,
                             dependencies=execution.dependencies())
    execution, codec, payload, frozen = _open_execution(session, query, bound,
                                                      basis=basis, uniform=uniform, as_of=as_of)
    dependencies = execution.dependencies()
    rows = execution.scan(admitted)
    _sort_rows(execution, rows, query.order_by.keys)
    binding = None
    if payload is not None:
        binding = execution.binding(session, query, admitted, basis, uniform, dependencies)
        codec.verify(payload, binding)
        rows = _boundary_resume(rows, payload["boundary"])

    def mint(boundary: str) -> str:
        nonlocal binding
        binding = binding or execution.binding(session, query, admitted, basis, uniform, dependencies)
        return codec.mint(binding, as_of=frozen.isoformat(), boundary=boundary)

    limit = query.page.limit
    out, size, last, has_more, reason = [], 0, None, False, None
    select = query.select.fields
    for index, row in enumerate(rows):
        if index >= limit:
            has_more, reason = True, "limit"
            break
        rendered = _output_row(execution, admitted, row, select)
        cost = wire_bytes(rendered) + 2 * bool(out)
        if size + cost > MAX_RESULT_BYTES - _PAGE_RESERVE:
            if not out:
                raise QueryError("QUERY_RESULT_TOO_LARGE")
            has_more, reason = True, "bytes"
            break
        out.append(rendered)
        size += cost
        last = row.item_key
    else:
        last = rows[-1].item_key if rows else None
    token = mint(last) if has_more else None
    result = {"rows": out, "returned": len(out), "has_more": has_more, "next_cursor": token,
              "total": execution.count, "as_of": frozen.isoformat(),
              "source": asdict(query.source), "query_version": query.version,
              "schema_version": manifest.schema.version, "schema_fingerprint":
                  (binding or execution.binding(session, query, admitted, basis, uniform, dependencies))["schema"],
              "truncated": has_more, "truncation_reason": reason,
              "mode": query.mode, "execution_profile": session.limits.profile,
              "combined": execution.combined()}
    session.check()
    if wire_bytes(result) > MAX_RESULT_BYTES:
        raise QueryError("QUERY_RESULT_TOO_LARGE")
    return result


# --- grouped reductions (C) -----------------------------------------------------


def reduce(session, query, bound: dict, *, basis, uniform, manifest, as_of=None) -> dict:
    """One filtered base-row reduction: whole groups plus a page of the combined rows."""
    _refuse(query, session)
    collection_id = query.source.ref
    admitted = session.admit(collection_id)
    execution, codec, payload, frozen = _open_execution(session, query, bound,
                                                      basis=basis, uniform=uniform, as_of=as_of)
    targets = {name: hops[-1].manifest for name, hops in execution.chains}
    shape = reductions._shape(query, manifest, targets)
    dependencies = execution.dependencies()
    visit_reason = "filtered" if (query.where is not None
                                or any(value.where is not None for value in shape.values)) else "references"
    basis_out = None if shape.basis is None else {
        "field": shape.basis.field, "bucket": shape.timed.bucket, "offset": shape.basis.offset,
        "kind": shape.basis.kind, "from": shape.timed.window_from, "to": shape.timed.window_to}
    plan = {"strategy": "base", "reason": visit_reason,
            "release": "uniform" if uniform else "admitted", "rollup": None, "basis": basis_out}
    if query.mode in {"explain", "preview", "dry_run"}:
        result = {"source": asdict(query.source), "query_version": query.version,
                  "schema_version": manifest.schema.version, "mode": query.mode, "as_of": frozen.isoformat(),
                  "execution_profile": session.limits.profile, "bounds": session.limits.bounds(),
                  "plan": plan, "references": execution.names()}
        if query.mode == "explain":
            return result
        if query.mode == "preview":
            return {**result, "visit_charge": admitted.visible_count}
        return {**result, "admitted": True, "visit_charge": admitted.visible_count}

    wheres = {value.name: _aggregate_where(execution, value, frozen)
              for value in shape.values if value.where is not None}
    combined_rows: list[_Row] = []
    retained = [0]

    def include(context) -> bool | None:
        verdict = row_predicates.evaluate(
            query.where, lambda field, ctx=context: execution.value_of(ctx, field), instant=frozen)
        if verdict is None:
            execution.excluded_unresolved += 1
            execution.pairs.update(context.pairs)
            return None
        if verdict is False:
            return False
        execution.count += 1
        execution.pairs.update(context.pairs)
        if "unresolved" in context.states.values():
            execution.unresolved_rows += 1
        combined_rows.append(context)
        return True

    sql, decode = reductions._scan_sql(
        session, collection_id, admitted.layout, admitted.membership_sql,
        dependencies, with_row_id=True)
    project = session._project_values
    injected = [(field, f"{field.alias}.{field.path}") for field in execution.fields
                if field.alias is not None]

    def scanned():
        visited = 0
        cursor = session.connection.execute(sql, (collection_id,))
        try:
            buffer = []
            for raw in cursor:
                visited += 1
                if not visited % reductions._CHECK_EVERY:
                    session.check()
                try:
                    row_id, values = decode(raw)
                except (typed_storage.TypedStorageError, ValueError, TypeError) as error:
                    raise QueryError("QUERY_UNAVAILABLE") from error
                if project is not None:
                    values = project(values)
                buffer.append((row_id, execution._item_key(admitted.layout, raw[1]), values))
                if len(buffer) == _CHUNK:
                    yield from _classified(execution, buffer, injected, retained)
                    buffer = []
            if buffer:
                yield from _classified(execution, buffer, injected, retained)
        finally:
            cursor.close()

    groups, flagged = reductions._group_rows(
        session.limits, shape, scanned(), None, include=include, wheres=wheres)
    session.check()
    if shape.timed is None and not shape.others and not groups:
        groups[("", "[]")] = [(), 0, {name: Accumulator() for name in shape.fields},
                              {"filtered": {}, "unresolved_rows": 0, "pairs": Counter()}]
    ordered = sorted(groups.items(), key=lambda item: item[0])
    combined_rows.sort(key=lambda row: row.item_key)

    binding = None
    if payload is not None:
        binding = execution.binding(session, query, admitted, basis, uniform, dependencies)
        codec.verify(payload, binding)
        combined_rows = _boundary_resume(combined_rows, payload["boundary"])

    def mint(boundary: str) -> str:
        nonlocal binding
        binding = binding or execution.binding(session, query, admitted, basis, uniform, dependencies)
        return codec.mint(binding, as_of=frozen.isoformat(), boundary=boundary)

    group_rows = [reductions._row(shape, bucket, *state) for (bucket, _), state in ordered]
    envelope = {"source": asdict(query.source), "query_version": query.version,
                "schema_version": manifest.schema.version, "mode": query.mode,
                "as_of": frozen.isoformat(), "execution_profile": session.limits.profile,
                "bounds": session.limits.bounds(), "plan": plan,
                "flagged_rows": flagged, "flagged_scope": "collection",
                "admitted_rows": admitted.visible_count,
                "groups": group_rows, "combined": execution.combined()}
    if wire_bytes({**envelope, "rows": [], "returned": 0, "has_more": False,
                   "next_cursor": None, "truncated": False, "truncation_reason": None}) \
            > MAX_RESULT_BYTES - _PAGE_RESERVE:
        raise QueryError("QUERY_RESULT_TOO_LARGE",
                         "the groups alone exceed the result cap; coarsen group_by")
    out, size, last, has_more, reason = [], 0, None, False, None
    item_key_withheld = session._field_plans[collection_id].item_key_withheld
    for index, row in enumerate(combined_rows):
        if index >= query.page.limit:
            has_more, reason = True, "limit"
            break
        rendered = _combined_row(execution, admitted, row, item_key_withheld=item_key_withheld)
        cost = wire_bytes(rendered) + 2 * bool(out)
        if size + cost > MAX_RESULT_BYTES - _PAGE_RESERVE:
            if not out:
                raise QueryError("QUERY_RESULT_TOO_LARGE")
            has_more, reason = True, "bytes"
            break
        out.append(rendered)
        size += cost
        last = row.item_key
    token = mint(last) if has_more else None
    result = {**envelope, "rows": out, "returned": len(out), "has_more": has_more,
              "next_cursor": token, "truncated": has_more, "truncation_reason": reason}
    session.check()
    if wire_bytes(result) > MAX_RESULT_BYTES:
        raise QueryError("QUERY_RESULT_TOO_LARGE")
    return result


def _aggregate_where(execution, value, instant):
    """One aggregate's own filter over the row context, three-valued like the query's."""

    def accept(context) -> bool | None:
        return row_predicates.evaluate(
            value.where, lambda field, ctx=context: execution.value_of(ctx, field), instant=instant)

    return accept


def _classified(execution, buffer, injected, retained):
    """Resolve one chunk and yield (item_key, values with path values injected, row context)."""
    execution._resolve_chunk(buffer)
    for row_id, item_key, values in buffer:
        states, terminals, pairs = execution._row_state(values)
        row = _Row(row_id, item_key, values, states, terminals, pairs)
        merged = dict(values)
        for field, public in injected:
            value = execution.value_of(row, field)
            if value is not MISSING:
                merged[public] = value
        retained[0] += wire_bytes(values) + _ROW_BYTES
        if retained[0] > execution.session.limits.max_state_bytes:
            raise QueryError("QUERY_COST_LIMIT")
        yield item_key, merged, row


def _combined_row(execution, admitted, row: _Row, *, item_key_withheld: bool) -> dict:
    """One combined row of a reduction: admitted base fields, references and related."""
    session = execution.session
    fields = tuple(name for name in admitted.fields if name != "item_key")
    selected = session.selected_values(
        row.row_id, admitted.layout, fields,
        max_bytes=min(reductions._MAX_DECODE_BYTES, session.limits.max_temp_bytes), check=session.check)
    if session._project_values is not None:
        selected = session._project_values(selected)
        selected = {name: value for name, value in selected.items() if name in fields}
    out = {} if item_key_withheld else {"item_key": row.item_key}
    out.update({name: selected[name] for name in admitted.fields if name in selected})
    out["references"] = dict(row.states)
    out["related"] = execution.related_out(row)
    return out
