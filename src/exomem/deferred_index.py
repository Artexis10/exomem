"""Durable registry for deferred semantic-index paths."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .kbdir import kb_dirname


def store_path(vault_root: Path) -> Path:
    return vault_root / kb_dirname() / ".deferred-index.sqlite"


def _connect(vault_root: Path, *, create: bool) -> sqlite3.Connection:
    path = store_path(vault_root)
    if not create:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_upserts (
            rel_path TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
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
    return conn


def add(vault_root: Path, rel_paths: list[str]) -> int:
    return _add(vault_root, rel_paths, table="semantic_upserts")


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
