"""Writer-owned, restartable derived index builds; reads never install objects."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace

from .. import vault
from ..query_engine.indexes import (
    IndexDeclarationError,
    normalize_indexes,
    require_index_dependencies,
)
from ..query_engine.scalars import ScalarValueError
from . import native_metadata, query_freshness, rollups, typed_storage
from .query_indexes import ProjectionPlan, build_projection_plan

_MAX_CELL_BYTES = 256 * 1024
_MAX_BATCH_BYTES = 4 * 1024 * 1024


class AccountedWriter:
    """Route maintenance SQL through the writer's change accounting."""

    def __init__(self, connection, execute):
        self.connection = connection
        self.execute = execute

    def executemany(self, statement, rows):
        # The writer's ``_execute`` accounts a many-row statement as one, as sqlite3's executemany runs it.
        return self.execute(statement, rows, many=True)

    @property
    def in_transaction(self):
        return self.connection.in_transaction


def _transaction(conn):
    if not conn.in_transaction:
        raise RuntimeError("query index maintenance requires a writer transaction")


def _descriptor(plan):
    from .native_projections import NativeProjectionPlan

    native = isinstance(plan, NativeProjectionPlan)
    return {"version": 2 if native else 1,
            "fields": {s.field: {"type": s.kind} for s in plan.scalars},
            "paths": {s.field: list(s.path) for s in plan.scalars},
            "indexes": [{"name": index.name, "keys": [{"field": key.field, "direction": key.direction}
                         for key in index.keys]} for index in plan.indexes],
            **({"payload_generation": plan.layout.generation, "file_role": plan.role.value} if native else {})}


