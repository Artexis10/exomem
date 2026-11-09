"""Background verification of stored originals (OpenSpec change `verify-stored-originals`).

A stored original is a file that a binding names with its path and SHA-256: a sidecar's
`binary_sha256`, a member manifest's pool blob, or the text that an imported Source page
fences under its `original_sha256`. Nothing read those bytes again after the write, so a
corrupted, edited or deleted original went unnoticed until someone opened it.

One daemon thread walks the bindings into this module's table at start and then once a day,
and runs a pass every hour. A pass stats every known original, hashes at once a file whose
identity changed, and re-hashes the rest, oldest verification first, within a byte budget and
under a read-rate cap. A damaged original is restored only from a copy whose bytes hash to its
binding; its changed bytes go to Trash. A missing original and a damaged text original are
reported and never restored.

The table is private state of the vault, keyed by vault path and payload SHA-256. It never
holds the expected hash: that always comes from the binding, so a lost table costs only
verification times, and the next walk rebuilds it.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import stat
import threading
import time
import zlib
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from . import (
    foreground_priority,
    held_fs,
    originals,
    private_state,
    reserved_paths,
    state_paths,
    trash_metadata,
)
from .cli_ops import OpError
from .import_stage import Identity

log = logging.getLogger(__name__)

STORE_DIRNAME = "original-fixity"
TABLE_NAME = "fixity.sqlite"
SCHEMA_VERSION = 1

# Row states and causes are the closed protocol values of spec original-fixity, not vault vocabulary.
UNVERIFIED = "unverified"
VERIFIED = "verified"
RESTORED = "restored"
MISSING = "missing"
CORRUPT_NO_COPY = "corrupt_no_copy"
EVICTED = "evicted"
EDITED = "edited"  # the file's identity changed since its last verification
CORRUPTED = "corrupted"  # the identity is the recorded one, so no write explains the change
UNKNOWN_CAUSE = "unknown"  # no identity was recorded before the change, as after a table rebuild

# How a row's payload is read: a whole file, a gzip pool blob, or the text a Source page fences.
# A closed set this module implements, one reader each.
FILE = "file"
BLOB = "blob"
TEXT = "text"

# Where a matching copy lives, in the order a restore tries them (design D5). Closed protocol tokens.
MEDIA_STORE = "media_store"
POOL_BLOB = "pool_blob"
STORED_ORIGINAL = "stored_original"
LOCAL_IMPORT = "local_import"
IMPORT_AGAIN = "import_again"  # a text original's outside file, which only a new import brings back
# The copies whose path is a vault path; the others name a file outside the vault.
_VAULT_COPIES = (MEDIA_STORE, POOL_BLOB, STORED_ORIGINAL)

# The readiness block's states: a count after a pass, none before one, or none because it is off.
SIGNAL_MEASURED = "measured"
SIGNAL_UNKNOWN = "unknown"
SIGNAL_OFF = "off"

# The states that need the owner's action; the Cloud readiness count holds exactly these (D9).
UNRESOLVED = (MISSING, CORRUPT_NO_COPY)
# The states the owner's attention lists; a restore is reported but needs no action.
REPORTED = (RESTORED, MISSING, CORRUPT_NO_COPY)
# Rows whose bytes may restore another row; a copy is hashed again before use.
_PRESENT = (UNVERIFIED, VERIFIED, RESTORED)
_STATES = (UNVERIFIED, VERIFIED, RESTORED, MISSING, CORRUPT_NO_COPY, EVICTED)

# Defaults until task 5.1 measures them; each has an environment setting (design D4).
DEFAULT_PASS_SECONDS = 3600.0
DEFAULT_CYCLE_DAYS = 30.0
DEFAULT_READ_MIB_PER_SECOND = 32.0
DEFAULT_WALK_SECONDS = 86400.0
#: The least a pass may re-hash, so a small vault still finishes its cycle.
BUDGET_FLOOR_BYTES = 64 * 1024**2
_CHUNK = 1024 * 1024
_SQLITE_TIMEOUT_SECONDS = 5.0


class _Undecodable(Exception):
    """A Source page that no longer fences its text in the format the import wrote."""


# Payload decoders raise these for bytes that no longer decode; no binding's hash can match them.
_UNDECODABLE = (_Undecodable, UnicodeError, EOFError, zlib.error, gzip.BadGzipFile)

_ROW_COLUMNS = "path, sha256, kind, binding, size, stored, identity, state, cause, route, verified_at"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS originals ("
    " path TEXT NOT NULL, sha256 TEXT NOT NULL, kind TEXT NOT NULL, binding TEXT NOT NULL,"
    " size INTEGER, stored INTEGER NOT NULL DEFAULT 0, identity TEXT, state TEXT NOT NULL,"
    " cause TEXT, route TEXT NOT NULL DEFAULT '[]', verified_at REAL, PRIMARY KEY (path, sha256))",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def disabled() -> bool:
    """True when the operator stopped fixity: a rollback lever, not a gate."""
    return bool(os.environ.get("EXOMEM_DISABLE_ORIGINAL_FIXITY"))


def _setting(name: str, default: float) -> float:
    """A positive number from the environment, or the default when unset or unusable."""
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def pass_seconds() -> float:
    return _setting("EXOMEM_ORIGINAL_FIXITY_PASS_SECONDS", DEFAULT_PASS_SECONDS)


def cycle_seconds() -> float:
    return _setting("EXOMEM_ORIGINAL_FIXITY_CYCLE_DAYS", DEFAULT_CYCLE_DAYS) * 86400.0


def read_rate() -> float:
    """The read-rate cap in bytes per second."""
    return _setting("EXOMEM_ORIGINAL_FIXITY_READ_MIB_PER_SECOND", DEFAULT_READ_MIB_PER_SECOND) * 1024**2


def walk_seconds() -> float:
    return _setting("EXOMEM_ORIGINAL_FIXITY_WALK_SECONDS", DEFAULT_WALK_SECONDS)


# --- the table ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    path: str  # vault-relative payload file; the Source page itself for a text original
    sha256: str  # the hash the binding names
    kind: str
    binding: str  # vault-relative sidecar, manifest or Source page that names the hash
    size: int | None  # payload bytes, from the binding or the last hash
    stored: bool  # the binding names a stored version, so absence may be eviction
    identity: Identity | None  # the file's stat identity when it was last hashed
    state: str
    cause: str | None
    route: list[dict[str, str]]
    verified_at: float | None

    @classmethod
    def of(cls, values: tuple) -> Row:
        path, sha256, kind, binding, size, stored, identity, state, cause, route, verified_at = values
        return cls(path, sha256, kind, binding, size, bool(stored),
                   Identity.read(json.loads(identity)) if identity else None,
                   state, cause, json.loads(route), verified_at)


def table_path(vault: Path) -> Path:
    """Where the table lives; this computes the path and creates nothing."""
    return state_paths.vault_state_dir(Path(vault)) / STORE_DIRNAME / TABLE_NAME


def _connect(vault: Path) -> sqlite3.Connection:
    """Open the table for writing; a table of another schema starts again, every row unverified."""
    path = private_state.store(vault, STORE_DIRNAME) / TABLE_NAME
    new = not path.exists()
    connection = sqlite3.connect(path, timeout=_SQLITE_TIMEOUT_SECONDS)
    try:
        if new:
            connection.execute("PRAGMA journal_mode=WAL")
        # A power loss can drop the last verification times, which costs one more hash and no wrong answer.
        connection.execute("PRAGMA synchronous=NORMAL")
        for statement in _SCHEMA:
            connection.execute(statement)
        found = connection.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if found is None or found[0] != json.dumps(SCHEMA_VERSION):
            with connection:
                connection.execute("DELETE FROM originals")
                connection.execute("DELETE FROM meta")
                _set_meta(connection, schema=SCHEMA_VERSION)
    except BaseException:
        connection.close()
        raise
    return connection


def _reader(vault: Path) -> sqlite3.Connection | None:
    """A read-only connection, or None when no table of this schema exists. It creates nothing."""
    path = table_path(vault)
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=_SQLITE_TIMEOUT_SECONDS)
    try:
        found = connection.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.OperationalError:
        found = None  # a table file this version never finished creating
    if found is None or found[0] != json.dumps(SCHEMA_VERSION):
        connection.close()
        return None
    return connection


def _meta(connection: sqlite3.Connection) -> dict[str, Any]:
    return {key: json.loads(value) for key, value in connection.execute("SELECT key, value FROM meta")}


def _set_meta(connection: sqlite3.Connection, **values: object) -> None:
    connection.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [(key, json.dumps(value)) for key, value in values.items()])


def _rows(vault: Path) -> list[Row]:
    with closing(_connect(vault)) as connection:
        return [Row.of(values) for values in connection.execute(f"SELECT {_ROW_COLUMNS} FROM originals ORDER BY path")]


def _record(vault: Path, row: Row, *, state: str, identity: Identity | None, cause: str | None = None,
            route: list[dict[str, str]] | None = None, size: int | None = None, verified: bool = False) -> None:
    with closing(_connect(vault)) as connection, connection:
        connection.execute(
            "UPDATE originals SET state = ?, cause = ?, route = ?, identity = ?, size = COALESCE(?, size),"
            " verified_at = CASE WHEN ? THEN ? ELSE verified_at END WHERE path = ? AND sha256 = ?",
            (state, cause, json.dumps(route or []), json.dumps(identity.record()) if identity else None, size,
             verified, time.time(), row.path, row.sha256),
        )


def _retire(vault: Path, row: Row) -> None:
    """Drop a row whose binding no longer names its hash; the next walk adds what it names now."""
    with closing(_connect(vault)) as connection, connection:
        connection.execute("DELETE FROM originals WHERE path = ? AND sha256 = ?", (row.path, row.sha256))


# --- the binding walk --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Found:
    path: str
    sha256: str
    kind: str
    binding: str
    size: int | None
    stored: bool
    evicted: bool


def _bindings(vault: Path) -> Iterator[_Found]:
    """Every original a binding names: sidecars, member manifests, and fenced text originals.

    These are the walks that already exist; none reads a derived index.
    """
    from . import adopt, adoption_plan, archive_members, media_store
    from .backfill import iter_kb_files
    from .vault import kb_root

    for snapshot in media_store.bound_originals(vault):
        assert snapshot.binary_sha256 is not None  # bound_originals yields only hash-bearing bindings
        yield _Found(snapshot.path.relative_to(vault).as_posix(), snapshot.binary_sha256, FILE,
                     snapshot.sidecar_path.relative_to(vault).as_posix(), snapshot.binary_size,
                     snapshot.reference is not None, snapshot.state == "evicted")
    for candidate in iter_kb_files(kb_root(vault)):
        if not candidate.name.endswith(archive_members.MANIFEST_SUFFIX):
            continue
        relative = candidate.relative_to(vault).as_posix()
        manifest = archive_members.read_manifest(vault, relative)  # checks its binding and each member's shape
        if manifest is None:
            continue
        family = PurePosixPath(relative).parent
        for member in manifest.members:
            yield _Found((family / member["blob"]).as_posix(), member["sha256"], BLOB, relative, member["bytes"],
                         False, False)
    for entry in adoption_plan.read_import_index(vault):
        if not originals.is_sha256(entry.original_sha256):
            continue
        try:
            with reserved_paths.open_generic_binary(vault, entry.path, physical=True) as source:
                page = source.stream.read().decode("utf-8")
        except (UnicodeError, reserved_paths.ReservedPathLeafError):
            continue
        if adopt.text_source_body(page) is not None:  # an artifact Source has a sidecar binding instead
            yield _Found(entry.path, str(entry.original_sha256), TEXT, entry.path, None, False, False)


def walk(vault: Path) -> int:
    """Fill the table from the bindings and retire each row no binding names; return the count found.

    Nothing is written until every binding is read, so a walk that stops retires nothing.
    """
    vault = Path(vault).resolve()
    found: dict[tuple[str, str], _Found] = {}
    for item in foreground_priority.yielding(_bindings(vault)):
        found.setdefault((item.path, item.sha256), item)
    with closing(_connect(vault)) as connection, connection:
        known = set(connection.execute("SELECT path, sha256 FROM originals").fetchall())
        for key, item in found.items():
            if key in known:
                connection.execute(
                    "UPDATE originals SET kind = ?, binding = ?, stored = ?,"
                    " state = CASE WHEN ? THEN ? ELSE state END WHERE path = ? AND sha256 = ?",
                    (item.kind, item.binding, int(item.stored), item.evicted, EVICTED, *key))
            else:
                connection.execute(
                    "INSERT INTO originals(path, sha256, kind, binding, size, stored, state) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*key, item.kind, item.binding, item.size, int(item.stored),
                     EVICTED if item.evicted else UNVERIFIED))
        connection.executemany("DELETE FROM originals WHERE path = ? AND sha256 = ?", known - found.keys())
        _set_meta(connection, walk_at=time.time())
    return len(found)


def _still_bound(vault: Path, row: Row) -> bool:
    """Whether the row's binding still names its path and hash."""
    from . import adoption_plan, archive_members

    if row.kind == FILE:
        try:
            return originals.original_state(vault, row.path).binary_sha256 == row.sha256
        except (ValueError, reserved_paths.ReservedPathLeafError):
            return False
    if row.kind == BLOB:
        manifest = archive_members.read_manifest(vault, row.binding)
        family = PurePosixPath(row.binding).parent
        return manifest is not None and any(
            member["sha256"] == row.sha256 and (family / member["blob"]).as_posix() == row.path
            for member in manifest.members)
    entry = adoption_plan.read_import_entry(vault, vault / row.path)
    return entry is not None and entry.original_sha256 == row.sha256


