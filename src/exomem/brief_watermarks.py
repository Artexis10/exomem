"""What each reader's view last saw of each topic brief, kept so the next read can say what changed."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import sidecar_store, state_paths

SIDECAR_NAME = ".brief-watermarks.sqlite"
# Resource bounds of the brief-watermark contract, not vault vocabulary.
MAX_ROWS = 10_000
RETENTION_DAYS = 400
TIMEOUT_SECONDS = 0.05
#: Unit fingerprints one row keeps. A brief serves at most 40 notes and a note rarely holds more than a dozen served
#: units, so a row is cut only by an unusual brief; the cut is kept as a flag and never read as an empty set.
MAX_UNITS = 500
#: Hex digits kept of a member's content hash and of a unit ref's digest: a stored value only has to tell two states of one
#: page apart, and a collision costs one missed change in 2^64 (member) or one missed new unit in 2^48 (unit).
MEMBER_DIGITS = 16
UNIT_DIGITS = 12
_SCHEMA = """CREATE TABLE IF NOT EXISTS brief_watermarks (
    view TEXT NOT NULL, seed TEXT NOT NULL, members TEXT NOT NULL, units TEXT NOT NULL,
    units_cut INTEGER NOT NULL, read_at REAL NOT NULL, PRIMARY KEY (view, seed)
) WITHOUT ROWID"""


class WatermarkUnavailable(Exception):
    """The sidecar could not be read, so "first read" would be a guess."""


@dataclass(frozen=True, slots=True)
class Watermark:
    """The brief a view last read to its end: member ref to a short content hash, and a short digest of each unit ref."""

    members: dict[str, str]
    units: frozenset[str]
    #: False when the brief served more units than a row keeps, so a unit missing from `units` may be an old one.
    units_exact: bool
    read_at: float


def member_digest(content_hash: str) -> str:
    """The stored form of a member's content hash."""
    return content_hash[:MEMBER_DIGITS]


def unit_digest(unit_ref: str) -> str:
    """The stored form of a served unit's ref."""
    return hashlib.sha256(unit_ref.encode("utf-8")).hexdigest()[:UNIT_DIGITS]


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


def read(root: Path, view: str, seed: str, *, now: float | None = None) -> Watermark | None:
    """The watermark of `view` for `seed`, or None for a first read (none kept, expired or evicted)."""
    path = sidecar_path(root)
    if not path.is_file():
        return None
    cutoff = (time.time() if now is None else now) - RETENTION_DAYS * 86400
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=TIMEOUT_SECONDS)) as connection:
            row = connection.execute(
                "SELECT members, units, units_cut, read_at FROM brief_watermarks WHERE view = ? AND seed = ?",
                (view, seed),
            ).fetchone()
    except (OSError, sqlite3.Error) as error:
        raise WatermarkUnavailable from error
    if row is None or row[3] < cutoff:
        return None
    try:
        return Watermark(json.loads(row[0]), frozenset(json.loads(row[1])), not row[2], row[3])
    except ValueError as error:
        raise WatermarkUnavailable from error


def write(
    root: Path, view: str, seed: str, members: Mapping[str, str], unit_refs: Iterable[str],
    *, now: float | None = None,
) -> bool:
    """Advance the view's watermark to the brief just served; False when it could not be kept.

    `members` maps each member ref to its full content hash. A row keeps at most `MAX_UNITS` unit digests.
    """
    kept = list(dict.fromkeys(unit_digest(ref) for ref in unit_refs))
    cut = len(kept) > MAX_UNITS
    try:
        with closing(_connect(root)) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO brief_watermarks VALUES (?, ?, ?, ?, ?, ?)",
                (view, seed, json.dumps({ref: member_digest(value) for ref, value in members.items()}, sort_keys=True),
                 json.dumps(kept[:MAX_UNITS]), int(cut), time.time() if now is None else now),
            )
            connection.execute(
                "DELETE FROM brief_watermarks WHERE (view, seed) IN "
                "(SELECT view, seed FROM brief_watermarks ORDER BY read_at DESC LIMIT -1 OFFSET ?)",
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
