"""Dark whole-row typed execution; no field-classification or public route."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict, dataclass, replace

from ..collection_store import derived_rows, index_migrations, native_identity, native_operands
from ..collection_store.native_projections import NativeProjectionPlan
from ..collection_store.query_indexes import ProjectionPlan
from . import ir, typed_sql, validation
from .ordered_seeks import Seek
from .runtime import _MAX_DECODE_BYTES, MAX_RESULT_BYTES, QueryError, ReadSession, wire_bytes
from .scalars import MISSING

_SEAL = object()
_RESPONSE_RESERVE_BYTES = 8 * 1024


@dataclass(frozen=True, slots=True)
class AdmittedQuery:
    session: ReadSession
    query: ir.Query
    projection: ProjectionPlan
    compiled: typed_sql.CompiledRows
    schema_identity: tuple
    schema_version: int
    fields: tuple[str, ...]
    table: str
    index: str
    membership_sql: str
    layout: object
    uniform: bool
    released_count: int | None
    estimated_visits: int
    ordinal: int
    _seal: object
    items: str = "main.items"  # the relation standing for ``items``: the store's, or a derived file's

    def check(self):
        if self._seal is not _SEAL or self.session._queries.get(self.ordinal) is not self:
            raise QueryError("COLLECTION_NOT_FOUND")
        self.session.check()

    def __reduce__(self):
        raise TypeError("query admission is request-local")


@dataclass(frozen=True, slots=True)
class RowPage:
    rows: list[dict]
    has_more: bool
    last_order_key: tuple | None
    total: int | None = None


def logical_item_key(admitted, value):
    """Restore public identities; canonical items already retain their logical TEXT key."""
    if isinstance(admitted.projection, NativeProjectionPlan) and type(value) is bytes:
        try:
            return native_identity.decode(value, admitted.projection.layout.identity)
        except (ValueError, UnicodeError) as error:
            raise QueryError("QUERY_UNAVAILABLE") from error
    if type(value) is not str:
        raise QueryError("QUERY_UNAVAILABLE")
    return value


def indexed_relation(admitted, *, hinted=True):
    """Resolve native operand aliases without putting a table hint on a subquery."""
    if isinstance(admitted.projection, NativeProjectionPlan) and not admitted.table.startswith("temp."):
        return admitted.projection.read_relation(admitted.table.split(".", 1)[0],
                                                 index=admitted.index if hinted else None) + " p"
    return admitted.table + " p" + (" INDEXED BY " + admitted.index if hinted else "")


#: The closed predicate operators of the query IR that an index interval serves.
# nosemgrep: ep-word-set -- The query grammar fixes these comparison operators.
_SEEKING_OPS = frozenset({"eq", "gt", "gte", "lt", "lte", "between"})


def _index_interval(query, projection, schema, as_of):
    """The relation, predicate and parameters of the one index interval that bounds a copy's reads.

    Only conjunct leaves on an index's leading field narrow its scan, so only they are counted;
    the copy applies the request's whole predicate inside that interval.
    """
    leaves = typed_sql._leaves(query.where) or ()
    best = None
    for name, spec in zip(projection.index_names, projection.indexes, strict=True):
        seeking = tuple(leaf for leaf in leaves if leaf.op in _SEEKING_OPS and leaf.field.path == spec.keys[0].field)
        if seeking and (best is None or len(spec.keys) < best[0]):
            best = (len(spec.keys), name, seeking)
    if best is None:
        return None
    _, name, seeking = best
    where = seeking[0] if len(seeking) == 1 else ir.Filter("all", children=seeking)
    interval = typed_sql.compile_rows(replace(query, where=where), projection, as_of=as_of)
    return projection.read_relation(schema, index=name) + " p", interval.where_sql, interval.params


def _filter_fields(node) -> set[str]:
    if node is None:
        return set()
    if node.children:
        return set().union(*(_filter_fields(child) for child in node.children))
    return {node.field.path}


def _full_scan(query, projection, schema) -> str:
    """The relation a whole-collection copy reads: no index narrows it, so scanning the narrowest
    index that holds every order and filter operand lets SQLite read them instead of recomputing them."""
    needed = (_filter_fields(query.where) | {key.field.path for key in query.order_by.keys}) - {"item_key"}
    covering = sorted((len(spec.keys), name) for name, spec in zip(projection.index_names, projection.indexes,
                                                                    strict=True)
                      if needed <= {key.field for key in spec.keys})
    if covering:
        return projection.read_relation(schema, index=covering[0][1]) + " p"
    return projection.read_relation(schema, not_indexed=True) + " p"


def _populate_native(session, manifest, projection, private, table, scanned, where, params, layout, compiled, *,
                     operands=False):
    """Stream only admitted query dependencies into the request's scalar TEMP relation."""
    fields = {scalar.field for scalar in projection.scalars
              if scalar.field in manifest.schema.fields and scalar.field in compiled.dependency_paths}
    columns = ("row_id", "item_key", "row_version", *(f"k{ordinal}_{part}" for ordinal in range(len(private.scalars))
                                                     for part in ("rank", "tag", "band", "key")))
    if operands and fields <= session._field_plans[manifest.collection_id].whole_fields:
        # Every dependency is released whole, so the READY relation's operand expressions (the
        # ones its indexes are built on) equal the operands of the released values; ordinals the
        # query does not depend on stay missing, as the per-row path leaves them.
        selected, constants = ["p.row_id", "p.item_key", "p.row_version"], []
        for ordinal, scalar in enumerate(private.scalars):
            if scalar.field in fields:
                selected += [f"p.k{ordinal}_{part}" for part in ("rank", "tag", "band", "key")]
            else:
                selected += ["?"] * 4
                constants += native_operands.ranked_operand(MISSING, scalar.kind)
        session.connection.execute(f"INSERT INTO {table}({','.join(columns)}) SELECT {','.join(selected)} "
                                   f"FROM {scanned} WHERE {where}", (*constants, *params))
        return
    sql = f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})"

    def parameters(rows):
        for row_id, item_key, version in rows:
            session.check()
            values = session.selected_values(row_id, layout, fields, max_bytes=_MAX_DECODE_BYTES, check=session.check)
            logical = native_identity.decode(item_key, projection.layout.identity)
            encoded = private.admitted_parameters(row_id, logical, version, values)
            yield tuple(encoded[name] for name in columns)

    with closing(session.connection.execute(f"SELECT p.row_id,p.item_key,p.row_version FROM {scanned} WHERE {where}", params)) as rows:
        session.connection.executemany(sql, parameters(rows))


