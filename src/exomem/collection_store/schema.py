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
import threading
import uuid
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alembic.config import Config
    from sqlalchemy.engine import Connection

SCHEMA_VERSION = 8

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
# "1" while a view carries another store instance's stamp; business writes refuse until adopt-local or reconcile.
META_VIEW_DIVERGED = "diverged"
# Owner adopt-local: the previewed foreign replica digest the next publication keeps as evidence.
META_ADOPTED_FOREIGN_REPLICA = "adopted_foreign_replica"
# Digests of foreign evidence already reconciled into held corrections.
META_RECONCILED_FOREIGN = "reconciled_foreign"
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
    "rollup_definitions",
    "rollup_buckets",
    "rollup_members",
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


_PROJECTION_COLUMNS = ("path,collection_id,row_id,kind,published_row_version,published_sha256,"
                       "pending_row_version,pending_sha256,stat_identity,state,install_json")


def _migrate_to_6(conn: sqlite3.Connection) -> None:
    """Record each collection's view mode and admit summary-page projections.

    Existing collections are items mode. Summary pages are collection-level
    projections, so ``projection_state`` is rebuilt with the ``summary`` kind.
    """
    conn.execute("ALTER TABLE collections ADD COLUMN view_mode TEXT NOT NULL DEFAULT 'items' "
                 "CHECK (view_mode IN ('items', 'summary'))")
    conn.execute("""CREATE TABLE projection_state_v6(
      path TEXT PRIMARY KEY,
      collection_id TEXT REFERENCES collections(collection_id),
      row_id INTEGER REFERENCES items(row_id),
      kind TEXT NOT NULL
        CHECK (kind IN ('manifest', 'item', 'log', 'held', 'history', 'type', 'summary')),
      published_row_version INTEGER,
      published_sha256 TEXT,
      pending_row_version INTEGER,
      pending_sha256 TEXT,
      stat_identity TEXT,
      state TEXT NOT NULL CHECK (state IN ('current', 'pending', 'held')),
      install_json TEXT
    ) STRICT""")
    conn.execute(f"INSERT INTO projection_state_v6({_PROJECTION_COLUMNS}) "
                 f"SELECT {_PROJECTION_COLUMNS} FROM projection_state")
    conn.execute("DROP TABLE projection_state")
    conn.execute("ALTER TABLE projection_state_v6 RENAME TO projection_state")


def _migrate_to_7(conn: sqlite3.Connection) -> None:
    """Add durable preserved-source import jobs and their rejected positions.

    A job's checkpoint, counters and rejections change only inside the batch
    transaction that commits its rows (``importer``); the binding JSON holds
    identifiers and hashes, never credential material. ``identity`` is the
    digest of what an identical start binds, so a retry finds its job.
    """
    conn.execute("""CREATE TABLE import_jobs(
      job_id TEXT PRIMARY KEY,
      identity TEXT NOT NULL,
      collection_id TEXT NOT NULL REFERENCES collections(collection_id),
      binding_json TEXT NOT NULL CHECK (json_valid(binding_json)),
      state TEXT NOT NULL CHECK (state IN ('running', 'partial', 'failed', 'complete')),
      reason TEXT CHECK (reason IN ('authority_lost', 'time_cap', 'cancelled', 'invalid_row',
                                    'batch_error')),
      checkpoint_json TEXT NOT NULL CHECK (json_valid(checkpoint_json)),
      progress_json TEXT NOT NULL CHECK (json_valid(progress_json)),
      window_started INTEGER NOT NULL,
      window_expires INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    ) STRICT, WITHOUT ROWID""")
    conn.execute("CREATE INDEX import_jobs_by_state ON import_jobs(state, created_at)")
    conn.execute("CREATE INDEX import_jobs_by_identity ON import_jobs(identity, created_at)")
    conn.execute("""CREATE TABLE import_rejections(
      job_id TEXT NOT NULL REFERENCES import_jobs(job_id),
      ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
      byte_offset INTEGER NOT NULL CHECK (byte_offset >= 0),
      code TEXT NOT NULL,
      at TEXT NOT NULL,
      PRIMARY KEY (job_id, ordinal)
    ) STRICT, WITHOUT ROWID""")


def _migrate_to_8(conn: sqlite3.Connection) -> None:
    """Admit declared exact rollups: versioned definitions, buckets and extreme members.

    A definition is building until bounded writer-owned backfill covers every
    row, then ready. Buckets hold exact per-bucket reduction state; members
    index a bucket's rows only for rollups that reduce min/max/latest.
    """
    conn.execute("""CREATE TABLE rollup_definitions(
      rollup_id INTEGER PRIMARY KEY AUTOINCREMENT,
      collection_id TEXT NOT NULL REFERENCES collections(collection_id),
      name TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('building','ready')),
      definition_json TEXT NOT NULL CHECK(json_valid(definition_json)),
      last_row_id INTEGER NOT NULL DEFAULT 0,
      flagged INTEGER NOT NULL DEFAULT 0 CHECK(flagged >= 0),
      UNIQUE(collection_id,name)
    ) STRICT""")
    conn.execute("""CREATE TABLE rollup_buckets(
      rollup_id INTEGER NOT NULL REFERENCES rollup_definitions(rollup_id),
      bucket TEXT NOT NULL,
      groups TEXT NOT NULL CHECK(json_valid(groups)),
      state_json TEXT NOT NULL CHECK(json_valid(state_json)),
      PRIMARY KEY(rollup_id,bucket,groups)
    ) STRICT, WITHOUT ROWID""")
    conn.execute("""CREATE TABLE rollup_members(
      rollup_id INTEGER NOT NULL REFERENCES rollup_definitions(rollup_id),
      bucket TEXT NOT NULL,
      groups TEXT NOT NULL,
      row_id INTEGER NOT NULL REFERENCES items(row_id),
      PRIMARY KEY(rollup_id,bucket,groups,row_id)
    ) STRICT, WITHOUT ROWID""")


