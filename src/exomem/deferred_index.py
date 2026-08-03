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

from .kbdir import kb_dirname

_SEMANTIC_ISOLATION_CURSOR_KEY = "semantic_isolation_cursors:v1"
_SEMANTIC_UPSERTS_GENERATION_KEY = "semantic_upserts_generation"


def store_path(vault_root: Path) -> Path:
    return vault_root / kb_dirname() / ".deferred-index.sqlite"


def _connect_readonly(vault_root: Path) -> sqlite3.Connection:
    """Open an existing sidecar without schema repair or journal writes."""
    return sqlite3.connect(
        f"{store_path(vault_root).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
    )


def _connect(
    vault_root: Path, *, create: bool, connection_path: Path | None = None
) -> sqlite3.Connection:
    path = connection_path if connection_path is not None else store_path(vault_root)
    if not create:
        return _connect_readonly(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
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
            updated_at REAL NOT NULL
        )
        """
    )
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(semantic_upserts)")
    }
    if "revision" not in columns:
        conn.execute(
            "ALTER TABLE semantic_upserts "
            "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
        )
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
        conn = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True, timeout=5.0)
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
    return _add(vault_root, rel_paths, table="full_upserts")


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
                INSERT INTO {table}(rel_path, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(rel_path) DO UPDATE SET updated_at = excluded.updated_at
                """,
                [(rel, now, now) for rel in rels],
            )
        return len(rels) - existing
    finally:
        conn.close()


def list_paths(vault_root: Path, *, limit: int | None = None) -> list[str]:
    return [receipt.rel_path for receipt in snapshot(vault_root, limit=limit)]


def list_full_paths(vault_root: Path, *, limit: int | None = None) -> list[str]:
    paths = _list_paths(vault_root, table="full_upserts", limit=limit)
    valid = [rel for rel in paths if _safe_markdown_rel_path(rel) is not None]
    corrupt = [rel for rel in paths if _safe_markdown_rel_path(rel) is None]
    if corrupt:
        _purge_corrupt_paths(vault_root, "full_upserts", corrupt)
    return valid


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
        sql = f"SELECT rel_path, {revision} FROM semantic_upserts ORDER BY rel_path"
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
    if table not in {"semantic_upserts", "full_upserts"} or not paths:
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
    return {
        **result,
        "retryable": result["count"] > 0,
        "next_action": "retry deferred index refresh" if result["count"] else None,
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
    vault_root: Path, rel_paths: list[str]
) -> dict[str, EmbeddingFreshness]:
    """Classify paths without importing or initializing the embedding stack."""
    rels = sorted({rel.replace("\\", "/") for rel in rel_paths})
    result = {rel: EmbeddingFreshness.UNVERIFIABLE for rel in rels}
    if not rels:
        return result
    sidecar = _embedding_sidecar(vault_root)
    if not sidecar.is_file():
        return result
    try:
        before_sidecar = _sidecar_state(sidecar)
        wal_exists = before_sidecar[1][0]
        shm_exists = before_sidecar[2][0]
        rollback_journal_exists = before_sidecar[3][0]
        if wal_exists != shm_exists or rollback_journal_exists:
            return result
        disk_mtimes: dict[str, float] = {}
        disk_identities: dict[str, tuple[int, int, int, int]] = {}
        for rel in rels:
            path = vault_root / rel
            try:
                disk_identities[rel] = _file_identity(path)
                disk_mtimes[rel] = path.stat().st_mtime
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
        conn = sqlite3.connect(
            f"{query_sidecar.resolve().as_uri()}?{query}",
            uri=True,
            timeout=0.0,
        )
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout=0")
            stored: dict[str, float] = {}
            for offset in range(0, len(rels), 400):
                batch = rels[offset : offset + 400]
                rows = conn.execute(
                    "SELECT file_path, MAX(file_mtime) FROM chunks "
                    f"WHERE file_path IN ({','.join('?' for _ in batch)}) "
                    "GROUP BY file_path",
                    batch,
                ).fetchall()
                stored.update({str(row[0]): float(row[1]) for row in rows})
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
            result[rel] = (
                EmbeddingFreshness.CURRENT
                if row_mtime is not None and row_mtime == disk_mtime
                else EmbeddingFreshness.STALE
            )
        return result
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return result
