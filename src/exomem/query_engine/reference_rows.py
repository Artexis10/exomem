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

The scan reads a target row's fields where the filter, an order key, a group key or an
aggregate names a path field. It also reads every field the caller may read from each resolved
``related`` target, once, and keeps only a hash of them: a continuation binds those fields, so it
refuses once a value it would show has changed. Only a page row's ``related`` expansion keeps the
values themselves.
"""

from __future__ import annotations

import functools
from collections import Counter
from dataclasses import asdict, dataclass

from ..collection_store import typed_storage
from . import cursors, ir, reductions, row_predicates, typed_rows
from .buckets import Accumulator
from .errors import QueryError
from .reference_resolution import unresolved_values
from .row_predicates import UNRESOLVED
from .runtime import MAX_RESULT_BYTES, wire_bytes
from .scalars import MISSING, ScalarValueError, scalar_key

_CHUNK = 256
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


def _chains(query, bound: dict) -> list[tuple[str, list]]:
    """Every reference chain the query names, hop by hop: related entries and path aliases."""
    collection_id = query.source.ref
    chains: dict[str, list] = {}
    for entry in query.related:
        if entry.inverse is not None:
            continue
        chains.setdefault(entry.path, [bound[(collection_id, entry.path)]])
    for field in ir.fields(query):
        if field.alias is None:
            continue
        cid, hops = collection_id, []
        segments = field.alias.split(".")
        for index, segment in enumerate(segments):
            hop = bound[(cid, segment)]
            hops.append(hop)
            cid = hop.collection_id
            chains.setdefault(".".join(segments[: index + 1]), list(hops))
    return list(chains.items())


def _targets(chains) -> dict:
    """The manifest each chain's last hop reads, as the caller may see it."""
    return {name: hops[-1].manifest for name, hops in chains}


