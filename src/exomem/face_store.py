"""The account's face store: two SQLite databases in one owner-only directory under its state directory.

The face index holds what a re-scan recomputes: the faces found in each picture and keyframe, the groups
that no person names, the pages scanned, and the account's face setting with its consent record and
generation. Its registry family is rebuildable, so no backup carries it. A restored account therefore keeps
faces off until the owner opts in again.

The people store holds what only the owner created: people and their names, the groups linked to a person
with their centroids, faces moved out of those groups, and threshold overrides. Its family is not
rebuildable, so backups keep it and a restore keeps names. A group moves into the people store when the
owner names it and back when the name is cleared, keeping its id: a one-face unnamed group's centroid is
that face's embedding, so a stranger's face never reaches a backup.

Both run with `secure_delete` on, so a removed row's bytes are overwritten in the file. Reading the
setting never creates the store: an account that never turned faces on has no face directory at all.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import private_state, reserved_paths, state_paths

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

#: Groups and the faces moved out of them, in either store: the face index holds unnamed groups, the people
#: store named ones. A group keeps its id when it moves, so face rows keep pointing at it.
_GROUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    model_id TEXT NOT NULL,
    centroid_sum BLOB NOT NULL,
    face_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS groups_by_size ON groups (face_count DESC, id);
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
CREATE INDEX IF NOT EXISTS exclusions_by_page ON exclusions (page);
"""

_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_setting (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    consent_at TEXT,
    consent_surface TEXT,
    unavailable TEXT,
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
    face_count INTEGER NOT NULL,
    -- The picture's modification time when it was scanned; a changed original drops its faces.
    mtime REAL
);
""" + _GROUP_SCHEMA

_PEOPLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    folded_name TEXT NOT NULL UNIQUE
);
-- A named group: the person it belongs to. Unnamed groups live in the face index without this table.
CREATE TABLE IF NOT EXISTS group_people (
    group_id INTEGER PRIMARY KEY REFERENCES groups (id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES people (id)
);
CREATE INDEX IF NOT EXISTS group_people_by_person ON group_people (person_id);
CREATE TABLE IF NOT EXISTS threshold_overrides (
    name TEXT PRIMARY KEY,
    value REAL NOT NULL
);
""" + _GROUP_SCHEMA
# An exclusion names its face by page, keyframe and box, not by face row id: the face index is rebuilt with
# new ids, and a moved face must still never rejoin its old group.


@dataclass(frozen=True)
class FaceSetting:
    """The account's face setting as the face index records it."""

    enabled: bool
    consent_at: str | None
    consent_surface: str | None
    #: Why the face models cannot run on this host, as the face step last found; None when they ran.
    unavailable: str | None
    #: Changes whenever faces, groups or names change; a new face index never repeats an old value.
    generation: str


def store_dir(vault_root: Path) -> Path:
    """The face store's directory. Pure: it creates nothing."""
    return state_paths.vault_state_dir(vault_root) / reserved_paths.FACES_DIRNAME


def index_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(vault_root) / reserved_paths.FACE_INDEX_PATH


def people_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(vault_root) / reserved_paths.FACE_PEOPLE_PATH


class NewerStore(sqlite3.DatabaseError):
    """A face store that a newer release wrote: it may hold rows this one would misread or drop."""


def is_busy(error: sqlite3.Error) -> bool:
    """Whether `error` means another connection holds the store: it is readable, and a retry can succeed."""
    # `sqlite_errorcode` is the extended result code (such as SQLITE_BUSY_SNAPSHOT); its low byte is the primary.
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _readable_version(connection: sqlite3.Connection, path: Path) -> int:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise NewerStore(f"{path.name} has schema version {version}; this release reads {SCHEMA_VERSION}")
    return version


