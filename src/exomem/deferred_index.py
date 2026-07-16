"""Durable registry for deferred semantic-index paths."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .kbdir import kb_dirname


def store_path(vault_root: Path) -> Path:
    return vault_root / kb_dirname() / ".deferred-index.sqlite"


def _connect(vault_root: Path, *, create: bool) -> sqlite3.Connection:
    path = store_path(vault_root)
    if not create:
        return sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
        )
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


@dataclass(frozen=True, slots=True)
class DeferredReceipt:
    rel_path: str
    revision: int


class EmbeddingFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNVERIFIABLE = "unverifiable"


def add(vault_root: Path, rel_paths: list[str]) -> int:
    _receipts, added = _add_receipts(vault_root, rel_paths)
    return added


def add_receipts(vault_root: Path, rel_paths: list[str]) -> list[DeferredReceipt]:
    receipts, _added = _add_receipts(vault_root, rel_paths)
    return receipts


def _add_receipts(
    vault_root: Path, rel_paths: list[str]
) -> tuple[list[DeferredReceipt], int]:
    rels = sorted({rel.replace("\\", "/") for rel in rel_paths if rel.endswith(".md")})
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
    rels = sorted({rel.replace("\\", "/") for rel in rel_paths if rel.endswith(".md")})
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
    return _list_paths(vault_root, table="semantic_upserts", limit=limit)


def list_full_paths(vault_root: Path, *, limit: int | None = None) -> list[str]:
    return _list_paths(vault_root, table="full_upserts", limit=limit)


def snapshot(
    vault_root: Path, *, limit: int | None = None
) -> list[DeferredReceipt]:
    path = store_path(vault_root)
    if not path.exists():
        return []
    conn = _connect(vault_root, create=False)
    try:
        sql = "SELECT rel_path, revision FROM semantic_upserts ORDER BY rel_path"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(0, limit),)
        return [
            DeferredReceipt(str(row[0]), int(row[1]))
            for row in conn.execute(sql, params).fetchall()
        ]
    finally:
        conn.close()


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
