"""Durable per-vault intent journal: a write that cannot commit yet waits here, machine-local.

One SQLite file (`.write-intents.sqlite`, WAL, `synchronous=FULL`) sits in the vault's private state
directory beside the other private stores, and staged input bytes sit under `write-intents/inputs/`.
Neither is ever under the synced vault. A row holds no bearer, password or session secret: the caller
passes a bearer-free principal snapshot.

This module is storage and identity only. Nothing here dispatches a write, claims a retry key or
decides a conflict; the accept path and the applier call it.

Identity rule (design D3): the row stores the retry key K, the digest of the caller's original
arguments, K's replay window and a dispatch `request_id`. A unique index covers K over `accepted` and
`applying` rows, so `accept` called twice with one K returns the first intent.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import private_state, state_paths

log = logging.getLogger(__name__)

FORMAT_VERSION = 1

JOURNAL_FILENAME = ".write-intents.sqlite"
INPUTS_DIRNAME = "write-intents"

#: A terminal intent keeps its content-free fields, its compact terminal and (if rejected) its arguments
#: for this long, for polls and resends. It is also the floor for the idempotency receipt of its key.
INTENT_RETENTION_SECONDS = 24 * 60 * 60.0

#: Orphan staging younger than this may belong to an accept that has not committed its row yet.
ORPHAN_GRACE_SECONDS = 60 * 60.0

# Intent states are the journal's own state machine: code implements each transition, and no customer,
# vault or provider defines them, so this is a closed protocol set and not vocabulary (registry C6).
ACTIVE_STATES = ("accepted", "applying")
TERMINAL_STATES = ("committed", "rejected", "discarded")
_CONFLICTED = "conflicted"
# Counted toward the bounds: they still hold arguments, or wait on an owner decision.
_HELD_STATES = (*ACTIVE_STATES, _CONFLICTED)

ENV_MAX_INTENTS = "EXOMEM_WRITE_INTENT_MAX"
ENV_MAX_BYTES = "EXOMEM_WRITE_INTENT_MAX_BYTES"
DEFAULT_MAX_INTENTS = 1000
DEFAULT_MAX_BYTES = 8 * 1024**3
AUDIENCE_SHARE = 0.5
FREE_SPACE_FLOOR_BYTES = 1024**3

CAUSE_FULL = "journal_full"
CAUSE_UNAVAILABLE = "journal_unavailable"

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS intents (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL UNIQUE,
    vault_id TEXT NOT NULL,
    replica_id TEXT NOT NULL,
    audience TEXT NOT NULL,
    state TEXT NOT NULL,
    command TEXT NOT NULL,
    arguments TEXT,
    leaf_arguments TEXT,
    surface TEXT NOT NULL,
    principal TEXT NOT NULL,
    retry_key TEXT NOT NULL,
    digest TEXT NOT NULL,
    window_seconds REAL NOT NULL,
    request_id TEXT NOT NULL,
    base_guards TEXT NOT NULL,
    validation TEXT NOT NULL,
    planned_paths TEXT NOT NULL,
    target_hashes TEXT NOT NULL,
    inputs TEXT NOT NULL,
    held_bytes INTEGER NOT NULL,
    accepted_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    wait_cause TEXT,
    wait_since REAL,
    conflict TEXT,
    terminal TEXT,
    terminal_at REAL
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS intents_active_key
    ON intents(retry_key) WHERE state IN ('accepted', 'applying')""",
    "CREATE INDEX IF NOT EXISTS intents_by_state ON intents(state, seq)",
)

_JSON_COLUMNS = (
    "arguments",
    "leaf_arguments",
    "principal",
    "base_guards",
    "planned_paths",
    "target_hashes",
    "inputs",
    "conflict",
    "terminal",
)


class JournalRefusal(Exception):
    """Accept cannot take the write. `cause` is `journal_full` or `journal_unavailable`."""

    cause = CAUSE_UNAVAILABLE

    def __init__(self, message: str, *, cause: str | None = None):
        super().__init__(message)
        if cause is not None:
            self.cause = cause


