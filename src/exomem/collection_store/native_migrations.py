"""Bounded native payload construction and atomic query/payload publication."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace

from .. import item_keys
from . import (
    index_migrations,
    native_identity,
    native_layout,
    native_metadata,
    tokens,
    typed_storage,
)
from .native_operands import FileRole
from .native_projections import NativeProjectionPlan


def _candidate(conn, cid):
    row = conn.execute("SELECT generation,plan_json FROM query_projection_mappings "
                       "WHERE collection_id=? AND state='building'", (cid,)).fetchone()
    return None if row is None else index_migrations._plan(cid, *row, connection=conn)


def fail(conn, plan, diagnostic=None):
    """Fail only an unpublished pair; its descriptor and bounded reason remain inspectable."""
    index_migrations._failed(conn, plan)
    conn.execute(f"DROP TABLE {plan.table_name}")
    conn.execute("UPDATE typed_encoding_mappings SET state='failed',diagnostic_json=? "
                 "WHERE collection_id=? AND generation=? AND state='building'",
                 (None if diagnostic is None else typed_storage.canonical_json(diagnostic),
                  plan.collection_id, plan.layout.generation))
    return "failed"


def _template(conn, cid):
    """The declared query intent a conversion carries: a building or ready plan, else the current one that failed.

    A first build that failed on a stored row, such as one over the JSON index cell limit, still matches
    the current declaration, and the native pair can hold that row. A declaration that never normalized
    has no plan to match.
    """
    plan = _candidate(conn, cid) or index_migrations.ready_plan(conn, cid)
    if plan is None:
        row = conn.execute("SELECT m.generation,m.plan_json FROM query_projection_mappings m JOIN collections c "
                           "ON c.collection_id=m.collection_id AND c.query_plan_hash=m.plan_hash "
                           "WHERE m.collection_id=? AND m.state='failed' ORDER BY m.generation DESC LIMIT 1",
                           (cid,)).fetchone()
        plan = None if row is None else index_migrations._plan(cid, *row, connection=conn)
    return plan


def _begin(conn, cid, fields, *, identity=None):
    """Start a native candidate; its identity codec follows the collection's item key scheme, unless one is forced."""
    scheme, derived = conn.execute("SELECT item_key_scheme,derived FROM collections WHERE collection_id=?",
                                   (cid,)).fetchone()
    if identity is None:
        identity = native_identity.ORDERED if scheme == item_keys.ORDERED_V1 else native_identity.UUID16
    template = _template(conn, cid)
    if template is None:
        raise typed_storage.TypedStorageError("native migration requires declared query intent")
    pending = conn.execute("SELECT generation,layout_json FROM typed_encoding_mappings "
                           "WHERE collection_id=? AND state='building'", (cid,)).fetchone()
    if pending is not None:
        previous = typed_storage._layout(cid, *pending)
        if previous.encoding != typed_storage.TYPED_V1:
            raise typed_storage.TypedStorageError("native payload candidate has no matching query candidate")
        typed_storage._fail(conn, previous)
    if _candidate(conn, cid) is not None:
        index_migrations._failed(conn, template)
        conn.execute(f"DROP TABLE {template.table_name}")
    generation = conn.execute("SELECT COALESCE(MAX(generation),0)+1 FROM typed_encoding_mappings "
                              "WHERE collection_id=?", (cid,)).fetchone()[0]
    layout = (replace(fields, generation=generation, identity=identity) if isinstance(fields, native_layout.NativeLayout)
              else native_layout.build(cid, generation, fields, identity=identity))
    conn.execute("INSERT INTO typed_encoding_mappings(collection_id,generation,state,layout_json,layout_hash) "
                 "VALUES(?,?,'building',?,?)", (cid, generation, layout.encoded,
                                               hashlib.sha256(layout.encoded.encode()).hexdigest()))
    typed_storage.create_history(conn, layout)
    query_generation = conn.execute("SELECT COALESCE(MAX(generation),0)+1 FROM query_projection_mappings "
                                    "WHERE collection_id=?", (cid,)).fetchone()[0]
    plan = NativeProjectionPlan(cid, query_generation, template.scalars, template.indexes, layout,
                                FileRole.DERIVED if derived else FileRole.STORE)
    conn.execute("UPDATE collections SET query_plan_hash=? WHERE collection_id=?", (index_migrations._hash(plan), cid))
    index_migrations.begin_build(conn, plan)
    return plan