def declaration(manifest, basis, *, projected=frozenset(), item_key_withheld=False) -> dict:
    """The validator's declaration of one admitted collection.

    ``projected`` names fields whose values are projected per caller before a result
    is built; the validator lets a query read them but not filter, sort or join on them.
    ``item_key_withheld`` marks an item key that spells a natural-key field the caller cannot see: the validator
    refuses it everywhere a caller names a field, and still lets it break ties, which no response shows.
    """
    fields = {name: {"type": spec.type, "enum": spec.enum, **({"projected": True} if name in projected else {})}
              for name, spec in manifest.schema.fields.items()}
    fields["item_key"] = {"type": "string", **({"withheld": True} if item_key_withheld else {})}
    return {"domain": "collections", "type": basis.type_name, "vault": basis.logical_vault_id,
            "fields": fields, "field_admission": True}


def _validate(query, manifest, basis, *, item_key_withheld=False):
    """Rebind raw IR to the current schema using the existing closed validator."""
    def literal(value):
        if isinstance(value, tuple):
            return [literal(item) for item in value]
        return asdict(value) if isinstance(value, ir.RelativeDate | ir.Range) else value

    def predicate(node, depth=0):
        if depth > 8:
            raise QueryError("QUERY_INPUT_LIMIT")
        if node.op in {"all", "any", "not"}:
            if len(node.children) > 64 or (node.op == "not" and len(node.children) != 1):
                raise QueryError("QUERY_INPUT_LIMIT")
            children = [predicate(child, depth + 1) for child in node.children]
            return {node.op: children[0] if node.op == "not" else children}
        result = {"field": node.field.path, "op": node.op}
        if node.value is not None:
            result["value"] = literal(node.value)
        return result

    order = query.order_by.keys
    if order and order[-1] == ir.SortKey(ir.Field(query.source, "item_key", "string")):
        order = order[:-1]
    request = {"version": query.version, "select": [f.path for f in query.select.fields],
               "order_by": [{"field": k.field.path, "direction": k.direction, "nulls": k.nulls}
                            for k in order], "page": {"limit": query.page.limit}}
    if query.where is not None:
        request["where"] = predicate(query.where)
    if query.as_of is not None:
        request["as_of"] = query.as_of
    result = validation.normalize_query(request, declarations={manifest.collection_id: declaration(manifest, basis, item_key_withheld=item_key_withheld)},
                                        collection=manifest.collection_id)
    if result.findings:
        raise QueryError(result.findings[0].code)
    # Re-normalization also verifies source/type/enum bindings and tie-breakers.
    if result.query != query:
        raise QueryError("QUERY_VALUE_INVALID", "query does not match the current declaration")
    return tuple(manifest.schema.fields), tuple((name, spec.type, tuple(spec.enum)) for name, spec in manifest.schema.fields.items())


