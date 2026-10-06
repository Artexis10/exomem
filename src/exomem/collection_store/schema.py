"""DDL and forward migrations for the structured-collection store.

The store is the single source of truth for structured collections once a
vault migrates (OpenSpec ``move-structured-collections-to-sqlite``, design
§2 and §16). Until the GA gate it is dark: nothing routes to it.

Contract held here, not in the writers:

- every table is ``STRICT``;
- ``txns``, ``audit_effects``, ``item_versions``, ``item_sources``,
  ``collection_manifests``, ``collection_type_versions`` and
  ``version_identity`` are append-only: conflicting ``BEFORE INSERT``,
  ``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers abort the statement;
- ``version_identity`` is the version spine (schema 5): every JSON or typed
  item version has exactly one identity, and ``item_sources`` references it.
  A JSON payload mints its identity in the same statement; a typed-v1 identity
  requires its typed payload (``typed_storage``);
- ``items`` rows are never deleted (Records and Planning have no delete);
- a natural key is unique per collection when complete (a partial unique
  index), and a view path is unique across the store;
- ``txns.commit_seq`` is the store-wide sequence (A3): unique, contiguous from
  1, and every insert advances ``store_meta.commit_seq`` and
  ``store_meta.store_head_hash`` in the same statement, so the recorded head
  can never drift from the last transaction.

``txns`` is inserted last in a mutation with its final hashes; that is what
lets the append-only trigger admit it. Rows written earlier in the same
transaction (effects, versions, items) carry the pre-allocated ``txn_id``;
the effect-to-transaction reference is checked at ``COMMIT``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable

SCHEMA_VERSION = 6

META_SCHEMA_VERSION = "schema_version"
META_STORE_ID = "store_id"
META_INSTANCE_ID = "instance_id"
META_LINEAGE = "lineage"
META_FORKS = "forks"
META_COMMIT_SEQ = "commit_seq"
META_STORE_HEAD_HASH = "store_head_hash"
META_LAST_PUBLISHED_REPLICA_SHA256 = "last_published_replica_sha256"
META_PENDING_REPLICA_PUBLICATION = "pending_replica_publication"
META_PUBLISHED_REPLICA_HEAD = "published_replica_head"
META_REPLICA_DIVERGENCE = "replica_divergence"
META_CREATED_AT = "created_at"
META_MIGRATED_FROM = "migrated_from"
META_LEASE_EPOCH = "lease_epoch"

TABLES = (
    "store_meta",
    "collection_types",
    "collection_type_versions",
    "collection_manifests",
    "collections",
    "items",
    "item_versions",
    "item_sources",
    "txns",
    "audit_effects",
    "held_candidates",
    "projection_state",
    "query_projection_mappings",
    "query_cursor_state",
    "version_identity",
    "typed_encoding_mappings",
    "import_jobs",
    "import_rejections",
)
_V1_APPEND_ONLY_TABLES = (
    "txns",
    "audit_effects",
    "item_versions",
    "item_sources",
    "collection_manifests",
    "collection_type_versions",
)
APPEND_ONLY_TABLES = (*_V1_APPEND_ONLY_TABLES, "version_identity")

_TABLES_V1 = (
    """
    CREATE TABLE store_meta(
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE collection_types(
      name TEXT PRIMARY KEY,
      current_version INTEGER NOT NULL,
      builtin INTEGER NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE collection_type_versions(
      name TEXT NOT NULL REFERENCES collection_types(name),
      version INTEGER NOT NULL,
      declaration_json TEXT NOT NULL,
      declaration_hash TEXT NOT NULL,
      change_class TEXT NOT NULL,
      txn_id INTEGER NOT NULL,
      PRIMARY KEY (name, version)
    ) STRICT, WITHOUT ROWID
    """,
    """
    CREATE TABLE collection_manifests(
      collection_id TEXT NOT NULL
        REFERENCES collections(collection_id) DEFERRABLE INITIALLY DEFERRED,
      manifest_version INTEGER NOT NULL,
      manifest_text TEXT NOT NULL,
      manifest_hash TEXT NOT NULL,
      schema_json TEXT NOT NULL,
      natural_key_json TEXT,
      txn_id INTEGER NOT NULL,
      PRIMARY KEY (collection_id, manifest_version)
    ) STRICT, WITHOUT ROWID
    """,
    """
    CREATE TABLE collections(
      collection_id TEXT PRIMARY KEY,
      type_name TEXT NOT NULL REFERENCES collection_types(name),
      type_version INTEGER NOT NULL,
      manifest_path TEXT NOT NULL UNIQUE,
      source_path TEXT NOT NULL UNIQUE,
      layout TEXT NOT NULL CHECK (layout IN ('markdown-items', 'markdown-log')),
      manifest_version INTEGER NOT NULL,
      generation INTEGER NOT NULL,
      audit_head TEXT,
      audit_reader_version INTEGER NOT NULL,
      legacy_audit_status TEXT,
      verified_through_txn INTEGER,
      log_frame_json TEXT,
      created_txn INTEGER NOT NULL,
      updated_txn INTEGER NOT NULL,
      FOREIGN KEY (collection_id, manifest_version)
        REFERENCES collection_manifests(collection_id, manifest_version)
        DEFERRABLE INITIALLY DEFERRED
    ) STRICT
    """,
    """
    CREATE TABLE items(
      row_id INTEGER PRIMARY KEY,
      collection_id TEXT NOT NULL REFERENCES collections(collection_id),
      item_key TEXT NOT NULL,
      natural_key TEXT,
      row_version INTEGER NOT NULL,
      schema_version INTEGER NOT NULL,
      values_json TEXT NOT NULL,
      body TEXT NOT NULL DEFAULT '',
      payload_hash TEXT NOT NULL,
      view_path TEXT NOT NULL,
      created_txn INTEGER NOT NULL,
      updated_txn INTEGER NOT NULL,
      UNIQUE (collection_id, item_key),
      UNIQUE (view_path)
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX items_natural_key
      ON items(collection_id, natural_key) WHERE natural_key IS NOT NULL
    """,
    """
    CREATE TABLE item_versions(
      row_id INTEGER NOT NULL REFERENCES items(row_id),
      row_version INTEGER NOT NULL,
      values_json TEXT NOT NULL,
      body TEXT NOT NULL,
      payload_hash TEXT NOT NULL,
      txn_id INTEGER NOT NULL,
      PRIMARY KEY (row_id, row_version)
    ) STRICT, WITHOUT ROWID
    """,
    """
    CREATE TABLE item_sources(
      row_id INTEGER NOT NULL,
      row_version INTEGER NOT NULL,
      ordinal INTEGER NOT NULL,
      source_ref TEXT NOT NULL,
      PRIMARY KEY (row_id, row_version, ordinal),
      FOREIGN KEY (row_id, row_version) REFERENCES item_versions(row_id, row_version)
    ) STRICT, WITHOUT ROWID
    """,
    """
    CREATE TABLE txns(
      txn_id INTEGER PRIMARY KEY,
      transition_id TEXT NOT NULL UNIQUE,
      collection_id TEXT NOT NULL,
      operation TEXT NOT NULL,
      profile_operation TEXT,
      generation_before INTEGER NOT NULL,
      generation_after INTEGER NOT NULL,
      manifest_version_before INTEGER,
      manifest_version_after INTEGER,
      actor TEXT NOT NULL,
      why TEXT NOT NULL,
      request_id TEXT UNIQUE,
      request_hash TEXT,
      receipt_json TEXT NOT NULL,
      committed_at TEXT NOT NULL,
      prev_event_hash TEXT,
      event_hash TEXT NOT NULL,
      commit_seq INTEGER NOT NULL UNIQUE,
      store_head_hash TEXT NOT NULL UNIQUE,
      legacy_event_json TEXT
    ) STRICT
    """,
    "CREATE INDEX txns_by_collection ON txns(collection_id, txn_id)",
    """
    CREATE TABLE audit_effects(
      txn_id INTEGER NOT NULL REFERENCES txns(txn_id) DEFERRABLE INITIALLY DEFERRED,
      ordinal INTEGER NOT NULL,
      row_id INTEGER REFERENCES items(row_id),
      item_key TEXT,
      effect TEXT NOT NULL CHECK (effect IN ('insert', 'update', 'held', 'resume')),
      effect_label TEXT,
      version_before INTEGER,
      version_after INTEGER,
      hash_before TEXT,
      hash_after TEXT,
      source_ref TEXT,
      PRIMARY KEY (txn_id, ordinal)
    ) STRICT, WITHOUT ROWID
    """,
    "CREATE INDEX audit_effects_by_row ON audit_effects(row_id, txn_id)",
    """
    CREATE TABLE held_candidates(
      held_id TEXT PRIMARY KEY,
      collection_id TEXT REFERENCES collections(collection_id),
      kind TEXT NOT NULL CHECK (kind IN ('write-refusal', 'view-correction')),
      code TEXT,
      candidate_json TEXT,
      held_bytes BLOB,
      diagnostics_json TEXT,
      view_path TEXT,
      base_row_version INTEGER,
      created_txn INTEGER,
      updated_at TEXT
    ) STRICT
    """,
    """
    CREATE TABLE projection_state(
      path TEXT PRIMARY KEY,
      collection_id TEXT REFERENCES collections(collection_id),
      row_id INTEGER REFERENCES items(row_id),
      kind TEXT NOT NULL
        CHECK (kind IN ('manifest', 'item', 'log', 'held', 'history', 'type')),
      published_row_version INTEGER,
      published_sha256 TEXT,
      pending_row_version INTEGER,
      pending_sha256 TEXT,
      stat_identity TEXT,
      state TEXT NOT NULL CHECK (state IN ('current', 'pending', 'held'))
    ) STRICT
    """,
)


# Every primary/unique key that replacement conflict handling could delete.
_CONFLICT_KEYS = {
    "txns": (("txn_id",), ("transition_id",), ("request_id",), ("commit_seq",), ("store_head_hash",)),
    "audit_effects": (("txn_id", "ordinal"),),
    "item_versions": (("row_id", "row_version"),),
    "item_sources": (("row_id", "row_version", "ordinal"),),
    "collection_manifests": (("collection_id", "manifest_version"),),
    "collection_type_versions": (("name", "version"),),
    "items": (("row_id",), ("collection_id", "item_key"), ("view_path",), ("collection_id", "natural_key")),
    "version_identity": (("row_id", "row_version"),),
}


def _conflicting_key_predicate(table: str) -> str:
    return " OR ".join(
        "(" + " AND ".join(f"{column} = NEW.{column}" for column in key) + ")"
        for key in _CONFLICT_KEYS[table]
    )


def _conflicting_insert_trigger(table: str, name: str, message: str) -> str:
    return f"""
        CREATE TRIGGER IF NOT EXISTS {name} BEFORE INSERT ON {table}
        WHEN EXISTS (SELECT 1 FROM {table} WHERE {_conflicting_key_predicate(table)})
        BEGIN SELECT RAISE(ABORT, '{message}'); END
        """


def _append_only_triggers(table: str) -> tuple[str, str, str]:
    message = f"append-only: {table} rows cannot be changed"
    return (
        _conflicting_insert_trigger(table, f"{table}_append_only_insert", message),
        f"""
        CREATE TRIGGER IF NOT EXISTS {table}_append_only_update BEFORE UPDATE ON {table}
        BEGIN SELECT RAISE(ABORT, '{message}'); END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS {table}_append_only_delete BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, '{message}'); END
        """,
    )


_TRIGGERS_V1 = (
    *(statement for table in _V1_APPEND_ONLY_TABLES for statement in _append_only_triggers(table)),
    """
    CREATE TRIGGER IF NOT EXISTS items_never_deleted BEFORE DELETE ON items
    BEGIN SELECT RAISE(ABORT, 'items rows are never deleted'); END
    """,
    _conflicting_insert_trigger(
        "items", "items_never_replaced",
        "items rows cannot be replaced: row_id, item_key, view_path, natural_key conflict",
    ),
    f"""
    CREATE TRIGGER IF NOT EXISTS items_never_replaced_update BEFORE UPDATE ON items
    WHEN EXISTS (
      SELECT 1 FROM items
      WHERE row_id != OLD.row_id AND ({_conflicting_key_predicate("items")})
    )
    BEGIN SELECT RAISE(ABORT,
      'items rows cannot be replaced: row_id, item_key, view_path, natural_key conflict'); END
    """,
    # The store-wide sequence is contiguous from 1 and follows the recorded head.
    """
    CREATE TRIGGER IF NOT EXISTS txns_commit_seq_contiguous BEFORE INSERT ON txns
    WHEN NEW.commit_seq IS NOT
      (SELECT CAST(value AS INTEGER) + 1 FROM store_meta WHERE key = 'commit_seq')
    BEGIN SELECT RAISE(ABORT, 'commit_seq must be contiguous with the store head'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS txns_advance_store_head AFTER INSERT ON txns
    BEGIN
      UPDATE store_meta SET value = CAST(NEW.commit_seq AS TEXT) WHERE key = 'commit_seq';
      INSERT OR REPLACE INTO store_meta(key, value)
        VALUES ('store_head_hash', NEW.store_head_hash);
    END
    """,
)


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _migrate_to_1(conn: sqlite3.Connection) -> None:
    """Create the version-1 schema and mint this store's identity."""
    for statement in (*_TABLES_V1, *_TRIGGERS_V1):
        conn.execute(statement)
    instance_id = str(uuid.uuid4())
    identity = {
        META_STORE_ID: str(uuid.uuid4()),
        META_INSTANCE_ID: instance_id,
        META_LINEAGE: json.dumps(
            [
                {
                    "instance_id": instance_id,
                    "adopted_from": None,
                    "adopted_at_commit_seq": 0,
                    "head_hash": None,
                }
            ],
            separators=(",", ":"),
        ),
        META_FORKS: "[]",
        META_COMMIT_SEQ: "0",
        META_CREATED_AT: _utc_now(),
    }
    conn.executemany(
        "INSERT INTO store_meta(key, value) VALUES (?, ?)", sorted(identity.items())
    )


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    """Persist canonical authorization metadata without changing the shipped V1 DDL."""
    from .. import structured_collections as collections
    from .. import vault
    from . import governance

    conn.execute("ALTER TABLE collection_manifests ADD COLUMN governance_json TEXT")
    conn.execute("ALTER TABLE items ADD COLUMN governance_json TEXT")
    conn.execute("ALTER TABLE held_candidates ADD COLUMN governance_json TEXT")
    conn.execute("ALTER TABLE held_candidates ADD COLUMN governance_hash TEXT")
    conn.execute("DROP TRIGGER IF EXISTS collection_manifests_append_only_update")
    manifests = {}
    for cid, version, text, schema_json in conn.execute(
        "SELECT collection_id,manifest_version,manifest_text,schema_json FROM collection_manifests"
    ).fetchall():
        metadata = governance.manifest_metadata(text)
        data, _, _ = vault.parse_frontmatter(text, strict=True)
        item_schema = collections._parse_schema(data["schema_version"], json.loads(schema_json))
        manifests[cid, version] = (metadata, item_schema)
        conn.execute("UPDATE collection_manifests SET governance_json=? "
                     "WHERE collection_id=? AND manifest_version=?", (metadata, cid, version))
    for row_id, cid, version, values in conn.execute(
        "SELECT i.row_id,i.collection_id,c.manifest_version,i.values_json FROM items i "
        "JOIN collections c ON c.collection_id=i.collection_id"
    ).fetchall():
        metadata, item_schema = manifests[cid, version]
        conn.execute("UPDATE items SET governance_json=? WHERE row_id=?",
                     (governance.row_metadata(item_schema, json.loads(values), metadata), row_id))
    for held_id, cid, version, raw, candidate in conn.execute(
        "SELECT h.held_id,h.collection_id,c.manifest_version,h.held_bytes,h.candidate_json "
        "FROM held_candidates h JOIN collections c ON c.collection_id=h.collection_id"
    ).fetchall():
        metadata, item_schema = manifests[cid, version]
        candidate = json.loads(candidate)
        before = None
        if candidate.get("action") == "update":
            current = conn.execute("SELECT values_json FROM items WHERE collection_id=? AND item_key=?",
                                   (cid, candidate.get("item_key"))).fetchone()
            before = json.loads(current[0]) if current is not None else None
        conn.execute("UPDATE held_candidates SET governance_json=?,governance_hash=? WHERE held_id=?",
                     (governance.held_metadata(item_schema, candidate, metadata, before),
                      hashlib.sha256(raw).hexdigest(), held_id))
    for statement in _append_only_triggers("collection_manifests"):
        conn.execute(statement)


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """Bind pending projections to their transaction-owned adjacent files."""
    conn.execute("ALTER TABLE projection_state ADD COLUMN install_json TEXT")


def _migrate_to_4(conn: sqlite3.Connection) -> None:
    """Record disposable query projections and bound ordered backfill work."""
    conn.execute("ALTER TABLE collections ADD COLUMN query_plan_hash TEXT")
    conn.execute("""CREATE TABLE query_projection_mappings(
      collection_id TEXT NOT NULL REFERENCES collections(collection_id),
      generation INTEGER NOT NULL CHECK(generation > 0),
      state TEXT NOT NULL CHECK(state IN ('building','ready','failed')),
      plan_json TEXT NOT NULL,
      plan_hash TEXT NOT NULL,
      last_row_id INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(collection_id,generation)
    ) STRICT, WITHOUT ROWID""")
    for state in ("ready", "building"):
        conn.execute(f"CREATE UNIQUE INDEX query_projection_one_{state} "
                     f"ON query_projection_mappings(collection_id) WHERE state='{state}'")
    conn.execute("CREATE INDEX items_by_collection_row ON items(collection_id,row_id)")
    conn.execute("""CREATE TABLE query_cursor_state(
      collection_id TEXT PRIMARY KEY REFERENCES collections(collection_id),
      basis_id TEXT NOT NULL,
      membership_revision INTEGER NOT NULL CHECK(membership_revision >= 0),
      fields_json TEXT NOT NULL CHECK(json_valid(fields_json))
    ) STRICT, WITHOUT ROWID""")
    from cryptography.fernet import Fernet

    conn.execute("INSERT INTO store_meta(key,value) VALUES('query_cursor_key',?)",
                 (Fernet.generate_key().decode("ascii"),))


_ITEMS_V5 = """
    CREATE TABLE items_v5(
      row_id INTEGER PRIMARY KEY,
      collection_id TEXT NOT NULL REFERENCES collections(collection_id),
      item_key TEXT NOT NULL,
      natural_key TEXT,
      row_version INTEGER NOT NULL,
      schema_version INTEGER NOT NULL,
      values_json TEXT,
      body TEXT NOT NULL DEFAULT '',
      payload_hash TEXT NOT NULL,
      view_path TEXT,
      created_txn INTEGER NOT NULL,
      updated_txn INTEGER NOT NULL,
      governance_json TEXT,
      encoding TEXT NOT NULL DEFAULT 'json-v1' CHECK (encoding IN ('json-v1', 'typed-v1')),
      UNIQUE (collection_id, item_key),
      UNIQUE (view_path),
      CHECK ((encoding = 'json-v1') = (values_json IS NOT NULL))
    ) STRICT
    """
_ITEM_COLUMNS_V4 = ("row_id,collection_id,item_key,natural_key,row_version,schema_version,values_json,"
                    "body,payload_hash,view_path,created_txn,updated_txn,governance_json")

_TRIGGERS_V5 = (
    *_append_only_triggers("version_identity"),
    # A JSON payload mints its identity; a JSON identity needs its payload.
    """
    CREATE TRIGGER IF NOT EXISTS item_versions_mint_identity AFTER INSERT ON item_versions
    BEGIN
      INSERT INTO version_identity VALUES (NEW.row_id, NEW.row_version, 'json-v1', NEW.payload_hash,
        NEW.txn_id, (SELECT schema_version FROM items WHERE row_id = NEW.row_id));
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS version_identity_json_payload BEFORE INSERT ON version_identity
    WHEN NEW.encoding = 'json-v1' AND NOT EXISTS (
      SELECT 1 FROM item_versions WHERE row_id = NEW.row_id AND row_version = NEW.row_version)
    BEGIN SELECT RAISE(ABORT, 'version_identity requires its JSON payload'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS version_identity_typed_collection BEFORE INSERT ON version_identity
    WHEN NEW.encoding = 'typed-v1' AND NOT EXISTS (
      SELECT 1 FROM items i JOIN collections c ON c.collection_id = i.collection_id
      WHERE i.row_id = NEW.row_id AND i.encoding = 'typed-v1' AND c.encoding = 'typed-v1')
    BEGIN SELECT RAISE(ABORT, 'version_identity requires its typed payload in a typed-v1 collection'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS item_versions_json_rows_only BEFORE INSERT ON item_versions
    WHEN EXISTS (SELECT 1 FROM items WHERE row_id = NEW.row_id AND encoding <> 'json-v1')
    BEGIN SELECT RAISE(ABORT, 'typed-v1 rows take typed versions, not JSON payloads'); END
    """,
    # One collection, one encoding authority; typed-v1 has no in-place reverse.
    """
    CREATE TRIGGER IF NOT EXISTS items_encoding_matches_collection BEFORE INSERT ON items
    WHEN EXISTS (SELECT 1 FROM collections WHERE collection_id = NEW.collection_id AND encoding <> NEW.encoding)
    BEGIN SELECT RAISE(ABORT, 'item encoding must match its collection encoding'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_encoding_update_matches_collection
    BEFORE UPDATE OF encoding, collection_id ON items
    WHEN EXISTS (SELECT 1 FROM collections WHERE collection_id = NEW.collection_id AND encoding <> NEW.encoding)
    BEGIN SELECT RAISE(ABORT, 'item encoding must match its collection encoding'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collections_encoding_forward_only BEFORE UPDATE OF encoding ON collections
    WHEN OLD.encoding = 'typed-v1' AND NEW.encoding <> 'typed-v1'
    BEGIN SELECT RAISE(ABORT, 'collection encoding typed-v1 has no in-place reverse'); END
    """,
)


def _migrate_to_5(conn: sqlite3.Connection) -> None:
    """Add the version spine, encoding discriminators and nullable view paths.

    ``ensure_schema`` runs this with foreign keys off (SQLite's table-rebuild
    procedure) and checks every foreign key before commit. Existing item,
    version, source and audit rows keep their bytes; JSON history is not copied.
    """
    conn.execute("ALTER TABLE collections ADD COLUMN encoding TEXT NOT NULL DEFAULT 'json-v1' "
                 "CHECK (encoding IN ('json-v1', 'typed-v1'))")
    conn.execute(_ITEMS_V5)
    conn.execute(f"INSERT INTO items_v5({_ITEM_COLUMNS_V4},encoding) "
                 f"SELECT {_ITEM_COLUMNS_V4},'json-v1' FROM items")
    conn.execute("DROP TABLE items")
    conn.execute("ALTER TABLE items_v5 RENAME TO items")
    conn.execute("CREATE UNIQUE INDEX items_natural_key ON items(collection_id, natural_key) "
                 "WHERE natural_key IS NOT NULL")
    conn.execute("CREATE INDEX items_by_collection_row ON items(collection_id,row_id)")
    conn.execute("""CREATE TABLE version_identity(
      row_id INTEGER NOT NULL REFERENCES items(row_id),
      row_version INTEGER NOT NULL CHECK (row_version >= 1),
      encoding TEXT NOT NULL CHECK (encoding IN ('json-v1', 'typed-v1')),
      payload_hash TEXT NOT NULL,
      txn_id INTEGER NOT NULL REFERENCES txns(txn_id) DEFERRABLE INITIALLY DEFERRED,
      schema_version INTEGER NOT NULL,
      PRIMARY KEY (row_id, row_version)
    ) STRICT, WITHOUT ROWID""")
    # Revise refuses a schema-version change, so every version of an item was
    # hashed under the item's own recorded schema version.
    conn.execute("INSERT INTO version_identity SELECT v.row_id, v.row_version, 'json-v1', v.payload_hash, "
                 "v.txn_id, i.schema_version FROM item_versions v JOIN items i ON i.row_id = v.row_id")
    conn.execute("""CREATE TABLE item_sources_v5(
      row_id INTEGER NOT NULL,
      row_version INTEGER NOT NULL,
      ordinal INTEGER NOT NULL,
      source_ref TEXT NOT NULL,
      PRIMARY KEY (row_id, row_version, ordinal),
      FOREIGN KEY (row_id, row_version) REFERENCES version_identity(row_id, row_version)
    ) STRICT, WITHOUT ROWID""")
    conn.execute("INSERT INTO item_sources_v5 SELECT row_id,row_version,ordinal,source_ref FROM item_sources")
    conn.execute("DROP TABLE item_sources")
    conn.execute("ALTER TABLE item_sources_v5 RENAME TO item_sources")
    conn.execute("""CREATE TABLE typed_encoding_mappings(
      collection_id TEXT NOT NULL REFERENCES collections(collection_id),
      generation INTEGER NOT NULL CHECK (generation > 0),
      state TEXT NOT NULL CHECK (state IN ('building', 'ready', 'failed')),
      layout_json TEXT NOT NULL CHECK (json_valid(layout_json)),
      layout_hash TEXT NOT NULL,
      last_row_id INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (collection_id, generation)
    ) STRICT, WITHOUT ROWID""")
    for state in ("ready", "building"):
        conn.execute(f"CREATE UNIQUE INDEX typed_encoding_one_{state} "
                     f"ON typed_encoding_mappings(collection_id) WHERE state='{state}'")


def _migrate_to_6(conn: sqlite3.Connection) -> None:
    """Add durable preserved-source import jobs and their rejected positions.

    A job's checkpoint, counters and rejections change only inside the batch
    transaction that commits its rows (``importer``); the binding JSON holds
    identifiers and hashes, never credential material.
    """
    conn.execute("""CREATE TABLE import_jobs(
      job_id TEXT PRIMARY KEY,
      collection_id TEXT NOT NULL REFERENCES collections(collection_id),
      binding_json TEXT NOT NULL CHECK (json_valid(binding_json)),
      state TEXT NOT NULL CHECK (state IN ('running', 'partial', 'failed', 'complete')),
      reason TEXT CHECK (reason IN ('authority_lost', 'time_cap', 'invalid_row', 'cancelled')),
      checkpoint_json TEXT NOT NULL CHECK (json_valid(checkpoint_json)),
      progress_json TEXT NOT NULL CHECK (json_valid(progress_json)),
      window_started INTEGER NOT NULL,
      window_expires INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    ) STRICT, WITHOUT ROWID""")
    conn.execute("CREATE INDEX import_jobs_by_state ON import_jobs(state, created_at)")
    conn.execute("""CREATE TABLE import_rejections(
      job_id TEXT NOT NULL REFERENCES import_jobs(job_id),
      ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
      byte_offset INTEGER NOT NULL CHECK (byte_offset >= 0),
      code TEXT NOT NULL,
      at TEXT NOT NULL,
      PRIMARY KEY (job_id, ordinal)
    ) STRICT, WITHOUT ROWID""")


#: Forward migrations: ``MIGRATIONS[n]`` takes a store at version ``n - 1`` to ``n``.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_to_1, 2: _migrate_to_2, 3: _migrate_to_3, 4: _migrate_to_4, 5: _migrate_to_5,
    6: _migrate_to_6,
}


class SchemaVersionError(RuntimeError):
    """The store was written by a newer release than this one."""

    def __init__(self, found: int, supported: int) -> None:
        super().__init__(
            f"collection store schema {found} is newer than this release supports ({supported})"
        )
        self.found = found
        self.supported = supported


def schema_version(conn: sqlite3.Connection) -> int:
    """The recorded schema version, or 0 for an empty database."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_meta'"
    ).fetchone()
    if present is None:
        return 0
    row = conn.execute(
        "SELECT value FROM store_meta WHERE key = ?", (META_SCHEMA_VERSION,)
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Bring the store up to :data:`SCHEMA_VERSION` in one immediate transaction.

    Every missing step runs in order, then the version is recorded, all or
    nothing. A store newer than this release refuses rather than downgrading.
    Migrations run with foreign keys off, as SQLite's table-rebuild procedure
    requires, and commit only after a full foreign-key check.
    """
    target = SCHEMA_VERSION
    migrating = schema_version(conn) < target
    enforced = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if migrating and enforced:
        conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = schema_version(conn)
            if current > target:
                raise SchemaVersionError(current, target)
            for version in range(current + 1, target + 1):
                MIGRATIONS[version](conn)
            # Repair missing protections even when the recorded version is current.
            for statement in (*_TRIGGERS_V1, *(_TRIGGERS_V5 if target >= 5 else ())):
                conn.execute(statement)
            if target >= 5:
                from . import typed_storage

                typed_storage.repair_triggers(conn)
            conn.execute(
                "INSERT INTO store_meta(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (META_SCHEMA_VERSION, str(target)),
            )
            if migrating and conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("FOREIGN KEY constraint failed during schema migration")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        if migrating and enforced:
            conn.execute("PRAGMA foreign_keys=ON")
    return target