def _encoded(plan):
    return json.dumps(_descriptor(plan), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(plan):
    return hashlib.sha256(_encoded(plan).encode()).hexdigest()


def _plan(cid, generation, encoded, *, connection=None, published=False):
    raw = json.loads(encoded)
    version = raw.get("version") if isinstance(raw, dict) else None
    keys = {"version", "fields", "paths", "indexes"}  # nosemgrep: ep-word-set -- The persisted projection wire fixes these keys.
    if version == 2:
        keys.update(("payload_generation", "file_role"))
    if type(version) is not int or version not in (1, 2) or set(raw) != keys:
        raise ValueError("unsupported query projection descriptor")
    payload = None
    if version == 2:
        payload_generation = raw["payload_generation"]
        if connection is None or type(payload_generation) is not int or payload_generation < 1:
            raise ValueError("native query projection requires its payload binding")
        row = connection.execute("SELECT layout_json,layout_hash,state FROM typed_encoding_mappings "
                                 "WHERE collection_id=? AND generation=?", (cid, payload_generation)).fetchone()
        if row is None or hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise typed_storage.TypedStorageError("native query payload descriptor is absent or changed")
        if published and row[2] != "ready":
            raise typed_storage.TypedStorageError("native query payload generation is not published")
        payload = typed_storage._layout(cid, payload_generation, row[0])
        if payload.encoding != typed_storage.TYPED_V2:
            raise typed_storage.TypedStorageError("native query requires a native payload descriptor")
    definitions = {index["name"]: {"keys": index["keys"]} for index in raw["indexes"]}
    if len(definitions) != len(raw["indexes"]):
        raise ValueError("duplicate query projection index")
    indexes = normalize_indexes(raw["fields"], definitions)
    return build_projection_plan(cid, raw["fields"], indexes, generation=generation,
                                 scalar_paths={name: tuple(path) for name, path in raw["paths"].items()},
                                 payload_layout=payload, file_role=raw.get("file_role"))


def ready_plan(conn: sqlite3.Connection, collection_id: str) -> ProjectionPlan | None:
    """Resolve only a fully published mapping, without DDL or a fallback scan."""
    row = conn.execute("SELECT generation,plan_json,plan_hash FROM query_projection_mappings "
                       "WHERE collection_id=? AND state='ready'", (collection_id,)).fetchone()
    return _ready_plan(conn, collection_id, row)


def _ready_plan(conn, collection_id, row):
    if row is None:
        return None
    if hashlib.sha256(row[1].encode()).hexdigest() != row[2]:
        raise ValueError("query projection descriptor changed")
    return _plan(collection_id, row[0], row[1], connection=conn, published=True)


def begin_build(conn: sqlite3.Connection, plan: ProjectionPlan) -> None:
    """Install an empty candidate, preserving the current ready contract."""
    _transaction(conn)
    encoded = _encoded(plan)
    # Validate all authored labels as data again before any executable text.
    checked = _plan(plan.collection_id, plan.generation, encoded, connection=conn)
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
    rollups.maintain(conn, collection_id, row_id, item_key, values, previous=previous)
    _maintain_indexes(conn, collection_id, [(row_id, item_key, row_version, values)])


def maintain_new_items(conn, collection_id, rows) -> None:
    """Maintain one all-new batch without reloading declarations for each row."""
    _transaction(conn)
    query_freshness.advance_all(conn, collection_id, count=len(rows))
    rollups.maintain_new_items(conn, collection_id, ((row_id, key, values) for row_id, key, _, values in rows))
    _maintain_indexes(conn, collection_id, rows, many=True)


def _maintain_indexes(conn, collection_id, rows, *, many=False) -> None:
    mappings = [(state, _ready_plan(conn, collection_id, (generation, encoded, digest)) if state == "ready"
                 else _plan(collection_id, generation, encoded, connection=conn))
                for generation, state, encoded, digest in conn.execute(
                    "SELECT generation,state,plan_json,plan_hash FROM query_projection_mappings "
                    "WHERE collection_id=? AND state IN ('ready','building')", (collection_id,)).fetchall()]
    for state, plan in mappings:
        from .native_projections import NativeProjectionPlan

        if isinstance(plan, NativeProjectionPlan):
            # The canonical native write already maintains its ready current/query relation.
            if state == "ready":
                continue
            payload_state = conn.execute("SELECT state FROM typed_encoding_mappings WHERE collection_id=? AND generation=?",
                                         (collection_id, plan.layout.generation)).fetchone()
            if payload_state is not None and payload_state[0] == "ready":
                current = next((ready for state, ready in mappings if state == "ready"), None)
                if not isinstance(current, NativeProjectionPlan):
                    raise typed_storage.TypedStorageError("native query build has no current payload relation")
                for row_id, _, row_version, _ in rows:
                    if not _copy_native(conn, current, plan, "row_id=? AND row_version=?", (row_id, row_version)):
                        break
            # A payload-format build catches up by row version in bounded migration batches.
            continue
        try:
            parameters = [{"row_id": row_id, "item_key": item_key, "row_version": row_version,
                           "keys_json": plan.encode(values)} for row_id, item_key, row_version, values in rows]
        except ScalarValueError:
            if state == "ready":
                raise
            _failed(conn, plan)
            conn.execute(f"DROP TABLE {plan.table_name}")
            continue
        if many:
            conn.executemany(plan.upsert, parameters)
        else:
            conn.execute(plan.upsert, parameters[0])


def _copy_native(conn, source, target, where, parameters):
    """Copy one immutable payload layout without another decode, encoding or metadata allocation."""
    if source.layout.encoded != target.layout.encoded or source.layout.generation != target.layout.generation:
        raise typed_storage.TypedStorageError("query-only copy cannot change its payload descriptor")
    names = ("row_id", "item_key", "row_version", *source.layout.value_columns, "r")
    columns = ",".join(names)
    updates = ",".join(name + "=excluded." + name for name in names if name != "row_id")
    try:
        conn.execute(f"INSERT INTO {target.table_name}({columns}) SELECT {columns} FROM {source.table_name} "
                     f"WHERE {where} ON CONFLICT(row_id) DO UPDATE SET {updates}", parameters)
    except sqlite3.OperationalError as error:
        # An optional candidate's expression failure cannot stop canonical writes; busy/interrupt/corruption still propagate.
        if error.sqlite_errorcode != sqlite3.SQLITE_ERROR:
            raise
        _failed(conn, target)
        conn.execute(f"DROP TABLE {target.table_name}")
        return False
    return True


def _backfill_native(conn, plan, last, limit):
    """Reuse native payload cells for query-only builds; coordinate format builds as one pair."""
    from . import native_migrations

    state = conn.execute("SELECT state FROM typed_encoding_mappings WHERE collection_id=? AND generation=?",
                         (plan.collection_id, plan.layout.generation)).fetchone()
    if state is not None and state[0] == "building":
        return native_migrations.migrate_batch(conn, plan.collection_id, plan.layout, limit=limit) == "ready"
    source = ready_plan(conn, plan.collection_id)
    if source is None:
        raise typed_storage.TypedStorageError("native query rebuild requires a ready source")
    rows = conn.execute(f"SELECT row_id,values_bytes+length(item_key) FROM {source.table_name} "
                         "WHERE row_id>? ORDER BY row_id LIMIT ?", (last, limit + 1)).fetchall()
    total = 0
    for row_id, extent in rows[:limit]:
        if extent > _MAX_BATCH_BYTES:
            _failed(conn, plan)
            conn.execute(f"DROP TABLE {plan.table_name}")
            return False
        if total + extent > _MAX_BATCH_BYTES:
            break
        if not _copy_native(conn, source, plan, "row_id=?", (row_id,)):
            return False
        total += extent
        last = row_id
    conn.execute("UPDATE query_projection_mappings SET last_row_id=? WHERE collection_id=? AND generation=?",
                 (last, plan.collection_id, plan.generation))
    if conn.execute(f"SELECT 1 FROM {source.table_name} WHERE row_id>? LIMIT 1", (last,)).fetchone():
        return False
    conn.execute(f"DROP TABLE {source.table_name}")
    conn.execute("DELETE FROM query_projection_mappings WHERE collection_id=? AND generation=?",
                 (plan.collection_id, source.generation))
    conn.execute("UPDATE query_projection_mappings SET state='ready' WHERE collection_id=? AND generation=?",
                 (plan.collection_id, plan.generation))
    return True


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
    plan = _plan(collection_id, row[0], row[1], connection=conn)
    from .native_projections import NativeProjectionPlan

    desired = conn.execute("SELECT query_plan_hash FROM collections WHERE collection_id=?",
                           (collection_id,)).fetchone()
    if desired is None or desired[0] != _hash(plan):
        if isinstance(plan, NativeProjectionPlan) and conn.execute(
                "SELECT state FROM typed_encoding_mappings WHERE collection_id=? AND generation=?",
                (collection_id, plan.layout.generation)).fetchone() == ("building",):
            from .native_migrations import fail

            fail(conn, plan)
        else:
            _failed(conn, plan)
            conn.execute(f"DROP TABLE {plan.table_name}")
        return False
    if isinstance(plan, NativeProjectionPlan):
        return _backfill_native(conn, plan, row[2], limit)
    last = row[2]
    # A plan without scalars keys every row identically from no value, so it reads none
    # and no row is too large for it.
    reads = bool(plan.scalars)
    cursor = conn.execute("SELECT row_id,item_key,row_version,encoding FROM items "
                          "WHERE collection_id=? AND row_id>? ORDER BY row_id LIMIT ?", (collection_id, last, limit))
    rows = cursor.fetchall()
    typed = {row["row_id"]: row["values_json"] for row in typed_storage.hydrate(conn, [
        {"row_id": row[0], "collection_id": collection_id, "encoding": row[3], "values_json": None}
        for row in rows if reads and row[3] != typed_storage.JSON_V1])}
    raw = native_metadata.sqlite_connection(conn)
    total = 0
    try:
        for row_id, key, version, _ in rows:
            if not reads:
                conn.execute(plan.upsert, {"row_id": row_id, "item_key": key, "row_version": version, "keys_json": plan.encode({})})
                last = row_id
                continue
            if row_id in typed:
                size = len(typed[row_id].encode())
                values = typed[row_id] if size <= _MAX_CELL_BYTES else None
            else:
                # The blob handle gives the stored byte length without loading a cell too large to index.
                with raw.blobopen("items", "values_json", row_id, readonly=True) as cell:
                    size = len(cell)
                    values = cell.read() if size <= _MAX_CELL_BYTES else None
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
            conn.execute(plan.upsert, {"row_id": row_id, "item_key": key, "row_version": version, "keys_json": encoded})
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


def _declared(data, declared):
    """A manifest's declared fields and normalized index intent, inherited type indexes first."""
    fields = data["item_schema"]["fields"]
    inherited = {index.name: {"keys": [{"field": key.field, "direction": key.direction}
                 for key in index.keys]} for index in declared.indexes}
    authored = data.get("indexes", {})
    if not isinstance(authored, Mapping):
        raise ValueError("collection indexes must be a mapping")
    return fields, normalize_indexes(fields, {**inherited, **authored})


def _declare_paths(conn, collection_id, fields, plan) -> None:
    paths = {name: (name,) for name in fields}
    paths.update((scalar.field, scalar.path) for scalar in plan.scalars)
    query_freshness.declare(conn, collection_id, paths)


def begin_missing(conn, collection_id: str, declared) -> bool:
    """Start the first projection build of a collection that has none; True when one began.

    A collection enrolled before every collection carried a projection, or
    imported from files, has rows and no mapping in any state. It gets its
    current manifest's plan as a building candidate, which bounded backfill
    then fills. A failed build keeps its mapping and waits for a governed revise.
    """
    _transaction(conn)
    if conn.execute("SELECT 1 FROM query_projection_mappings WHERE collection_id=? LIMIT 1",
                    (collection_id,)).fetchone() is not None:
        return False
    text = conn.execute("SELECT m.manifest_text FROM collection_manifests m JOIN collections c "
                        "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version "
                        "WHERE c.collection_id=?", (collection_id,)).fetchone()[0]
    try:
        data, _, _ = vault.parse_frontmatter(text, strict=True)
        fields, specifications = _declared(data, declared)
        plan = build_projection_plan(collection_id, fields, specifications)
    except (ValueError, KeyError, TypeError) as error:
        # A declaration that never normalized (as one imported from files can) fails once and
        # stays failed: a governed revise repairs it, which a retry cannot.
        encoded = json.dumps({"version": 1, "error": getattr(error, "code", "INVALID_INDEX_DECLARATION")},
                             sort_keys=True, separators=(",", ":"))
        conn.execute("INSERT INTO query_projection_mappings(collection_id,generation,state,plan_json,plan_hash) "
                     "VALUES(?,1,'failed',?,?)", (collection_id, encoded, hashlib.sha256(encoded.encode()).hexdigest()))
        return False
    _declare_paths(conn, collection_id, fields, plan)
    conn.execute("UPDATE collections SET query_plan_hash=? WHERE collection_id=?", (_hash(plan), collection_id))
    begin_build(conn, plan)
    return True


def unready_state(conn: sqlite3.Connection, collection_id: str) -> str | None:
    """The newest projection attempt's state while none is ready: ``building`` or ``failed``.

    None means no attempt yet; the serving store thread starts one.
    """
    row = conn.execute("SELECT state FROM query_projection_mappings WHERE collection_id=? AND state<>'ready' "
                       "ORDER BY generation DESC LIMIT 1", (collection_id,)).fetchone()
    return None if row is None else row[0]


def backfill_due(conn) -> tuple[tuple[str, bool, bool], ...]:
    """Each collection whose projection or rollups await backfill: (id, projection, rollups).

    One read over the collections, for the serving store thread. A collection
    with no projection mapping in any state is due: ``begin_missing`` starts it.
    A derived collection's rollups build in its rebuild (``derived_rows``) instead.
    """
    return tuple((cid, bool(projection), bool(rollup)) for cid, projection, rollup in conn.execute(
        "SELECT * FROM (SELECT c.collection_id,"
        " EXISTS(SELECT 1 FROM query_projection_mappings m WHERE m.collection_id=c.collection_id"
        "  AND m.state='building') OR NOT EXISTS(SELECT 1 FROM query_projection_mappings m"
        "  WHERE m.collection_id=c.collection_id) AS projection,"
        " EXISTS(SELECT 1 FROM rollup_definitions r WHERE r.collection_id=c.collection_id"
        "  AND r.state='building') AND c.derived=0 AS rollup"
        " FROM collections c) WHERE projection OR rollup ORDER BY collection_id"))


def prepare_manifest(conn, manifest, data, declared) -> None:
    """Resolve logical index and rollup intent at governed create/revise, never at query.

    Every collection gets a projection, one with no index included: the typed
    row route reads a collection only through its ready projection.
    """
    _transaction(conn)
    rollups.prepare(conn, manifest, data)
    fields, specifications = _declared(data, declared)
    current = ready_plan(conn, manifest.collection_id)
    if current is not None:
        current_leading = {index.keys[0].field for index in current.indexes}
        dependencies = set()
        for view in manifest.normalized_views.values():
            query = view["query"]
            dependencies.update(f["column"] for f in query.get("filters", []))
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
    from .native_projections import NativeProjectionPlan

    native = isinstance(current, NativeProjectionPlan)
    plan = build_projection_plan(manifest.collection_id, fields, specifications, generation=generation,
                                 payload_layout=current.layout if native else None,
                                 file_role=current.role if native else None)
    _declare_paths(conn, manifest.collection_id, fields, plan)
    # A revise owns replacing its previous candidate, not the ready contract.
    pending = conn.execute("SELECT generation,plan_json FROM query_projection_mappings "
                           "WHERE collection_id=? AND state='building'", (manifest.collection_id,)).fetchone()
    if pending is not None:
        old = _plan(manifest.collection_id, pending[0], pending[1], connection=conn)
        if isinstance(old, NativeProjectionPlan) and conn.execute(
                "SELECT state FROM typed_encoding_mappings WHERE collection_id=? AND generation=?",
                (manifest.collection_id, old.layout.generation)).fetchone() == ("building",):
            from .native_migrations import fail

            fail(conn, old)
        else:
            conn.execute(f"DROP TABLE {old.table_name}")
            conn.execute("DELETE FROM query_projection_mappings WHERE collection_id=? AND generation=?",
                         (manifest.collection_id, old.generation))
    conn.execute("UPDATE collections SET query_plan_hash=? WHERE collection_id=?", (_hash(plan), manifest.collection_id))
    if current is not None and _encoded(current) == _encoded(plan):
        return
    begin_build(conn, plan)
    # Empty newly created collections can publish immediately. Populated ones
    # remain explicitly building until bounded writer-owned backfill completes.
    if conn.execute("SELECT 1 FROM items WHERE collection_id=? LIMIT 1", (manifest.collection_id,)).fetchone() is None:
        backfill_batch(conn, manifest.collection_id)
