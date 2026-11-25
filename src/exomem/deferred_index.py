"""Durable registry for deferred semantic-index paths."""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from . import reserved_paths, sidecar_store
from .kbdir import managed_split_parts

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
_GRAPH_FULL_REBUILD_FATAL_FAMILY_KEY = "graph_full_rebuild_fatal_family:v1"
_PENDING_VISIBILITY_GENERATION_KEY = "pending_visibility_generation:v1"
#: Set, in the transaction that rebuilds it, to the component names the table's
#: CHECK accepts, so a release that adds a component widens the CHECK once.
_COMPONENT_CHECK_KEY = "derived_batch_component_check:v1"

# Additive fields on the existing queue. Legacy writers may update revision
# alone; retry_revision prevents their new input inheriting an old refusal.
_SEMANTIC_RETRY_COLUMNS = {
    "next_attempt_at": "REAL NOT NULL DEFAULT 0",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "failure_code": "TEXT",
    "refused_input": "TEXT",
    "refused_policy": "TEXT",
    "retry_revision": "INTEGER NOT NULL DEFAULT 0",
}

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


_COMPONENT_SCHEDULE_INDEX = (
    "CREATE INDEX IF NOT EXISTS derived_components_schedule "
    "ON derived_batch_components(state, next_attempt_at, updated_at)"
)


def _known_components() -> tuple[str, ...]:
    """The receipt component names this release knows, from the enum that defines them."""
    # Imported here because derived_receipts imports this module.
    from .derived_receipts import DerivedComponent

    return tuple(component.value for component in DerivedComponent)


def _components_table_ddl(
    name: str, *, if_not_exists: bool, components: tuple[str, ...] | None = None
) -> str:
    """The component table. Its CHECK is the enum's names (plus any a rebuild keeps), so the two cannot drift."""
    names = ", ".join(
        "'" + component.replace("'", "''") + "'" for component in components or _known_components()
    )
    create = "CREATE TABLE IF NOT EXISTS" if if_not_exists else "CREATE TABLE"
    return f"""
        {create} {name} (
            batch_id TEXT NOT NULL,
            component TEXT NOT NULL CHECK(component IN ({names})),
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


def derived_component_check_current(conn: sqlite3.Connection) -> bool:
    """Whether the component table's CHECK already accepts every component this release knows.

    A later release's wider CHECK counts as current: narrowing it would refuse
    that release's rows after a rollback.
    """
    row = conn.execute(
        "SELECT value FROM maintenance_state WHERE key = ?", (_COMPONENT_CHECK_KEY,)
    ).fetchone()
    return row is not None and set(str(row[0]).split(",")) >= set(_known_components())


def _widen_component_check(conn: sqlite3.Connection) -> None:
    """Let a store made by an older release accept every component this release knows.

    SQLite cannot widen a CHECK in place, so the table is rebuilt once with the
    same rows copied across verbatim. The new CHECK also keeps every name a row
    already holds, so no stored row is refused. No row is added: a batch with no
    row for a component never owed that work, and the receipt reader says so (an
    older release can still write such a batch after a rollback).
    """
    if derived_component_check_current(conn):
        return
    nested = conn.in_transaction
    conn.execute("SAVEPOINT derived_component_check" if nested else "BEGIN IMMEDIATE")
    try:
        # Another opener may have finished while this one waited for the write lock.
        if not derived_component_check_current(conn):
            known = _known_components()
            stored = tuple(
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT component FROM derived_batch_components ORDER BY component"
                )
            )
            components = known + tuple(name for name in stored if name not in known)
            conn.execute(
                _components_table_ddl(
                    "derived_batch_components_rebuilt", if_not_exists=False, components=components
                )
            )
            conn.execute(
                "INSERT INTO derived_batch_components_rebuilt(batch_id, component, "
                "revision, state, lease_revision, claim_owner, claim_expires_at, "
                "attempt_count, next_attempt_at, created_at, updated_at, failure_code) "
                "SELECT batch_id, component, revision, state, lease_revision, claim_owner, "
                "claim_expires_at, attempt_count, next_attempt_at, created_at, updated_at, "
                "failure_code FROM derived_batch_components"
            )
            conn.execute("DROP TABLE derived_batch_components")
            conn.execute(
                "ALTER TABLE derived_batch_components_rebuilt RENAME TO derived_batch_components"
            )
            conn.execute(_COMPONENT_SCHEDULE_INDEX)
            conn.execute(
                "INSERT INTO maintenance_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_COMPONENT_CHECK_KEY, ",".join(components)),
            )
    except BaseException:
        if nested:
            conn.execute("ROLLBACK TO SAVEPOINT derived_component_check")
            conn.execute("RELEASE SAVEPOINT derived_component_check")
        else:
            conn.rollback()
        raise
    if nested:
        conn.execute("RELEASE SAVEPOINT derived_component_check")
    else:
        conn.commit()


def _ensure_derived_batch_schema(conn: sqlite3.Connection) -> None:
    """Add the exact derived-batch custody tables without touching old queues."""
    caller_transaction = conn.in_transaction
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
            ),
            proven_at REAL
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
            batch_seq INTEGER,
            PRIMARY KEY(batch_id, rel_path),
            FOREIGN KEY(batch_id) REFERENCES derived_batches(batch_id)
        )
        """
    )
    conn.execute(_components_table_ddl("derived_batch_components", if_not_exists=True))
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
    conn.execute(_COMPONENT_SCHEDULE_INDEX)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS derived_paths_lookup "
        "ON derived_batch_paths(rel_path, batch_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS pending_recall_visibility "
        "ON pending_recall_rows(state, canonical_generation)"
    )
    # Readers scan active custody; publication callbacks seek only named paths.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS pending_recall_active "
        "ON pending_recall_rows(rel_path, batch_id, component_revision, "
        "canonical_generation, state) WHERE state != 'retired'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS advisory_result_retention "
        "ON write_advisory_results(retention_deadline, terminal_replay_until)"
    )
    # Coverage asks "which later batch carries this path?". The batch's store
    # sequence on each path row lets that seek by path and sequence instead of
    # walking every later receipt, which are never pruned. A trigger fills it,
    # so every writer -- an older build after a rollback included -- keeps it.
    batch_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(derived_batches)")
    }
    if "proven_at" not in batch_columns:
        conn.execute("ALTER TABLE derived_batches ADD COLUMN proven_at REAL")
    conn.execute("SAVEPOINT derived_paths_sequence_migration")
    try:
        path_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(derived_batch_paths)")
        }
        if "batch_seq" not in path_columns:
            conn.execute("ALTER TABLE derived_batch_paths ADD COLUMN batch_seq INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS derived_paths_missing_sequence "
            "ON derived_batch_paths(batch_id) WHERE batch_seq IS NULL"
        )
        if conn.execute(
            "SELECT 1 FROM derived_batch_paths "
            "INDEXED BY derived_paths_missing_sequence "
            "WHERE batch_seq IS NULL LIMIT 1"
        ).fetchone():
            conn.execute(
                "UPDATE derived_batch_paths SET batch_seq = (SELECT b.rowid "
                "FROM derived_batches AS b WHERE b.batch_id = derived_batch_paths.batch_id) "
                "WHERE batch_seq IS NULL"
            )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS derived_paths_sequence_fill "
            "AFTER INSERT ON derived_batch_paths WHEN NEW.batch_seq IS NULL BEGIN "
            "UPDATE derived_batch_paths SET batch_seq = (SELECT b.rowid "
            "FROM derived_batches AS b WHERE b.batch_id = NEW.batch_id) "
            "WHERE batch_id = NEW.batch_id AND rel_path = NEW.rel_path; END"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS derived_paths_sequence "
            "ON derived_batch_paths(rel_path, batch_seq)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS derived_paths_after_sequence "
            "ON derived_batch_paths(rel_path, after_hash, batch_seq)"
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT derived_paths_sequence_migration")
        conn.execute("RELEASE SAVEPOINT derived_paths_sequence_migration")
        raise
    conn.execute("RELEASE SAVEPOINT derived_paths_sequence_migration")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS derived_batches_state ON derived_batches(state)"
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
    # A newly opened handle must be ready for the caller's BEGIN; a caller's
    # existing transaction still owns its commit or rollback.
    if not caller_transaction:
        conn.commit()
    _widen_component_check(conn)


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