class _Execution:
    """The shared scan: dependencies, named chains, resolution and per-row reference state."""

    def __init__(self, session, query, bound: dict, inverses: dict | None = None, *,
                 admitted, basis, uniform):
        self.session = session
        self.query = query
        self.bound = bound
        self.bound_inverses = inverses or {}
        self.admitted, self.basis, self.uniform = admitted, basis, uniform
        self.collection_id = query.source.ref
        self.fields = list(ir.fields(query))
        self.related: dict[str, int] = {entry.path: entry.limit for entry in query.related
                                        if entry.inverse is None}
        self.inverses: dict[str, tuple[int, object]] = {
            entry.path: (entry.limit, self.bound_inverses[entry.inverse])
            for entry in query.related if entry.inverse is not None}
        self.chains = _chains(query, bound)
        self.reads: dict = {}  # (bound, Target) -> the path fields read, for the scan
        self.shown: dict = {}  # (bound, Target) -> digest of every readable field, for a related target
        self.expansions: dict = {}  # (bound, Target) -> every readable field, for a rendered page row
        self._needed = self._target_fields()
        self._related_bounds = {hops[-1] for name, hops in self.chains if name in self.related}
        self.pairs: Counter = Counter()
        self.count = 0
        self.unresolved_rows = 0
        self.excluded_unresolved = 0
        self.state_bytes = 0
        self.codec = self.payload = self.frozen = None
        self._binding = None

    def freeze(self, as_of) -> None:
        """Fix the page's instant and open its continuation, when it has one."""
        self.codec, self.payload, self.frozen = cursors.freeze(self.session, self.query, as_of)

    def retain(self, size: int) -> None:
        """Count bytes this execution holds across rows against the session's bound."""
        self.state_bytes += size
        if self.state_bytes > self.session.limits.max_state_bytes:
            raise QueryError("QUERY_COST_LIMIT")

    def _target_fields(self) -> dict:
        """The fields the scan reads from each bound reference's resolved targets."""
        needed: dict = {}
        for name, hops in self.chains:
            for field in self.fields:
                if field.alias == name:
                    needed.setdefault(hops[-1], set()).add(field.path)
            if len(hops) == 2:
                needed.setdefault(hops[0], set()).add(hops[1].declaration.path[0])
        return {bound: sorted(names) for bound, names in needed.items()}

    def dependencies(self) -> tuple[str, ...]:
        """The source fields the scan decodes: every base field used plus each hop's source field."""
        deps = {field.path for field in self.fields if field.alias is None and field.path != "item_key"}
        for _, hops in self.chains:
            deps.add(hops[0].declaration.path[0])
        return tuple(sorted(deps))

    def names(self) -> list[str]:
        return [name for name, _ in self.chains] + [name for name in self.inverses]

    def visit_charge(self) -> int:
        """The preview's charge: the admitted rows, plus one lookup per row for each bound reference
        and each inverse's bounded index rows.

        An upper bound, since the values to look up are read only when the query runs: a
        many-valued reference may need more lookups per row than it charges.
        """
        bounds = {bound for _, hops in self.chains for bound in hops}
        charge = self.admitted.visible_count * (1 + len(bounds))
        for _, (_, bound) in self.inverses.items():
            charge += self.session.connection.execute(
                "SELECT count(*) FROM (SELECT 1 FROM item_references "
                "WHERE collection_id=? AND field_path=? LIMIT ?)",
                (bound.collection_id, bound.dotted,
                 self.session.limits.max_row_visits + 1)).fetchone()[0]
        return charge

    def _read_target(self, bound, target) -> None:
        """Read the scan's fields of one resolved target, once.

        A related target also folds every field the caller may read into a digest, so a continuation
        refuses once a value it would show changed; the values themselves are read again for a page row.
        """
        if target is None:
            return
        fields = self._needed.get(bound)
        if fields and (bound, target) not in self.reads:
            values = self.reads[bound, target] = bound.read(target, fields)
            self.retain(wire_bytes(values) + _ROW_BYTES)
        if bound in self._related_bounds and (bound, target) not in self.shown:
            self.shown[bound, target] = cursors._hash(bound.read(target, sorted(bound.manifest.schema.fields)))
            self.retain(_ROW_BYTES)

    def _resolve_chunk(self, rows: list[tuple]) -> None:
        """Resolve one chunk's distinct values hop by hop, then read each new target's path fields once."""
        hops = {(chain[0], chain[1] if len(chain) == 2 else None) for _, chain in self.chains}
        for first in {first for first, _ in hops}:
            first.resolve([value for *_, values in rows for value in first.declaration.values(values)])
            for *_, values in rows:
                for value in first.declaration.values(values):
                    self._read_target(first, first._found.get(value))
        for first, second in hops:
            if second is None:
                continue
            wanted = []
            for *_, values in rows:
                for value in first.declaration.values(values):
                    target = first._found.get(value)
                    if target is not None:
                        wanted += second.declaration.values(self.reads.get((first, target), {}))
            second.resolve(wanted)
            for value in wanted:
                self._read_target(second, second._found.get(value))

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

    def chunks(self):
        """The admitted rows in chunks of ``(row_id, item_key, values)``, each chunk's references resolved."""
        admitted, session = self.admitted, self.session
        sql, decode = reductions._scan_sql(
            session, self.collection_id, admitted.layout, admitted.membership_sql,
            self.dependencies(), with_row_id=True)
        project = session._project_values
        visited = 0
        cursor = session.connection.execute(sql, (self.collection_id,))
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
                buffer.append((row_id, typed_rows.decode_item_key(admitted.layout, raw[1]), values))
                if len(buffer) == _CHUNK:
                    self._resolve_chunk(buffer)
                    yield buffer
                    buffer = []
            if buffer:
                self._resolve_chunk(buffer)
                yield buffer
        finally:
            cursor.close()

    def scan(self) -> list[_Row]:
        """Scan the admitted rows and classify each against the filter and its reference chains.

        Returns the combined rows; the counters hold the rest of ``combined``.
        """
        combined = []
        for buffer in self.chunks():
            for row_id, item_key, values in buffer:
                row = _Row(row_id, item_key, values, *self._row_state(values))
                if self.include(row):
                    combined.append(row)
        self.session.check()
        return combined

    def include(self, row: _Row) -> bool:
        """Whether the filter combines ``row``, counting the row into ``combined`` as it decides.

        A row the filter leaves unknown only because a reference did not resolve is excluded and counted.
        """
        outcome = row_predicates.evaluate(
            self.query.where, lambda field: self.value_of(row, field), instant=self.frozen)
        if outcome is False:
            return False
        self.pairs.update(row.pairs)
        if outcome is None:
            self.excluded_unresolved += 1
            return False
        self.count += 1
        if "unresolved" in row.states.values():
            self.unresolved_rows += 1
        self.retain(wire_bytes(row.values) + _ROW_BYTES)
        return True

    def combined(self) -> dict:
        return {"count": self.count, "unresolved_rows": self.unresolved_rows,
                "excluded_unresolved": self.excluded_unresolved,
                "unresolved_values": unresolved_values(self.pairs)}

    def _expanded(self, bound, target) -> dict:
        """Every field of a resolved target that the caller may read, read when a page row needs it."""
        values = self.expansions.get((bound, target))
        if values is None:
            values = self.expansions[bound, target] = bound.read(target, sorted(bound.manifest.schema.fields))
            self.retain(wire_bytes(values) + _ROW_BYTES)
        return values

    def related_out(self, row: _Row) -> dict:
        """The ``related`` object of one output row: state, rows and bounded many-valued items."""
        result = {}
        for name, (limit, bound) in self.inverses.items():
            result[name] = self.inverse_out(name, row, bound, limit)
        for name, limit in self.related.items():
            bound = self.bound[(self.collection_id, name)]
            state = row.states[name]
            if not bound.many:
                out = {"state": state}
                if state == "resolved":
                    target = row.terminals[name]
                    out["row"] = {**self._expanded(bound, target), "item_key": target.item_key}
                result[name] = out
                continue
            values = bound.declaration.values(row.values)
            items = []
            for value in values[:limit]:
                target = bound._found.get(value)
                item = {"value": value, "state": "resolved" if target is not None else "unresolved"}
                if target is not None:
                    item["row"] = {**self._expanded(bound, target), "item_key": target.item_key}
                items.append(item)
            result[name] = {"state": state, "count": len(values), "items": items,
                            "truncated": len(items) < len(values)}
        return result

    def inverse_out(self, name: str, row: _Row, bound, limit: int) -> dict:
        """One row's inverse expansion: the source rows that refer to it, bounded and release-filtered."""
        if not bound.ready(self.session.connection):
            return {"state": "warming"}
        natural = self.session.connection.execute(
            "SELECT natural_key FROM items WHERE row_id=?", (row.row_id,)).fetchone()
        candidates = bound.candidates(
            self.session.connection, natural[0] if natural is not None else None, row.item_key)
        # One visit per RELEASED candidate, as `admit` charges; withheld rows cost and show nothing.
        released = []
        for row_id, key in candidates:
            if bound.released(key):
                released.append((row_id, key))
        self.session.charge_visits(len(released))
        released.sort(key=lambda item: item[1])
        items = [{"item_key": key, **bound.read(row_id)} for row_id, key in released[:limit]]
        return {"state": "ready", "count": len(released), "items": items,
                "truncated": len(released) > len(items)}

    def digest(self) -> str:
        """The resolution digest a continuation binds: each distinct value's outcome and the values it shows."""
        seen = []
        for name, hops in self.chains:
            for position, bound in enumerate(hops):
                entries = []
                for value, target in sorted(bound._found.items(), key=lambda item: item[0]):
                    entries.append([value, None if target is None else
                                    [target.item_key, self.reads.get((bound, target), {}),
                                     self.shown.get((bound, target))]])
                seen.append([f"{name}#{position}", entries])
        return cursors._hash(seen)

    def schema_hash(self) -> str:
        """The schema every collection this query reads has, as the result reports it."""
        targets = sorted({bound.collection_id: bound for _, hops in self.chains for bound in hops}.items())
        return cursors._hash([
            [self.basis.manifest_hash, self.basis.type_name, self.basis.type_version,
             self.basis.declaration_hash],
            [[bound.collection_id, bound.manifest.manifest_version.hash] for _, bound in targets],
            [[bound.collection_id, bound.manifest.manifest_version.hash, bound.dotted]
             for name, (_, bound) in sorted(self.inverses.items())]])

    def binding(self) -> dict:
        """What a continuation binds: the query, the caller, each collection's schema and what the scan saw."""
        if self._binding is None:
            schema = self.schema_hash()
            visible = cursors._hash([
                cursors.visible_basis(self.session, self.collection_id, uniform=self.uniform,
                                      layout=self.admitted.layout, dependencies=set(self.dependencies())),
                self.digest(),
                [[name, bound.ready(self.session.connection)]
                 for name, (_, bound) in sorted(self.inverses.items())]])
            self._binding = {**cursors.caller_binding(self.session, self.query), "schema": schema,
                             "visible": cursors.with_rows(self.session, self.collection_id, visible)}
        return self._binding

    def resume(self, rows: list[_Row]) -> list[_Row]:
        """The rows past the continuation's boundary; a continuation that no longer fits is refused."""
        if self.payload is None:
            return rows
        self.codec.verify(self.payload, self.binding())
        position = next((n for n, row in enumerate(rows) if row.item_key == self.payload["boundary"]), None)
        if position is None:
            raise QueryError("QUERY_CURSOR_STALE")
        return rows[position + 1:]

    def page(self, rows: list[_Row], render) -> dict:
        """One page of ``rows`` within the row limit and the result byte cap, with its continuation."""
        out, size, reason = [], 0, None
        for index, row in enumerate(rows):
            if index >= self.query.page.limit:
                reason = "limit"
                break
            rendered = render(row)
            cost = wire_bytes(rendered) + 2 * bool(out)
            if size + cost > MAX_RESULT_BYTES - _PAGE_RESERVE:
                if not out:
                    raise QueryError("QUERY_RESULT_TOO_LARGE")
                reason = "bytes"
                break
            out.append(rendered)
            size += cost
        token = None
        if reason is not None:
            self.codec = self.codec or cursors._codec(self.session)
            token = self.codec.mint(self.binding(), as_of=self.frozen.isoformat(),
                                    boundary=rows[len(out) - 1].item_key)
        return {"rows": out, "returned": len(out), "has_more": reason is not None, "next_cursor": token,
                "truncated": reason is not None, "truncation_reason": reason}


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