def _open(path: Path, schema: str, *, synchronous: str, create: bool, shared: bool) -> sqlite3.Connection:
    # Without `create`, a missing database raises `sqlite3.OperationalError` instead of starting an empty one.
    # A `shared` connection serves the threads that take turns under the caller's lock.
    target, uri = (path, False) if create else (path.as_uri() + "?mode=rw", True)
    connection = sqlite3.connect(target, uri=uri, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=not shared)
    try:
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA synchronous={synchronous}")
        version = _readable_version(connection, path)
        if version == SCHEMA_VERSION:
            return connection  # The schema is in place; writing it again would cost a synced transaction.
        with connection:
            connection.executescript(schema)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    except BaseException:
        connection.close()
        raise
    return connection


def connect_index(vault_root: Path, *, create: bool = True, shared: bool = False) -> sqlite3.Connection:
    """Open the face index, creating the owner-only store and the schema when absent.

    With `create=False` a missing face index raises `sqlite3.OperationalError`: face work in flight must not
    bring back a store that the owner deleted.
    """
    if create:
        private_state.store(vault_root, reserved_paths.FACES_DIRNAME)
    # A lost last transaction on power loss costs a re-scan of one picture.
    connection = _open(index_path(vault_root), _INDEX_SCHEMA, synchronous="NORMAL", create=create, shared=shared)
    with connection:
        connection.execute("INSERT OR IGNORE INTO face_setting (id, epoch) VALUES (1, ?)", (secrets.token_hex(8),))
    return connection


def connect_people(vault_root: Path, *, create: bool = True, shared: bool = False) -> sqlite3.Connection:
    """Open the people store, creating the owner-only store and the schema when absent (see `connect_index`)."""
    if create:
        private_state.store(vault_root, reserved_paths.FACES_DIRNAME)
    # Names and corrections exist nowhere else, so each commit waits for the disk.
    return _open(people_path(vault_root), _PEOPLE_SCHEMA, synchronous="FULL", create=create, shared=shared)


def read_setting(vault_root: Path) -> FaceSetting | None:
    """The account's face setting, or None when the face index does not exist. It never creates the store."""
    path = index_path(vault_root)
    if not path.is_file():
        return None
    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT enabled, consent_at, consent_surface, unavailable, epoch, generation FROM face_setting WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    enabled, consent_at, consent_surface, unavailable, epoch, generation = row
    return FaceSetting(bool(enabled), consent_at, consent_surface, unavailable, f"{epoch}:{generation}")


def enabled(vault_root: Path) -> bool:
    """Whether the owner turned faces on. False when the store does not exist; it never creates one."""
    setting = read_setting(vault_root)
    return setting is not None and setting.enabled


#: The deployment setting that lets a Cloud or hosted deployment run faces. Its operator sets it to `1`.
HOSTED_SETTING = "EXOMEM_FACES_HOSTED"


def deployment_refusal() -> str | None:
    """Why this deployment does not run faces, or None when it may.

    A self-hosted deployment may. A Cloud or hosted deployment may only when its operator set
    `EXOMEM_FACES_HOSTED=1`. Turn-on, status, the face step and the `people` restriction all ask here.
    """
    from . import cloud_cell

    if not (cloud_cell.cloud_mode_enabled() or cloud_cell.hosted_mode_enabled()):
        return None
    if os.environ.get(HOSTED_SETTING, "").strip() == "1":
        return None
    return "the operator has not enabled faces on this deployment"


def active(vault_root: Path) -> bool:
    """Whether the face step runs here now: the owner turned faces on and the deployment allows them.

    A face index that cannot be read counts as off here, so a face failure never stops a picture's CLIP
    step; `faces.status` names the damage and the fix.
    """
    try:
        on = enabled(vault_root)
    except (sqlite3.Error, OSError) as error:
        log.warning("faces: the face index cannot be read, so the face step is skipped: %s", error)
        return False
    return on and deployment_refusal() is None