def _connect_readonly(vault_root: Path, *, deadline: float | None = None) -> sqlite3.Connection:
    """Open an existing sidecar without schema repair or journal writes."""
    with reserved_paths._subsystem_authority_scope("deferred_index"):
        with reserved_paths._identity_coordination_scope(
            vault_root,
            descriptor_ids=("deferred-index-store",),
            identity_may_change=False,
            deadline=deadline,
        ):
            # A deadline bounds the wait for a lock; it does not refuse one at once.
            return _connect_readonly_owned(
                vault_root,
                timeout=5.0 if deadline is None else max(0.0, deadline - time.monotonic()),
            )


def _connect_readonly_owned(vault_root: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
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
            timeout=timeout,
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
            signature TEXT,
            held_proof TEXT,
            failure_revision INTEGER,
            failure_cause TEXT
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
    for name, declaration in _SEMANTIC_RETRY_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE semantic_upserts ADD COLUMN {name} {declaration}")
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
    failure_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(graph_failures)")}
    for column, declaration in (
        ("held_proof", "TEXT"), ("failure_revision", "INTEGER"), ("failure_cause", "TEXT"),
    ):
        if column not in failure_columns:
            conn.execute(f"ALTER TABLE graph_failures ADD COLUMN {column} {declaration}")
    if "read_grace_since" not in graph_columns:
        _install_graph_read_grace(conn)
    _install_graph_receipt_revisions(conn)
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


def _graph_receipt_revisions_current(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master "
        "WHERE type = 'index' AND name = 'graph_upserts_queue_order') "
        "AND EXISTS(SELECT 1 FROM maintenance_state WHERE key = ?)",
        (_GRAPH_UPSERTS_GENERATION_KEY,),
    ).fetchone()[0])


