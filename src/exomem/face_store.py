"""The account's face store: two SQLite databases in one owner-only directory under its state directory.

The face index holds what a re-scan recomputes: the faces found in each picture and keyframe, the pages
scanned, and the account's face setting with its consent record and generation. Its registry family is
rebuildable, so no backup carries it. A restored account therefore keeps faces off until the owner opts in
again.

The people store holds what only the owner created: groups with their centroids, people and their names,
group-to-person links, faces moved out of a group, and threshold overrides. Its family is not rebuildable,
so backups keep it and a restore keeps names.

Both run with `secure_delete` on, so a removed row's bytes are overwritten in the file. Reading the
setting never creates the store: an account that never turned faces on has no face directory at all.
"""

from __future__ import annotations

import secrets
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import private_state, reserved_paths, state_paths

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_setting (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    consent_at TEXT,
    consent_surface TEXT,
    epoch TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY,
    page TEXT NOT NULL,
    frame_ts REAL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    width REAL NOT NULL,
    height REAL NOT NULL,
    score REAL NOT NULL,
    model_id TEXT NOT NULL,
    vector BLOB NOT NULL,
    group_id INTEGER
);
CREATE INDEX IF NOT EXISTS faces_by_page ON faces (page);
CREATE INDEX IF NOT EXISTS faces_by_group ON faces (group_id, page);
CREATE TABLE IF NOT EXISTS scanned_pages (
    page TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    face_count INTEGER NOT NULL
);
"""

_PEOPLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    folded_name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    model_id TEXT NOT NULL,
    centroid_sum BLOB NOT NULL,
    face_count INTEGER NOT NULL,
    person_id INTEGER REFERENCES people (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS groups_by_person ON groups (person_id);
CREATE TABLE IF NOT EXISTS exclusions (
    group_id INTEGER NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    page TEXT NOT NULL,
    frame_ts REAL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    width REAL NOT NULL,
    height REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS exclusions_by_group ON exclusions (group_id, page);
CREATE TABLE IF NOT EXISTS threshold_overrides (
    name TEXT PRIMARY KEY,
    value REAL NOT NULL
);
"""
# An exclusion names its face by page, keyframe and box, not by face row id: the face index is rebuilt with
# new ids, and a moved face must still never rejoin its old group.


@dataclass(frozen=True)
class FaceSetting:
    """The account's face setting as the face index records it."""

    enabled: bool
    consent_at: str | None
    consent_surface: str | None
    #: Changes whenever faces, groups or names change; a new face index never repeats an old value.
    generation: str


def store_dir(vault_root: Path) -> Path:
    """The face store's directory. Pure: it creates nothing."""
    return state_paths.vault_state_dir(vault_root) / reserved_paths.FACES_DIRNAME


def index_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(vault_root) / reserved_paths.FACE_INDEX_PATH


def people_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(vault_root) / reserved_paths.FACE_PEOPLE_PATH


def _open(path: Path, schema: str, *, synchronous: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA synchronous={synchronous}")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        # A store that a newer release wrote may hold rows this one would misread or drop.
        if version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"{path.name} has schema version {version}; this release reads {SCHEMA_VERSION}")
        with connection:
            connection.executescript(schema)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    except BaseException:
        connection.close()
        raise
    return connection


def connect_index(vault_root: Path) -> sqlite3.Connection:
    """Open the face index, creating the owner-only store and the schema when absent."""
    private_state.store(vault_root, reserved_paths.FACES_DIRNAME)
    # A lost last transaction on power loss costs a re-scan of one picture.
    connection = _open(index_path(vault_root), _INDEX_SCHEMA, synchronous="NORMAL")
    with connection:
        connection.execute("INSERT OR IGNORE INTO face_setting (id, epoch) VALUES (1, ?)", (secrets.token_hex(8),))
    return connection


def connect_people(vault_root: Path) -> sqlite3.Connection:
    """Open the people store, creating the owner-only store and the schema when absent."""
    private_state.store(vault_root, reserved_paths.FACES_DIRNAME)
    # Names and corrections exist nowhere else, so each commit waits for the disk.
    return _open(people_path(vault_root), _PEOPLE_SCHEMA, synchronous="FULL")


def read_setting(vault_root: Path) -> FaceSetting | None:
    """The account's face setting, or None when the face index does not exist. It never creates the store."""
    path = index_path(vault_root)
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)) as connection:
        row = connection.execute(
            "SELECT enabled, consent_at, consent_surface, epoch, generation FROM face_setting WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    enabled, consent_at, consent_surface, epoch, generation = row
    return FaceSetting(bool(enabled), consent_at, consent_surface, f"{epoch}:{generation}")


def enabled(vault_root: Path) -> bool:
    """Whether the owner turned faces on. False when the store does not exist; it never creates one."""
    setting = read_setting(vault_root)
    return setting is not None and setting.enabled


def advance_generation(index: sqlite3.Connection) -> None:
    """Record a change to faces, groups or names, inside the caller's transaction on the face index."""
    index.execute("UPDATE face_setting SET generation = generation + 1 WHERE id = 1")


def fold_name(name: str) -> str:
    """The key under which two person names are the same: Unicode NFKC case folding."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())


def pack_vector(vector: np.ndarray) -> bytes:
    """A face vector or centroid sum as stored: little-endian float32."""
    return np.asarray(vector, dtype="<f4").tobytes()


def unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")
