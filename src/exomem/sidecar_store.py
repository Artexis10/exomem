"""SQLite sidecar lifecycle helpers shared by derived indexes."""

from __future__ import annotations

import logging
import random
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

INSTANCE_MIN = 1
INSTANCE_MAX = 2**31 - 1


def apply_sidecar_pragmas(conn: sqlite3.Connection) -> None:
    """Apply WAL-oriented pragmas for local per-machine sidecars."""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error as e:  # pragma: no cover - WAL unavailable on unusual FSes
        log.warning("sidecar WAL pragmas failed (%s); continuing on default journal", e)


def file_block(keys: list[str], rel_path: str) -> tuple[int, int]:
    """Locate `rel_path`'s contiguous row block in a sorted key list."""
    lo = hi = None
    for i, k in enumerate(keys):
        if k == rel_path:
            if lo is None:
                lo = i
            hi = i + 1
        elif hi is not None:
            break
    if lo is not None:
        return lo, hi
    ins = len(keys)
    for i, k in enumerate(keys):
        if k > rel_path:
            ins = i
            break
    return ins, ins


def ensure_meta_table(
    conn: sqlite3.Connection, data_table: str, sidecar_name: str
) -> None:
    """Create the generation-token table if absent."""
    existed = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        is not None
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER)")
    if not existed:
        instance = random.SystemRandom().randint(INSTANCE_MIN, INSTANCE_MAX)
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('instance', ?)",
            (instance,),
        )
        conn.commit()
        has_rows = (
            conn.execute(f"SELECT 1 FROM {data_table} LIMIT 1").fetchone() is not None
        )
        if has_rows:
            log.info(
                "%s: created generation-meta over an existing non-empty sidecar "
                "(legacy migration; mtime-keyed cache until the first gen-bumping write)",
                sidecar_name,
            )


def read_meta_token(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Return `(epoch, generation, instance)` from a sidecar meta table."""
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('epoch', 'generation', 'instance')"
    ).fetchall()
    d = {k: v for k, v in rows}
    return (
        int(d.get("epoch") or 0),
        int(d.get("generation") or 0),
        int(d.get("instance") or 0),
    )


def bump_meta(conn: sqlite3.Connection, key: str) -> int:
    """Increment `meta[key]` inside the caller's open write transaction."""
    cur = conn.execute("UPDATE meta SET value = value + 1 WHERE key = ?", (key,))
    if cur.rowcount == 0:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, 1)", (key,))
    return int(
        conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()[0]
    )


def reload_reason(old_cache, new_epoch: int, new_gen: int) -> str:
    """Observability tag for a full matrix load: cold|legacy|epoch|genuine."""
    if old_cache is None:
        return "cold"
    if new_gen == 0:
        return "legacy"
    if new_epoch != old_cache.epoch:
        return "epoch"
    return "genuine"


def peek_sidecar_token(path: Path) -> tuple[int, int, int] | None:
    """Read `(epoch, generation, instance)` without creating or migrating a sidecar."""
    if not path.exists():
        return (0, 0, 0)
    try:
        conn = sqlite3.connect(path)
        try:
            has_meta = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
                ).fetchone()
                is not None
            )
            return read_meta_token(conn) if has_meta else (0, 0, 0)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def sidecar_cache_token(path: Path) -> tuple[int, int, int]:
    """Read-only cache token for hot-cache freshness keys."""
    return peek_sidecar_token(path) or (0, 0, 0)


def cache_is_fresh(c, path: Path, epoch: int, gen: int, instance: int) -> bool:
    """Return whether a warm matrix cache can serve the current sidecar token."""
    if gen >= 1:
        return c.generation == gen and c.epoch == epoch and c.instance == instance
    try:
        return c.mtime == path.stat().st_mtime
    except OSError:
        return False


def try_serve_cached(c, path: Path):
    """Return `c` if it can serve the current sidecar token, else None."""
    token = peek_sidecar_token(path)
    if token is None:
        if c is not None:
            log.warning(
                "sidecar token read failed for %s; serving the warm cache", path
            )
        return c
    if c is not None and cache_is_fresh(c, path, *token):
        return c
    return None
