"""Durable registry for deferred semantic-index paths."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from . import reserved_paths, sidecar_store
from .kbdir import kb_dirname

_SEMANTIC_ISOLATION_CURSOR_KEY = "semantic_isolation_cursors:v1"
_MIXED_DRAIN_TURN_KEY = "mixed_drain_turn:v1"
_SEMANTIC_UPSERTS_GENERATION_KEY = "semantic_upserts_generation"
_GRAPH_UPSERTS_GENERATION_KEY = "graph_upserts_generation"
_GRAPH_FULL_REBUILD_KEY = "graph_full_rebuild_generation"
_GRAPH_FULL_REBUILD_SEQUENCE_KEY = "graph_full_rebuild_sequence"
#: Counts every raise of whole-vault debt, including a repeat that leaves the
#: marker's value unchanged, so a retirement can tell "no new debt" from "new
#: debt at the same value".
_GRAPH_FULL_REBUILD_MARKS_KEY = "graph_full_rebuild_marks"
_PENDING_VISIBILITY_GENERATION_KEY = "pending_visibility_generation:v1"

#: The queues this store carries, and the tables behind them.  The graph queue
#: is the newest and the reason the mapping exists: it reuses the semantic
#: queue's shape verbatim rather than introducing a parallel store, so the
#: crash-safety, receipt-CAS and poison-isolation properties are the ones
#: already in production rather than a second implementation of them.
_QUEUE_TABLES = {
    "semantic": "semantic_upserts",
    "full": "full_upserts",
    "graph": "graph_upserts",
}

#: A rotated receipt has to sort strictly *behind* untouched work, and
#: `time.time()` on Windows is coarse enough that a rotation within the same
#: tick as the insert leaves the poisoned path first in the queue -- pinning
#: exactly the work rotation exists to unpin.  Rotation therefore takes the
#: later of the wall clock and one epsilon past the queue's current maximum.
_ROTATION_EPSILON_SECONDS = 1e-6


def _ensure_derived_batch_schema(conn: sqlite3.Connection) -> None:
    """Add the exact derived-batch custody tables without touching old queues."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS derived_batches (
            schema_version INTEGER NOT NULL CHECK(schema_version = 1),
            batch_id TEXT PRIMARY KEY CHECK(length(batch_id) BETWEEN 1 AND 128),
            mutation_attempt_digest TEXT NOT NULL
                CHECK(length(mutation_attempt_digest) = 64),
            canonical_generation TEXT NOT NULL
                CHECK(length(canonical_generation) BETWEEN 1 AND 128),
            checkpoint_id TEXT NOT NULL
                CHECK(length(checkpoint_id) BETWEEN 1 AND 128),
            state TEXT NOT NULL CHECK(state IN (
                'prepared', 'ready', 'completed', 'aborted', 'superseded',
                'reconcile_required'
            )),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            failure_code TEXT CHECK(
                failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 64
            )
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS derived_batch_paths (
            batch_id TEXT NOT NULL,
            rel_path TEXT NOT NULL CHECK(length(rel_path) BETWEEN 1 AND 1024),
            before_hash TEXT CHECK(before_hash IS NULL OR length(before_hash) = 64),
            after_hash TEXT CHECK(after_hash IS NULL OR length(after_hash) = 64),
            stable_memory_ref TEXT CHECK(
                stable_memory_ref IS NULL OR length(stable_memory_ref) BETWEEN 1 AND 256
            ),
            PRIMARY KEY(batch_id, rel_path),
            FOREIGN KEY(batch_id) REFERENCES derived_batches(batch_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS derived_batch_components (
            batch_id TEXT NOT NULL,
            component TEXT NOT NULL CHECK(component IN (
                'freshness', 'memory_refs', 'resolver', 'semantic_purge',
                'lexstore', 'graph', 'embeddings', 'claims', 'write_advisory'
            )),
            revision INTEGER NOT NULL CHECK(revision >= 1),
            state TEXT NOT NULL CHECK(state IN (
                'prepared', 'ready', 'claimed', 'retryable', 'completed',
                'not_required', 'aborted', 'superseded', 'reconcile_required',
                'failed'
            )),
            lease_revision INTEGER NOT NULL DEFAULT 0 CHECK(lease_revision >= 0),
            claim_owner TEXT CHECK(
                claim_owner IS NULL OR length(claim_owner) BETWEEN 1 AND 128
            ),
            claim_expires_at REAL,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            next_attempt_at REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            failure_code TEXT CHECK(
                failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 64
            ),
            PRIMARY KEY(batch_id, component),
            FOREIGN KEY(batch_id) REFERENCES derived_batches(batch_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_recall_rows (
            batch_id TEXT NOT NULL,
            rel_path TEXT NOT NULL CHECK(length(rel_path) BETWEEN 1 AND 1024),
            component_revision INTEGER NOT NULL CHECK(component_revision >= 1),
            canonical_generation TEXT NOT NULL
                CHECK(length(canonical_generation) BETWEEN 1 AND 128),
            state TEXT NOT NULL CHECK(state IN ('prepared', 'live', 'retired')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(batch_id, rel_path),
            FOREIGN KEY(batch_id, rel_path)
                REFERENCES derived_batch_paths(batch_id, rel_path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS write_advisory_results (
            result_id TEXT PRIMARY KEY CHECK(length(result_id) BETWEEN 1 AND 64),
            batch_id TEXT NOT NULL,
            component_revision INTEGER NOT NULL CHECK(component_revision >= 1),
            target_rel_path TEXT CHECK(
                target_rel_path IS NULL OR length(target_rel_path) BETWEEN 1 AND 1024
            ),
            target_fingerprint TEXT NOT NULL
                CHECK(length(target_fingerprint) BETWEEN 1 AND 128),
            counterpart_fingerprint TEXT CHECK(
                counterpart_fingerprint IS NULL
                OR length(counterpart_fingerprint) BETWEEN 1 AND 128
            ),
            state TEXT NOT NULL CHECK(state IN (
                'pending', 'ready', 'failed', 'superseded'
            )),
            failure_code TEXT CHECK(
                failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 64
            ),
            advisory_ref TEXT CHECK(
                advisory_ref IS NULL OR length(advisory_ref) BETWEEN 1 AND 256
            ),
            review_ref TEXT CHECK(
                review_ref IS NULL OR length(review_ref) BETWEEN 1 AND 256
            ),
            retention_deadline REAL NOT NULL,
            terminal_replay_until REAL NOT NULL,
            publication_revision INTEGER NOT NULL DEFAULT 1
                CHECK(publication_revision >= 1),
            published_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(batch_id, component_revision)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS write_advisory_result_candidates (
            result_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 7),
            counterpart_rel_path TEXT NOT NULL
                CHECK(length(counterpart_rel_path) BETWEEN 1 AND 1024),
            counterpart_fingerprint TEXT NOT NULL
                CHECK(length(counterpart_fingerprint) BETWEEN 1 AND 128),
            warning TEXT NOT NULL CHECK(length(warning) BETWEEN 1 AND 300),
            advisory_ref TEXT NOT NULL
                CHECK(length(advisory_ref) BETWEEN 1 AND 256),
            review_ref TEXT NOT NULL CHECK(length(review_ref) BETWEEN 1 AND 256),
            triage_fingerprint TEXT NOT NULL CHECK(length(triage_fingerprint) = 24),
            PRIMARY KEY(result_id, ordinal),
            FOREIGN KEY(result_id) REFERENCES write_advisory_results(result_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS derived_components_schedule "
        "ON derived_batch_components(state, next_attempt_at, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS derived_paths_lookup "
        "ON derived_batch_paths(rel_path, batch_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS pending_recall_visibility "
        "ON pending_recall_rows(state, canonical_generation)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS advisory_result_retention "
        "ON write_advisory_results(retention_deadline, terminal_replay_until)"
    )
    advisory_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(write_advisory_results)")
    }
    if "target_rel_path" not in advisory_columns:
        conn.execute(
            "ALTER TABLE write_advisory_results ADD COLUMN target_rel_path TEXT"
        )
    # Seed the fence only when it is genuinely absent.  An unconditional write
    # here would make every ``create=True`` open queue for the SQLite write lock
    # behind a live writer, which is exactly what the read seams must not do.
    if conn.execute(
        "SELECT 1 FROM maintenance_state WHERE key = ?",
        (_PENDING_VISIBILITY_GENERATION_KEY,),
    ).fetchone() is None:
        conn.execute(
            "INSERT OR IGNORE INTO maintenance_state(key, value) VALUES (?, '0')",
            (_PENDING_VISIBILITY_GENERATION_KEY,),
        )
    for event in ("INSERT", "UPDATE", "DELETE"):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS pending_visibility_generation_{event.lower()} "
            f"AFTER {event} ON pending_recall_rows BEGIN "
            "INSERT INTO maintenance_state(key, value) VALUES "
            f"('{_PENDING_VISIBILITY_GENERATION_KEY}', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1; END"
        )
    component_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(derived_batch_components)")
    }
    if "lease_revision" not in component_columns:
        conn.execute(
            "ALTER TABLE derived_batch_components "
            "ADD COLUMN lease_revision INTEGER NOT NULL DEFAULT 0"
        )
        # The rejected schema used ``revision`` for both durable lineage and
        # lease attempts.  Every schema-v1 batch/pending/advisory row has one
        # durable revision; preserve the excess as lease lineage instead of
        # stranding revision-1 related rows behind a revision-2+ component.
        conn.execute(
            "UPDATE derived_batch_components SET "
            "lease_revision = CASE WHEN revision > 1 THEN revision - 1 ELSE 0 END, "
            "revision = 1"
        )
        conn.execute("UPDATE pending_recall_rows SET component_revision = 1")
        conn.execute("UPDATE write_advisory_results SET component_revision = 1")
    # DDL itself is durable without a caller transaction, but the additive
    # generation initialization and any repair DML are not. Commit schema
    # migration before returning a handle whose caller may begin immediately.
    conn.commit()


def _ensure_vocabulary_provenance_schema(conn: sqlite3.Connection) -> None:
    """Add bounded vocabulary provenance checkpoints to the existing sidecar."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocabulary_provenance_checkpoints (
            target_path TEXT PRIMARY KEY,
            target_hash TEXT NOT NULL CHECK(length(target_hash) = 64),
            graph_generation TEXT NOT NULL CHECK(length(graph_generation) BETWEEN 1 AND 128),
            revision INTEGER NOT NULL CHECK(revision >= 1),
            after_path TEXT NOT NULL,
            complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
            evidence_digest TEXT CHECK(evidence_digest IS NULL OR length(evidence_digest) = 64),
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocabulary_provenance_sources (
            target_path TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            source_hash TEXT NOT NULL CHECK(length(source_hash) = 64),
            declared INTEGER NOT NULL CHECK(declared IN (0, 1)),
            component TEXT NOT NULL,
            PRIMARY KEY(target_path, source_path),
            FOREIGN KEY(target_path) REFERENCES vocabulary_provenance_checkpoints(target_path)
                ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocabulary_provenance_aliases (
            target_path TEXT NOT NULL,
            alias TEXT NOT NULL,
            source_path TEXT NOT NULL,
            PRIMARY KEY(target_path, alias, source_path),
            FOREIGN KEY(target_path, source_path)
                REFERENCES vocabulary_provenance_sources(target_path, source_path)
                ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS vocabulary_provenance_alias_lookup "
        "ON vocabulary_provenance_aliases(target_path, alias)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS vocabulary_provenance_component_lookup "
        "ON vocabulary_provenance_sources(target_path, component, source_path)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocabulary_provenance_components (
            target_path TEXT NOT NULL,
            component TEXT NOT NULL,
            parent TEXT NOT NULL,
            is_root INTEGER NOT NULL DEFAULT 1 CHECK(is_root IN (0, 1)),
            rank INTEGER NOT NULL DEFAULT 0,
            first_path TEXT,
            first_ref TEXT,
            first_hash TEXT,
            declared_path TEXT,
            declared_ref TEXT,
            declared_hash TEXT,
            PRIMARY KEY(target_path, component),
            FOREIGN KEY(target_path) REFERENCES vocabulary_provenance_checkpoints(target_path)
                ON DELETE CASCADE
        )
        """
    )
    component_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(vocabulary_provenance_components)")
    }
    if "is_root" not in component_columns:
        conn.execute(
            "ALTER TABLE vocabulary_provenance_components "
            "ADD COLUMN is_root INTEGER NOT NULL DEFAULT 1 CHECK(is_root IN (0, 1))"
        )
        conn.execute(
            "UPDATE vocabulary_provenance_components SET is_root = (parent = component)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS vocabulary_provenance_roots "
        "ON vocabulary_provenance_components(target_path, is_root, first_path)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vocabulary_provenance_retired "
        "(target_path TEXT PRIMARY KEY, needs_cleanup INTEGER NOT NULL DEFAULT 1 CHECK(needs_cleanup IN (0, 1)))"
    )
    retired_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(vocabulary_provenance_retired)")
    }
    if "needs_cleanup" not in retired_columns:
        conn.execute(
            "ALTER TABLE vocabulary_provenance_retired "
            "ADD COLUMN needs_cleanup INTEGER NOT NULL DEFAULT 1 CHECK(needs_cleanup IN (0, 1))"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS vocabulary_provenance_retired_pending "
        "ON vocabulary_provenance_retired(needs_cleanup)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vocabulary_provenance_active "
        "(logical_path TEXT PRIMARY KEY, target_path TEXT NOT NULL)"
    )
    conn.commit()


def _sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("deferred_index"):
        return _sqlite_connect_owned(database, *args, **kwargs)


def _sqlite_connect_owned(
    database: Any, *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    return sqlite3.connect(database, *args, **kwargs)


def store_path(vault_root: Path) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / ".deferred-index.sqlite"


def _connect_readonly(vault_root: Path) -> sqlite3.Connection:
    """Open an existing sidecar without schema repair or journal writes."""
    with reserved_paths._subsystem_authority_scope("deferred_index"):
        with reserved_paths._identity_coordination_scope(
            vault_root,
            descriptor_ids=("deferred-index-store",),
            identity_may_change=False,
        ):
            return _connect_readonly_owned(vault_root)


def _connect_readonly_owned(vault_root: Path) -> sqlite3.Connection:
    path = store_path(vault_root)
    with reserved_paths._sqlite_owner_target_scope(
        vault_root,
        path,
        "deferred-index-store",
        create=False,
    ) as retained_path:
        conn = _sqlite_connect_owned(
            f"{retained_path.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        try:
            reserved_paths._publish_sqlite_owner_family(
                vault_root,
                path,
                "deferred-index-store",
                conn,
            )
            return conn
        except BaseException:
            conn.close()
            raise


def _connect(
    vault_root: Path, *, create: bool, connection_path: Path | None = None
) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("deferred_index"):
        with reserved_paths._identity_coordination_scope(
            vault_root,
            descriptor_ids=("deferred-index-store",),
            identity_may_change=create,
        ):
            return _connect_owned(
                vault_root,
                create=create,
                connection_path=connection_path,
            )


def _connect_owned(
    vault_root: Path, *, create: bool, connection_path: Path | None = None
) -> sqlite3.Connection:
    path = connection_path if connection_path is not None else store_path(vault_root)
    if not create:
        return _connect_readonly_owned(vault_root)
    if connection_path is None or path == store_path(vault_root):
        with reserved_paths._sqlite_owner_target_scope(
            vault_root,
            path,
            "deferred-index-store",
            create=True,
        ) as retained_path:
            return _connect_created_owned(
                vault_root,
                retained_path,
                publish=True,
            )
    return _connect_created_owned(vault_root, path, publish=False)


def _connect_created_owned(
    vault_root: Path,
    path: Path,
    *,
    publish: bool,
) -> sqlite3.Connection:
    sidecar_store.ensure_sidecar_parent(path)
    conn = _sqlite_connect_owned(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_upserts (
            rel_path TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS maintenance_state ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS semantic_upserts_generation_insert "
        "AFTER INSERT ON semantic_upserts BEGIN "
        "INSERT INTO maintenance_state(key, value) VALUES "
        f"('{_SEMANTIC_UPSERTS_GENERATION_KEY}', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1; END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS semantic_upserts_generation_update "
        "AFTER UPDATE ON semantic_upserts BEGIN "
        "INSERT INTO maintenance_state(key, value) VALUES "
        f"('{_SEMANTIC_UPSERTS_GENERATION_KEY}', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1; END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS semantic_upserts_generation_delete "
        "AFTER DELETE ON semantic_upserts BEGIN "
        "INSERT INTO maintenance_state(key, value) VALUES "
        f"('{_SEMANTIC_UPSERTS_GENERATION_KEY}', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1; END"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS full_upserts (
            rel_path TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_upserts (
            rel_path TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_debt_generations (
            generation INTEGER PRIMARY KEY,
            recorded_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_failures (
            rel_path TEXT PRIMARY KEY,
            attempts INTEGER NOT NULL,
            first_failed_at REAL NOT NULL,
            last_failed_at REAL NOT NULL,
            quarantined INTEGER NOT NULL DEFAULT 0,
            signature TEXT
        )
        """
    )
    for event in ("INSERT", "UPDATE", "DELETE"):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS graph_upserts_generation_{event.lower()} "
            f"AFTER {event} ON graph_upserts BEGIN "
            "INSERT INTO maintenance_state(key, value) VALUES "
            f"('{_GRAPH_UPSERTS_GENERATION_KEY}', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1; END"
        )
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(semantic_upserts)")
    }
    if "revision" not in columns:
        conn.execute(
            "ALTER TABLE semantic_upserts "
            "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
        )
    full_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(full_upserts)")}
    if "revision" not in full_columns:
        conn.execute(
            "ALTER TABLE full_upserts ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
        )
    graph_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(graph_upserts)")
    }
    if "graph_generation" not in graph_columns:
        # Deliberately nullable with no default: an existing row was queued
        # before anything recorded which generation it owed, and inventing one
        # would let a pre-upgrade receipt bless a lineage gap it knows nothing
        # about. NULL reads back as "unknown", which never counts as coverage.
        conn.execute("ALTER TABLE graph_upserts ADD COLUMN graph_generation INTEGER")
    _ensure_derived_batch_schema(conn)
    _ensure_vocabulary_provenance_schema(conn)
    if publish:
        try:
            reserved_paths._publish_sqlite_owner_family(
                vault_root,
                path,
                "deferred-index-store",
                conn,
            )
        except BaseException:
            conn.close()
            raise
    return conn


def semantic_isolation_cursors(vault_root: Path) -> dict[str, dict[str, str]]:
    """Read durable bounded-census cursors without creating a sidecar."""
    if not store_path(vault_root).exists():
        return {}
    try:
        conn = _connect_readonly(vault_root)
        try:
            row = conn.execute(
                "SELECT value FROM maintenance_state WHERE key = ?",
                (_SEMANTIC_ISOLATION_CURSOR_KEY,),
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return {}
    if row is None:
        return {}
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}
    cursors = payload.get("cursors")
    if not isinstance(cursors, dict):
        return {}
    return {
        component: {"cursor": value["cursor"], "signature": value["signature"]}
        for component, value in cursors.items()
        if isinstance(component, str)
        and isinstance(value, dict)
        and isinstance(value.get("cursor"), str)
        and isinstance(value.get("signature"), str)
    }


def semantic_isolation_signature(
    vault_root: Path, *, connection_path: Path | None = None
) -> str | None:
    """In-band semantic receipt generation, isolated from cursor writes."""
    target = connection_path if connection_path is not None else store_path(vault_root)
    if not target.exists():
        return "semantic:0"
    try:
        conn = (
            _connect_readonly(vault_root)
            if connection_path is None
            else _sqlite_connect(
                f"{target.as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        )
        try:
            row = conn.execute(
                "SELECT value FROM maintenance_state WHERE key = ?",
                (_SEMANTIC_UPSERTS_GENERATION_KEY,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return f"semantic:{row[0] if row is not None else '0'}"


def set_semantic_isolation_cursors(
    vault_root: Path, cursors: dict[str, dict[str, str]]
) -> bool:
    """Atomically replace durable audit cursors after a successful repair page."""
    payload = {
        "version": 1,
        "cursors": {
            component: {"cursor": value["cursor"], "signature": value["signature"]}
            for component, value in cursors.items()
            if isinstance(component, str)
            and isinstance(value, dict)
            and isinstance(value.get("cursor"), str)
            and isinstance(value.get("signature"), str)
        },
    }
    if not payload["cursors"] and not store_path(vault_root).exists():
        return True
    try:
        conn = _connect(vault_root, create=True)
        try:
            with conn:
                if payload["cursors"]:
                    conn.execute(
                        "INSERT INTO maintenance_state(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (_SEMANTIC_ISOLATION_CURSOR_KEY, json.dumps(payload, sort_keys=True)),
                    )
                else:
                    conn.execute(
                        "DELETE FROM maintenance_state WHERE key = ?",
                        (_SEMANTIC_ISOLATION_CURSOR_KEY,),
                    )
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return False
    return True


def claim_mixed_drain_queue(vault_root: Path) -> str:
    """Atomically alternate a one-slot drain between full and semantic work."""
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            # A deferred transaction would let concurrent claimants read the
            # same turn before either writes it. Reserve the writer slot first
            # so each claimant observes the prior claimant's committed flip.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM maintenance_state WHERE key = ?",
                (_MIXED_DRAIN_TURN_KEY,),
            ).fetchone()
            current = (
                str(row[0])
                if row is not None and str(row[0]) in {"full", "semantic"}
                else "full"
            )
            next_queue = "semantic" if current == "full" else "full"
            conn.execute(
                "INSERT INTO maintenance_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_MIXED_DRAIN_TURN_KEY, next_queue),
            )
        return current
    finally:
        conn.close()


@dataclass(frozen=True, slots=True)
class DeferredReceipt:
    rel_path: str
    revision: int
    #: The graph-sync generation this path was queued for, or None when it is
    #: not known -- a row written before the column existed, or a caller with no
    #: checkpoint to name. Unknown is never usable as coverage: a receipt that
    #: cannot say which generation it owes cannot prove that generation queued.
    graph_generation: int | None = None


class EmbeddingFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNVERIFIABLE = "unverifiable"


def _safe_markdown_rel_path(value: object) -> str | None:
    """Normalize one persisted Markdown identity without permitting traversal."""
    if not isinstance(value, str):
        return None
    if "\\" in value:
        return None
    normalized = value
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "\0" in normalized
        or (len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or not normalized.lower().endswith(".md")
        or not path.parts
        or path.parts[0] != kb_dirname()
    ):
        return None
    return path.as_posix()


def _semantic_admission(vault_root: Path, rel: str) -> bool | None:
    """True/false for a present leaf; None means absent and remains retryable."""
    path = Path(vault_root).joinpath(*rel.split("/"))
    if not os.path.lexists(path):
        return None
    from . import recall_policy

    return recall_policy.is_recall_candidate(vault_root, path)


def add(vault_root: Path, rel_paths: list[str]) -> int:
    _receipts, added = _add_receipts(vault_root, rel_paths)
    return added


def add_receipts(vault_root: Path, rel_paths: list[str]) -> list[DeferredReceipt]:
    receipts, _added = _add_receipts(vault_root, rel_paths)
    return receipts


def _add_receipts(
    vault_root: Path, rel_paths: list[str]
) -> tuple[list[DeferredReceipt], int]:
    rels: list[str] = []
    rejected: list[str] = []
    for raw in rel_paths:
        rel = _safe_markdown_rel_path(raw)
        if rel is None:
            continue
        admission = _semantic_admission(vault_root, rel)
        if admission is False:
            rejected.append(rel)
        else:
            rels.append(rel)
    rels = sorted(set(rels))
    if rejected:
        clear_semantic_receipts(vault_root, rejected)
    if not rels:
        return [], 0
    now = time.time()
    receipts: list[DeferredReceipt] = []
    added = 0
    conn = _connect(vault_root, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for rel in rels:
                row = conn.execute(
                    "SELECT revision FROM semantic_upserts WHERE rel_path = ?",
                    (rel,),
                ).fetchone()
                if row is None:
                    revision = 1
                    added += 1
                    conn.execute(
                        "INSERT INTO semantic_upserts"
                        "(rel_path, created_at, updated_at, revision) VALUES (?, ?, ?, ?)",
                        (rel, now, now, revision),
                    )
                else:
                    revision = int(row[0]) + 1
                    conn.execute(
                        "UPDATE semantic_upserts SET updated_at = ?, revision = ? "
                        "WHERE rel_path = ?",
                        (now, revision, rel),
                    )
                receipts.append(DeferredReceipt(rel, revision))
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return receipts, added
    finally:
        conn.close()


def add_full(vault_root: Path, rel_paths: list[str]) -> int:
    """Durably queue a complete lexical/resolver/graph/semantic refresh."""
    _receipts, added = _add_full_receipts(vault_root, rel_paths)
    return added


def add_full_receipts(vault_root: Path, rel_paths: list[str]) -> list[DeferredReceipt]:
    """Queue full refreshes and return their exact transaction-local revisions."""
    receipts, _added = _add_full_receipts(vault_root, rel_paths)
    return receipts


def _add_full_receipts(
    vault_root: Path, rel_paths: list[str]
) -> tuple[list[DeferredReceipt], int]:
    return _add_plain_receipts(vault_root, rel_paths, table="full_upserts")


def _add_plain_receipts(
    vault_root: Path,
    rel_paths: list[str],
    *,
    table: str,
    generation: int | None = None,
) -> tuple[list[DeferredReceipt], int]:
    """Queue paths in an admission-free queue and return exact revisions.

    "Plain" is the distinction from the semantic queue, which additionally
    consults recall admission per path. The full and graph queues do not: what
    they may index is decided upstream, at the seam that produced the paths.
    """
    rels = sorted(
        {
            rel
            for raw in rel_paths
            if (rel := _safe_markdown_rel_path(raw)) is not None
        }
    )
    if not rels:
        return [], 0
    now = time.time()
    receipts: list[DeferredReceipt] = []
    added = 0
    conn = _connect(vault_root, create=True)
    try:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        tracks_generation = "graph_generation" in columns
        conn.execute("BEGIN IMMEDIATE")
        try:
            for rel in rels:
                stored = "graph_generation" if tracks_generation else "NULL"
                row = conn.execute(
                    f"SELECT revision, {stored} FROM {table} WHERE rel_path = ?", (rel,)
                ).fetchone()
                if row is None:
                    revision = 1
                    added += 1
                    recorded = generation if tracks_generation else None
                    if tracks_generation:
                        conn.execute(
                            f"INSERT INTO {table}"
                            "(rel_path, created_at, updated_at, revision, graph_generation) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (rel, now, now, revision, generation),
                        )
                    else:
                        conn.execute(
                            f"INSERT INTO {table}"
                            "(rel_path, created_at, updated_at, revision) VALUES (?, ?, ?, ?)",
                            (rel, now, now, revision),
                        )
                else:
                    revision = int(row[0]) + 1
                    previous = None if row[1] is None else int(row[1])
                    # The newest KNOWN generation wins, and an unknown enqueue
                    # never erases one: the row still owes the repair it was
                    # queued for, and forgetting that would refuse coverage the
                    # queue genuinely holds. A stale generation can never rise
                    # this way -- only a re-queue at a later one moves it.
                    recorded = (
                        previous
                        if generation is None
                        else max(generation, previous if previous is not None else generation)
                    )
                    if tracks_generation:
                        conn.execute(
                            f"UPDATE {table} SET updated_at = ?, revision = ?, "
                            "graph_generation = ? WHERE rel_path = ?",
                            (now, revision, recorded, rel),
                        )
                    else:
                        conn.execute(
                            f"UPDATE {table} SET updated_at = ?, revision = ? "
                            "WHERE rel_path = ?",
                            (now, revision, rel),
                        )
                receipts.append(DeferredReceipt(rel, revision, recorded))
            if tracks_generation and generation is not None and rels:
                _record_graph_debt_generation_locked(conn, int(generation), now)
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return receipts, added
    finally:
        conn.close()


def _note_graph_debt() -> None:
    """Tell the drain daemon this process just queued graph repair.

    Imported here rather than at module scope: `graph_drain` reaches back into
    `index_sync`, which imports this module. Best-effort by design -- queueing
    the work is the durable part, and a missed signal costs at worst the
    daemon's idle poll rather than the repair itself.
    """
    try:
        from . import graph_drain

        graph_drain.note_graph_debt()
    except Exception:  # noqa: BLE001 - never let signalling break an enqueue
        # Silent on purpose: this module keeps no logger, and a missed signal is
        # already covered by the daemon's idle poll. The enqueue itself, which
        # is the part that must not be lost, has already committed.
        pass


def add_graph(
    vault_root: Path, rel_paths: list[str], *, generation: int | None = None
) -> int:
    """Durably queue pages whose epistemic-graph projection needs re-deriving."""
    _receipts, added = _add_plain_receipts(
        vault_root, rel_paths, table="graph_upserts", generation=generation
    )
    if added:
        _note_graph_debt()
    return added


def add_graph_receipts(
    vault_root: Path, rel_paths: list[str], *, generation: int | None = None
) -> list[DeferredReceipt]:
    """Queue graph work and return its exact transaction-local revisions.

    `generation` is the graph-sync generation this repair is owed for. It is
    what lets a later predecessor probe prove a lineage gap is covered by
    durable work rather than guess it from a path name, so a caller that knows
    it must pass it; one that does not leaves the row unknown, and an unknown
    row never counts as coverage.

    THE CONVENTION A RECORDED GENERATION ENTERS INTO: recording generation G
    claims G's COMPLETE path set. `_lineage_gap_is_receipt_covered` reads one
    present generation as "that whole step is queued" -- it cannot see how many
    paths G was owed, so half of G recorded under G would bless a gap this
    queue only half owns, and the write that trusted it would publish over real
    divergence with no rebuild to catch it. A caller that cannot make that claim
    passes `generation=None` and is honest about knowing nothing; a caller that
    records a deliberately partial set must be sure no probe can ask about the
    generation it records (the adopted residue is the one such site, and it
    records the *acknowledged* generation for exactly that reason). The sites
    are enumerated and must each declare their claim --
    `tests/test_graph_deferred_queue.py::test_every_generation_recording_enqueue_declares_its_path_set`.
    """
    receipts, _added = _add_plain_receipts(
        vault_root, rel_paths, table="graph_upserts", generation=generation
    )
    if receipts:
        _note_graph_debt()
    return receipts


def enqueue_graph_checkpoint(vault_root: Path, checkpoint: Any) -> int:
    """Record one canonical batch's graph debt from the checkpoint it already writes.

    The checkpoint has always carried exactly which pages changed and which were
    created, per generation, crash-safely. Until now the graph threw that away
    and re-walked the vault. This is the seam that keeps it.

    A full-scope batch -- one over the checkpoint's path limit, which therefore
    carries no path list at all -- records a rebuild marker instead. A queue of
    "every page" is not a queue.
    """
    if getattr(checkpoint, "scope", "paths") == "full":
        mark_graph_full_rebuild(vault_root, generation=int(checkpoint.generation))
        return 0
    rels = [rel for rel, _content_hash in checkpoint.paths]
    rels.extend(checkpoint.created_paths)
    return add_graph(vault_root, rels, generation=int(checkpoint.generation))


def mark_graph_full_rebuild(vault_root: Path, *, generation: int) -> None:
    """Record that a whole-vault rebuild is owed, at or after this generation."""
    conn = _connect(vault_root, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = dict(
                conn.execute(
                    "SELECT key, value FROM maintenance_state WHERE key IN (?, ?)",
                    (_GRAPH_FULL_REBUILD_KEY, _GRAPH_FULL_REBUILD_SEQUENCE_KEY),
                ).fetchall()
            )
            current = (
                int(rows[_GRAPH_FULL_REBUILD_KEY])
                if _GRAPH_FULL_REBUILD_KEY in rows
                else None
            )
            sequence = int(rows.get(_GRAPH_FULL_REBUILD_SEQUENCE_KEY, 0))
            requested = int(generation)
            # An absent marker means the previous debt was retired. New debt
            # must advance past that retired generation so a delayed old drain
            # cannot clear it. While debt remains, an equal checkpoint is an
            # idempotent repeat and a newer checkpoint advances naturally.
            recorded = (
                max(sequence + 1, requested)
                if current is None
                else max(current, requested)
            )
            conn.execute(
                "INSERT INTO maintenance_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_GRAPH_FULL_REBUILD_KEY, str(recorded)),
            )
            conn.execute(
                "INSERT INTO maintenance_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_GRAPH_FULL_REBUILD_SEQUENCE_KEY, str(max(sequence, recorded))),
            )
            _count_graph_full_rebuild_mark_locked(conn)
        except Exception:
            conn.rollback()
            raise
        conn.commit()
    finally:
        conn.close()
    # A whole-vault marker is graph debt too, and the one the drain most needs
    # to hear about: it is raised exactly when the changed scope is unknown.
    _note_graph_debt()


def advance_graph_full_rebuild(vault_root: Path, *, after_generation: int = 0) -> int:
    """Atomically append whole-vault debt after every marker already observed.

    A direct refresh has no canonical graph checkpoint generation to use. Read-
    then-write arithmetic would let two writers choose the same next value, so
    a drain that started between them could clear the later writer's debt. The
    immediate transaction serializes the increment with the drain's marker
    snapshot and returns the exact generation it persisted.
    """
    conn = _connect(vault_root, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = dict(
                conn.execute(
                    "SELECT key, value FROM maintenance_state WHERE key IN (?, ?)",
                    (_GRAPH_FULL_REBUILD_KEY, _GRAPH_FULL_REBUILD_SEQUENCE_KEY),
                ).fetchall()
            )
            current = int(rows.get(_GRAPH_FULL_REBUILD_KEY, 0))
            sequence = int(rows.get(_GRAPH_FULL_REBUILD_SEQUENCE_KEY, 0))
            generation = max(current, sequence, int(after_generation)) + 1
            conn.execute(
                "INSERT INTO maintenance_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_GRAPH_FULL_REBUILD_KEY, str(generation)),
            )
            conn.execute(
                "INSERT INTO maintenance_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_GRAPH_FULL_REBUILD_SEQUENCE_KEY, str(generation)),
            )
            _count_graph_full_rebuild_mark_locked(conn)
        except Exception:
            conn.rollback()
            raise
        conn.commit()
    finally:
        conn.close()
    _note_graph_debt()
    return generation


def _count_graph_full_rebuild_mark_locked(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO maintenance_state(key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
        (_GRAPH_FULL_REBUILD_MARKS_KEY,),
    )


def graph_full_rebuild_observation(vault_root: Path) -> tuple[int, int] | None:
    """The standing marker and its raise count, read together, or None.

    What a whole-vault pass records before it samples its epoch, so that its
    publication can retire exactly the debt that existed then and nothing
    raised after -- see `retire_observed_graph_full_rebuild`.
    """
    if not store_path(vault_root).exists():
        return None
    try:
        conn = _connect_readonly(vault_root)
        try:
            rows = dict(
                conn.execute(
                    "SELECT key, value FROM maintenance_state WHERE key IN (?, ?)",
                    (_GRAPH_FULL_REBUILD_KEY, _GRAPH_FULL_REBUILD_MARKS_KEY),
                ).fetchall()
            )
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None
    if _GRAPH_FULL_REBUILD_KEY not in rows:
        return None
    try:
        return int(rows[_GRAPH_FULL_REBUILD_KEY]), int(rows.get(_GRAPH_FULL_REBUILD_MARKS_KEY, 0))
    except (TypeError, ValueError):
        return None


def retire_observed_graph_full_rebuild(
    vault_root: Path, observation: tuple[int, int]
) -> bool:
    """Retire the marker only if no debt was raised since `observation`.

    Stricter than `clear_graph_full_rebuild`'s value compare-and-swap: a repeat
    raise keeps the marker's value but moves its raise count, so debt a batch
    raised while a pass ran survives even when it did not change the value.
    That is what lets a publication retire its debt after leaving the canonical
    boundary rather than under it.
    """
    if not store_path(vault_root).exists():
        return False
    marker, marks = observation
    conn = _connect(vault_root, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = dict(
                conn.execute(
                    "SELECT key, value FROM maintenance_state WHERE key IN (?, ?, ?)",
                    (
                        _GRAPH_FULL_REBUILD_KEY,
                        _GRAPH_FULL_REBUILD_SEQUENCE_KEY,
                        _GRAPH_FULL_REBUILD_MARKS_KEY,
                    ),
                ).fetchall()
            )
            if (
                _GRAPH_FULL_REBUILD_KEY not in rows
                or int(rows[_GRAPH_FULL_REBUILD_KEY]) != marker
                or int(rows.get(_GRAPH_FULL_REBUILD_MARKS_KEY, 0)) != marks
            ):
                conn.commit()
                return False
            sequence = int(rows.get(_GRAPH_FULL_REBUILD_SEQUENCE_KEY, 0))
            # The same persistence `clear_graph_full_rebuild` makes: later debt
            # must advance past the generation retired here.
            conn.execute(
                "INSERT INTO maintenance_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_GRAPH_FULL_REBUILD_SEQUENCE_KEY, str(max(sequence, marker))),
            )
            changed = conn.execute(
                "DELETE FROM maintenance_state WHERE key = ?",
                (_GRAPH_FULL_REBUILD_KEY,),
            ).rowcount
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


def graph_full_rebuild_pending(vault_root: Path) -> int | None:
    """The generation a whole-vault rebuild is owed for, or None."""
    if not store_path(vault_root).exists():
        return None
    try:
        conn = _connect_readonly(vault_root)
        try:
            row = conn.execute(
                "SELECT value FROM maintenance_state WHERE key = ?",
                (_GRAPH_FULL_REBUILD_KEY,),
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def clear_graph_full_rebuild(vault_root: Path, *, generation: int | None = None) -> bool:
    """Retire the rebuild marker, but only if nothing newer arrived meanwhile.

    Compare-and-swap for the same reason receipts are: a rebuild that started
    against generation N must not erase a marker a later batch raised to N+1
    while it ran.
    """
    if not store_path(vault_root).exists():
        return False
    conn = _connect(vault_root, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = dict(
                conn.execute(
                    "SELECT key, value FROM maintenance_state WHERE key IN (?, ?)",
                    (_GRAPH_FULL_REBUILD_KEY, _GRAPH_FULL_REBUILD_SEQUENCE_KEY),
                ).fetchall()
            )
            if _GRAPH_FULL_REBUILD_KEY not in rows:
                conn.commit()
                return False
            marker = int(rows[_GRAPH_FULL_REBUILD_KEY])
            sequence = int(rows.get(_GRAPH_FULL_REBUILD_SEQUENCE_KEY, 0))
            # Upgrade migration and steady-state safety share the same seam:
            # persist the marker's generation before deleting it. A legacy
            # sidecar has no sequence row, and a delayed second drain may still
            # hold this marker value after the first one clears it.
            conn.execute(
                "INSERT INTO maintenance_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_GRAPH_FULL_REBUILD_SEQUENCE_KEY, str(max(sequence, marker))),
            )
            if generation is None:
                changed = conn.execute(
                    "DELETE FROM maintenance_state WHERE key = ?",
                    (_GRAPH_FULL_REBUILD_KEY,),
                ).rowcount
            else:
                changed = conn.execute(
                    "DELETE FROM maintenance_state WHERE key = ? "
                    "AND CAST(value AS INTEGER) <= ?",
                    (_GRAPH_FULL_REBUILD_KEY, int(generation)),
                ).rowcount
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


def _add(vault_root: Path, rel_paths: list[str], *, table: str) -> int:
    rels = sorted(
        {
            rel
            for raw in rel_paths
            if (rel := _safe_markdown_rel_path(raw)) is not None
        }
    )
    if not rels:
        return 0
    now = time.time()
    conn = _connect(vault_root, create=True)
    try:
        placeholders = ",".join("?" for _ in rels)
        existing = int(
            conn.execute(
                f"SELECT count(*) FROM {table} WHERE rel_path IN ({placeholders})",
                rels,
            ).fetchone()[0]
        )
        with conn:
            conn.executemany(
                f"""
                INSERT INTO {table}(rel_path, created_at, updated_at, revision)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(rel_path) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    revision = {table}.revision + 1
                """,
                [(rel, now, now) for rel in rels],
            )
        return len(rels) - existing
    finally:
        conn.close()


def list_paths(vault_root: Path, *, limit: int | None = None) -> list[str]:
    return [receipt.rel_path for receipt in snapshot(vault_root, limit=limit)]


def list_full_paths(vault_root: Path, *, limit: int | None = None) -> list[str]:
    return [receipt.rel_path for receipt in snapshot_full(vault_root, limit=limit)]


def snapshot(
    vault_root: Path, *, limit: int | None = None
) -> list[DeferredReceipt]:
    path = store_path(vault_root)
    if not path.exists():
        return []
    conn = _connect(vault_root, create=False)
    try:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(semantic_upserts)")
        }
        revision = "revision" if "revision" in columns else "1 AS revision"
        sql = (
            f"SELECT rel_path, {revision} FROM semantic_upserts "
            "ORDER BY updated_at, rel_path"
        )
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(0, limit),)
        receipts = [
            DeferredReceipt(str(row[0]), int(row[1]))
            for row in conn.execute(sql, params).fetchall()
        ]
    finally:
        conn.close()
    valid: list[DeferredReceipt] = []
    rejected: list[DeferredReceipt] = []
    for receipt in receipts:
        rel = _safe_markdown_rel_path(receipt.rel_path)
        if rel is None:
            rejected.append(receipt)
            continue
        admission = _semantic_admission(vault_root, rel)
        if admission is False:
            rejected.append(receipt)
        else:
            valid.append(DeferredReceipt(rel, receipt.revision))
    if rejected:
        clear_receipts(vault_root, rejected)
    return valid


def snapshot_full(
    vault_root: Path,
    *,
    limit: int | None = None,
    paths: set[str] | None = None,
) -> list[DeferredReceipt]:
    return _snapshot_plain(vault_root, table="full_upserts", limit=limit, paths=paths)


def snapshot_graph(
    vault_root: Path,
    *,
    limit: int | None = None,
    paths: set[str] | None = None,
) -> list[DeferredReceipt]:
    """Queued graph work, oldest first, with corrupt legacy rows purged."""
    return _snapshot_plain(vault_root, table="graph_upserts", limit=limit, paths=paths)


#: Generations kept in the durable debt record. A gap wider than this is far
#: outside anything a path-keyed queue could cover, and pruning the oldest can
#: only make the probe refuse coverage it might have granted -- an extra
#: rebuild, never a blessed divergence.
MAX_GRAPH_DEBT_GENERATIONS = 512


def _record_graph_debt_generation_locked(conn: Any, generation: int, now: float) -> None:
    """Record that generation `generation`'s graph debt was queued. Under the
    caller's open transaction, so it lands with the receipts or not at all.

    WHY THIS IS NOT THE RECEIPTS THEMSELVES. `graph_upserts` is keyed by
    rel_path with ONE generation column, so a later enqueue of the same path
    overwrites the generation an earlier one recorded -- `max(new, old)`, by
    design, because the row does still owe the newer repair. That makes the
    receipts a fine queue and a terrible ledger: measured under load, six
    writes over six paths collapsed `{2,3,4,5}` into `{6}` the moment the
    watcher re-queued those paths at its own checkpoint, and the next write's
    predecessor probe then read a proven divergence where every generation's
    repair was in fact queued. It cost a whole-vault rebuild about two writes
    in six, and only under contention, which is exactly the batch-ingest
    condition task 1.13 exists for.

    So the proof gets its own append-only row per generation. Nothing
    overwrites it, the drain does not consume it, and it is written in the same
    durable step as the receipts and before the write acknowledges -- which is
    what makes "a committed checkpoint implies its debt was recorded" a
    property of the storage rather than of the timing.
    """
    conn.execute(
        "INSERT OR IGNORE INTO graph_debt_generations(generation, recorded_at) "
        "VALUES (?, ?)",
        (generation, now),
    )
    conn.execute(
        "DELETE FROM graph_debt_generations WHERE generation <= ("
        "  SELECT generation FROM graph_debt_generations "
        "  ORDER BY generation DESC LIMIT 1 OFFSET ?"
        ")",
        (MAX_GRAPH_DEBT_GENERATIONS,),
    )


def graph_debt_generations_recorded(
    vault_root: Path, generations: Iterable[int]
) -> tuple[frozenset[int], bool]:
    """Which of `generations` have a durable debt record, and whether one exists.

    The second value distinguishes "this store has the record and these
    generations are absent from it" from "this store predates the record", so a
    caller can fall back rather than read an empty table as a proven divergence.
    """
    wanted = sorted({int(generation) for generation in generations})
    if not wanted:
        return frozenset(), True
    path = store_path(vault_root)
    if not path.exists():
        return frozenset(), False
    conn = _connect(vault_root, create=False)
    try:
        return _graph_debt_generations_locked(conn, wanted)
    finally:
        conn.close()


def _graph_debt_generations_locked(
    conn: Any, wanted: list[int]
) -> tuple[frozenset[int], bool]:
    """The debt lookup on a connection the caller already has open."""
    available = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("graph_debt_generations",),
        ).fetchone()
    )
    if not available:
        return frozenset(), False
    rows = conn.execute(
        "SELECT generation FROM graph_debt_generations WHERE generation IN "
        f"({','.join('?' for _ in wanted)})",
        tuple(wanted),
    ).fetchall()
    return frozenset(int(row[0]) for row in rows), True


def graph_gap_coverage(
    vault_root: Path, generations: Iterable[int]
) -> tuple[frozenset[int], bool, frozenset[int], bool]:
    """Everything the predecessor probe needs about a gap, on ONE connection.

    The probe runs on the write path, and every `_connect` here takes a
    reserved-identity boundary entry -- the per-item re-entry that already
    dominates the mutation-lock log. Asking two questions of the same store
    through two connections doubled that for no reading anyone needs
    separately, so they are asked together.

    Returns the receipt generations in range, whether any queued receipt cannot
    say what it owes, the generations with a durable debt record, and whether
    that record exists in this store at all.
    """
    wanted = sorted({int(generation) for generation in generations})
    if not wanted:
        return frozenset(), False, frozenset(), True
    path = store_path(vault_root)
    if not path.exists():
        return frozenset(), False, frozenset(), False
    conn = _connect(vault_root, create=False)
    try:
        receipts = _snapshot_plain(
            vault_root, table="graph_upserts", generations=wanted, connection=conn
        )
        known = frozenset(
            receipt.graph_generation
            for receipt in receipts
            if receipt.graph_generation is not None
        )
        unknown = any(receipt.graph_generation is None for receipt in receipts)
        recorded, has_record = _graph_debt_generations_locked(conn, wanted)
    finally:
        conn.close()
    return known, unknown, recorded, has_record


def clear_graph_debt_generations(vault_root: Path) -> int:
    """Drop the whole debt record. Pairs with clearing the whole queue."""
    path = store_path(vault_root)
    if not path.exists():
        return 0
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            return int(conn.execute("DELETE FROM graph_debt_generations").rowcount)
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def graph_receipt_generations(
    vault_root: Path, *, generations: Iterable[int] | None = None
) -> tuple[frozenset[int], bool]:
    """Which generations the queued graph receipts name, and whether any is unknown.

    The durable artifact a predecessor probe reads to decide whether a lineage
    gap is already owned by the queue. Two values because they are acted on
    differently: a generation present in the set has its paths queued, and a
    single unknown row means some queued repair cannot say what it owes -- which
    is not evidence about any generation, so the probe refuses on it rather than
    reasoning around it.

    `generations` scopes the read to the gap the caller is actually asking
    about, plus the unknown rows it must fail closed on. A probe runs on the
    write path against a queue that holds one row per deferred page, so the
    unscoped read this used to take grew with the backlog while the question
    never did -- a six-generation gap needs six generations' rows, not the
    table. The unscoped form is kept for callers reporting on the whole queue.

    THE CONVENTION THIS RESTS ON, stated because a probe cannot check it: an
    enqueue that records generation G records G's *complete* path set. A site
    that recorded a partial set under G would make this answer "covered" for a
    gap it only half owns. `tests/test_graph_deferred_queue.py` enumerates the
    recording sites so a new one has to declare itself rather than inherit the
    claim silently.
    """
    receipts = _snapshot_graph_generation_rows(vault_root, generations)
    known = {
        receipt.graph_generation
        for receipt in receipts
        if receipt.graph_generation is not None
    }
    unknown = any(receipt.graph_generation is None for receipt in receipts)
    return frozenset(known), unknown


def _snapshot_graph_generation_rows(
    vault_root: Path, generations: Iterable[int] | None
) -> list[DeferredReceipt]:
    """Graph receipts for `generations`, plus every row that names none.

    Same corrupt-row purge and same safety filter as `snapshot_graph`; it is
    `_snapshot_plain` with a generation predicate rather than a path one.
    """
    if generations is None:
        return _snapshot_plain(vault_root, table="graph_upserts")
    wanted = sorted({int(generation) for generation in generations})
    return _snapshot_plain(
        vault_root, table="graph_upserts", generations=wanted
    )


def list_graph_paths(vault_root: Path, *, limit: int | None = None) -> list[str]:
    return [receipt.rel_path for receipt in snapshot_graph(vault_root, limit=limit)]


def clear_graph_receipts(vault_root: Path, receipts: list[DeferredReceipt]) -> int:
    """CAS-clear graph receipts; a path derived at last forgets its failures."""
    cleared = _clear_plain_receipts(vault_root, receipts, table="graph_upserts")
    if receipts:
        forget_graph_failures(vault_root, {receipt.rel_path for receipt in receipts})
    return cleared


def note_graph_failure(vault_root: Path, rel_path: str) -> tuple[int, float]:
    """Count one failed isolated attempt to derive `rel_path`.

    Returns the attempts so far and when the first of them failed. Counted per
    path, not per receipt revision: a page that cannot be read fails the same
    way whatever revision queued it.
    """
    now = time.time()
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            conn.execute(
                "INSERT INTO graph_failures(rel_path, attempts, first_failed_at, last_failed_at) "
                "VALUES (?, 1, ?, ?) ON CONFLICT(rel_path) DO UPDATE SET "
                "attempts = attempts + 1, last_failed_at = excluded.last_failed_at",
                (rel_path, now, now),
            )
            row = conn.execute(
                "SELECT attempts, first_failed_at FROM graph_failures WHERE rel_path = ?",
                (rel_path,),
            ).fetchone()
        if row is None:
            return 1, now
        return int(row[0]), float(row[1])
    finally:
        conn.close()


def quarantine_graph_receipt(
    vault_root: Path, receipt: DeferredReceipt, *, signature: str | None
) -> bool:
    """Set a receipt no drain can derive aside, by exact revision.

    Its row leaves the queue, so the queue can empty around it, and the path
    is recorded as quarantined for the lag and the doctor with the stat
    `signature` it failed at: a change to it earns a retry
    (`release_graph_quarantine`). A newer revision -- a write that landed
    since -- stays queued as ordinary work.
    """
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            removed = conn.execute(
                "DELETE FROM graph_upserts WHERE rel_path = ? AND revision = ?",
                (receipt.rel_path, receipt.revision),
            ).rowcount
            if removed:
                now = time.time()
                conn.execute(
                    "INSERT INTO graph_failures(rel_path, attempts, first_failed_at, "
                    "last_failed_at, quarantined, signature) VALUES (?, 1, ?, ?, 1, ?) "
                    "ON CONFLICT(rel_path) DO UPDATE SET quarantined = 1, "
                    "last_failed_at = excluded.last_failed_at, signature = excluded.signature",
                    (receipt.rel_path, now, now, signature),
                )
        return bool(removed)
    finally:
        conn.close()


def quarantined_graph_paths(
    vault_root: Path,
) -> list[tuple[str, str | None, float, int]]:
    """Each quarantined path, the stat signature it failed at, when it last failed,
    and how many attempts have failed.

    An absent or unreadable store reads as none.
    """
    rows = _graph_failure_rows(vault_root, "WHERE quarantined = 1")
    return [
        (str(rel), None if sig is None else str(sig), float(at), int(attempts))
        for rel, sig, at, attempts in rows
    ]


def graph_failure_paths(vault_root: Path) -> list[str]:
    """Every path with a recorded failure, quarantined or not. Absent store: none."""
    return [str(row[0]) for row in _graph_failure_rows(vault_root, "")]


def _graph_failure_rows(vault_root: Path, where: str) -> list[tuple[Any, ...]]:
    if not store_path(vault_root).exists():
        return []
    try:
        conn = _connect_readonly(vault_root)
        try:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'graph_failures'"
            ).fetchone():
                return []
            return conn.execute(
                "SELECT rel_path, signature, last_failed_at, attempts FROM graph_failures "
                f"{where} ORDER BY rel_path"
            ).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return []


def release_graph_quarantine(vault_root: Path, rel_paths: list[str]) -> int:
    """Queue quarantined paths for one more attempt; return how many were queued.

    Queued first and released second, so a crash between the two leaves a path
    both queued and quarantined -- retried, and re-quarantined or forgotten by
    that attempt -- rather than neither. The failure count and its first
    failure stay: a retry that fails again is set aside on that one attempt.
    """
    if not rel_paths:
        return 0
    add_graph(vault_root, list(rel_paths))
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            conn.executemany(
                "UPDATE graph_failures SET quarantined = 0 WHERE rel_path = ?",
                [(rel,) for rel in sorted(set(rel_paths))],
            )
    finally:
        conn.close()
    return len(set(rel_paths))


def graph_quarantined_count(vault_root: Path) -> int:
    """How many paths are quarantined as underivable. An unreadable store reads 0."""
    if not store_path(vault_root).exists():
        return 0
    try:
        conn = _connect_readonly(vault_root)
        try:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'graph_failures'"
            ).fetchone():
                return 0
            row = conn.execute(
                "SELECT count(*) FROM graph_failures WHERE quarantined = 1"
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return 0
    return int(row[0] or 0)


def forget_graph_failures(vault_root: Path, rel_paths: set[str]) -> None:
    """Drop the failure record of paths that derived; best effort, one read first."""
    if not rel_paths or not store_path(vault_root).exists():
        return
    try:
        conn = _connect(vault_root, create=True)
        try:
            with conn:
                if not conn.execute("SELECT 1 FROM graph_failures LIMIT 1").fetchone():
                    return
                conn.executemany(
                    "DELETE FROM graph_failures WHERE rel_path = ?",
                    [(rel,) for rel in sorted(rel_paths)],
                )
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        # The record only feeds the quarantine count; a stale one costs a
        # quarantine a little sooner, never a lost repair.
        return


def clear_graph(vault_root: Path, rel_paths: list[str] | None = None) -> int:
    """Drop queued graph work; a whole-queue clear drops the debt record too.

    Clearing named paths leaves the debt record alone -- those generations were
    still recorded, and their repair either landed or moved to another row.
    Clearing the WHOLE queue is the caller saying this queue's history no longer
    describes anything, and a proof that outlived the queue it describes would
    bless a gap nothing is converging.
    """
    cleared = _clear(vault_root, table="graph_upserts", rel_paths=rel_paths)
    if rel_paths is None:
        clear_graph_debt_generations(vault_root)
    return cleared


def rotate_graph_receipts(vault_root: Path, receipts: list[DeferredReceipt]) -> int:
    return rotate_receipts(vault_root, receipts, queue="graph")


def graph_queue_age(vault_root: Path) -> tuple[int, float | None]:
    """How many graph receipts are queued, and how long ago the oldest was.

    One indexed read on one connection, for lag reporting. The age runs from
    a row's first enqueue: a requeue keeps it, so repair that keeps losing its
    compare-and-swap still reads as old. An unreadable store reads as empty.
    """
    if not store_path(vault_root).exists():
        return 0, None
    try:
        conn = _connect_readonly(vault_root)
        try:
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(graph_upserts)")
            }
            if not columns:
                return 0, None
            count, oldest = conn.execute(
                "SELECT count(*), min(created_at) FROM graph_upserts"
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return 0, None
    if not count or oldest is None:
        return int(count or 0), None
    return int(count), max(0.0, time.time() - float(oldest))


def graph_status(vault_root: Path | None) -> dict[str, Any]:
    result = _status(vault_root, table="graph_upserts")
    return {
        **result,
        "full_rebuild_pending": (
            graph_full_rebuild_pending(vault_root) if vault_root is not None else None
        ),
    }


def _snapshot_plain(
    vault_root: Path,
    *,
    table: str,
    limit: int | None = None,
    paths: set[str] | None = None,
    generations: list[int] | None = None,
    connection: Any | None = None,
) -> list[DeferredReceipt]:
    path = store_path(vault_root)
    if not path.exists():
        return []
    # A caller with a connection already open lends it rather than paying a
    # second reserved-identity boundary entry for the same store.
    borrowed = connection is not None
    conn = connection if borrowed else _connect(vault_root, create=False)
    try:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if not columns:
            # No such table: a store that predates this queue. Readers run
            # before writers on an upgraded vault -- a drain asks what is
            # queued before it queues anything -- and they open read-only,
            # where the table cannot be created. Nothing is queued in a queue
            # that does not exist yet; the next writable open migrates it.
            return []
        revision = "revision" if "revision" in columns else "1 AS revision"
        generation = (
            "graph_generation" if "graph_generation" in columns else "NULL AS graph_generation"
        )
        sql = (
            f"SELECT rel_path, {revision}, {generation} FROM {table} "
        )
        params: list[Any] = []
        if paths is not None:
            wanted = sorted(
                {
                    rel
                    for raw in paths
                    if (rel := _safe_markdown_rel_path(raw)) is not None
                }
            )
            if not wanted:
                return []
            sql += f"WHERE rel_path IN ({','.join('?' for _ in wanted)}) "
            params.extend(wanted)
        if generations is not None:
            # The unknown rows come back too, and must: one receipt that cannot
            # say what it owes disqualifies the whole queue as evidence, so a
            # read scoped to the asked-about generations would answer "covered"
            # on a queue it never looked at properly.
            # The predicate needs the raw column, not the SELECT alias: on a
            # store that predates the column every row is unknown, and
            # `NULL IS NULL` is the honest way to say so.
            column = "graph_generation" if "graph_generation" in columns else "NULL"
            placeholders = ",".join("?" for _ in generations)
            clause = f"{column} IS NULL"
            if generations:
                clause += f" OR {column} IN ({placeholders})"
            sql += ("AND " if paths is not None else "WHERE ") + f"({clause}) "
            params.extend(generations)
        sql += "ORDER BY updated_at, rel_path"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, limit))
        receipts = [
            DeferredReceipt(
                str(row[0]), int(row[1]), None if row[2] is None else int(row[2])
            )
            for row in conn.execute(sql, tuple(params)).fetchall()
        ]
    finally:
        if not borrowed:
            conn.close()
    valid = [
        DeferredReceipt(rel, receipt.revision, receipt.graph_generation)
        for receipt in receipts
        if (rel := _safe_markdown_rel_path(receipt.rel_path)) is not None
    ]
    corrupt = [
        receipt.rel_path
        for receipt in receipts
        if _safe_markdown_rel_path(receipt.rel_path) is None
    ]
    if corrupt:
        _purge_corrupt_paths(vault_root, table, corrupt)
    return valid


def clear_receipts(vault_root: Path, receipts: list[DeferredReceipt]) -> int:
    if not receipts or not store_path(vault_root).exists():
        return 0
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            changed = sum(
                conn.execute(
                    "DELETE FROM semantic_upserts WHERE rel_path = ? AND revision = ?",
                    (receipt.rel_path, receipt.revision),
                ).rowcount
                for receipt in receipts
            )
        return int(changed)
    finally:
        conn.close()


def clear_full_receipts(vault_root: Path, receipts: list[DeferredReceipt]) -> int:
    return _clear_plain_receipts(vault_root, receipts, table="full_upserts")


def _clear_plain_receipts(
    vault_root: Path, receipts: list[DeferredReceipt], *, table: str
) -> int:
    if not receipts or not store_path(vault_root).exists():
        return 0
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            changed = sum(
                conn.execute(
                    f"DELETE FROM {table} WHERE rel_path = ? AND revision = ?",
                    (receipt.rel_path, receipt.revision),
                ).rowcount
                for receipt in receipts
            )
        return int(changed)
    finally:
        conn.close()


def rotate_receipts(
    vault_root: Path,
    receipts: list[DeferredReceipt],
    *,
    queue: str = "semantic",
) -> int:
    """Move failed receipt revisions behind untouched work without changing CAS identity."""
    if not receipts or not store_path(vault_root).exists():
        return 0
    table = _QUEUE_TABLES[queue]
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            # Strictly behind, not merely re-stamped: on a coarse clock the
            # wall-clock value can equal the insert's, which leaves the poisoned
            # receipt sorting first and pins the queue it was rotated to unpin.
            newest = conn.execute(f"SELECT MAX(updated_at) FROM {table}").fetchone()[0]
            behind = max(
                time.time(),
                float(newest) + _ROTATION_EPSILON_SECONDS if newest is not None else 0.0,
            )
            changed = sum(
                conn.execute(
                    f"UPDATE {table} SET updated_at = ? "
                    "WHERE rel_path = ? AND revision = ?",
                    (behind, receipt.rel_path, receipt.revision),
                ).rowcount
                for receipt in receipts
            )
        return int(changed)
    finally:
        conn.close()


def clear_semantic_receipts(vault_root: Path, rel_paths: list[str]) -> int:
    """CAS-clear current receipts for paths made structured-only.

    A direct edit can make an older suppression observation stale. Read exact
    revisions first, then delete only those revisions so a newer admitted write
    cannot be erased by this cleanup.
    """
    wanted = {_safe_markdown_rel_path(rel) for rel in rel_paths}
    wanted.discard(None)
    if not wanted or not store_path(vault_root).exists():
        return 0
    conn = _connect(vault_root, create=False)
    try:
        rows = conn.execute(
            "SELECT rel_path, revision FROM semantic_upserts WHERE rel_path IN "
            f"({','.join('?' for _ in wanted)})",
            tuple(sorted(wanted)),
        ).fetchall()
    finally:
        conn.close()
    return clear_receipts(
        vault_root, [DeferredReceipt(str(row[0]), int(row[1])) for row in rows]
    )


def _list_paths(
    vault_root: Path, *, table: str, limit: int | None = None
) -> list[str]:
    path = store_path(vault_root)
    if not path.exists():
        return []
    conn = _connect(vault_root, create=False)
    try:
        if not any(conn.execute(f"PRAGMA table_info({table})")):
            return []  # Predates this queue; see `_snapshot_plain`.
        sql = f"SELECT rel_path FROM {table} ORDER BY rel_path"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(0, limit),)
        return [str(row[0]) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _purge_corrupt_paths(
    vault_root: Path,
    table: str,
    paths: list[str],
    *,
    connection_path: Path | None = None,
) -> int:
    """Delete exact unsafe legacy rows without turning them into filesystem paths."""
    if table not in set(_QUEUE_TABLES.values()) or not paths:
        return 0
    conn = _connect(vault_root, create=True, connection_path=connection_path)
    try:
        with conn:
            return int(
                conn.execute(
                    f"DELETE FROM {table} WHERE rel_path IN "
                    f"({','.join('?' for _ in paths)})",
                    tuple(paths),
                ).rowcount
            )
    finally:
        conn.close()


def purge_exact_persisted_semantic_rows(
    vault_root: Path, values: list[str], *, connection_path: Path | None = None
) -> int:
    """Drop quarantined semantic receipts without normalizing their spellings."""
    target = connection_path if connection_path is not None else store_path(vault_root)
    if not target.exists():
        return 0
    return _purge_corrupt_paths(
        vault_root, "semantic_upserts", values, connection_path=connection_path
    )


def clear(vault_root: Path, rel_paths: list[str] | None = None) -> int:
    return _clear(vault_root, table="semantic_upserts", rel_paths=rel_paths)


def clear_full(vault_root: Path, rel_paths: list[str] | None = None) -> int:
    return _clear(vault_root, table="full_upserts", rel_paths=rel_paths)


def _clear(
    vault_root: Path, *, table: str, rel_paths: list[str] | None = None
) -> int:
    path = store_path(vault_root)
    if not path.exists():
        return 0
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            if rel_paths is None:
                changed = conn.execute(f"DELETE FROM {table}").rowcount
            else:
                rels = sorted({rel.replace("\\", "/") for rel in rel_paths})
                if not rels:
                    return 0
                changed = conn.execute(
                    f"DELETE FROM {table} WHERE rel_path IN "
                    f"({','.join('?' for _ in rels)})",
                    rels,
                ).rowcount
        return int(changed)
    finally:
        conn.close()


def status(vault_root: Path | None) -> dict[str, Any]:
    return _status(vault_root, table="semantic_upserts")


def full_status(vault_root: Path | None) -> dict[str, Any]:
    result = _status(vault_root, table="full_upserts")
    command = (
        f'exomem index --vault "{vault_root}" --scope vault'
        if vault_root is not None
        else "exomem index --scope vault"
    )
    return {
        **result,
        "retryable": result["count"] > 0,
        "next_action": command if result["count"] else None,
    }


def _status(vault_root: Path | None, *, table: str) -> dict[str, Any]:
    empty = {"count": 0, "paths": [], "truncated": False, "roots": 0}
    if vault_root is None or not store_path(vault_root).exists():
        return empty
    try:
        conn = _connect(vault_root, create=False)
        try:
            count = int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            paths = [
                str(row[0])
                for row in conn.execute(
                    f"SELECT rel_path FROM {table} ORDER BY rel_path LIMIT 50"
                ).fetchall()
            ]
        finally:
            conn.close()
        return {
            "count": count,
            "paths": paths,
            "truncated": count > len(paths),
            "roots": int(count > 0),
        }
    except (OSError, sqlite3.Error):
        return empty


def _embedding_sidecar(vault_root: Path) -> Path:
    from . import index_paths

    return index_paths.sidecar_path(vault_root)


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _sidecar_state(sidecar: Path) -> tuple[tuple[bool, tuple[int, int, int, int] | None], ...]:
    paths = (
        sidecar,
        Path(f"{sidecar}-wal"),
        Path(f"{sidecar}-shm"),
        Path(f"{sidecar}-journal"),
    )
    state: list[tuple[bool, tuple[int, int, int, int] | None]] = []
    for path in paths:
        if os.path.lexists(path):
            state.append((True, _file_identity(path)))
        else:
            state.append((False, None))
    return tuple(state)


def inspect_embedding_freshness(
    vault_root: Path,
    rel_paths: list[str],
    *,
    mtime_slack_seconds: float = 0.0,
) -> dict[str, EmbeddingFreshness]:
    """Classify paths without initializing an embedding model.

    Exact mtime comparison remains the default for existing admission callers.
    The deferred drain opts into the incremental indexer's one-second
    filesystem-jitter allowance.
    """
    rels = sorted({rel.replace("\\", "/") for rel in rel_paths})
    result = {rel: EmbeddingFreshness.UNVERIFIABLE for rel in rels}
    if not rels:
        return result
    sidecar = _embedding_sidecar(vault_root)
    if not sidecar.is_file():
        return result
    try:
        from . import access, embeddings, find, semantic_index

        before_sidecar = _sidecar_state(sidecar)
        wal_exists = before_sidecar[1][0]
        shm_exists = before_sidecar[2][0]
        rollback_journal_exists = before_sidecar[3][0]
        if wal_exists != shm_exists or rollback_journal_exists:
            return result
        disk_mtimes: dict[str, float] = {}
        disk_identities: dict[str, tuple[int, int, int, int]] = {}
        chunked: dict[str, bool] = {}
        parent_states: dict[str, Any] = {}
        for rel in rels:
            path = vault_root / rel
            try:
                disk_identities[rel] = _file_identity(path)
                disk_mtimes[rel] = path.stat().st_mtime
                page = find._CACHE.get(path, vault_root)
                if page is None or not access.is_indexable(vault_root, rel):
                    continue
                chunked[rel] = bool(embeddings._chunks_for_page(vault_root, page))
                try:
                    parent_states[rel] = semantic_index.build_parent_index_state(
                        vault_root, path
                    )
                except (OSError, UnicodeError, ValueError):
                    pass
            except OSError:
                continue
        query_sidecar = sidecar
        snapshot_dir = None
        query = "mode=ro&immutable=1"
        if wal_exists:
            # SQLite's WAL reader mutates lock bytes in the source -shm even for a
            # mode=ro connection. Query a private byte-for-byte snapshot instead.
            snapshot_dir = tempfile.TemporaryDirectory(
                prefix="exomem-embedding-read-"
            )
            query_sidecar = Path(snapshot_dir.name) / sidecar.name
            for source in (
                sidecar,
                Path(f"{sidecar}-wal"),
                Path(f"{sidecar}-shm"),
            ):
                shutil.copyfile(
                    source,
                    Path(snapshot_dir.name) / source.name,
                )
            query = "mode=ro"
        conn = _sqlite_connect(
            f"{query_sidecar.resolve().as_uri()}?{query}",
            uri=True,
            timeout=0.0,
        )
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout=0")
            stored: dict[str, float] = {}
            stored_units: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
            for offset in range(0, len(rels), 400):
                batch = rels[offset : offset + 400]
                rows = conn.execute(
                    "SELECT file_path, MAX(file_mtime) FROM chunks "
                    f"WHERE file_path IN ({','.join('?' for _ in batch)}) "
                    "GROUP BY file_path",
                    batch,
                ).fetchall()
                stored.update({str(row[0]): float(row[1]) for row in rows})
            has_unit_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'semantic_unit_vectors'"
            ).fetchone()
            if has_unit_table:
                for offset in range(0, len(rels), 400):
                    batch = rels[offset : offset + 400]
                    rows = conn.execute(
                        "SELECT parent_path, parent_generation, unit_ref "
                        "FROM semantic_unit_vectors WHERE parent_path IN "
                        f"({','.join('?' for _ in batch)})",
                        batch,
                    ).fetchall()
                    for parent_path, generation, unit_ref in rows:
                        generations, unit_refs = stored_units.setdefault(
                            str(parent_path), (frozenset(), frozenset())
                        )
                        stored_units[str(parent_path)] = (
                            generations | {str(generation)},
                            unit_refs | {str(unit_ref)},
                        )
        finally:
            conn.close()
            if snapshot_dir is not None:
                snapshot_dir.cleanup()
        if _sidecar_state(sidecar) != before_sidecar:
            return result
        for rel, disk_mtime in disk_mtimes.items():
            path = vault_root / rel
            try:
                if _file_identity(path) != disk_identities[rel]:
                    continue
            except OSError:
                continue
            row_mtime = stored.get(rel)
            if mtime_slack_seconds > 0:
                mtime_current = (
                    row_mtime is not None
                    and disk_mtime <= row_mtime + mtime_slack_seconds
                )
            else:
                mtime_current = row_mtime is not None and disk_mtime == row_mtime
            file_current = (
                mtime_current
                if chunked.get(rel, False)
                else row_mtime is None
            )
            parent_state = parent_states.get(rel)
            if parent_state is None:
                unit_current = rel not in stored_units
            else:
                expected_refs = frozenset(
                    unit.unit_ref
                    for unit in parent_state.document.units
                    if unit.unit_ref is not None
                )
                expected = (frozenset({parent_state.parent_generation}), expected_refs)
                actual = stored_units.get(rel)
                unit_current = actual == expected or (actual is None and not expected_refs)
            result[rel] = (
                EmbeddingFreshness.CURRENT
                if file_current and unit_current
                else EmbeddingFreshness.STALE
            )
        return result
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return result