def _value_bound(tag, kind, length):
    """One typed-v1 value cell's canonical text bound, or None when its stored kind does not match its tag."""
    if tag in (typed_storage.MISSING, typed_storage.NULL):
        return 0 if kind == "null" else None
    if tag in (typed_storage.BOOLEAN, typed_storage.INT64):
        return 32 if kind == "integer" else None
    if tag == typed_storage.FLOAT:
        return 32 if kind == "blob" and length == 8 else None
    if tag == typed_storage.STRING:
        return 6 * length + 2 if kind == "text" else None
    # Decoding accepts only exact digits; a JSON cell's bound holds once _extent proves it canonical.
    if tag in (typed_storage.BIGINT, typed_storage.JSON):
        return length if kind == "text" else None
    return None


def _canonical(raw, table, name, row_id):
    """Stream one stored JSON cell against its canonical spelling without hydrating it."""
    import ijson

    from .native_json import canonical_events

    with raw.blobopen(table, name, row_id, readonly=True) as source, \
            raw.blobopen(table, name, row_id, readonly=True) as comparison:
        try:
            for _ in canonical_events(source, max_bytes=len(source), check=lambda: None,
                                      comparison=comparison, byte_length=len(source)):
                pass
        except (typed_storage.TypedStorageError, ijson.JSONError):
            return False
    return True


def _extent(conn, source, row_id):
    """Inspect lengths before fetching values, including the source proof's key and body."""
    raw = native_metadata.sqlite_connection(conn)
    with raw.blobopen("items", "item_key", row_id, readonly=True) as key, \
            raw.blobopen("items", "body", row_id, readonly=True) as body:
        size = len(key) + len(body)
    if source is None:
        with raw.blobopen("items", "values_json", row_id, readonly=True) as values:
            return size + len(values)
    count = len(source.fields)
    # A tag is fetched only when stored as an integer; every other cell is sized through its blob handle.
    columns = ",".join(f"CASE WHEN typeof(t{i})='integer' THEN t{i} END,typeof(t{i}),typeof(v{i}),typeof(k{i})"
                       for i in range(count))
    row = conn.execute(f"SELECT {columns},typeof(r) FROM {source.current_relation} WHERE row_id=?",
                       (row_id,)).fetchone()
    if row is None:
        raise typed_storage.TypedStorageError("source typed row is absent")

    def cell(name, kind):
        """The escape bound (one stored byte can render as six) and the stored length of one cell."""
        if kind not in ("text", "blob"):
            return (0 if kind == "null" else 32), None
        with raw.blobopen(source.current_table, name, row_id, readonly=True) as handle:
            return 6 * len(handle), len(handle)

    loose = tight = size + 32 * count + len(typed_storage.canonical_json(source.fields).encode())
    proofs = []
    for i in range(count):
        tag, tag_kind, value_kind, key_kind = row[4 * i:4 * i + 4]
        charge, length = cell(f"v{i}", value_kind)
        charge += cell(f"t{i}", tag_kind)[0] + cell(f"k{i}", key_kind)[0]
        bound = _value_bound(tag, value_kind, length)
        loose += charge
        # The sort key is never reconstructed; an unknown tag or a mismatched kind keeps the escape bound.
        tight += charge if bound is None else 32 + bound
        if bound is not None and tag == typed_storage.JSON:
            proofs.append(f"v{i}")
    charge, length = cell("r", row[-1])
    loose += charge
    tight += length if row[-1] == "text" else charge
    if row[-1] == "text":
        proofs.append("r")
    # The escape bound stands unless it alone refuses the row. A non-canonical JSON cell could re-spell
    # its numbers larger before parity rejects it, so the tag-aware bound needs each JSON cell streamed
    # as canonical first, and only a row in that band pays for the stream.
    if loose <= index_migrations._MAX_BATCH_BYTES or tight > index_migrations._MAX_BATCH_BYTES:
        return loose
    return tight if all(_canonical(raw, source.current_table, name, row_id) for name in proofs) else loose


def _copy_one(conn, plan, source, row_id):
    version, key, schema_version, raw, body, payload = conn.execute(
        "SELECT row_version,item_key,schema_version,values_json,body,payload_hash FROM items WHERE row_id=?",
        (row_id,)).fetchone()
    values = json.loads(raw) if source is None else typed_storage.current_values(conn, source, row_id)
    canonical = typed_storage.canonical_json(values)
    if (raw is not None and canonical != raw) or tokens.payload_hash(schema_version, key, values, body) != payload:
        raise typed_storage.TypedStorageError("source row does not match its canonical payload hash")
    parameters = plan.parameters(conn, row_id, key, version, values)
    conn.execute(plan.upsert, parameters)
    layout = replace(plan.layout, current=plan.table_name)
    decoded = typed_storage.current_values(conn, layout, row_id)
    if typed_storage.canonical_json(decoded) != canonical:
        raise typed_storage.TypedStorageError("native reconstruction differs from its source")