class JournalFull(JournalRefusal):
    cause = CAUSE_FULL


class JournalUnavailable(JournalRefusal):
    cause = CAUSE_UNAVAILABLE


class JournalFormatTooNew(JournalUnavailable):
    """The file was written by a newer release; this one neither applies nor accepts."""

    def __init__(self, found: int, known: int):
        super().__init__(f"intent journal format {found} is newer than this release knows ({known})")
        self.found = found
        self.known = known


@dataclass(frozen=True)
class Bounds:
    max_intents: int = DEFAULT_MAX_INTENTS
    max_bytes: int = DEFAULT_MAX_BYTES
    audience_share: float = AUDIENCE_SHARE
    free_space_floor: int = FREE_SPACE_FLOOR_BYTES

    @classmethod
    def from_env(cls) -> Bounds:
        return cls(
            max_intents=_positive_env(ENV_MAX_INTENTS, DEFAULT_MAX_INTENTS),
            max_bytes=_positive_env(ENV_MAX_BYTES, DEFAULT_MAX_BYTES),
        )


def _positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if raw.isascii() and raw.isdigit() and int(raw) > 0:
        return int(raw)
    log.warning("%s is not a positive integer; using %d", name, default)
    return default


@dataclass(frozen=True)
class InputFile:
    """One local file to stage. A `move` source is deleted once its copy is durable and verified."""

    name: str
    source: Path
    move: bool = False


@dataclass(frozen=True)
class Intent:
    seq: int
    intent_id: str
    vault_id: str
    replica_id: str
    audience: str
    state: str
    command: str
    arguments: Any
    leaf_arguments: Any
    surface: str
    principal: Any
    retry_key: str
    digest: str
    window_seconds: float
    request_id: str
    base_guards: Any
    validation: str
    planned_paths: Any
    target_hashes: Any
    inputs: Any
    held_bytes: int
    accepted_at: float
    updated_at: float
    attempts: int
    wait_cause: str | None
    wait_since: float | None
    conflict: Any
    terminal: Any
    terminal_at: float | None

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES


def _decode(row: sqlite3.Row) -> Intent:
    values = dict(row)
    for column in _JSON_COLUMNS:
        if values[column] is not None:
            values[column] = json.loads(values[column])
    return Intent(**values)


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unavailable_on_sqlite_error(method):  # noqa: ANN001, ANN202
    """A SQLite failure inside a public method is `journal_unavailable`, the one cause callers handle."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        try:
            return method(self, *args, **kwargs)
        except sqlite3.Error as error:
            raise JournalUnavailable(f"intent journal cannot be used: {error}") from error

    return wrapper


class WriteIntentJournal:
    """The journal of one vault and replica. Every method opens its own short connection."""

    def __init__(
        self,
        vault_root: Path,
        *,
        vault_id: str,
        replica_id: str,
        bounds: Bounds | None = None,
        clock: Callable[[], float] = time.time,
        free_bytes: Callable[[Path], int] | None = None,
    ):
        self.vault_root = Path(vault_root)
        self.vault_id = vault_id
        self.replica_id = replica_id
        self.bounds = bounds if bounds is not None else Bounds.from_env()
        self.clock = clock
        self._free_bytes = free_bytes if free_bytes is not None else lambda path: shutil.disk_usage(path).free
        try:
            self.state_dir = state_paths.ensure_vault_state_dir(vault_root)
            self.path = self.state_dir / JOURNAL_FILENAME
            self.inputs_root = private_state.store(vault_root, INPUTS_DIRNAME) / "inputs"
            self.inputs_root.mkdir(mode=0o700, exist_ok=True)
        except (OSError, ValueError) as error:
            raise JournalUnavailable(f"intent journal state directory unavailable: {error}") from error
        self.byte_bound_warning = self._check_byte_bound_relation()
        self._open_or_create()

    # -- open -----------------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=FULL")
            return conn
        except sqlite3.Error as error:
            raise JournalUnavailable(f"intent journal cannot open: {error}") from error

    def _open_or_create(self) -> None:
        created = not self.path.exists()
        try:
            conn = self._connect()
            try:
                if created:
                    conn.execute("PRAGMA journal_mode=WAL")
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise JournalUnavailable("intent journal failed its integrity check")
                has_meta = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
                ).fetchone()
                if has_meta:
                    self._refuse_newer_format(conn)
                conn.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('format_version', ?)",
                    (str(FORMAT_VERSION),),
                )
                conn.execute("COMMIT")
                self._refuse_newer_format(conn)
            finally:
                conn.close()
        except sqlite3.Error as error:
            raise JournalUnavailable(f"intent journal cannot open: {error}") from error
        self._harden_files()

    def _refuse_newer_format(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value FROM meta WHERE key = 'format_version'").fetchone()
        if row is None:
            return
        try:
            found = int(row[0])
        except ValueError as error:
            raise JournalUnavailable("intent journal format version is unreadable") from error
        if found > FORMAT_VERSION:
            raise JournalFormatTooNew(found, FORMAT_VERSION)

    def _harden_files(self) -> None:
        if os.name == "nt":
            return
        for suffix in ("", "-wal", "-shm"):
            candidate = self.path.with_name(self.path.name + suffix)
            try:
                if stat.S_IMODE(candidate.stat().st_mode) != 0o600:
                    os.chmod(candidate, 0o600)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise JournalUnavailable(f"intent journal file cannot be made private: {error}") from error

    def _check_byte_bound_relation(self) -> str | None:
        """Warn when one audience's share of the byte bound could not hold the largest synchronous input.

        The largest input one synchronous write accepts is an upload session (2 GiB by default, above the
        100 MiB per fetched file), so the journal never refuses for its own size a write that the
        synchronous path takes only while the bound is at least twice that.
        """
        from . import upload_sessions

        largest = upload_sessions.max_bytes_from_env()
        if self.bounds.max_bytes >= 2 * largest:
            return None
        message = (
            f"{ENV_MAX_BYTES}={self.bounds.max_bytes} is below twice the largest synchronous input "
            f"({largest}); a single audience share may refuse a write that the synchronous path accepts"
        )
        log.warning(message)
        return message

    # -- accept ---------------------------------------------------------------------------------------

    @_unavailable_on_sqlite_error
    def accept(
        self,
        *,
        audience: str,
        command: str,
        arguments: Mapping[str, Any],
        surface: str,
        principal: Mapping[str, Any],
        digest: str,
        retry_key: str | None = None,
        window_seconds: float | None = None,
        base_guards: Mapping[str, Any] | None = None,
        validation: str = "complete",
        planned_paths: Sequence[str] = (),
        target_hashes: Mapping[str, str] | None = None,
        leaf_arguments: Mapping[str, Any] | None = None,
        inputs: Sequence[InputFile] = (),
    ) -> tuple[Intent, bool]:
        """Record an intent and return `(intent, created)`.

        A caller holds its claim on `retry_key` while it calls this. A second call under the same key
        returns the first intent with `created=False`; its staged bytes are removed. A write with no retry
        identity passes `retry_key=None` and gets `intent:<intent_id>` with the 24 h window.

        Raises `JournalFull` at a bound or the free-space floor, and `JournalUnavailable` when the
        journal cannot take the row. Staged inputs are durable and hash-checked before the row commits.
        """
        existing = self.find_by_key(retry_key) if retry_key is not None else None
        if existing is not None and existing.active:
            return existing, False
        intent_id = uuid.uuid4().hex
        # The canonical UUIDv4 spelling: the receipt's terminal projection rejects any other request id.
        request_id = str(uuid.uuid4())
        key = retry_key if retry_key is not None else f"intent:{intent_id}"
        window = window_seconds if window_seconds is not None else INTENT_RETENTION_SECONDS
        argument_text = _dump(arguments)
        sizes = [self._stat_input(item) for item in inputs]
        held_bytes = len(argument_text.encode("utf-8")) + sum(sizes)
        self._admit(audience, held_bytes, staged_bytes=sum(sizes))
        staged = self._stage_inputs(intent_id, inputs)
        try:
            now = self.clock()
            row = {
                "intent_id": intent_id,
                "vault_id": self.vault_id,
                "replica_id": self.replica_id,
                "audience": audience,
                "state": "accepted",
                "command": command,
                "arguments": argument_text,
                "leaf_arguments": None if leaf_arguments is None else _dump(leaf_arguments),
                "surface": surface,
                "principal": _dump(principal),
                "retry_key": key,
                "digest": digest,
                "window_seconds": window,
                "request_id": request_id,
                "base_guards": _dump(base_guards or {}),
                "validation": validation,
                "planned_paths": _dump(list(planned_paths)),
                "target_hashes": _dump(target_hashes or {}),
                "inputs": _dump(staged),
                "held_bytes": held_bytes,
                "accepted_at": now,
                "updated_at": now,
            }
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # Re-checked under the write lock: another process may have filled the journal or
                    # inserted this key since the cheap check above.
                    winner = self._active_for_key(conn, key)
                    if winner is not None:
                        conn.execute("ROLLBACK")
                        self._remove_staged(intent_id)
                        return winner, False
                    self._admit_in(conn, audience, held_bytes)
                    columns = ", ".join(row)
                    marks = ", ".join("?" for _ in row)
                    conn.execute(f"INSERT INTO intents({columns}) VALUES ({marks})", tuple(row.values()))
                    conn.execute("COMMIT")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                created = conn.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as error:
            self._remove_staged(intent_id)
            raise JournalUnavailable(f"intent journal cannot write: {error}") from error
        except BaseException:
            self._remove_staged(intent_id)
            raise
        return _decode(created), True

    def _active_for_key(self, conn: sqlite3.Connection, key: str) -> Intent | None:
        row = conn.execute(
            "SELECT * FROM intents WHERE retry_key = ? AND state IN ('accepted', 'applying')", (key,)
        ).fetchone()
        return None if row is None else _decode(row)

    def _admit(self, audience: str, held_bytes: int, *, staged_bytes: int) -> None:
        conn = self._connect()
        try:
            self._admit_in(conn, audience, held_bytes)
        finally:
            conn.close()
        free = self._free_bytes(self.state_dir)
        if free - staged_bytes < self.bounds.free_space_floor:
            raise JournalFull("the state volume would fall below the free-space floor")

    def _admit_in(self, conn: sqlite3.Connection, audience: str, held_bytes: int) -> None:
        marks = ", ".join("?" for _ in _HELD_STATES)
        total_count, total_bytes = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(held_bytes), 0) FROM intents WHERE state IN ({marks})",
            _HELD_STATES,
        ).fetchone()
        own_count, own_bytes = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(held_bytes), 0) FROM intents "
            f"WHERE state IN ({marks}) AND audience = ?",
            (*_HELD_STATES, audience),
        ).fetchone()
        bounds = self.bounds
        share = bounds.audience_share
        if total_count + 1 > bounds.max_intents or own_count + 1 > bounds.max_intents * share:
            raise JournalFull("the intent count bound is reached")
        if total_bytes + held_bytes > bounds.max_bytes or own_bytes + held_bytes > bounds.max_bytes * share:
            raise JournalFull("the intent byte bound is reached")

    # -- staged inputs --------------------------------------------------------------------------------

    def input_path(self, intent_id: str, name: str) -> Path:
        return self.inputs_root / intent_id / name

    @staticmethod
    def _stat_input(item: InputFile) -> int:
        try:
            return item.source.stat().st_size
        except OSError as error:
            raise JournalUnavailable(f"staged input {item.name} cannot be read: {error}") from error

    def _stage_inputs(self, intent_id: str, inputs: Sequence[InputFile]) -> list[dict[str, Any]]:
        if not inputs:
            return []
        directory = self.inputs_root / intent_id
        staged: list[dict[str, Any]] = []
        try:
            directory.mkdir(mode=0o700)
            for item in inputs:
                if Path(item.name).name != item.name or item.name in {"", ".", ".."}:
                    raise JournalUnavailable(f"staged input name {item.name!r} is not a plain file name")
                target = directory / item.name
                digest = hashlib.sha256()
                size = 0
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as sink, item.source.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        sink.write(block)
                        digest.update(block)
                        size += len(block)
                    sink.flush()
                    os.fsync(sink.fileno())
                if _sha256_file(target) != digest.hexdigest():
                    raise JournalUnavailable(f"staged input {item.name} does not match its source")
                staged.append({"name": item.name, "sha256": digest.hexdigest(), "size": size})
            _fsync_directory(directory)
            _fsync_directory(self.inputs_root)
            for item in inputs:
                if item.move:
                    private_state.remove(item.source)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return staged

    def _remove_staged(self, intent_id: str) -> None:
        shutil.rmtree(self.inputs_root / intent_id, ignore_errors=True)

    # -- reads ----------------------------------------------------------------------------------------

    @_unavailable_on_sqlite_error
    def get(self, intent_id: str) -> Intent | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
        finally:
            conn.close()
        return None if row is None else _decode(row)

    @_unavailable_on_sqlite_error
    def find_by_key(self, retry_key: str) -> Intent | None:
        """The newest intent under `retry_key` still inside its replay window, terminal or not."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM intents WHERE retry_key = ? ORDER BY seq DESC LIMIT 1", (retry_key,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        intent = _decode(row)
        if intent.state in TERMINAL_STATES and intent.terminal_at is not None:
            if self.clock() - intent.terminal_at > max(intent.window_seconds, INTENT_RETENTION_SECONDS):
                return None
        return intent

    @_unavailable_on_sqlite_error
    def has_active(self) -> bool:
        """Whether any accepted or applying intent exists. The cheap check that orders other writes behind them."""
        marks = ", ".join("?" for _ in ACTIVE_STATES)
        conn = self._connect()
        try:
            return conn.execute(f"SELECT 1 FROM intents WHERE state IN ({marks}) LIMIT 1", ACTIVE_STATES).fetchone() is not None
        finally:
            conn.close()

    @_unavailable_on_sqlite_error
    def non_terminal(self, *, audience: str | None = None) -> list[Intent]:
        """Active intents in acceptance order; `audience` limits them to one principal audience."""
        marks = ", ".join("?" for _ in ACTIVE_STATES)
        query = f"SELECT * FROM intents WHERE state IN ({marks})"
        params: tuple[Any, ...] = ACTIVE_STATES
        if audience is not None:
            query += " AND audience = ?"
            params = (*params, audience)
        conn = self._connect()
        try:
            rows = conn.execute(query + " ORDER BY seq", params).fetchall()
        finally:
            conn.close()
        return [_decode(row) for row in rows]

    # -- apply (the applier's transitions) --------------------------------------------------------------

    @property
    def applier_lock_path(self) -> Path:
        """The OS lock file of the one applier of this vault (design D4); its sidecar names a preferred applier."""
        return self.state_dir / f"{JOURNAL_FILENAME}.applier.lock"

    @_unavailable_on_sqlite_error
    def next_accepted(self) -> Intent | None:
        """The oldest `accepted` intent. An `applying` row left by a dead applier is skipped: crash recovery owns it."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM intents WHERE state = 'accepted' ORDER BY seq LIMIT 1").fetchone()
        finally:
            conn.close()
        return None if row is None else _decode(row)

    @_unavailable_on_sqlite_error
    def applying(self) -> list[Intent]:
        """The `applying` rows, oldest first. Only a dead applier, or one whose last journal write failed, leaves one."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM intents WHERE state = 'applying' ORDER BY seq").fetchall()
        finally:
            conn.close()
        return [_decode(row) for row in rows]

    @_unavailable_on_sqlite_error
    def begin_apply(self, intent_id: str) -> bool:
        """Move one `accepted` intent to `applying` and count the attempt. False when another writer moved it first."""
        return self._move(intent_id, "accepted", "applying", count_attempt=True)

    @_unavailable_on_sqlite_error
    def requeue(self, intent_id: str, cause: str) -> bool:
        """Return an `applying` intent to `accepted` after an attempt that committed nothing, naming why it waits.

        `wait_since` keeps the time the same cause began, so the age of a wait survives repeated attempts.
        """
        return self._move(intent_id, "applying", "accepted", wait_cause=cause)

    def _move(
        self,
        intent_id: str,
        source: str,
        target: str,
        *,
        count_attempt: bool = False,
        wait_cause: str | None = None,
    ) -> bool:
        now = self.clock()
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE intents SET state = ?, updated_at = ?, attempts = attempts + ?, "
                "wait_since = CASE WHEN ? IS NULL THEN wait_since WHEN wait_cause IS ? THEN wait_since ELSE ? END, "
                "wait_cause = COALESCE(?, wait_cause) "
                "WHERE intent_id = ? AND state = ?",
                (target, now, int(count_attempt), wait_cause, wait_cause, now, wait_cause, intent_id, source),
            )
            return cursor.rowcount == 1
        finally:
            conn.close()

    @_unavailable_on_sqlite_error
    def set_leaf_arguments(self, intent_id: str, leaf_arguments: Mapping[str, Any]) -> bool:
        """Record the arguments the leaf receives when the applier moved the target. The key and its digest stay."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE intents SET leaf_arguments = ?, updated_at = ? WHERE intent_id = ? AND state = 'applying'",
                (_dump(leaf_arguments), self.clock(), intent_id),
            )
            return cursor.rowcount == 1
        finally:
            conn.close()

    # -- review reads -----------------------------------------------------------------------------------

    @_unavailable_on_sqlite_error
    def conflicted(self, *, audience: str | None = None) -> list[Intent]:
        """Intents that wait for an owner decision, oldest first; `audience` limits them to one principal audience."""
        query = "SELECT * FROM intents WHERE state = 'conflicted'"
        params: tuple[Any, ...] = ()
        if audience is not None:
            query += " AND audience = ?"
            params = (audience,)
        conn = self._connect()
        try:
            rows = conn.execute(query + " ORDER BY seq", params).fetchall()
        finally:
            conn.close()
        return [_decode(row) for row in rows]

    @_unavailable_on_sqlite_error
    def open_intents(self, *, audience: str | None = None, limit: int | None = None) -> list[Intent]:
        """Active and conflicted intents, newest first: what a reader still has to watch or decide.

        `audience` limits them to one principal audience; `limit` bounds the page.
        """
        states = (*ACTIVE_STATES, "conflicted")
        marks = ", ".join("?" for _ in states)
        query = f"SELECT * FROM intents WHERE state IN ({marks})"
        params: tuple[Any, ...] = states
        if audience is not None:
            query += " AND audience = ?"
            params = (*params, audience)
        query += " ORDER BY seq DESC"
        if limit is not None:
            query += " LIMIT ?"
            params = (*params, limit)
        conn = self._connect()
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        return [_decode(row) for row in rows]

    @_unavailable_on_sqlite_error
    def count_open(self, *, audience: str | None = None) -> dict[str, int]:
        """Counts of `accepted` (accepted or applying) and `conflicted` intents, for one audience or all."""
        marks = ", ".join("?" for _ in ACTIVE_STATES)
        query = (
            f"SELECT SUM(state IN ({marks})), SUM(state = 'conflicted') FROM intents "
            "WHERE state IN (" + marks + ", 'conflicted')"
        )
        params: tuple[Any, ...] = (*ACTIVE_STATES, *ACTIVE_STATES)
        if audience is not None:
            query += " AND audience = ?"
            params = (*params, audience)
        conn = self._connect()
        try:
            active, conflicted = conn.execute(query, params).fetchone()
        finally:
            conn.close()
        return {"accepted": int(active or 0), "conflicted": int(conflicted or 0)}

    @_unavailable_on_sqlite_error
    def queue_status(self) -> dict[str, Any]:
        """Depth and the oldest waiting intent: what a health check and the queue gauges report."""
        conn = self._connect()
        try:
            counts = dict(conn.execute("SELECT state, COUNT(*) FROM intents GROUP BY state").fetchall())
            oldest = conn.execute(
                "SELECT accepted_at, wait_cause FROM intents WHERE state = 'accepted' ORDER BY seq LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return {
            "accepted": counts.get("accepted", 0),
            "applying": counts.get("applying", 0),
            "conflicted": counts.get("conflicted", 0),
            "oldest_accepted_at": None if oldest is None else oldest[0],
            "oldest_wait_cause": None if oldest is None else oldest[1],
            "format_version": FORMAT_VERSION,
        }

    @_unavailable_on_sqlite_error
    def count_conflicted(self) -> int:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM intents WHERE state = 'conflicted'"
            ).fetchone()[0]
        finally:
            conn.close()

    @_unavailable_on_sqlite_error
    def after(self, seq: int) -> list[Intent]:
        """Every intent accepted after `seq` that is not discarded, oldest first: what a retry would overtake."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM intents WHERE seq > ? AND state != 'discarded' ORDER BY seq", (seq,)
            ).fetchall()
        finally:
            conn.close()
        return [_decode(row) for row in rows]

    # -- end of life ----------------------------------------------------------------------------------

    @_unavailable_on_sqlite_error
    def finish(
        self,
        intent_id: str,
        state: str,
        *,
        terminal: Mapping[str, Any] | None = None,
        conflict: Mapping[str, Any] | None = None,
    ) -> Intent | None:
        """Move an intent to `committed`, `rejected`, `discarded` or `conflicted`, applying retention.

        A committed or discarded intent drops its arguments and inputs at once. A rejected one drops its
        inputs and keeps its arguments for the retention window. A conflicted one keeps both until an
        owner decision.
        """
        if state not in (*TERMINAL_STATES, _CONFLICTED):
            raise ValueError(f"{state!r} is not an end state")
        now = self.clock()
        drop_arguments = state in ("committed", "discarded")
        drop_inputs = state != _CONFLICTED
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE intents SET state = ?, updated_at = ?, terminal = ?, conflict = ?, terminal_at = ?, "
                "arguments = CASE WHEN ? THEN NULL ELSE arguments END, "
                "leaf_arguments = CASE WHEN ? THEN NULL ELSE leaf_arguments END "
                "WHERE intent_id = ?",
                (
                    state,
                    now,
                    None if terminal is None else _dump(terminal),
                    None if conflict is None else _dump(conflict),
                    None if state == _CONFLICTED else now,
                    drop_arguments,
                    drop_arguments,
                    intent_id,
                ),
            )
            conn.execute("COMMIT")
            row = conn.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
        except sqlite3.Error as error:
            raise JournalUnavailable(f"intent journal cannot write: {error}") from error
        finally:
            conn.close()
        if drop_inputs:
            self._remove_staged(intent_id)
        ended = None if row is None else _decode(row)
        if ended is not None:
            from . import intent_observability

            intent_observability.ended(self, ended, cause=(conflict or {}).get("cause"))
        return ended

    @_unavailable_on_sqlite_error
    def retry(
        self,
        intent_id: str,
        *,
        arguments: Mapping[str, Any],
        digest: str,
        base_guards: Mapping[str, Any],
        reason: str,
        audience: str,
        principal: Any,
    ) -> Intent | None:
        """Replace one `conflicted` intent with a new `accepted` one under a new key at the end of the line (design D3, rule 6).

        One transaction ends the old intent `discarded`, which releases nothing here but drops its arguments, and
        inserts the new row under the deciding reader's audience and principal, with the old row's surface,
        validation, targets and staged inputs. The new key is
        `intent:<id>:retry:1`: an intent is retried once, because the retry replaces it. Returns None when the intent
        is not `conflicted`. The caller releases any vocabulary binding of the old key before it calls this.
        """
        old = self.get(intent_id)
        if old is None or old.state != _CONFLICTED:
            return None
        new_id = uuid.uuid4().hex
        staged_source = self.inputs_root / intent_id
        staged_copy = self.inputs_root / new_id
        if staged_source.is_dir():
            try:
                shutil.copytree(staged_source, staged_copy, copy_function=shutil.copy2)
                _fsync_directory(staged_copy)
                _fsync_directory(self.inputs_root)
            except OSError as error:
                shutil.rmtree(staged_copy, ignore_errors=True)
                raise JournalUnavailable(f"staged inputs cannot be copied: {error}") from error
        argument_text = _dump(arguments)
        now = self.clock()
        row = {
            "intent_id": new_id,
            "vault_id": old.vault_id,
            "replica_id": old.replica_id,
            "audience": audience,
            "state": "accepted",
            "command": old.command,
            "arguments": argument_text,
            "leaf_arguments": None,
            "surface": old.surface,
            "principal": _dump(principal),
            "retry_key": f"intent:{intent_id}:retry:1",
            "digest": digest,
            "window_seconds": INTENT_RETENTION_SECONDS,
            "request_id": str(uuid.uuid4()),
            "base_guards": _dump(base_guards),
            "validation": old.validation,
            "planned_paths": _dump(old.planned_paths),
            "target_hashes": _dump(old.target_hashes),
            "inputs": _dump(old.inputs),
            "held_bytes": len(argument_text.encode("utf-8"))
            + sum(int(item.get("size", 0)) for item in old.inputs or ()),
            "accepted_at": now,
            "updated_at": now,
        }
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ended = conn.execute(
                    "UPDATE intents SET state = 'discarded', updated_at = ?, terminal = ?, conflict = NULL, "
                    "terminal_at = ?, arguments = NULL, leaf_arguments = NULL WHERE intent_id = ? AND state = 'conflicted'",
                    (
                        now,
                        _dump({"ok": False, "state": "discarded", "reason": reason}),
                        now,
                        intent_id,
                    ),
                )
                if ended.rowcount != 1:
                    conn.execute("ROLLBACK")
                    shutil.rmtree(staged_copy, ignore_errors=True)
                    return None
                conn.execute(
                    f"INSERT INTO intents({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                    tuple(row.values()),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            created = conn.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (new_id,)
            ).fetchone()
        except BaseException as error:
            shutil.rmtree(staged_copy, ignore_errors=True)
            if isinstance(error, sqlite3.Error):
                raise JournalUnavailable(f"intent journal cannot write: {error}") from error
            raise
        finally:
            conn.close()
        self._remove_staged(intent_id)
        retried = _decode(created)
        from . import intent_observability

        intent_observability.ended(self, dataclasses.replace(old, state="discarded", updated_at=now, terminal_at=now))
        intent_observability.accepted(self, retried, log_row=True)
        return retried

    @_unavailable_on_sqlite_error
    def purge_expired(self) -> int:
        """Delete terminal rows past their retention. Returns the number removed."""
        now = self.clock()
        marks = ", ".join("?" for _ in TERMINAL_STATES)
        conn = self._connect()
        try:
            cursor = conn.execute(
                f"DELETE FROM intents WHERE state IN ({marks}) AND terminal_at IS NOT NULL "
                "AND terminal_at <= ? - MAX(window_seconds, ?)",
                (*TERMINAL_STATES, now, INTENT_RETENTION_SECONDS),
            )
            return cursor.rowcount
        except sqlite3.Error as error:
            raise JournalUnavailable(f"intent journal cannot write: {error}") from error
        finally:
            conn.close()

    def sweep_orphans(self, *, min_age_seconds: float = ORPHAN_GRACE_SECONDS) -> list[str]:
        """Delete each staged input directory whose intent is missing or terminal.

        A directory younger than `min_age_seconds` is skipped: an accept in another process may have
        staged it and not yet committed its row. A directory of an `accepted`, `applying` or
        `conflicted` intent always stays. Returns the intent ids removed.
        """
        removed: list[str] = []
        if not self.inputs_root.is_dir():
            return removed
        now = self.clock()
        for entry in sorted(self.inputs_root.iterdir()):
            if not entry.is_dir():
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age < min_age_seconds:
                continue
            intent = self.get(entry.name)
            if intent is not None and intent.state not in TERMINAL_STATES:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
        return removed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