def check_shape(session, query, bound: dict, *, manifest) -> None:
    """Every refusal ``run`` and ``reduce`` make from the request and the declarations; reads no values."""
    _refuse(query, session)
    if query.aggregate is not None:
        reductions._shape(query, manifest, _targets(_chains(query, bound)))


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


def _explain_plan(session, query, execution, *, manifest, uniform, dependencies):
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
        return {**result, "visit_charge": execution.visit_charge()}
    return {**result, "admitted": True, "visit_charge": execution.visit_charge()}


def run(session, query, bound: dict, inverses: dict | None = None, *,
        basis, uniform, manifest, as_of=None) -> dict:
    """Execute, explain, preview or dry-run a row query that names a reference."""
    _refuse(query, session)
    admitted = session.admit(query.source.ref)
    execution = _Execution(session, query, bound, inverses,
                           admitted=admitted, basis=basis, uniform=uniform)
    if query.mode in {"explain", "preview", "dry_run"}:
        return _explain_plan(session, query, execution, manifest=manifest,
                             uniform=uniform, dependencies=execution.dependencies())
    execution.freeze(as_of)
    rows = execution.scan()
    _sort_rows(execution, rows, query.order_by.keys)
    rows = execution.resume(rows)
    page = execution.page(rows, lambda row: _output_row(execution, admitted, row, query.select.fields))
    result = {**page, "total": execution.count, "as_of": execution.frozen.isoformat(),
              "source": asdict(query.source), "query_version": query.version,
              "schema_version": manifest.schema.version, "schema_fingerprint": execution.schema_hash(),
              "mode": query.mode, "execution_profile": session.limits.profile,
              "combined": execution.combined()}
    session.check()
    if wire_bytes(result) > MAX_RESULT_BYTES:
        raise QueryError("QUERY_RESULT_TOO_LARGE")
    return result