def _refuse_request(query) -> None:
    """Refusals a row query's request decides alone, before any declaration or value is read."""
    if (not isinstance(query, ir.Query) or query.source.domain != "collections"
            or query.joins or query.aggregate is not None or query.having is not None
            or query.text is not None or query.graph is not None):
        raise QueryError("QUERY_UNSUPPORTED")
    if query.execution_profile != "interactive":
        raise QueryError("QUERY_UNSUPPORTED", "row pages run under the interactive profile")


def check_shape(query, manifest, basis, *, item_key_withheld=False) -> None:
    """Every refusal a row query gets from its request and declaration; compose runs it too."""
    _refuse_request(query)
    _validate(query, manifest, basis, item_key_withheld=item_key_withheld)


def admit_query(session: ReadSession, query: ir.Query, *, as_of: str) -> AdmittedQuery:
    session.check()
    if getattr(query, "mode", None) != "execute":
        raise QueryError("QUERY_UNSUPPORTED")
    _refuse_request(query)
    if query.page.after is not None:
        raise QueryError("QUERY_VALUE_INVALID", "an authenticated continuation is required")
    conn = session.connection
    with session._manifest(query.source.ref) as (manifest, basis, subjects, uniform):
        fields, schema = _validate(query, manifest, basis, item_key_withheld=session._field_plans[manifest.collection_id].item_key_withheld)
        source = session.row_source(manifest.collection_id)
        if source is not derived_rows.STORE and not uniform:
            raise QueryError("QUERY_UNAVAILABLE", "a derived collection's rows are released whole or not at all")
        projection = index_migrations.ready_plan(conn, manifest.collection_id)
        if projection is None:
            # Disclosed only after the session admitted the collection: an operator diagnostic.
            if index_migrations.unready_state(conn, manifest.collection_id) == "failed":
                raise QueryError("QUERY_PROJECTION_FAILED", "this collection's query projection could not be built")
            raise QueryError("QUERY_PROJECTION_BUILDING", "this collection's query projection is still building")
        for scalar in projection.scalars:
            spec = manifest.schema.fields.get(scalar.field)
            if spec is None:
                continue
            if spec.type != scalar.kind or scalar.path != (scalar.field,):
                raise QueryError("QUERY_UNAVAILABLE")
        compiled = typed_sql.compile_rows(query, projection, as_of=as_of)
        ordinal = len(session._queries) + 1
        ids = f"query_ids_{ordinal}"
        materialize = not uniform or compiled.usable_index is None
        copy = bound = None
        if uniform and materialize and isinstance(projection, NativeProjectionPlan) and not compiled.residual:
            # SQL fills a uniform copy from the READY relation's operand columns. Only a predicate proven
            # to seek an index interval narrows what the copy reads, and so what it is charged.
            bound = _index_interval(query, projection, source.schema, as_of)
            relation = bound[0] if bound else _full_scan(query, projection, source.schema)
            copy = (relation, compiled.where_sql, compiled.params)
        count = None
        if not uniform:
            conn.execute(f"CREATE TEMP TABLE {ids}(row_id INTEGER PRIMARY KEY)")
            count = 0
            for subject in subjects:
                session.check()
                if session._authorization.decision(subject).level >= 6:
                    count += 1
                    if count + session._estimated_visits > session.limits.max_row_visits:
                        raise QueryError("QUERY_COST_LIMIT")
                    conn.execute(f"INSERT INTO temp.{ids} VALUES(?)", (subject.row_id,))
                    session.check_temp()
        elif bound is not None:
            relation, where, params = bound
            count = conn.execute(f"SELECT count(*) FROM (SELECT 1 FROM {relation} WHERE {where} LIMIT ?)",
                                 (*params, session.limits.max_row_visits + 1)).fetchone()[0]
        elif not compiled.page_bound:
            count = conn.execute(
                f"SELECT count(*) FROM (SELECT 1 FROM {source.items} WHERE collection_id=? LIMIT ?)",
                (manifest.collection_id, session.limits.max_row_visits + 1),
            ).fetchone()[0]
        cost = query.page.limit + 1 if count is None else count * 2
        if materialize:
            # Copy plus all B-tree insertion/search work is charged before the copy runs, on the
            # rows it can read: the whole collection, or a proven index interval counted by its
            # own seek. There is no external sort spill.
            cost += count * (2 + (len(projection.indexes) + 2) * max(1, count.bit_length()))
        if cost + session._estimated_visits > session.limits.max_row_visits:
            raise QueryError("QUERY_COST_LIMIT")
        session._estimated_visits += cost
        membership = "1" if uniform else f"EXISTS (SELECT 1 FROM temp.{ids} a WHERE a.row_id=i.row_id)"
        stored = f"{source.schema}.{projection.table_name}"
        table = stored
        index = compiled.usable_index
        if materialize:
            # A private template ordinal is separate from the frozen READY
            # mapping generation. Each handle owns its own TEMP relation.
            private = replace(projection, generation=ordinal)
            compiled = typed_sql.compile_rows(query, private, as_of=as_of)
            for statement in private.create_ddl(temporary=True):
                conn.execute(statement)
            table = "temp." + private.table_name
            index = compiled.usable_index
            if index is None:
                index = f"{private.table_name}_order"
                order = compiled.order_sql.replace("p.", "")
                conn.execute(f"CREATE INDEX temp.{index} ON {private.table_name}({order})")
            if copy is not None:
                scanned, where, params = copy
            elif uniform:
                scanned = f"{stored} p NOT INDEXED"
                where, params = "1", ()
            else:
                scanned = f"temp.{ids} a CROSS JOIN {stored} p NOT INDEXED"
                where, params = "p.row_id=a.row_id", ()
            # Projection ordinals are fixed by the validated internal layout; withheld keys never enter TEMP.
            if isinstance(projection, NativeProjectionPlan):
                _populate_native(session, manifest, projection, private, table, scanned, where, params,
                                 source.layout or session.typed_layout(manifest.collection_id), compiled,
                                 operands=copy is not None)
            else:
                keys = ",".join(f"json_extract(p.keys_json,'$[{ordinal}]')" if scalar.field in manifest.schema.fields
                                else "json('[0,null]')" for ordinal, scalar in enumerate(projection.scalars))
                conn.execute(f"INSERT INTO {table}(row_id,item_key,row_version,keys_json) "
                             f"SELECT p.row_id,p.item_key,p.row_version,json_array({keys}) FROM {scanned} WHERE {where}", params)
        session.check_temp()
        selected = tuple(f.path for f in query.select.fields) or fields
        dependencies = set(compiled.dependency_paths) | set(selected)
        schema = tuple(entry for entry in schema if entry[0] in dependencies)
        result = AdmittedQuery(session, query, projection, compiled,
                               (basis.manifest_hash, basis.type_name, basis.type_version, basis.declaration_hash, schema),
                               manifest.schema.version,
                               selected,
                               table, index, membership, source.layout or session.typed_layout(manifest.collection_id),
                               uniform, count, cost, ordinal, _SEAL, source.items)
        session._queries[ordinal] = result
        return result