# --- reading payloads --------------------------------------------------------------------------


class _Pace:
    """Keeps every read under the read-rate cap and yields to foreground work between chunks.

    Time spent elsewhere earns no burst: each chunk waits out its own share of the cap.
    """

    def __init__(self, rate: float) -> None:
        self.rate = rate
        self.due = 0.0

    def chunks(self, parts: Iterable[bytes]) -> Iterator[bytes]:
        for chunk in parts:
            yield chunk
            foreground_priority.yield_to_foreground()
            now = time.monotonic()
            self.due = max(self.due, now) + len(chunk) / self.rate
            foreground_priority.wait_for_retry(self.due - now)


class _Absent(Exception):
    """The payload file is not there."""


class _Unreadable(Exception):
    """The payload could not be read this pass; the row keeps its state until the next one."""


def _open(vault: Path, row: Row):
    # A blob or text path came from a walk of the disk, so it opens as spelled there; a sidecar path as bound.
    return reserved_paths.open_generic_binary(vault, row.path, physical=row.kind != FILE)


@contextmanager
def _held(vault: Path, row: Row) -> Iterator[reserved_paths.GenericBinarySource]:
    """The row's file, held; a missing file raises `_Absent`, any other refusal `_Unreadable`."""
    try:
        with _open(vault, row) as source:
            yield source
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            raise _Absent from None
        raise _Unreadable(error.code) from None