def _publish(conn, plan, source):
    cid = plan.collection_id
    old = index_migrations.ready_plan(conn, cid)
    conn.execute("UPDATE typed_encoding_mappings SET state='retired' WHERE collection_id=? AND state='ready'", (cid,))
    conn.execute("UPDATE collections SET encoding=? WHERE collection_id=?", (typed_storage.TYPED_V2, cid))
    conn.execute("UPDATE items SET encoding=?,values_json=NULL WHERE collection_id=?", (typed_storage.TYPED_V2, cid))
    conn.execute("UPDATE typed_encoding_mappings SET state='ready' WHERE collection_id=? AND generation=?",
                 (cid, plan.layout.generation))
    conn.execute(typed_storage._identity_trigger(plan.layout))
    if old is not None:
        conn.execute(f"DROP TABLE {old.table_name}")
        conn.execute("DELETE FROM query_projection_mappings WHERE collection_id=? AND generation=?", (cid, old.generation))
    conn.execute("UPDATE query_projection_mappings SET state='ready' WHERE collection_id=? AND generation=?",
                 (cid, plan.generation))
    if source is not None and (old is None or source.current_table != old.table_name):
        conn.execute(f"DROP TABLE {source.current_table}")
    return "ready"


def migrate_batch(conn, cid, fields, *, limit=128):
    """Build at most 500 rows/4 MiB, preserving the ready pair until exact parity and catch-up."""
    typed_storage._transaction(conn)
    if type(limit) is not int or not 0 < limit <= 500:
        raise ValueError("typed migration limit must be between 1 and 500")
    if typed_storage.collection_encoding(conn, cid) == typed_storage.TYPED_V2:
        return "ready"
    source = typed_storage.ready_layout(conn, cid)
    plan = _candidate(conn, cid)
    if not isinstance(plan, NativeProjectionPlan):
        plan = _begin(conn, cid, fields)
    last = conn.execute("SELECT last_row_id FROM typed_encoding_mappings WHERE collection_id=? AND generation=?",
                        (cid, plan.layout.generation)).fetchone()[0]
    remaining_rows, remaining_bytes = limit, index_migrations._MAX_BATCH_BYTES
    for catchup in (False, True):
        where = "(t.row_id IS NULL OR t.row_version<>i.row_version)" if catchup else "i.row_id>?"
        parameters = (cid,) if catchup else (cid, last)
        rows = conn.execute(f"SELECT i.row_id,i.row_version FROM items i LEFT JOIN {plan.table_name} t ON t.row_id=i.row_id "
                            f"WHERE i.collection_id=? AND {where} ORDER BY i.row_id LIMIT ?",
                            (*parameters, remaining_rows + 1)).fetchall()
        for row_id, version in rows:
            if not remaining_rows:
                return "building"
            size = _extent(conn, source, row_id)
            if size > index_migrations._MAX_BATCH_BYTES:
                return fail(conn, plan, {"code": "ROW_REQUIRES_STREAMING_MIGRATION", "row_id": row_id,
                    "row_version": version, "source_bytes" if source is None else "source_bytes_upper_bound": size,
                    "copy_budget_bytes": index_migrations._MAX_BATCH_BYTES})
            if size > remaining_bytes:
                return "building"
            key = conn.execute("SELECT item_key FROM items WHERE row_id=?", (row_id,)).fetchone()[0]
            if plan.layout.identity == "uuid16" and native_identity.choose((key,), canonical_uuid_ingress=True) != "uuid16":
                # Only the unpublished representation restarts; accepted opaque identities stay exact.
                fail(conn, plan)
                _begin(conn, cid, fields, identity="utf8")
                return "building"
            try:
                _copy_one(conn, plan, source, row_id)
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_ERROR:
                    raise
                return fail(conn, plan, {"code": "NATIVE_CANDIDATE_SQL_FAILED", "row_id": row_id,
                                         "row_version": version})
            except (typed_storage.TypedStorageError, ValueError, TypeError, RecursionError) as error:
                return fail(conn, plan, {"code": "NATIVE_COPY_PARITY_FAILED", "row_id": row_id,
                                         "row_version": version, "reason": type(error).__name__})
            remaining_rows -= 1
            remaining_bytes -= size
            if not catchup:
                conn.execute("UPDATE typed_encoding_mappings SET last_row_id=? WHERE collection_id=? AND generation=?",
                             (row_id, cid, plan.layout.generation))
    return _publish(conn, plan, source)
