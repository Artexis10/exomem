"""What each principal last saw of each topic brief, kept so the next read can say what changed."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import sidecar_store, state_paths

SIDECAR_NAME = ".brief-watermarks.sqlite"
# Resource bounds of the brief-watermark contract, not vault vocabulary.
MAX_ROWS = 10_000
RETENTION_DAYS = 400
TIMEOUT_SECONDS = 0.05
_SCHEMA = """CREATE TABLE IF NOT EXISTS brief_watermarks (
    principal TEXT NOT NULL, seed TEXT NOT NULL, members TEXT NOT NULL, units TEXT NOT NULL,
    read_at REAL NOT NULL, PRIMARY KEY (principal, seed)
) WITHOUT ROWID"""


class WatermarkUnavailable(Exception):
    """The sidecar could not be read, so "first read" would be a guess."""


@dataclass(frozen=True, slots=True)
class Watermark:
    """The brief a principal last read to its end: member ref to content hash, unit ref to unit hash."""

    members: dict[str, str]
    units: dict[str, str]
    read_at: float


def sidecar_path(root: Path) -> Path:
    return state_paths.vault_state_dir(root) / SIDECAR_NAME


def _connect(root: Path) -> sqlite3.Connection:
    path = sidecar_path(root)
    sidecar_store.ensure_sidecar_parent(path)
    new = not path.exists()
    connection = sqlite3.connect(path, timeout=TIMEOUT_SECONDS)
    try:
        if new:
            connection.execute("PRAGMA journal_mode=WAL")
        # A watermark lost on power loss reads as a first read; a read never waits on a FULL fsync.
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(_SCHEMA)
    except BaseException:
        connection.close()
        raise
    return connection


def read(root: Path, principal: str, seed: str, *, now: float | None = None) -> Watermark | None:
    """The principal's watermark for `seed`, or None for a first read (none kept, expired or evicted)."""
    path = sidecar_path(root)
    if not path.is_file():
        return None
    cutoff = (time.time() if now is None else now) - RETENTION_DAYS * 86400
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=TIMEOUT_SECONDS)) as connection:
            row = connection.execute(
                "SELECT members, units, read_at FROM brief_watermarks WHERE principal = ? AND seed = ?",
                (principal, seed),
            ).fetchone()
    except (OSError, sqlite3.Error) as error:
        raise WatermarkUnavailable from error
    if row is None or row[2] < cutoff:
        return None
    try:
        return Watermark(json.loads(row[0]), json.loads(row[1]), row[2])
    except ValueError as error:
        raise WatermarkUnavailable from error


def write(
    root: Path, principal: str, seed: str, members: dict[str, str], units: dict[str, str],
    *, now: float | None = None,
) -> bool:
    """Advance the principal's watermark to the brief just served; False when it could not be kept."""
    try:
        with closing(_connect(root)) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO brief_watermarks VALUES (?, ?, ?, ?, ?)",
                (principal, seed, json.dumps(members, sort_keys=True), json.dumps(units, sort_keys=True),
                 time.time() if now is None else now),
            )
            connection.execute(
                "DELETE FROM brief_watermarks WHERE (principal, seed) IN "
                "(SELECT principal, seed FROM brief_watermarks ORDER BY read_at DESC LIMIT -1 OFFSET ?)",
                (MAX_ROWS,),
            )
        return True
    except (OSError, sqlite3.Error):
        return False


def sweep(root: Path, *, now: float | None = None) -> int:
    """Maintenance removes watermarks past their retention; `write` keeps the row bound."""
    if not sidecar_path(root).is_file():
        return 0
    cutoff = (time.time() if now is None else now) - RETENTION_DAYS * 86400
    with closing(_connect(root)) as connection, connection:
        before = connection.total_changes
        connection.execute("DELETE FROM brief_watermarks WHERE read_at < ?", (cutoff,))
        return connection.total_changes - before