def _parts(row: Row, stream: BinaryIO) -> Iterator[bytes]:
    """The bytes the binding hashes: the file, a pool blob's uncompressed member, or the fenced text."""
    from . import adopt

    if row.kind == TEXT:
        body = adopt.text_source_body(stream.read().decode("utf-8"))
        if body is None:
            raise _Undecodable
        yield body.encode("utf-8")
        return
    reader: BinaryIO = gzip.GzipFile(fileobj=stream, mode="rb") if row.kind == BLOB else stream
    while chunk := reader.read(_CHUNK):
        yield chunk


@dataclass(frozen=True)
class _Hashed:
    sha256: str | None  # None when the bytes no longer decode, which no binding's hash matches
    size: int
    version: held_fs.FileVersion  # the held version, for the re-check under the guard
    identity: Identity  # the stat identity, compared with the presence check's stat


def _hash(vault: Path, row: Row, pace: _Pace) -> _Hashed:
    digest, size, decoded = hashlib.sha256(), 0, True
    with _held(vault, row) as source:
        # From the held descriptor's stat, the source that the presence check's `lstat` is compared with.
        identity = Identity.of(os.fstat(source.stream.fileno()))
        try:
            for chunk in pace.chunks(_parts(row, source.stream)):
                digest.update(chunk)
                size += len(chunk)
        except _UNDECODABLE:
            decoded = False
    return _Hashed(digest.hexdigest() if decoded else None, size, source.version, identity)