# --- grouped reductions (C) -----------------------------------------------------


def reduce(session, query, bound: dict, inverses: dict | None = None, *,
           basis, uniform, manifest, as_of=None) -> dict:
    """One filtered base-row reduction: whole groups plus a page of the combined rows."""
    _refuse(query, session)
    collection_id = query.source.ref
    admitted = session.admit(collection_id)
    execution = _Execution(session, query, bound, inverses,
                           admitted=admitted, basis=basis, uniform=uniform)
    execution.freeze(as_of)
    shape = reductions._shape(query, manifest, _targets(execution.chains))
    visit_reason = "filtered" if (query.where is not None
                                  or any(value.where is not None for value in shape.values)) else "references"
    basis_out = None if shape.basis is None else {
        "field": shape.basis.field, "bucket": shape.timed.bucket, "offset": shape.basis.offset,
        "kind": shape.basis.kind, "from": shape.timed.window_from, "to": shape.timed.window_to}
    plan = {"strategy": "base", "reason": visit_reason,
            "release": "uniform" if uniform else "admitted", "rollup": None, "basis": basis_out}
    if query.mode in {"explain", "preview", "dry_run"}:
        result = {"source": asdict(query.source), "query_version": query.version,
                  "schema_version": manifest.schema.version, "mode": query.mode,
                  "as_of": execution.frozen.isoformat(),
                  "execution_profile": session.limits.profile, "bounds": session.limits.bounds(),
                  "plan": plan, "references": execution.names()}
        if query.mode == "explain":
            return result
        if query.mode == "preview":
            return {**result, "visit_charge": execution.visit_charge()}
        return {**result, "admitted": True, "visit_charge": execution.visit_charge()}

    wheres = {value.name: _aggregate_where(execution, value) for value in shape.values if value.where is not None}
    combined_rows: list[_Row] = []

    def include(row: _Row) -> bool:
        if not execution.include(row):
            return False
        combined_rows.append(row)
        return True

    injected = [(field, f"{field.alias}.{field.path}") for field in execution.fields
                if field.alias is not None]

    def scanned():
        for buffer in execution.chunks():
            yield from _classified(execution, buffer, injected)

    groups, flagged = reductions._group_rows(
        session.limits, shape, scanned(), None, include=include, wheres=wheres)
    session.check()
    if shape.timed is None and not shape.others and not groups:
        groups[("", "[]")] = [(), 0, {name: Accumulator() for name in shape.fields},
                              {"filtered": {}, "unresolved_rows": 0, "pairs": Counter()}]
    ordered = sorted(groups.items(), key=lambda item: item[0])
    combined_rows.sort(key=lambda row: row.item_key)
    combined_rows = execution.resume(combined_rows)

    group_rows = [reductions._row(shape, bucket, *state) for (bucket, _), state in ordered]
    envelope = {"source": asdict(query.source), "query_version": query.version,
                "schema_version": manifest.schema.version, "mode": query.mode,
                "as_of": execution.frozen.isoformat(), "execution_profile": session.limits.profile,
                "bounds": session.limits.bounds(), "plan": plan,
                "flagged_rows": flagged, "flagged_scope": "collection",
                "admitted_rows": admitted.visible_count,
                "groups": group_rows, "combined": execution.combined()}
    if wire_bytes({**envelope, "rows": [], "returned": 0, "has_more": False,
                   "next_cursor": None, "truncated": False, "truncation_reason": None}) \
            > MAX_RESULT_BYTES - _PAGE_RESERVE:
        raise QueryError("QUERY_RESULT_TOO_LARGE",
                         "the groups alone exceed the result cap; coarsen group_by")
    item_key_withheld = session._field_plans[collection_id].item_key_withheld
    page = execution.page(combined_rows, lambda row: _combined_row(
        execution, admitted, row, item_key_withheld=item_key_withheld))
    result = {**envelope, **page}
    session.check()
    if wire_bytes(result) > MAX_RESULT_BYTES:
        raise QueryError("QUERY_RESULT_TOO_LARGE")
    return result


def _aggregate_where(execution, value):
    """One aggregate's own filter over the row context, three-valued like the query's."""

    def accept(row: _Row) -> bool | None:
        return row_predicates.evaluate(
            value.where, lambda field: execution.value_of(row, field), instant=execution.frozen)

    return accept


def _classified(execution, buffer, injected):
    """Yield (item_key, values with path values injected, row context) for one resolved chunk."""
    for row_id, item_key, values in buffer:
        row = _Row(row_id, item_key, values, *execution._row_state(values))
        merged = dict(values)
        for field, public in injected:
            value = execution.value_of(row, field)
            if value is not MISSING:
                merged[public] = value
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
