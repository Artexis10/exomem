"""DDL and forward migrations for the structured-collection store.

The store is the single source of truth for structured collections once a
vault migrates (OpenSpec ``move-structured-collections-to-sqlite``, design
§2 and §16). Until the GA gate it is dark: nothing routes to it.

Contract held here, not in the writers:

- every table is ``STRICT``;
- ``txns``, ``audit_effects``, ``item_versions``, ``item_sources``,
  ``collection_manifests`` and ``collection_type_versions`` are append-only:
  conflicting ``BEFORE INSERT``, ``BEFORE UPDATE`` and ``BEFORE DELETE``
  triggers abort the statement;
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

SCHEMA_VERSION = 3

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
)
APPEND_ONLY_TABLES = (
    "txns",
    "audit_effects",
    "item_versions",
    "item_sources",
    "collection_manifests",
    "collection_type_versions",
)

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
    *(statement for table in APPEND_ONLY_TABLES for statement in _append_only_triggers(table)),
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


#: Forward migrations: ``MIGRATIONS[n]`` takes a store at version ``n - 1`` to ``n``.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_to_1, 2: _migrate_to_2, 3: _migrate_to_3,
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
    """
    target = SCHEMA_VERSION
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = schema_version(conn)
        if current > target:
            raise SchemaVersionError(current, target)
        for version in range(current + 1, target + 1):
            MIGRATIONS[version](conn)
        # Repair missing protections even when the recorded version is current.
        for statement in _TRIGGERS_V1:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO store_meta(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (META_SCHEMA_VERSION, str(target)),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return target