def _version(vault: Path, row: Row) -> held_fs.FileVersion:
    with _held(vault, row) as source:
        return source.version


# --- the pass ----------------------------------------------------------------------------------


@dataclass
class PassResult:
    budget_bytes: int = 0
    rehashed_bytes: int = 0  # unchanged originals re-hashed within the budget
    changed_bytes: int = 0  # changed originals, hashed outside the budget
    outcomes: dict[str, int] = field(default_factory=dict)  # rows that reached each state this pass
    unreadable: int = 0
    rescheduled: int = 0


def budget_for(rows: Iterable[Row], *, interval: float, cycle: float) -> int:
    """The re-hash budget of one pass: the local bytes times its share of the cycle, with a floor."""
    local = sum(_size(row) for row in rows if row.state not in (EVICTED, MISSING))
    return max(BUDGET_FLOOR_BYTES, int(local * interval / cycle))


def _size(row: Row) -> int:
    if row.size is not None:
        return row.size
    return row.identity.size if row.identity is not None else 0


def run_pass(vault: Path, *, budget: int | None = None, rate: float | None = None) -> PassResult:
    """Check every known original for presence and identity, then re-hash within the budget.

    `budget` and `rate` default to the configured ones; a caller passes them to bound one pass.
    """
    vault = Path(vault).resolve()
    rows = _rows(vault)
    with closing(_connect(vault)) as connection, connection:
        credit = int(_meta(connection).get("credit", 0))
        _set_meta(connection, pass_started_at=time.time())
    pace = _Pace(rate if rate is not None else read_rate())
    result = PassResult()
    changed: list[Row] = []
    steady: list[Row] = []
    for row in foreground_priority.yielding(rows):
        try:
            info = os.lstat(vault / row.path)
        except FileNotFoundError:
            _absent(vault, row, result)
            continue
        except OSError:
            result.unreadable += 1
            continue
        if not stat.S_ISREG(info.st_mode):
            result.unreadable += 1
            continue
        if row.state == EVICTED:
            # The media store brought it back after checking its digest; it waits for its turn like any other.
            row = replace(row, state=UNVERIFIED)
            _record(vault, row, state=UNVERIFIED, identity=row.identity)
        if row.state == MISSING or (row.identity is not None and Identity.of(info) != row.identity):
            changed.append(row)
        else:
            steady.append(row)
    for row in changed:
        result.changed_bytes += _verify(vault, row, pace, result)
    result.budget_bytes = budget if budget is not None else budget_for(rows, interval=pass_seconds(),
                                                                        cycle=cycle_seconds())
    available, carried = result.budget_bytes + credit, 0
    for row in sorted(steady, key=lambda item: (item.state != UNVERIFIED, item.verified_at or 0.0, item.path)):
        if result.rehashed_bytes + _size(row) > available:
            # An original larger than one budget banks this pass's budget, so a later pass can afford it.
            carried = available if result.rehashed_bytes == 0 else 0
            break
        result.rehashed_bytes += _verify(vault, row, pace, result)
    with closing(_connect(vault)) as connection, connection:
        unresolved = connection.execute(
            f"SELECT COUNT(*) FROM originals WHERE state IN ({_marks(UNRESOLVED)})", UNRESOLVED).fetchone()[0]
        _set_meta(connection, credit=carried, pass_at=time.time(), unresolved=unresolved)
    return result