def _install_graph_receipt_revisions(conn: sqlite3.Connection) -> None:
    """Reserve legacy identities once and index the order used by the drain."""
    if _graph_receipt_revisions_current(conn):
        return
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if _graph_receipt_revisions_current(conn):
            return
        conn.execute(
            "CREATE INDEX IF NOT EXISTS graph_upserts_queue_order ON graph_upserts(updated_at, rel_path)"
        )
        conn.execute(
            "INSERT INTO maintenance_state(key, value) VALUES (?, MAX("
            "coalesce((SELECT max(revision) FROM graph_upserts), 0), "
            "coalesce((SELECT max(failure_revision) FROM graph_failures), 0))) "
            "ON CONFLICT(key) DO UPDATE SET value = MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
            (_GRAPH_UPSERTS_GENERATION_KEY,),
        )


def _install_graph_read_grace(conn: sqlite3.Connection) -> None:
    """Index receipt-local permission failures without a queue scan on every retry."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if any(row[1] == "read_grace_since" for row in conn.execute("PRAGMA table_info(graph_upserts)")):
            return
        # Older rows acquire an exact classification on their next isolated failure.
        conn.execute("ALTER TABLE graph_upserts ADD COLUMN read_grace_since REAL")
        conn.execute("CREATE INDEX graph_upserts_read_grace ON graph_upserts(read_grace_since)")
        conn.execute("CREATE INDEX graph_upserts_oldest ON graph_upserts(created_at)")
        conn.execute(
            "CREATE TRIGGER graph_upserts_clear_read_grace "
            "AFTER UPDATE OF revision, graph_generation, rel_path ON graph_upserts "
            "WHEN NEW.read_grace_since IS NOT NULL AND "
            "(OLD.revision IS NOT NEW.revision OR OLD.graph_generation IS NOT NEW.graph_generation "
            "OR OLD.rel_path IS NOT NEW.rel_path) "
            "BEGIN UPDATE graph_upserts SET read_grace_since = NULL WHERE rel_path = NEW.rel_path; END"
        )
        # The unreadable cause is the closed permission-grace outcome of graph repair.
        # Releasing a quarantine leaves a fresh receipt to classify.
        for name, event, condition in (
            ("insert", "INSERT", ""),
            ("update", "UPDATE OF first_failed_at, failure_revision, failure_cause, quarantined",
             "AND OLD.quarantined = 0 "),
        ):
            conn.execute(
                f"CREATE TRIGGER graph_failures_read_grace_{name} AFTER {event} ON graph_failures "
                "BEGIN UPDATE graph_upserts SET read_grace_since = CASE WHEN "
                "NEW.failure_revision = revision AND NEW.failure_cause = 'unreadable' "
                f"AND NEW.quarantined = 0 {condition}THEN NEW.first_failed_at END "
                "WHERE rel_path = NEW.rel_path; END"
            )
        conn.execute(
            "CREATE TRIGGER graph_failures_read_grace_delete AFTER DELETE ON graph_failures "
            "BEGIN UPDATE graph_upserts SET read_grace_since = NULL "
            "WHERE rel_path = OLD.rel_path AND read_grace_since IS NOT NULL; END"
        )


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
    next_attempt_at: float = field(default=0.0, compare=False)
    attempt_count: int = field(default=0, compare=False)
    failure_code: str | None = field(default=None, compare=False)
    refused_input: str | None = field(default=None, compare=False)
    refused_policy: str | None = field(default=None, compare=False)
    # Disposable pagination metadata, never receipt identity or clearing authority.
    scan_rowid: int | None = field(default=None, compare=False)


def semantic_receipt_eligible(
    receipt: DeferredReceipt, *, input_signature: str, policy: str, now: float
) -> bool:
    """A refused parent waits for changed input or effective preparation policy."""
    if receipt.failure_code == "resource_budget_exceeded":
        return (receipt.refused_input, receipt.refused_policy) != (input_signature, policy)
    return receipt.next_attempt_at <= now


def retry_semantic(
    vault_root: Path,
    receipt: DeferredReceipt,
    *,
    failure_code: str,
    input_signature: str | None = None,
    policy: str | None = None,
    now: float | None = None,
) -> bool:
    """Record bounded eligibility only if the attempted revision still owns debt."""
    if failure_code not in {
        "embedding_failed", "resource_budget_exceeded", "preparation_busy",
        "input_unavailable", "input_changed",
    }:
        raise ValueError("unsupported semantic retry code")
    if failure_code == "resource_budget_exceeded" and (
        not input_signature or not policy
        or len(input_signature) > 128 or len(policy) > 128
    ):
        raise ValueError("budget refusal requires bounded input and policy identities")
    at = time.time() if now is None else now
    if not math.isfinite(at):
        raise ValueError("semantic retry time must be finite")
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            row = conn.execute(
                "SELECT attempt_count, retry_revision FROM semantic_upserts "
                "WHERE rel_path = ? AND revision = ?",
                (receipt.rel_path, receipt.revision),
            ).fetchone()
            if row is None:
                return False
            attempts = max(0, int(row[0])) if int(row[1]) == receipt.revision else 0
            delay = min(60.0, 2.0 ** min(attempts, 6))
            result = conn.execute(
                "UPDATE semantic_upserts SET next_attempt_at = ?, attempt_count = ?, "
                "failure_code = ?, refused_input = ?, refused_policy = ?, retry_revision = ? "
                "WHERE rel_path = ? AND revision = ?",
                (at + delay, min(attempts + 1, 64), failure_code,
                 input_signature if failure_code == "resource_budget_exceeded" else None,
                 policy if failure_code == "resource_budget_exceeded" else None,
                 receipt.revision, receipt.rel_path, receipt.revision),
            )
            return result.rowcount == 1
    finally:
        conn.close()


class EmbeddingFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNVERIFIABLE = "unverifiable"


def _safe_markdown_rel_path(
    value: object, *, knowledge_base_only: bool = True
) -> str | None:
    """Normalize one persisted Markdown identity without permitting traversal.

    ``knowledge_base_only=False`` keeps every traversal refusal but admits a
    vault-relative page outside the knowledge base -- what a vault-scope
    advisory can name as its counterpart.
    """
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
        or (knowledge_base_only and managed_split_parts(path.parts) is None)
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


def ensure_receipts(vault_root: Path, rel_paths: list[str]) -> list[DeferredReceipt]:
    """Retain existing execution custody without revising an in-flight receipt.

    Canonical producers still use ``add_receipts`` for a new edit. Repeated
    component/warmup handoffs merely ensure debt exists; they are not edits.
    """
    receipts, _added = _add_receipts(vault_root, rel_paths, preserve_existing=True)
    return receipts


def _add_receipts(
    vault_root: Path, rel_paths: list[str], *, preserve_existing: bool = False
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
                elif preserve_existing:
                    revision = int(row[0])
                else:
                    revision = int(row[0]) + 1
                    conn.execute(
                        "UPDATE semantic_upserts SET updated_at = ?, revision = ? "
                        ", next_attempt_at = 0, attempt_count = 0, failure_code = NULL "
                        ", refused_input = NULL, refused_policy = NULL, retry_revision = 0 "
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
        tracks_generation = table == "graph_upserts"
        conn.execute("BEGIN IMMEDIATE")
        try:
            graph_revision_floor = None
            if tracks_generation:
                # Each enqueue fires a generation trigger in this transaction.
                # The queue clock stays ahead even after a receipt leaves the queue.
                graph_revision_floor = int(conn.execute(
                    "SELECT value FROM maintenance_state WHERE key = ?",
                    (_GRAPH_UPSERTS_GENERATION_KEY,),
                ).fetchone()[0])
            for ordinal, rel in enumerate(rels, start=1):
                stored = "graph_generation" if tracks_generation else "NULL"
                row = conn.execute(
                    f"SELECT revision, {stored} FROM {table} WHERE rel_path = ?", (rel,)
                ).fetchone()
                if graph_revision_floor is None:
                    revision = 1 if row is None else int(row[0]) + 1
                else:
                    revision = graph_revision_floor + ordinal
                if row is None:
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
    if not rels:
        # A generation that changed no page owes no receipt, but a later write's predecessor probe still asks for it.
        record_graph_debt_generation(vault_root, generation=int(checkpoint.generation))
        return 0
    return add_graph(vault_root, rels, generation=int(checkpoint.generation))


def record_graph_debt_generation(vault_root: Path, *, generation: int) -> None:
    """Record a generation's complete graph debt when that debt is no path at all."""
    conn = _connect(vault_root, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _record_graph_debt_generation_locked(conn, generation, time.time())
        except Exception:
            conn.rollback()
            raise
        conn.commit()
    finally:
        conn.close()


def mark_graph_full_rebuild(
    vault_root: Path, *, generation: int, fatal_family: str | None = None,
) -> None:
    """Record that a whole-vault rebuild is owed, at or after this generation."""
    if fatal_family is not None:
        from .graph_sync import _is_digest

        if not _is_digest(fatal_family):
            raise ValueError("fatal graph family must be a SHA-256 digest")
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
            if fatal_family is not None:
                conn.execute(
                    "INSERT INTO maintenance_state(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (_GRAPH_FULL_REBUILD_FATAL_FAMILY_KEY, fatal_family),
                )
            elif current is None:
                _discard_orphaned_fatal_family_locked(conn)
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
            if _GRAPH_FULL_REBUILD_KEY not in rows:
                _discard_orphaned_fatal_family_locked(conn)
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


def _discard_orphaned_fatal_family_locked(conn: sqlite3.Connection) -> None:
    """Drop a fatal-family row that outlived its marker.

    Retirement deletes the family with its marker, but an older release that
    retires only the marker leaves the row behind after a downgrade. Raising
    new debt with no marker standing must not inherit that stale fence: a
    healthy sidecar that reuses the damaged file's identity would otherwise be
    refused as needing replacement.
    """
    conn.execute(
        "DELETE FROM maintenance_state WHERE key = ?",
        (_GRAPH_FULL_REBUILD_FATAL_FAMILY_KEY,),
    )


def _count_graph_full_rebuild_mark_locked(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO maintenance_state(key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
        (_GRAPH_FULL_REBUILD_MARKS_KEY,),
    )


class GraphFullRebuildDetails(NamedTuple):
    token: tuple[int, int]
    fatal_family: str | None  # Stable main-file identity, independent of content and times.


class GraphFullRebuildStateUnavailable(RuntimeError):
    """The marker state could not be read or validated."""


def graph_full_rebuild_observation(vault_root: Path) -> tuple[int, int] | None:
    """The standing marker and its raise count, read together, or None.

    What a whole-vault pass records before it samples its epoch, so that its
    publication can retire exactly the debt that existed then and nothing
    raised after -- see `retire_observed_graph_full_rebuild`.
    """
    try:
        details = graph_full_rebuild_details(vault_root)
    except GraphFullRebuildStateUnavailable:
        return None
    return details.token if details is not None else None


def graph_full_rebuild_details(
    vault_root: Path, *, deadline: float | None = None,
) -> GraphFullRebuildDetails | None:
    """Read one marker snapshot. None means the marker is absent."""
    from .cli_ops import OpError
    from .graph_sync import _is_digest

    try:
        try:
            store_path(vault_root).lstat()
        except FileNotFoundError:
            return None
        with closing(_connect_readonly(vault_root, deadline=deadline)) as conn:
            conn.execute("BEGIN")
            try:
                rows = dict(conn.execute(
                    "SELECT key, value FROM maintenance_state WHERE key IN (?, ?, ?)",
                    (_GRAPH_FULL_REBUILD_KEY, _GRAPH_FULL_REBUILD_MARKS_KEY, _GRAPH_FULL_REBUILD_FATAL_FAMILY_KEY),
                ))
            except sqlite3.OperationalError:
                # A store from before maintenance markers could not hold one.
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'maintenance_state'").fetchone() is None:
                    return None
                raise
        if _GRAPH_FULL_REBUILD_KEY not in rows:
            return None
        token = (int(rows[_GRAPH_FULL_REBUILD_KEY]), int(rows.get(_GRAPH_FULL_REBUILD_MARKS_KEY, 0)))
        fatal_family = rows.get(_GRAPH_FULL_REBUILD_FATAL_FAMILY_KEY)
        if min(token) < 0 or (fatal_family is not None and not _is_digest(fatal_family)):
            raise ValueError("malformed graph rebuild marker")
        return GraphFullRebuildDetails(token, fatal_family)
    except (OSError, OpError, RuntimeError, sqlite3.Error, TypeError, ValueError) as error:
        raise GraphFullRebuildStateUnavailable("graph rebuild marker state is unavailable") from error


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
            conn.execute(
                "DELETE FROM maintenance_state WHERE key IN (?, ?)",
                (_GRAPH_FULL_REBUILD_KEY, _GRAPH_FULL_REBUILD_FATAL_FAMILY_KEY),
            )
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return True
    finally:
        conn.close()


def graph_full_rebuild_pending(vault_root: Path) -> int | None:
    """The generation a whole-vault rebuild is owed for, or None."""
    if not store_path(vault_root).exists():
        return None
    try:
        conn = _connect_readonly(vault_root)
        try:
            return _graph_full_rebuild_generation_locked(conn)
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None


def _graph_full_rebuild_generation_locked(conn: sqlite3.Connection) -> int | None:
    """The whole-vault rebuild marker's generation, on a connection the caller holds."""
    row = conn.execute(
        "SELECT value FROM maintenance_state WHERE key = ?",
        (_GRAPH_FULL_REBUILD_KEY,),
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def graph_repair_owed_outside(
    vault_root: Path, rel_paths: Iterable[str], *, deadline: float | None = None,
) -> bool:
    """Whether graph repair is owed beyond `rel_paths`.

    True for a queued receipt for any other path, a whole-vault rebuild marker,
    or a store that cannot be read: a caller that publishes the graph as current
    on False never publishes over work it could not see. An absent store owes
    nothing. The lookup stops at the first outside receipt, so it examines at
    most the number of paths in the proof plus one.
    """
    if not store_path(vault_root).exists():
        return False
    wanted = sorted(
        {rel for raw in rel_paths if (rel := _safe_markdown_rel_path(raw)) is not None}
    )
    try:
        conn = _connect_readonly(vault_root, deadline=deadline)
        try:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if (
                "maintenance_state" in tables
                and _graph_full_rebuild_generation_locked(conn) is not None
            ):
                return True
            if "graph_upserts" not in tables:
                return False
            return conn.execute(
                "SELECT 1 FROM graph_upserts "
                "WHERE rel_path NOT IN (SELECT value FROM json_each(?)) LIMIT 1",
                (json.dumps(wanted),),
            ).fetchone() is not None
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return True


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
            if changed:
                conn.execute(
                    "DELETE FROM maintenance_state WHERE key = ?",
                    (_GRAPH_FULL_REBUILD_FATAL_FAMILY_KEY,),
                )
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


def semantic_scan_ceiling(vault_root: Path) -> int:
    """Freeze the current scan tail; arrivals cannot extend this sweep."""
    if not store_path(vault_root).exists():
        return 0
    conn = _connect(vault_root, create=False)
    try:
        return int(conn.execute("SELECT coalesce(max(rowid), 0) FROM semantic_upserts").fetchone()[0])
    finally:
        conn.close()


def semantic_next_retry_at(vault_root: Path, *, after: float) -> float | None:
    """Earliest time a time-gated semantic retry falls due after `after`.

    Same receipts as `semantic_receipt_eligible`'s time branch: a budget refusal
    waits on its input, not on the clock, and a stale revision's retry is void.
    """
    if not store_path(vault_root).exists():
        return None
    conn = _connect(vault_root, create=False)
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(semantic_upserts)")}
        if not set(_SEMANTIC_RETRY_COLUMNS) <= columns:
            return None
        row = conn.execute(
            "SELECT min(next_attempt_at) FROM semantic_upserts "
            "WHERE next_attempt_at > ? AND retry_revision = revision "
            "AND coalesce(failure_code, '') != 'resource_budget_exceeded'",
            (after,),
        ).fetchone()
        return None if row[0] is None else float(row[0])
    finally:
        conn.close()


def snapshot(
    vault_root: Path,
    *,
    limit: int | None = None,
    paths: set[str] | None = None,
    after_path: str | None = None,
    after_rowid: int | None = None,
    through_rowid: int | None = None,
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
        retry_columns = set(_SEMANTIC_RETRY_COLUMNS) <= columns
        retry_select = ", " + ", ".join(_SEMANTIC_RETRY_COLUMNS) if retry_columns else ""
        sql = f"SELECT rel_path, {revision}{retry_select}, rowid FROM semantic_upserts "
        params: list[Any] = []
        clauses: list[str] = []
        if paths is not None:
            wanted = sorted({rel for raw in paths if (rel := _safe_markdown_rel_path(raw))})
            if not wanted:
                return []
            clauses.append(f"rel_path IN ({','.join('?' for _ in wanted)})")
            params.extend(wanted)
        if after_path is not None:
            clauses.append("rel_path > ?")
            params.append(after_path)
        if after_rowid is not None:
            clauses.append("rowid > ?")
            params.append(after_rowid)
        if through_rowid is not None:
            clauses.append("rowid <= ?")
            params.append(through_rowid)
        if clauses:
            sql += "WHERE " + " AND ".join(clauses) + " "
        order = "rowid" if after_rowid is not None or through_rowid is not None else "rel_path" if after_path is not None else "updated_at, rel_path"
        sql += "ORDER BY " + order
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, limit))
        receipts = []
        for row in conn.execute(sql, params).fetchall():
            metadata = {}
            if retry_columns and int(row[7]) == int(row[1]):
                metadata = dict(zip(tuple(_SEMANTIC_RETRY_COLUMNS)[:-1], row[2:7], strict=True))
            receipts.append(DeferredReceipt(str(row[0]), int(row[1]), scan_rowid=int(row[-1]), **metadata))
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
            valid.append(receipt)
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
    deadline: float | None = None,
) -> list[DeferredReceipt]:
    """Queued graph work, oldest first, with corrupt legacy rows purged."""
    if deadline is None or not store_path(vault_root).exists():
        return _snapshot_plain(vault_root, table="graph_upserts", limit=limit, paths=paths)
    with closing(_connect_readonly(vault_root, deadline=deadline)) as conn:
        return _snapshot_plain(
            vault_root, table="graph_upserts", limit=limit, paths=paths, connection=conn,
        )


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


def graph_receipts_cover(vault_root: Path, paths: Iterable[str]) -> bool:
    """Check scoped ownership with one primary-key lookup per observed path."""
    wanted = list(paths)
    if (
        any(_safe_markdown_rel_path(path) != path for path in wanted)
        or not store_path(vault_root).exists()
    ):
        return False
    try:
        with closing(_connect_readonly(vault_root)) as conn:
            return bool(conn.execute(
                "SELECT NOT EXISTS(SELECT 1 FROM maintenance_state WHERE key = ?) "
                "AND EXISTS(SELECT 1 FROM graph_upserts) "
                "AND NOT EXISTS(SELECT 1 FROM json_each(?) AS observed "
                "WHERE NOT EXISTS(SELECT 1 FROM graph_upserts WHERE rel_path = observed.value))",
                (_GRAPH_FULL_REBUILD_KEY, json.dumps(wanted)),
            ).fetchone()[0])
    except (OSError, sqlite3.Error):
        return False


def clear_graph_receipts(vault_root: Path, receipts: list[DeferredReceipt]) -> int:
    """CAS-clear exact graph receipts and their completed failure state together."""
    if not receipts or not store_path(vault_root).exists():
        return 0
    wanted = json.dumps([(r.rel_path, r.revision, r.graph_generation) for r in receipts])
    with closing(_connect(vault_root, create=True)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        matched = conn.execute(
            "SELECT q.rel_path FROM json_each(?) AS wanted CROSS JOIN graph_upserts AS q "
            "ON q.rel_path = json_extract(wanted.value, '$[0]') "
            "AND q.revision = json_extract(wanted.value, '$[1]') "
            "AND q.graph_generation IS json_extract(wanted.value, '$[2]')",
            (wanted,),
        ).fetchall()
        if not matched:
            return 0
        paths = json.dumps([row[0] for row in matched])
        removed = conn.execute(
            "DELETE FROM graph_upserts WHERE rel_path IN (SELECT value FROM json_each(?))",
            (paths,),
        ).rowcount
        conn.execute(
            "DELETE FROM graph_failures WHERE rel_path IN (SELECT value FROM json_each(?))",
            (paths,),
        )
        return removed


@dataclass(frozen=True, slots=True)
class GraphHeldProof:
    checkpoint_digest: str
    receipt_revision: int
    signature: str
    retained_source: str
    graph_instance: str
    policy: tuple[str, str]
    topology: str
    cold_generation: tuple[int, int, int] | None

    def render(self) -> str:
        return json.dumps([
            self.checkpoint_digest, self.receipt_revision, self.signature,
            self.retained_source, self.graph_instance, self.policy, self.topology, self.cold_generation,
        ])

    @classmethod
    def parse(cls, raw: str) -> GraphHeldProof | None:
        from .graph_sync import _is_digest

        try:
            digest, revision, signature, source, instance, policy, topology, cold = json.loads(raw)
            if (
                not all(_is_digest(value) for value in (digest, source, topology))
                or type(revision) is not int or revision < 1
                or not isinstance(signature, str) or not signature
                or not isinstance(instance, str) or not instance
                or not isinstance(policy, list) or len(policy) != 2
                or not all(isinstance(value, str) for value in policy)
                or (cold is not None and (
                    not isinstance(cold, list) or len(cold) != 3
                    or not all(type(value) is int for value in cold)
                ))
            ):
                return None
            return cls(
                digest, revision, signature, source, instance, (policy[0], policy[1]), topology,
                (cold[0], cold[1], cold[2]) if cold is not None else None,
            )
        except (TypeError, ValueError, RecursionError):
            return None


def held_graph_proofs(
    vault_root: Path, *, digest: str, paths: Iterable[str], deadline: float | None = None,
) -> dict[str, GraphHeldProof]:
    """Read exact quarantines in one bounded query, excluding superseding receipts."""
    wanted = list(paths)
    if not wanted or not store_path(vault_root).exists():
        return {}
    with closing(_connect_readonly(vault_root, deadline=deadline)) as conn:
        if "held_proof" not in {row[1] for row in conn.execute("PRAGMA table_info(graph_failures)")}:
            return {}
        rows = conn.execute(
            "SELECT f.rel_path, f.held_proof, f.signature, f.failure_revision "
            "FROM graph_failures f WHERE f.quarantined = 1 "
            "AND length(f.held_proof) <= 1024 "
            "AND f.rel_path IN (SELECT value FROM json_each(?)) "
            "AND NOT EXISTS (SELECT 1 FROM graph_upserts q WHERE q.rel_path = f.rel_path)",
            (json.dumps(wanted),),
        ).fetchall()
    result = {}
    for rel, encoded, signature, revision in rows:
        proof = GraphHeldProof.parse(encoded)
        if (
            proof is not None and proof.checkpoint_digest == digest
            and proof.signature == signature and proof.receipt_revision == revision
        ):
            result[str(rel)] = proof
    return result


def graph_receipts_in_read_grace(vault_root: Path, *, seconds: float) -> bool:
    """Only an entirely classified queue keeps its permission grace during a stall."""
    if not store_path(vault_root).exists():
        return False
    try:
        with closing(_connect_readonly(vault_root)) as conn:
            return conn.execute(
                # SQLite orders NULL first. A missing classification or the
                # earliest failure outside the grace makes the queue ineligible.
                "SELECT read_grace_since > ? FROM graph_upserts INDEXED BY graph_upserts_read_grace "
                "ORDER BY read_grace_since LIMIT 1",
                (time.time() - seconds,),
            ).fetchone() == (1,)
    except (OSError, sqlite3.Error):
        return False


def graph_oldest_receipt_age(vault_root: Path) -> float | None:
    """Read the scheduler's age without counting the diagnostic queue depth."""
    if not store_path(vault_root).exists():
        return None
    try:
        with closing(_connect_readonly(vault_root)) as conn:
            row = conn.execute("SELECT min(created_at) FROM graph_upserts").fetchone()
    except (OSError, sqlite3.Error):
        return None
    return max(0.0, time.time() - row[0]) if row and row[0] is not None else None


def note_graph_failure(
    vault_root: Path, receipt: DeferredReceipt, *, cause: str,
) -> tuple[int, float] | None:
    """Count one failed isolated attempt for a receipt still in the queue.

    Attempts and the first failure time carry across requeues of that path.
    A superseded receipt supplies no failure evidence for its replacement.
    """
    now = time.time()
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM graph_upserts WHERE rel_path = ? AND revision = ? AND graph_generation IS ?",
                (receipt.rel_path, receipt.revision, receipt.graph_generation),
            ).fetchone() is None:
                return None
            conn.execute(
                "INSERT INTO graph_failures(rel_path, attempts, first_failed_at, last_failed_at, "
                "failure_revision, failure_cause) VALUES (?, 1, ?, ?, ?, ?) "
                "ON CONFLICT(rel_path) DO UPDATE SET attempts = attempts + 1, "
                "last_failed_at = excluded.last_failed_at, failure_revision = excluded.failure_revision, "
                "failure_cause = excluded.failure_cause",
                (receipt.rel_path, now, now, receipt.revision, cause),
            )
            row = conn.execute(
                "SELECT attempts, first_failed_at FROM graph_failures WHERE rel_path = ?",
                (receipt.rel_path,),
            ).fetchone()
        return int(row[0]), float(row[1])
    finally:
        conn.close()


def quarantine_graph_receipt(
    vault_root: Path, receipt: DeferredReceipt, *, signature: str | None,
    held: GraphHeldProof | None = None,
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
                "DELETE FROM graph_upserts WHERE rel_path = ? AND revision = ? AND graph_generation IS ?",
                (receipt.rel_path, receipt.revision, receipt.graph_generation),
            ).rowcount
            if removed:
                now = time.time()
                conn.execute(
                    "INSERT INTO graph_failures(rel_path, attempts, first_failed_at, "
                    "last_failed_at, quarantined, signature, held_proof, failure_revision) "
                    "VALUES (?, 1, ?, ?, 1, ?, ?, ?) ON CONFLICT(rel_path) DO UPDATE SET "
                    "quarantined = 1, last_failed_at = excluded.last_failed_at, "
                    "signature = excluded.signature, held_proof = excluded.held_proof, "
                    "failure_revision = excluded.failure_revision",
                    (receipt.rel_path, now, now, signature, held.render() if held else None, receipt.revision),
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
                "UPDATE graph_failures SET quarantined = 0, held_proof = NULL WHERE rel_path = ?",
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


def graph_queue_pending(vault_root: Path) -> bool:
    """Whether graph repair has a queued receipt or a whole-vault marker."""
    if not store_path(vault_root).exists():
        return False
    try:
        with closing(_connect_readonly(vault_root)) as conn:
            conn.execute("BEGIN")
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('maintenance_state', 'graph_upserts')"
            )}
            return (
                "maintenance_state" in tables and _graph_full_rebuild_generation_locked(conn) is not None
            ) or (
                "graph_upserts" in tables
                and bool(conn.execute("SELECT EXISTS(SELECT 1 FROM graph_upserts)").fetchone()[0])
            )
    except (OSError, sqlite3.Error):
        return False


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
    if not receipts or not store_path(vault_root).exists():
        return 0
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            changed = sum(
                conn.execute(
                    "DELETE FROM full_upserts WHERE rel_path = ? AND revision = ?",
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


def semantic_debt_status(vault_root: Path | None) -> dict[str, Any]:
    """Read first-enqueue age without creating state or disguising read failures."""
    unknown = {"state": "unknown", "count": None, "oldest_age_seconds": None,
               "resource_refused_count": None}
    if vault_root is None:
        return unknown
    try:
        if not store_path(vault_root).exists():
            return {"state": "absent", "count": 0, "oldest_age_seconds": None,
                    "resource_refused_count": 0}
        conn = _connect_readonly(vault_root)
        try:
            count, oldest = conn.execute(
                "SELECT count(*), min(created_at) FROM semantic_upserts"
            ).fetchone()
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(semantic_upserts)")}
            refused = conn.execute(
                "SELECT count(*) FROM semantic_upserts WHERE failure_code = ? AND retry_revision = revision",
                ("resource_budget_exceeded",),
            ).fetchone()[0] if {"failure_code", "retry_revision"} <= columns else None
        finally:
            conn.close()
        return {
            "state": "ok", "count": int(count),
            "oldest_age_seconds": max(0.0, time.time() - float(oldest)) if oldest is not None else None,
            "resource_refused_count": int(refused) if refused is not None else None,
        }
    except (OSError, sqlite3.Error, ValueError):
        return unknown


def exact_full_status(vault_root: Path, relative: str) -> dict[str, Any]:
    """Read one full-index demand without repair; absence does not prove freshness."""
    result = {
        "source": "full_upserts", "coverage": "exact-path", "state": "unavailable",
        "count": None, "paths": [], "freshness": "unknown",
    }
    try:
        conn = _connect_readonly(vault_root)
        try:
            row = conn.execute(
                "SELECT rel_path FROM full_upserts WHERE rel_path = ?", (relative,),
            ).fetchone()
        finally:
            conn.close()
        return {**result, "state": "available", "count": int(row is not None),
                "paths": [relative] if row is not None else []}
    except (OSError, sqlite3.Error, ValueError):
        return result


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


def _copy_live_sqlite_files(sources: tuple[Path, ...], directory: Path) -> None:
    """Copy live SQLite files byte for byte and leave this process's locks alone."""
    if os.name == "nt":
        # A LockFileEx byte-range lock belongs to the handle that took it, so
        # closing these copy handles leaves every SQLite lock in place.
        for source in sources:
            shutil.copyfile(source, directory / source.name)
        return
    # Closing any descriptor for a file drops every POSIX lock that this
    # process holds on it, such as its own SQLite connections' locks. A child
    # process reads the bytes, so the descriptors it closes are not ours.
    try:
        subprocess.run(
            ["cp", "--", *(os.fspath(source) for source in sources), os.fspath(directory)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as error:
        raise OSError("live SQLite files could not be copied") from error


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
        from . import access, embedding_index, embeddings, find, semantic_index

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
            _copy_live_sqlite_files(
                (sidecar, Path(f"{sidecar}-wal"), Path(f"{sidecar}-shm")),
                Path(snapshot_dir.name),
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
                stored_units = embedding_index.stored_parent_states(conn, rels)
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
                # Every parsed parent owes a coverage record, even with no occurrence.
                unit_current = stored_units.get(rel) == embedding_index.expected_parent_state(
                    parent_state
                )
            result[rel] = (
                EmbeddingFreshness.CURRENT
                if file_current and unit_current
                else EmbeddingFreshness.STALE
            )
        return result
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return result
