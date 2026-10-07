"""Dark whole-row typed execution; no field-classification or public route."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict, dataclass, replace

from ..collection_store import index_migrations
from ..collection_store.query_indexes import ProjectionPlan
from . import ir, typed_sql, validation
from .runtime import _MAX_DECODE_BYTES, MAX_RESULT_BYTES, QueryError, ReadSession, wire_bytes

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


def declaration(manifest, basis, *, withheld=frozenset()) -> dict:
    """The validator's declaration of one admitted collection.

    ``withheld`` names fields some of whose values may be withheld from the caller;
    the validator lets a query select them but not filter, sort, group or join on them.
    """
    fields = {name: {"type": spec.type, "enum": spec.enum, **({"withheld_values": True} if name in withheld else {})}
              for name, spec in manifest.schema.fields.items()}
    fields["item_key"] = {"type": "string"}
    return {"domain": "collections", "type": basis.type_name, "vault": basis.logical_vault_id, "fields": fields}


def _validate(query, manifest, basis):
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
    result = validation.normalize_query(request, declarations={manifest.collection_id: declaration(manifest, basis)},
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


def check_shape(query, manifest, basis) -> None:
    """Every refusal a row query gets from its request and declaration; compose runs it too."""
    _refuse_request(query)
    _validate(query, manifest, basis)


def admit_query(session: ReadSession, query: ir.Query, *, as_of: str) -> AdmittedQuery:
    session.check()
    if getattr(query, "mode", None) != "execute":
        raise QueryError("QUERY_UNSUPPORTED")
    _refuse_request(query)
    if query.page.after is not None:
        raise QueryError("QUERY_VALUE_INVALID", "an authenticated continuation is required")
    conn = session.connection
    with session._manifest(query.source.ref) as (manifest, basis, subjects, uniform):
        fields, schema = _validate(query, manifest, basis)
        projection = index_migrations.ready_plan(conn, manifest.collection_id)
        if projection is None:
            raise QueryError("QUERY_UNAVAILABLE")
        for scalar in projection.scalars:
            spec = manifest.schema.fields.get(scalar.field)
            if spec is None or spec.type != scalar.kind or scalar.path != (scalar.field,):
                raise QueryError("QUERY_UNAVAILABLE")
        compiled = typed_sql.compile_rows(query, projection, as_of=as_of)
        ordinal = len(session._queries) + 1
        ids = f"query_ids_{ordinal}"
        materialize = not uniform or compiled.usable_index is None
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
        elif not compiled.page_bound:
            count = conn.execute(
                "SELECT count(*) FROM (SELECT 1 FROM items WHERE collection_id=? LIMIT ?)",
                (manifest.collection_id, session.limits.max_row_visits + 1),
            ).fetchone()[0]
        cost = query.page.limit + 1 if count is None else count * 2
        if materialize:
            # Copy plus all B-tree insertion/search work is bounded before keys
            # or predicates are evaluated; there is no external sort spill.
            cost += count * (2 + (len(projection.indexes) + 2) * max(1, count.bit_length()))
        if cost + session._estimated_visits > session.limits.max_row_visits:
            raise QueryError("QUERY_COST_LIMIT")
        session._estimated_visits += cost
        membership = "1" if uniform else f"EXISTS (SELECT 1 FROM temp.{ids} a WHERE a.row_id=i.row_id)"
        table = "main." + projection.table_name
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
            if uniform:
                source = f"main.{projection.table_name} p NOT INDEXED"
                where, params = "1", ()
            else:
                source = f"temp.{ids} a CROSS JOIN main.{projection.table_name} p NOT INDEXED"
                where, params = "p.row_id=a.row_id", ()
            conn.execute(f"INSERT INTO {table}(row_id,item_key,row_version,keys_json) "
                         f"SELECT p.row_id,p.item_key,p.row_version,p.keys_json FROM {source} WHERE {where}", params)
        session.check_temp()
        result = AdmittedQuery(session, query, projection, compiled,
                               (basis.manifest_hash, basis.type_name, basis.type_version, basis.declaration_hash, schema),
                               manifest.schema.version,
                               tuple(f.path for f in query.select.fields) or fields,
                               table, index, membership, session.typed_layout(manifest.collection_id),
                               uniform, count, cost, ordinal, _SEAL)
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
    terms = ",".join(column for column, _ in compiled.order_terms)
    cursor = conn.execute(f"SELECT p.row_id,{terms} FROM {admitted.table} p INDEXED BY {admitted.index} "
                          f"WHERE {compiled.where_sql} ORDER BY {compiled.order_sql} LIMIT ?",
                          (*compiled.params, admitted.query.page.limit + 1))
    rows, size, last = [], 2, None
    with closing(session.fetch(cursor)) as batches:
        for batch in batches:
            for row_id, *order in batch:
                if len(rows) == admitted.query.page.limit:
                    return RowPage(rows, True, last)
                identity = conn.execute(
                    f"SELECT CASE WHEN {admitted.membership_sql} THEN i.item_key END "
                    "FROM main.items i WHERE i.row_id=? AND i.collection_id=?",
                    (row_id, admitted.query.source.ref),
                ).fetchone()
                if identity is None or identity[0] is None:
                    raise QueryError("QUERY_UNAVAILABLE")
                try:
                    raw = values(row_id, identity[0])
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