def _marks(values: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)


def _count(result: PassResult, state: str) -> None:
    result.outcomes[state] = result.outcomes.get(state, 0) + 1


def _absent(vault: Path, row: Row, result: PassResult) -> None:
    if row.state in (EVICTED, MISSING):
        _count(result, row.state)  # Reading an evicted original would download it, and the store holds it.
        return
    if not _still_bound(vault, row):
        _retire(vault, row)
        return
    if row.kind == FILE and originals.original_state(vault, row.path).state == "evicted":
        _record(vault, row, state=EVICTED, identity=row.identity)
        _count(result, EVICTED)
        return
    _record(vault, row, state=MISSING, identity=row.identity, route=_copies(vault, row))
    _count(result, MISSING)


def _verify(vault: Path, row: Row, pace: _Pace, result: PassResult) -> int:
    """Hash one original and act on the answer; return the payload bytes read."""
    try:
        hashed = _hash(vault, row, pace)
    except _Absent:
        _absent(vault, row, result)
        return 0
    except _Unreadable:
        result.unreadable += 1
        return 0
    if hashed.sha256 == row.sha256:
        _record(vault, row, state=VERIFIED, identity=hashed.identity, size=hashed.size, verified=True)
        _count(result, VERIFIED)
        return hashed.size
    cause = UNKNOWN_CAUSE if row.identity is None else (CORRUPTED if hashed.identity == row.identity else EDITED)
    confirmed = _confirm_damage(vault, row, hashed)
    if confirmed is None:
        result.rescheduled += 1
    elif not confirmed:
        _retire(vault, row)
    elif row.kind == TEXT:
        # A repair would write the Source page, which the append-only guard forbids (D10).
        _record(vault, row, state=CORRUPT_NO_COPY, identity=hashed.identity, cause=cause,
                route=_text_route(vault, row), verified=True)
        _count(result, CORRUPT_NO_COPY)
    else:
        _repair(vault, row, hashed, cause, pace, result)
    return hashed.size


def _guard(vault: Path, operation: str):
    from .writer_lease import get_manager

    return get_manager().mutation_guard(vault, operation=operation, holder_kind="background")


def _confirm_damage(vault: Path, row: Row, hashed: _Hashed) -> bool | None:
    """Under the vault guard: whether the hashed file is unchanged and its binding still names the hash.

    False when the binding moved on, so a governed write explains the change. None when the guard
    is busy or the file changed again; the next pass decides then.
    """
    try:
        with _guard(vault, "original_fixity_check"):
            if not _still_bound(vault, row):
                return False
            return True if _version(vault, row) == hashed.version else None
    except (OpError, _Absent, _Unreadable):
        return None


# --- restore -----------------------------------------------------------------------------------


class _Changed(Exception):
    """The file or its binding changed after the hash; nothing moves."""


def _copies(vault: Path, row: Row) -> list[dict[str, str]]:
    """The copies that may hold the row's bytes, in the order a restore tries them (D5). This reads none."""
    from . import local_import

    found: list[dict[str, str]] = []
    if row.kind == FILE and row.stored:
        snapshot = originals.original_state(vault, row.path)
        if snapshot.reference is not None and snapshot.binary_sha256 == row.sha256:
            found.append({"source": MEDIA_STORE, "path": row.path})
    with closing(_connect(vault)) as connection:
        others = connection.execute(
            f"SELECT kind, path FROM originals WHERE sha256 = ? AND path != ? AND kind IN (?, ?)"
            f" AND state IN ({_marks(_PRESENT)}) ORDER BY kind = ?, path",
            (row.sha256, row.path, BLOB, FILE, *_PRESENT, FILE)).fetchall()
    found.extend({"source": POOL_BLOB if kind == BLOB else STORED_ORIGINAL, "path": path} for kind, path in others)
    for outside, sha256, _size_then in local_import.recorded_files(vault):
        if sha256 == row.sha256 and outside.is_file():
            found.append({"source": LOCAL_IMPORT, "path": str(outside)})
    return found


def _text_route(vault: Path, row: Row) -> list[dict[str, str]]:
    from . import adoption_plan

    entry = adoption_plan.read_import_entry(vault, vault / row.path)
    return [{"source": IMPORT_AGAIN, "path": str(entry.imported_from)}] if entry is not None else []


def clear_staging(vault: Path) -> None:
    """Remove the staged copies that a stopped process left; only this module writes them."""
    folder = table_path(vault).parent
    for leftover in folder.glob("*.part") if folder.is_dir() else ():
        leftover.unlink(missing_ok=True)