_MIGRATION_PATH = Path(__file__).with_name("migrations")
# Alembic's context/op proxies are process-global, so environment lifetimes cannot overlap.
_ALEMBIC_ENVIRONMENT_LOCK = threading.Lock()


def _migration_config(conn: Connection | None = None) -> Config:
    from alembic.config import Config

    config = Config()
    # ConfigParser requires literal percent signs in installation paths to be escaped.
    config.set_main_option("script_location", str(_MIGRATION_PATH).replace("%", "%%"))
    config.attributes["connection"] = conn
    return config


@lru_cache(maxsize=1)
def _revisions(path: Path) -> frozenset[str]:
    from alembic.script import ScriptDirectory

    return frozenset(revision.revision for revision in ScriptDirectory(str(path)).walk_revisions())


class SchemaMetadataError(ValueError):
    """Installation revision metadata does not identify one compatible schema."""


class SchemaVersionError(RuntimeError):
    """The store was written by a newer release than this one."""

    def __init__(self, found: int, supported: int) -> None:
        super().__init__(
            f"collection store schema {found} is newer than this release supports ({supported})"
        )
        self.found = found
        self.supported = supported


def schema_version(conn: sqlite3.Connection, *, ceiling: int | None = None) -> int:
    """The recorded schema version, or 0 for an empty database."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_meta'"
    ).fetchone()
    row = None if present is None else conn.execute(
        "SELECT value FROM store_meta WHERE key = ?", (META_SCHEMA_VERSION,)
    ).fetchone()
    version = 0 if row is None else int(row[0])
    if ceiling is not None and version > ceiling:
        raise SchemaVersionError(version, ceiling)
    revision_table = conn.execute(
        "SELECT type FROM sqlite_master WHERE name='alembic_version' COLLATE NOCASE"
    ).fetchone()
    if revision_table is not None:
        # Contradictory metadata risks a wrong migration; refuse this store for repair.
        columns = conn.execute("PRAGMA table_xinfo(alembic_version)").fetchall()
        strict = conn.execute("SELECT strict FROM pragma_table_list WHERE schema='main' AND name='alembic_version' COLLATE NOCASE").fetchone()
        if (revision_table != ("table",) or strict != (1,) or len(columns) != 1 or columns[0][1] != "version_num"
                or columns[0][2].upper() != "TEXT" or columns[0][5] != 1):
            raise SchemaMetadataError("malformed installation revision table")
        revisions = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        if len(revisions) != 1 or revisions[0][0] not in _revisions(_MIGRATION_PATH):
            raise SchemaMetadataError("unknown or missing installation revision")
        if revisions[0][0] != str(version):
            raise SchemaMetadataError("installation revision disagrees with compatibility version")
    return version


def ensure_schema(conn: Connection) -> int:
    """Apply packaged revisions in one writer-owned immediate transaction."""
    # Store libraries load with the first store, not with every CLI import.
    from alembic import command
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.schema import CreateTable

    from . import tables
    from .connection import rollback

    raw = conn.connection.driver_connection
    target = SCHEMA_VERSION
    current = schema_version(raw, ceiling=target)
    migrating = current < target
    enforced = raw.execute("PRAGMA foreign_keys").fetchone()[0]
    if migrating and enforced:
        raw.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.begin()
        try:
            config = _migration_config(conn)
            conn.execute(CreateTable(tables.alembic_version, if_not_exists=True))
            with _ALEMBIC_ENVIRONMENT_LOCK:
                if not raw.execute("SELECT 1 FROM alembic_version").fetchone() and current:
                    command.stamp(config, str(current))
                command.upgrade(config, str(target))
            for statement in (*_TRIGGERS_V1, *(_TRIGGERS_V5 if target >= 5 else ())):
                raw.execute(statement)
            if target >= 5:
                from . import typed_storage

                typed_storage.repair_triggers(raw)
            revision = raw.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            conn.execute(tables.SET_META, {"key": META_SCHEMA_VERSION, "value": revision})
            if migrating and raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("FOREIGN KEY constraint failed during schema migration")
            conn.commit()
        except BaseException as error:
            rollback(conn)
            if isinstance(error, DBAPIError):
                raise error.orig from error
            raise
    finally:
        if migrating and enforced:
            raw.execute("PRAGMA foreign_keys=ON")
    return target
