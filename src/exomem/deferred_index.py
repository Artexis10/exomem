"""Durable registry for deferred semantic-index paths."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from . import reserved_paths, sidecar_store
from .kbdir import kb_dirname

_SEMANTIC_ISOLATION_CURSOR_KEY = "semantic_isolation_cursors:v1"
_SEMANTIC_UPSERTS_GENERATION_KEY = "semantic_upserts_generation"
_GRAPH_UPSERTS_GENERATION_KEY = "graph_upserts_generation"
_GRAPH_FULL_REBUILD_KEY = "graph_full_rebuild_generation"

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


def _sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("deferred_index"):
        return _sqlite_connect_owned(database, *args, **kwargs)


def _sqlite_connect_owned(
    database: Any, *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    return sqlite3.connect(database, *args, **kwargs)


def store_path(vault_root: Path) -> Path:
    return vault_root / kb_dirname() / ".deferred-index.sqlite"


def _connect_readonly(vault_root: Path) -> sqlite3.Connection:
    """Open an existing sidecar without schema repair or journal writes."""
    with reserved_paths._subsystem_authority_scope("deferred_index"):
        with reserved_paths._identity_coordination_scope(vault_root):
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
        with reserved_paths._identity_coordination_scope(vault_root):
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


@dataclass(frozen=True, slots=True)
class DeferredReceipt:
    rel_path: str
    revision: int


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
    vault_root: Path, rel_paths: list[str], *, table: str
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
        conn.execute("BEGIN IMMEDIATE")
        try:
            for rel in rels:
                row = conn.execute(
                    f"SELECT revision FROM {table} WHERE rel_path = ?", (rel,)
                ).fetchone()
                if row is None:
                    revision = 1
                    added += 1
                    conn.execute(
                        f"INSERT INTO {table}"
                        "(rel_path, created_at, updated_at, revision) VALUES (?, ?, ?, ?)",
                        (rel, now, now, revision),
                    )
                else:
                    revision = int(row[0]) + 1
                    conn.execute(
                        f"UPDATE {table} SET updated_at = ?, revision = ? "
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


def add_graph(vault_root: Path, rel_paths: list[str]) -> int:
    """Durably queue pages whose epistemic-graph projection needs re-deriving."""
    _receipts, added = _add_plain_receipts(vault_root, rel_paths, table="graph_upserts")
    if added:
        _note_graph_debt()
    return added


def add_graph_receipts(vault_root: Path, rel_paths: list[str]) -> list[DeferredReceipt]:
    """Queue graph work and return its exact transaction-local revisions."""
    receipts, _added = _add_plain_receipts(
        vault_root, rel_paths, table="graph_upserts"
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
    return add_graph(vault_root, rels)


def mark_graph_full_rebuild(vault_root: Path, *, generation: int) -> None:
    """Record that a whole-vault rebuild is owed, at or after this generation."""
    conn = _connect(vault_root, create=True)
    try:
        with conn:
            conn.execute(
                "INSERT INTO maintenance_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = "
                "CASE WHEN CAST(excluded.value AS INTEGER) > CAST(value AS INTEGER) "
                "THEN excluded.value ELSE value END",
                (_GRAPH_FULL_REBUILD_KEY, str(int(generation))),
            )
    finally:
        conn.close()
    # A whole-vault marker is graph debt too, and the one the drain most needs
    # to hear about: it is raised exactly when the changed scope is unknown.
    _note_graph_debt()


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
        with conn:
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


def list_graph_paths(vault_root: Path, *, limit: int | None = None) -> list[str]:
    return [receipt.rel_path for receipt in snapshot_graph(vault_root, limit=limit)]


def clear_graph_receipts(vault_root: Path, receipts: list[DeferredReceipt]) -> int:
    return _clear_plain_receipts(vault_root, receipts, table="graph_upserts")


def clear_graph(vault_root: Path, rel_paths: list[str] | None = None) -> int:
    return _clear(vault_root, table="graph_upserts", rel_paths=rel_paths)


def rotate_graph_receipts(vault_root: Path, receipts: list[DeferredReceipt]) -> int:
    return rotate_receipts(vault_root, receipts, queue="graph")


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
) -> list[DeferredReceipt]:
    path = store_path(vault_root)
    if not path.exists():
        return []
    conn = _connect(vault_root, create=False)
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
        sql = (
            f"SELECT rel_path, {revision} FROM {table} "
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
        sql += "ORDER BY updated_at, rel_path"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, limit))
        receipts = [
            DeferredReceipt(str(row[0]), int(row[1]))
            for row in conn.execute(sql, tuple(params)).fetchall()
        ]
    finally:
        conn.close()
    valid = [
        DeferredReceipt(rel, receipt.revision)
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
    return vault_root / kb_dirname() / ".embeddings.sqlite"


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