@contextmanager
def _copy_parts(vault: Path, copy: dict[str, str]) -> Iterator[Iterable[bytes]]:
    """The uncompressed bytes of a vault or journal copy."""
    if copy["source"] == LOCAL_IMPORT:
        descriptor = os.open(copy["path"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as stream:
            yield iter(lambda: stream.read(_CHUNK), b"")
        return
    copy_row = Row(copy["path"], "", BLOB if copy["source"] == POOL_BLOB else FILE, "", None, False, None,
                   UNVERIFIED, None, [], None)
    with _open(vault, copy_row) as source:
        yield _parts(copy_row, source.stream)


def _stage(vault: Path, row: Row, copy: dict[str, str], pace: _Pace) -> Path | None:
    """Write one candidate into private state as the row's file bytes; keep it only if it hashes to the binding."""
    from . import archive_members, media_store

    staged = private_state.store(vault, STORE_DIRNAME) / f"{secrets.token_hex(16)}.part"
    try:
        with open(staged, "xb") as sink:
            with archive_members.blob_writer(sink) if row.kind == BLOB else _plain(sink) as target:
                if copy["source"] == MEDIA_STORE:
                    snapshot = originals.original_state(vault, row.path)
                    if snapshot.reference is None:
                        raise _Unreadable("no stored version")
                    head = media_store.write_stored_original(vault, snapshot.reference, target)
                    sha256, size = head.sha256, head.size
                else:
                    digest, size = hashlib.sha256(), 0
                    with _copy_parts(vault, copy) as parts:
                        for chunk in pace.chunks(parts):
                            digest.update(chunk)
                            target.write(chunk)
                            size += len(chunk)
                    sha256 = digest.hexdigest()
            sink.flush()
            os.fsync(sink.fileno())
        if sha256 == row.sha256 and (row.size is None or size == row.size):
            return staged
    except (OSError, reserved_paths.ReservedPathLeafError, media_store.MediaStoreError, _Unreadable,
            *_UNDECODABLE) as error:
        log.info("original fixity: a %s copy could not be read: %s", copy["source"], type(error).__name__)
    staged.unlink(missing_ok=True)
    return None


@contextmanager
def _plain(sink: BinaryIO) -> Iterator[BinaryIO]:
    yield sink


def _repair(vault: Path, row: Row, hashed: _Hashed, cause: str, pace: _Pace, result: PassResult) -> None:
    """Restore a damaged original from the first copy that verifies, or record that none does."""
    for copy in _copies(vault, row):
        staged = _stage(vault, row, copy, pace)
        if staged is None:
            continue
        try:
            identity = _install(vault, row, hashed, staged, cause)
        except (_Changed, OpError):
            result.rescheduled += 1  # The next pass hashes the file again and decides afresh.
            return
        except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError) as error:
            # Damaged and unchanged, with a copy the owner can use: the state that asks for their action.
            log.warning("original fixity: a restore failed: %s", type(error).__name__)
            _record(vault, row, state=CORRUPT_NO_COPY, identity=hashed.identity, cause=cause, route=[copy],
                    verified=True)
            _count(result, CORRUPT_NO_COPY)
            return
        finally:
            staged.unlink(missing_ok=True)
        _after_restore(vault, row)
        _record(vault, row, state=RESTORED, identity=identity, cause=cause, route=[copy], verified=True)
        _count(result, RESTORED)
        return
    _record(vault, row, state=CORRUPT_NO_COPY, identity=hashed.identity, cause=cause, verified=True)
    _count(result, CORRUPT_NO_COPY)


def _after_restore(vault: Path, row: Row) -> None:
    """A cached original records its new file identity, so the media store keeps trusting its copy."""
    from . import media_store

    if row.kind != FILE or not row.stored:
        return
    try:
        media_store.recover_cache(vault, vault / row.path, originals.original_state(vault, row.path))
    except Exception:  # noqa: BLE001 - the restore stands; an unrecorded cache costs only a later hash
        log.warning("original fixity: the media cache of a restored original was not recorded", exc_info=True)


def _install(vault: Path, row: Row, hashed: _Hashed, staged: Path, cause: str) -> Identity:
    """Put the verified bytes in place of the damaged original, and the changed bytes in Trash.

    Both copies are written beside the original, outside the vault guard. Under the guard the file
    and its binding are checked again, the changed bytes move to Trash with their metadata, and the
    verified bytes replace the original in one rename. Returns the restored file's stat identity.
    """
    from .kbdir import kb_dirname

    target = PurePosixPath(row.path)
    now = dt.datetime.now()
    day, entry = trash_metadata.placement(row.path, now)
    damaged_name, restored_name = held_fs.publish_temporary_name(), held_fs.publish_temporary_name()
    written: dict[str, held_fs.StableIdentity] = {}
    with held_fs.acquire(vault).require() as filesystem, \
            filesystem.parent(target.parent.as_posix(), access="mutate").require() as parent:
        try:
            with filesystem.file(parent, target.name).require() as damaged:
                if damaged.version() != hashed.version:
                    raise _Changed
                filesystem.copy(damaged, parent, damaged_name).require()
                written[damaged_name] = _identity_of(filesystem, parent, damaged_name)
                if damaged.version() != hashed.version:
                    raise _Changed
            with filesystem.file(parent, restored_name, access="write", create=True, exclusive=True).require() as fresh:
                written[restored_name] = fresh.identity  # known before the write, so a failed one is still removed
                filesystem.write(fresh, held_fs.Chunks(open=lambda: _file_parts(staged))).require()
                written[restored_name] = fresh.identity
            with _guard(vault, "original_fixity_restore"):
                if not _still_bound(vault, row):
                    raise _Changed
                with filesystem.file(parent, target.name).require() as current:
                    if current.version() != hashed.version:
                        raise _Changed
                trash = PurePosixPath(kb_dirname(), trash_metadata.TRASH_SUBPATH, day).as_posix()
                with filesystem.parent(trash, create=True, access="mutate").require() as folder:
                    leaf = _free_trash_leaf(filesystem, folder, entry)
                    metadata = {
                        "original_path": row.path, "trashed_at": now.isoformat(), "deleted_by": "exomem",
                        "reason": "original_fixity", "cause": cause,
                        "expected_sha256": row.sha256, "found_sha256": hashed.sha256,
                    }
                    held_fs.publish_bytes(filesystem, folder, leaf + trash_metadata.METADATA_SUFFIX,
                                          json.dumps(metadata, indent=2).encode("utf-8")).require()
                    _rename(filesystem, parent, damaged_name, written, folder, leaf)
                    filesystem.flush_directory(folder).require()
                _rename(filesystem, parent, restored_name, written, parent, target.name, replace=True)
                filesystem.flush_directory(parent).require()
                with _open(vault, row) as placed:
                    return Identity.of(os.fstat(placed.stream.fileno()))
        finally:
            for name, identity in written.items():
                with suppress(held_fs.HeldFsError, OSError):
                    leftover = filesystem.file(parent, name, access="mutate")
                    if leftover.ok:
                        with leftover.require() as residue:
                            if residue.identity == identity:
                                filesystem.unlink(residue).require()


def _identity_of(filesystem, parent, name: str) -> held_fs.StableIdentity:
    with filesystem.file(parent, name).require() as file:
        return file.identity


def _file_parts(path: Path) -> Iterator[bytes]:
    with open(path, "rb") as stream:
        yield from iter(lambda: stream.read(_CHUNK), b"")


def _rename(filesystem, parent, name: str, written: dict[str, held_fs.StableIdentity], destination, leaf: str, *,
            replace: bool = False) -> None:
    with filesystem.file(parent, name, access="mutate").require() as file:
        if file.identity != written[name]:
            raise _Changed
        filesystem.rename(file, destination, leaf, replace=replace).require()
    del written[name]


def _free_trash_leaf(filesystem, folder, entry: str) -> str:
    """The first Trash entry name whose payload, metadata and companion names are free, as delete picks it."""
    def taken(name: str) -> bool:
        for slot in trash_metadata.destination_paths(Path(name)):
            found = filesystem.file(folder, slot.name)
            if found.ok:
                found.require().close()
                return True
        return False

    candidate, counter = entry, 2
    stem, dot, suffix = entry.rpartition(".")
    while taken(candidate):
        candidate = f"{stem}-{counter}.{suffix}" if dot else f"{entry}-{counter}"
        counter += 1
    return candidate


# --- what the owner and the operator see -------------------------------------------------------


def _when(value: float | None) -> str | None:
    return dt.datetime.fromtimestamp(value, dt.UTC).isoformat() if value else None


def readiness(vault: Path | None) -> dict[str, object]:
    """The content-free readiness block: the count of unresolved originals, `unknown` before a pass, or `off`.

    It carries no path, name or hash, and never makes a cell unready (D9).
    """
    if disabled():
        return {"state": SIGNAL_OFF}
    try:
        connection = _reader(Path(vault)) if vault is not None else None
        if connection is None:
            return {"state": SIGNAL_UNKNOWN}
        with closing(connection):
            meta = _meta(connection)
    except (sqlite3.Error, OSError, ValueError):
        return {"state": SIGNAL_UNKNOWN}
    if "pass_at" not in meta or not isinstance(meta.get("unresolved"), int):
        return {"state": SIGNAL_UNKNOWN}
    return {"state": SIGNAL_MEASURED, "unresolved": meta["unresolved"]}


def status(vault: Path | None) -> dict[str, object]:
    """The `originals` block of `exomem status`: counts by state, or `unknown` before the first pass.

    A pass that started and has not finished marks the counts partial. An unknown count is never zero.
    """
    if disabled():
        return {"state": SIGNAL_OFF}
    try:
        connection = _reader(Path(vault)) if vault is not None else None
        if connection is None:
            return {"state": SIGNAL_UNKNOWN}
        with closing(connection):
            meta = _meta(connection)
            counts = dict(connection.execute("SELECT state, COUNT(*) FROM originals GROUP BY state").fetchall())
            oldest = connection.execute(
                "SELECT MIN(verified_at) FROM originals WHERE state = ?", (VERIFIED,)).fetchone()[0]
    except (sqlite3.Error, OSError, ValueError):
        return {"state": SIGNAL_UNKNOWN, "code": "ORIGINAL_FIXITY_UNREADABLE"}
    if "pass_at" not in meta:
        return {"state": SIGNAL_UNKNOWN}
    return {
        "state": SIGNAL_MEASURED,
        "coverage": "partial" if meta.get("pass_started_at", 0) > meta["pass_at"] else "complete",
        "counts": {state: counts.get(state, 0) for state in _STATES},
        "last_pass_at": _when(meta["pass_at"]),
        "last_walk_at": _when(meta.get("walk_at")),
        "oldest_verification_at": _when(oldest),
    }


_FIX = {
    RESTORED: "Nothing to do: the original was restored from the copy in the route, and its changed bytes "
              "are in Trash. This item clears at the original's next verification.",
    MISSING: "Copy a source from the route back to the path, or restore the file from a backup. Exomem does "
             "not restore a deleted original itself, because the deletion may be deliberate.",
    CORRUPT_NO_COPY: "Restore the file from a backup, or follow the route. Exomem changed nothing.",
}


def attention_findings(vault: Path) -> tuple[list[Any], str | None]:
    """One finding for each restored, missing or damaged original; read-only.

    Returns the findings and None, or no findings and "unknown" when the table cannot be read.
    No table yet means no recorded damage, which is complete.
    """
    from .audit import AuditFinding
    from .governance import egress

    try:
        connection = _reader(Path(vault))
        if connection is None:
            return [], None
        with closing(connection):
            rows = [Row.of(values) for values in connection.execute(
                f"SELECT {_ROW_COLUMNS} FROM originals WHERE state IN ({_marks(REPORTED)}) ORDER BY path", REPORTED)]
    except (sqlite3.Error, OSError, ValueError):
        return [], "unknown"
    # An outside path names the owner's own filesystem; another audience receives only the kind of copy.
    restricted = bool(rows) and egress.visible_page_filter(Path(vault)) is not None

    def route(row: Row) -> list[dict[str, str]]:
        return [copy if copy["source"] in _VAULT_COPIES or not restricted else {"source": copy["source"]}
                for copy in row.route]

    return [
        AuditFinding(
            category="original_fixity",
            severity="info" if row.state == RESTORED else "error",
            path=row.binding,
            # Every vault file the finding names, so an audience that may not see one of them never receives it.
            paths=list(dict.fromkeys(
                [row.path, *(copy["path"] for copy in row.route if copy["source"] in _VAULT_COPIES)])),
            detail=f"stored original {row.path} is {row.state}" + (f" ({row.cause})" if row.cause else ""),
            proposed_fix=_FIX[row.state],
            meta={"original_path": row.path, "state": row.state, "cause": row.cause,
                  "expected_sha256": row.sha256, "route": route(row)},
        )
        for row in rows
    ], None


# --- the thread --------------------------------------------------------------------------------

_LOCK = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None


def start(vault_root: Path) -> threading.Thread | None:
    """Start the fixity daemon. Idempotent: a second call returns the live one."""
    global _thread
    if disabled():
        log.info("original fixity disabled by EXOMEM_DISABLE_ORIGINAL_FIXITY")
        return None
    with _LOCK:
        if _thread is not None and _thread.is_alive():
            if _stop.is_set():
                raise RuntimeError("original fixity is stopping")
            return _thread
        _stop.clear()
        thread = threading.Thread(target=_run, args=(Path(vault_root).resolve(),), name="exomem-original-fixity",
                                  daemon=True)
        _thread = thread
        thread.start()
        return thread


def stop(timeout: float = 2.0, *, worker: threading.Thread | None = None) -> None:
    """Stop the daemon and wait briefly; a pass stops at its next chunk."""
    global _thread
    with _LOCK:
        thread = _thread
        if worker is not None and thread is not worker:
            return
        _stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=timeout)
    with _LOCK:
        if _thread is thread and (thread is None or not thread.is_alive()):
            _thread = None


def _run(vault: Path) -> None:
    try:
        clear_staging(vault)
    except OSError:
        log.warning("original fixity: leftover staged copies stayed", exc_info=True)
    walked_at: float | None = None
    while not _stop.is_set():
        try:
            with foreground_priority.bulk(stop=_stop):
                if walked_at is None or time.time() - walked_at >= walk_seconds():
                    walk(vault)
                    walked_at = time.time()
                run_pass(vault)
        except foreground_priority.BulkCancelled:
            return
        except Exception:  # noqa: BLE001 - a failed pass must not end verification or deny the service
            log.warning("original fixity pass failed; it runs again at the next interval", exc_info=True)
        _stop.wait(pass_seconds())