def execute_rows(admitted: AdmittedQuery, *, max_response_bytes: int | None = None) -> RowPage:
    """Execute only a sealed request-bound handle; order metadata stays internal."""
    if not isinstance(admitted, AdmittedQuery):
        raise QueryError("QUERY_UNSUPPORTED")
    admitted.check()
    budget = MAX_RESULT_BYTES - _RESPONSE_RESERVE_BYTES
    if max_response_bytes is not None:
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise QueryError("QUERY_VALUE_INVALID")
        budget = min(budget, max_response_bytes)
    session, compiled = admitted.session, admitted.compiled
    conn = session.connection

    def text(op, payload, value):
        admitted.check()
        result = typed_sql.text_predicate(op, payload, value)
        admitted.check()
        return result

    def identity_text(op, payload, value):
        admitted.check()
        logical = logical_item_key(admitted, payload)
        result = typed_sql.text_predicate(op, logical.encode("utf-8").hex(), value)
        admitted.check()
        return result

    def values(row_id, item_key):
        try:
            admitted.check()
            maximum = min(_MAX_DECODE_BYTES, session.limits.max_temp_bytes)
            selected = session.selected_values(row_id, admitted.layout,
                                               (name for name in admitted.fields if name != "item_key"),
                                               max_bytes=maximum, check=admitted.check)
            if "item_key" in admitted.fields:
                selected["item_key"] = item_key
            if session._project_values is not None:
                selected = session._project_values(selected)
                selected = {name: value for name, value in selected.items() if name in admitted.fields}
            encoded = json.dumps(selected, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            admitted.check()
            return encoded
        except (ValueError, TypeError, RecursionError) as error:
            raise QueryError("QUERY_COST_LIMIT") from error

    conn.create_function("exomem_typed_text", 3, text)
    if isinstance(admitted.projection, NativeProjectionPlan):
        conn.create_function("exomem_typed_identity_text", 3, identity_text)
    terms = ",".join(column for column, _ in compiled.order_terms)
    rows, size, last = [], 2, None
    seeks = ((Seek(compiled.where_sql, compiled.params, compiled.order_sql),)
             if compiled.seeks is None else compiled.seeks)
    relation = indexed_relation(admitted)
    for seek in seeks:
        admitted.check()
        order = " ORDER BY " + seek.order_sql if seek.order_sql else ""
        cursor = conn.execute(f"SELECT p.row_id,{terms} FROM {relation} "
                              f"WHERE {seek.where_sql}{order} LIMIT ?",
                              (*seek.params, admitted.query.page.limit + 1 - len(rows)))
        with closing(session.fetch(cursor)) as batches:
            for batch in batches:
                for row_id, *order in batch:
                    if len(rows) == admitted.query.page.limit:
                        return RowPage(rows, True, last)
                    identity = conn.execute(
                        f"SELECT CASE WHEN {admitted.membership_sql} THEN i.item_key END "
                        f"FROM {admitted.items} i WHERE i.row_id=? AND i.collection_id=?",
                        (row_id, admitted.query.source.ref),
                    ).fetchone()
                    if identity is None or identity[0] is None:
                        raise QueryError("QUERY_UNAVAILABLE")
                    try:
                        raw = values(row_id, logical_item_key(admitted, identity[0]))
                    except QueryError as error:
                        if error.code == "QUERY_RESULT_TOO_LARGE" and rows:
                            return RowPage(rows, True, last)
                        raise
                    row = json.loads(raw)
                    row_size = wire_bytes(row) + 2 * bool(rows)
                    if size + row_size > budget:
                        if not rows:
                            raise QueryError("QUERY_RESULT_TOO_LARGE")
                        return RowPage(rows, True, last)
                    size += row_size
                    rows.append(row)
                    last = tuple(order)
    admitted.check()
    return RowPage(rows, False, last)
