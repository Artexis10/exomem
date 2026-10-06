"""Writer-owned, restartable derived index builds; reads never install objects."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace

from ..query_engine.indexes import (
    IndexDeclarationError,
    normalize_indexes,
    require_index_dependencies,
)
from ..query_engine.scalars import ScalarValueError
from . import query_freshness, typed_storage
from .query_indexes import ProjectionPlan, build_projection_plan

_MAX_CELL_BYTES = 256 * 1024
_MAX_BATCH_BYTES = 4 * 1024 * 1024


class AccountedWriter:
    """Route only this module's individual SQL statements through the writer."""

    def __init__(self, connection, execute):
        self.connection = connection
        self.execute = execute

    @property
    def in_transaction(self):
        return self.connection.in_transaction


def _transaction(conn):
    if not conn.in_transaction:
        raise RuntimeError("query index maintenance requires a writer transaction")


def _descriptor(plan):
    return {"version": 1,
            "fields": {s.field: {"type": s.kind} for s in plan.scalars},
            "paths": {s.field: list(s.path) for s in plan.scalars},
            "indexes": [{"name": index.name, "keys": [{"field": key.field, "direction": key.direction}
                         for key in index.keys]} for index in plan.indexes]}


def _encoded(plan):
    return json.dumps(_descriptor(plan), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(plan):
    return hashlib.sha256(_encoded(plan).encode()).hexdigest()


def _plan(cid, generation, encoded):
    raw = json.loads(encoded)
    if set(raw) != {"version", "fields", "paths", "indexes"} or raw["version"] != 1:
        raise ValueError("unsupported query projection descriptor")
    definitions = {index["name"]: {"keys": index["keys"]} for index in raw["indexes"]}
    if len(definitions) != len(raw["indexes"]):
        raise ValueError("duplicate query projection index")
    indexes = normalize_indexes(raw["fields"], definitions)
    return build_projection_plan(cid, raw["fields"], indexes, generation=generation,
                                 scalar_paths={name: tuple(path) for name, path in raw["paths"].items()})


def ready_plan(conn: sqlite3.Connection, collection_id: str) -> ProjectionPlan | None:
    """Resolve only a fully published mapping, without DDL or a fallback scan."""
    row = conn.execute("SELECT generation,plan_json,plan_hash FROM query_projection_mappings "
                       "WHERE collection_id=? AND state='ready'", (collection_id,)).fetchone()
    if row is None:
        return None
    if hashlib.sha256(row[1].encode()).hexdigest() != row[2]:
        raise ValueError("query projection descriptor changed")
    return _plan(collection_id, row[0], row[1])


def begin_build(conn: sqlite3.Connection, plan: ProjectionPlan) -> None:
    """Install an empty candidate, preserving the current ready contract."""
    _transaction(conn)
    encoded = _encoded(plan)
    # Validate all authored labels as data again before any executable text.
    checked = _plan(plan.collection_id, plan.generation, encoded)
    if checked != plan:
        raise ValueError("query projection descriptor is not canonical")
    desired = conn.execute("SELECT query_plan_hash FROM collections WHERE collection_id=?",
                           (plan.collection_id,)).fetchone()
    if desired is None or desired[0] != _hash(plan):
        raise IndexDeclarationError("INDEX_BUILD_STALE", "index build is not the current declaration intent", "indexes")
    for statement in checked.ddl:
        conn.execute(statement)
    conn.execute("INSERT INTO query_projection_mappings"
                 "(collection_id,generation,state,plan_json,plan_hash) VALUES(?,?,'building',?,?)",
                 (plan.collection_id, plan.generation, encoded, hashlib.sha256(encoded.encode()).hexdigest()))


def _failed(conn, plan):
    conn.execute("UPDATE query_projection_mappings SET state='failed' "
                 "WHERE collection_id=? AND generation=?", (plan.collection_id, plan.generation))


def maintain_item(conn, collection_id, row_id, item_key, row_version, values: Mapping, *, previous=None) -> None:
    """Maintain ready and building indexes in the canonical item transaction."""
    _transaction(conn)
    query_freshness.maintain(conn, collection_id, values, previous=previous)
    mappings = conn.execute("SELECT generation,state,plan_json FROM query_projection_mappings "
                            "WHERE collection_id=? AND state IN ('ready','building')",
                            (collection_id,)).fetchall()
    for generation, state, encoded in mappings:
        plan = _plan(collection_id, generation, encoded)
        try:
            keys = plan.encode(values)
        except ScalarValueError:
            if state == "ready":
                raise
            _failed(conn, plan)
            conn.execute(f"DROP TABLE {plan.table_name}")
            continue
        conn.execute(plan.upsert_sql, (row_id, item_key, row_version, keys))


def backfill_batch(conn, collection_id: str, *, limit: int = 128) -> bool:
    """Commit at most 500 rows/4 MiB; True means atomic publication completed.

    The caller owns the writer lease, transaction and collection authorization.
    An unsupported old value fails this candidate, not canonical data or the old
    ready mapping. No job, read-triggered build or cross-call snapshot is created.
    """
    _transaction(conn)
    if type(limit) is not int or not 0 < limit <= 500:
        raise ValueError("index backfill limit must be between 1 and 500")
    row = conn.execute("SELECT generation,plan_json,last_row_id FROM query_projection_mappings "
                       "WHERE collection_id=? AND state='building'", (collection_id,)).fetchone()
    if row is None:
        return False
    plan = _plan(collection_id, row[0], row[1])
    desired = conn.execute("SELECT query_plan_hash FROM collections WHERE collection_id=?",
                           (collection_id,)).fetchone()
    if desired is None or desired[0] != _hash(plan):
        _failed(conn, plan)
        conn.execute(f"DROP TABLE {plan.table_name}")
        return False
    last = row[2]
    cursor = conn.execute("SELECT row_id,item_key,row_version,"
                          "CASE WHEN length(CAST(values_json AS BLOB))<=? THEN values_json END,"
                          "length(CAST(values_json AS BLOB)),encoding FROM items "
                          "WHERE collection_id=? AND row_id>? ORDER BY row_id LIMIT ?",
                          (_MAX_CELL_BYTES, collection_id, last, limit))
    rows = cursor.fetchall()
    typed = {row["row_id"]: row["values_json"] for row in typed_storage.hydrate(conn, [
        {"row_id": row[0], "collection_id": collection_id, "encoding": row[5], "values_json": None}
        for row in rows if row[5] != typed_storage.JSON_V1])}
    total = 0
    try:
        for row_id, key, version, values, size, _ in rows:
            if row_id in typed:
                size = len(typed[row_id].encode())
                values = typed[row_id] if size <= _MAX_CELL_BYTES else None
            if values is None:
                _failed(conn, plan)
                return False
            if total + size > _MAX_BATCH_BYTES:
                break
            try:
                encoded = plan.encode(json.loads(values))
            except (ScalarValueError, ValueError, TypeError, RecursionError):
                _failed(conn, plan)
                return False
            conn.execute(plan.upsert_sql, (row_id, key, version, encoded))
            total += size
            last = row_id
    finally:
        cursor.close()
        # A failed build retains only its bounded diagnostic descriptor, not
        # an abandoned copy of indexed data. Close the active main cursor
        # first: SQLite cannot drop any table while that cursor holds it.
        if conn.execute("SELECT 1 FROM query_projection_mappings WHERE collection_id=? "
                        "AND generation=? AND state='failed'", (collection_id, plan.generation)).fetchone():
            conn.execute(f"DROP TABLE {plan.table_name}")
    conn.execute("UPDATE query_projection_mappings SET last_row_id=? "
                 "WHERE collection_id=? AND generation=?", (last, collection_id, plan.generation))
    if conn.execute("SELECT 1 FROM items WHERE collection_id=? AND row_id>? LIMIT 1",
                    (collection_id, last)).fetchone():
        return False
    old = ready_plan(conn, collection_id)
    if old is not None:
        conn.execute(f"DROP TABLE {old.table_name}")
        conn.execute("DELETE FROM query_projection_mappings WHERE collection_id=? AND generation=?",
                     (collection_id, old.generation))
    conn.execute("UPDATE query_projection_mappings SET state='ready' "
                 "WHERE collection_id=? AND generation=?", (collection_id, plan.generation))
    return True


def prepare_manifest(conn, manifest, data, declared) -> None:
    """Resolve logical index intent at governed create/revise, never at query."""
    _transaction(conn)
    fields = data["item_schema"]["fields"]
    inherited = {index.name: {"keys": [{"field": key.field, "direction": key.direction}
                 for key in index.keys]} for index in declared.indexes}
    authored = data.get("indexes", {})
    if not isinstance(authored, Mapping):
        raise ValueError("collection indexes must be a mapping")
    specifications = normalize_indexes(fields, {**inherited, **authored})
    current = ready_plan(conn, manifest.collection_id)
    if current is not None:
        current_leading = {index.keys[0].field for index in current.indexes}
        dependencies = set()
        for view in manifest.normalized_views.values():
            query = view["query"]
            dependencies.update(f["field"] for f in query.get("filters", []))
            dependencies.update(query[key] for key in ("sort_by", "date_column") if key in query)
        require_index_dependencies(specifications, dependencies & current_leading)
        # Integer/number use exactly the same numeric keys. Widening can
        # update the descriptor without rewriting data or denying a valid
        # fractional write while a separate index build is pending.
        scalars = []
        for scalar in current.scalars:
            kind = fields.get(scalar.field, {}).get("type", scalar.kind)
            if kind != scalar.kind and (scalar.kind, kind) != ("integer", "number"):
                raise IndexDeclarationError("INDEX_TYPE_MIGRATION_REQUIRED",
                    "projected type change requires a staged migration before publication", f"fields.{scalar.field}")
            scalars.append(replace(scalar, kind=kind))
        compatible = replace(current, scalars=tuple(scalars))
        if compatible != current:
            conn.execute("UPDATE query_projection_mappings SET plan_json=?,plan_hash=? "
                         "WHERE collection_id=? AND generation=?",
                         (_encoded(compatible), _hash(compatible), manifest.collection_id, current.generation))
            current = compatible
    generation = conn.execute("SELECT COALESCE(MAX(generation),0)+1 FROM query_projection_mappings "
                              "WHERE collection_id=?", (manifest.collection_id,)).fetchone()[0]
    plan = build_projection_plan(manifest.collection_id, fields, specifications, generation=generation)
    paths = {name: (name,) for name in fields}
    paths.update((scalar.field, scalar.path) for scalar in plan.scalars)
    query_freshness.declare(conn, manifest.collection_id, paths)
    # A revise owns replacing its previous candidate, not the ready contract.
    pending = conn.execute("SELECT generation,plan_json FROM query_projection_mappings "
                           "WHERE collection_id=? AND state='building'", (manifest.collection_id,)).fetchone()
    if pending is not None:
        old = _plan(manifest.collection_id, pending[0], pending[1])
        conn.execute(f"DROP TABLE {old.table_name}")
        conn.execute("DELETE FROM query_projection_mappings WHERE collection_id=? AND generation=?",
                     (manifest.collection_id, old.generation))
    conn.execute("UPDATE collections SET query_plan_hash=? WHERE collection_id=?", (_hash(plan), manifest.collection_id))
    if (not specifications and current is None) or (current is not None and _encoded(current) == _encoded(plan)):
        return
    begin_build(conn, plan)
    # Empty newly created collections can publish immediately. Populated ones
    # remain explicitly building until bounded writer-owned backfill completes.
    if conn.execute("SELECT 1 FROM items WHERE collection_id=? LIMIT 1", (manifest.collection_id,)).fetchone() is None:
        backfill_batch(conn, manifest.collection_id)