def unavailable_reason(setting: FaceSetting | None) -> str | None:
    """Why faces cannot run here: the deployment's refusal, else the face step's last model failure."""
    return deployment_refusal() or (setting.unavailable if setting is not None else None)


def advance_generation(index: sqlite3.Connection) -> str:
    """Record a change to faces, groups or names, inside the caller's transaction on the face index.

    Returns the new generation, in the form `FaceSetting.generation` reports.
    """
    index.execute("UPDATE face_setting SET generation = generation + 1 WHERE id = 1")
    return current_generation(index)


def current_generation(index: sqlite3.Connection) -> str:
    """The face index's generation as `FaceSetting.generation` reports it, read on the caller's connection."""
    epoch, generation = index.execute("SELECT epoch, generation FROM face_setting WHERE id = 1").fetchone()
    return f"{epoch}:{generation}"


def record_opt_in(index: sqlite3.Connection, *, at: str, surface: str) -> None:
    """Turn faces on with the owner's consent record. The caller has already shown the disclosure."""
    with index:
        index.execute(
            "UPDATE face_setting SET enabled = 1, consent_at = ?, consent_surface = ? WHERE id = 1", (at, surface)
        )
        advance_generation(index)


def record_opt_out(index: sqlite3.Connection) -> None:
    """Turn faces off. Stored faces, groups, names and the consent record stay."""
    with index:
        index.execute("UPDATE face_setting SET enabled = 0 WHERE id = 1")
        advance_generation(index)


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open one face store database for reading; it never creates the file or its directory.

    Raises `sqlite3.DatabaseError` when the file is not a database this release reads.
    """
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        _readable_version(connection, path)
    except BaseException:
        connection.close()
        raise
    return connection


def record_availability(index: sqlite3.Connection, reason: str | None) -> None:
    """Record why the face models cannot run here, or None once they ran, for `faces.status`."""
    with index:
        index.execute("UPDATE face_setting SET unavailable = ? WHERE id = 1", (reason,))


def fold_name(name: str) -> str:
    """The key under which two person names are the same: Unicode NFKC case folding."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())


def pack_vector(vector: np.ndarray) -> bytes:
    """A face vector or centroid sum as stored: little-endian float32."""
    return np.asarray(vector, dtype="<f4").tobytes()


def unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


def remove_store(vault_root: Path) -> list[str]:
    """Remove every file of the face store, then its directory. Returns what remains, by state-relative name.

    The caller has closed every connection. The face store's directory holds nothing but its two databases
    and their `-wal`, `-shm` and `-journal` files, so everything in it goes. A file that cannot be removed
    stays listed, and a repeat removes it.
    """
    directory = store_dir(vault_root)
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return []
    remaining = []
    for entry in entries:
        try:
            entry.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            remaining.append(f"{reserved_paths.FACES_DIRNAME}/{entry.name}")
    try:
        directory.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        remaining.append(reserved_paths.FACES_DIRNAME)
    return remaining


_DELETION_RECEIPT = "last"


def deletion_receipt_path(vault_root: Path) -> Path:
    """Where the last delete's receipt lives, outside the face store so the delete keeps it."""
    return state_paths.vault_state_dir(vault_root) / reserved_paths.FACE_DELETION_DIRNAME / f"{_DELETION_RECEIPT}.json"


def write_deletion_receipt(vault_root: Path, receipt: dict) -> None:
    """Keep the last delete's receipt (time, outcome, counts, what remains) for `faces.status`. No face data."""
    store = private_state.store(vault_root, reserved_paths.FACE_DELETION_DIRNAME)
    private_state.write_record(store, _DELETION_RECEIPT, receipt, temp_suffix=".tmp", durable=True)
    private_state.sync_directory(store)


def read_deletion_receipt(vault_root: Path) -> dict | None:
    """The last delete's receipt, or None when faces were never deleted here or it cannot be read."""
    try:
        receipt = json.loads(deletion_receipt_path(vault_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return receipt if isinstance(receipt, dict) else None
