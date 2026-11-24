"""Resumable upload sessions: one file sent in parts, preserved once its SHA-256 matches.

`/upload/sessions` speaks the tus 1.0 core protocol with its creation,
expiration and termination extensions (see `server_transfer`). This module
holds the protocol's constants, which `handoff` advertises to an upload
handoff, and the session store under the routes: a `.part` file and a JSON record per
session, in a private directory under the vault's state directory, the same
shape as `held_uploads`. The client declares the whole file's length and
SHA-256 at creation; the server keeps a running SHA-256 in memory and, after a
restart, re-hashes the `.part` file once before it accepts more bytes.

A session is addressed by its id and authenticated by a 256-bit secret that
only the client holds; the record keeps its SHA-256. An unknown id, a missing
secret and a wrong one get the same answer. The record moves through
`receiving`, `verifying`, `committing`, then `committed` or `failed` with a
stable code. The final part returns at once and a background thread verifies
and commits. A mismatch or content that can never be preserved fails the
session and deletes its bytes, and the bytes are deleted after a commit. A
commit that fails for any other reason, such as a full disk or a lease handoff,
keeps the bytes and leaves the session `retryable`; the next read retries it.
When the vault was only busy or warming, the server also retries it after a wait
that doubles from 2 to 60 seconds, until the session expires. A per-session
file lock serialises appends, commit and cleanup across processes, and a
store-wide lock serialises creation.

A session expires `TTL_SECONDS` after its last accepted part. Delegated
Source offsets are published only after syncing the bytes, and terminal
Source receipts have their own retention window. The serving runtime sweeps
expired sessions at startup and in bounded batches while serving. An expired
lookup also cleans up its own session.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from . import media_capacity, private_state

log = logging.getLogger(__name__)

STORE_DIRNAME = "upload-sessions"
# Older session readers do not open this namespace or accept this ID prefix.
SOURCE_STORE_DIRNAME = "source-upload-sessions"
SOURCE_ID_PREFIX = "source-"
_RECEIPTS_DIRNAME = "receipts"
_SESSION_LOCK_SUFFIX = ".lock"
_COMMIT_WITNESS_SUFFIX = ".commit"
_PENDING_COMMIT_WITNESS_SUFFIX = ".commit-pending"
#: A session lives this long after its last accepted part.
TTL_SECONDS = 24 * 3600
# Wait five minutes between passes. A large store yields every 128 directory entries.
_SWEEP_INTERVAL_SECONDS = 5 * 60.0
_SWEEP_BATCH_SIZE = 128
_SWEEP_BATCH_PAUSE_SECONDS = 1.0
#: Bytes one request may carry: under the proxy edge's 100 MB body cap.
MAX_PATCH_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_BYTES = 2 * 1024**3
MAX_BYTES_ENV = "EXOMEM_UPLOAD_SESSION_MAX_BYTES"
#: Sessions one credential binding may keep open at once.
MAX_LIVE_PER_BINDING = 4
#: The route a session is created at; a session lives at `<SESSIONS_PATH>/<id>`.
SESSIONS_PATH = "/upload/sessions"
#: tus 1.0 (tus.io resumable-upload protocol): the version every request names.
TUS_VERSION = "1.0.0"
#: The session secret travels only in this header, never in a URL.
SECRET_HEADER = "Exomem-Upload-Secret"
#: The default lane of sessions created before lane selection was recorded.
LANE = "evidence"
#: `Upload-Metadata` keys that creation requires; `description`, `raw_protection` and
#: `archive` are optional.
# nosemgrep: ep-word-set -- the creation route fixes these metadata keys.
REQUIRED_METADATA = ("filename", "sha256", "scope", "category")
#: The shell recipe an upload handoff links to rather than carries.
RECIPE_URL = (
    "https://github.com/Artexis10/exomem/blob/main/docs/deployment.md"
    "#resumable-uploads-from-a-shell"
)

RECEIVING = "receiving"
VERIFYING = "verifying"
COMMITTING = "committing"
COMMITTED = "committed"
FAILED = "failed"
#: All bytes held and verified; the last commit failed for a reason other than the bytes.
RETRYABLE = "retryable"
SOURCE_RECEIPT_PENDING = "SOURCE_RECEIPT_PENDING"
_LIVE = (RECEIVING, VERIFYING, COMMITTING, RETRYABLE)
#: States a commit starts or resumes from.
_COMMITTABLE = (VERIFYING, COMMITTING, RETRYABLE)

_SESSION_ID = re.compile(r"[0-9a-f]{32}")
_SECRET = re.compile(r"[A-Za-z0-9_-]{43}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1024 * 1024
#: How long a session waits for an earlier part or failed worker to release its lock.
_LOCK_WAIT_SECONDS = 30.0
# A commit that ends retryable for a busy vault is started again by the server: first after
# `REATTEMPT_INITIAL_SECONDS`, then after twice the previous wait, to `REATTEMPT_MAX_SECONDS`.
REATTEMPT_INITIAL_SECONDS = 2.0
REATTEMPT_MAX_SECONDS = 60.0
# These parks can resolve without another request. Other codes wait for the client, or for expiry.
# nosemgrep: ep-word-set -- these are the vault's documented transient error codes.
_REATTEMPT_CODES = frozenset({
    "MUTATION_BUSY", "COLLECTION_STORE_BUSY", "MUTATION_WARMING", "UPLOAD_ORIGIN_BUSY", media_capacity.UPLOAD_SPACE_BUSY,
})
_now = time.time


class SessionError(Exception):
    """A content-free refusal with the HTTP status the route answers."""

    def __init__(self, code: str, reason: str, status: int) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.status = status


class CommitFailed(Exception):
    """A commit refused with a stable code.

    Unless `retryable`, the bytes can never be preserved as declared: the session
    fails and its bytes are deleted. A retryable refusal keeps them.
    """

    def __init__(self, code: str, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.retryable = retryable


def _not_found() -> SessionError:
    return SessionError("UPLOAD_SESSION_NOT_FOUND", "no upload session answers this request", 404)


def header_count(value: str) -> int | None:
    """Parse the protocol's non-negative byte count without unbounded integer conversion."""
    # A signed 64-bit byte count has at most 19 decimal digits.
    if value.isascii() and value.isdigit() and len(value) <= 19:
        return int(value)
    return None


def max_bytes_from_env() -> int:
    """The longest session; a malformed setting falls back to the default."""
    raw = os.environ.get(MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    if raw.isascii() and raw.isdigit() and int(raw) > 0:
        return int(raw)
    log.warning("%s is not a positive integer; using %d", MAX_BYTES_ENV, DEFAULT_MAX_BYTES)
    return DEFAULT_MAX_BYTES


def required_metadata(lane: str) -> tuple[str, ...]:
    """The fixed creation fields for each supported upload protocol lane."""
    if lane == "source":
        return ("filename", "sha256")
    if lane == LANE:
        return REQUIRED_METADATA
    raise ValueError("unsupported upload session lane")


def handoff(base_url: str, *, lane: str = LANE) -> dict[str, Any]:
    """What an upload handoff tells a client about sessions under `base_url`.

    Only what a client needs to open and fill a session; the recipe stays in
    the docs, because every field here costs every caller's result.
    """
    return {
        "create_url": f"{base_url}{SESSIONS_PATH}",
        "protocol": f"tus {TUS_VERSION}",
        "resend_header": SECRET_HEADER,
        "max_part_bytes": MAX_PATCH_BYTES,
        "max_bytes": max_bytes_from_env(),
        "metadata": list(required_metadata(lane)),
        "recipe": RECIPE_URL,
    }


@dataclass
class _Running:
    """Per-session process state: the running digest and a pending retry."""

    size: int = -1
    digest: Any = None
    parked: str | None = None  # the code of the park last recorded for a caller
    # The one pending re-attempt, and the wait that the next one gets.
    reattempt: threading.Timer | None = None
    reattempt_at: float = 0
    reattempt_wait: float = field(default_factory=lambda: REATTEMPT_INITIAL_SECONDS)


_RUNNING: dict[str, _Running] = {}
_RUNNING_LOCK = threading.Lock()


def _running(session_id: str) -> _Running:
    with _RUNNING_LOCK:
        return _RUNNING.setdefault(session_id, _Running())


def _forget(session_id: str) -> None:
    with _RUNNING_LOCK:
        running = _RUNNING.pop(session_id, None)
    if running is not None and running.reattempt is not None:
        running.reattempt.cancel()


def _store(vault_root: Path, *, delegated: bool = False) -> Path:
    return private_state.store(vault_root, SOURCE_STORE_DIRNAME if delegated else STORE_DIRNAME)


def _existing_stores(vault_root: Path) -> Iterator[Path]:
    from .state_paths import vault_state_dir

    root = vault_state_dir(vault_root)
    for delegated in (False, True):
        if (root / (SOURCE_STORE_DIRNAME if delegated else STORE_DIRNAME)).exists():
            yield _store(vault_root, delegated=delegated)


def delegated_source(session_id: str) -> bool:
    return session_id.startswith(SOURCE_ID_PREFIX)


def valid_session_id(session_id: str) -> bool:
    return _SESSION_ID.fullmatch(session_id.removeprefix(SOURCE_ID_PREFIX)) is not None


def _witness_path(store: Path, session_id: str) -> Path:
    return store / f"{session_id}{_COMMIT_WITNESS_SUFFIX}"


def file_commit_witness_path(vault_root: Path, session_id: str) -> Path:
    """Locate a delegated Source commit witness without creating its store."""
    from .state_paths import vault_state_dir

    if not delegated_source(session_id) or not valid_session_id(session_id):
        raise ValueError("invalid Source upload session ID")
    return _witness_path(vault_state_dir(vault_root) / SOURCE_STORE_DIRNAME, session_id)


def pending_file_commit_witness(path: Path) -> Path:
    """Locate the seal's pending marker beside its witness."""
    return path.with_suffix(_PENDING_COMMIT_WITNESS_SUFFIX)


def valid_session_secret(secret: str) -> bool:
    return _SECRET.fullmatch(secret) is not None


def _secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


_remove = private_state.remove


def _remove_session_payloads(store: Path, session_id: str) -> None:
    if delegated_source(session_id):
        witness = _witness_path(store, session_id)
        _remove(witness, pending_file_commit_witness(witness))
    _remove(store / f"{session_id}.part")


def _write_record(store: Path, record: dict) -> None:
    # `sweep` owns the `.tmp` temp; `.part` here is a session's bytes.
    delegated = store.name == SOURCE_STORE_DIRNAME
    private_state.write_record(store, record["id"], record, temp_suffix=".tmp", durable=delegated)
    if delegated:
        private_state.sync_directory(store)


def _finish(store: Path, record: dict, vault_root: Path | None) -> None:
    delegated = delegated_source(record["id"])
    if delegated:
        record = {**record, "expires": _now() + TTL_SECONDS}
    _write_record(store, record)
    if delegated:
        # The live record already holds the outcome if a stop interrupts this move.
        receipts = store / _RECEIPTS_DIRNAME
        receipts.mkdir(mode=0o700, exist_ok=True)
        leaf = f"{record['id']}.json"
        os.replace(store / leaf, receipts / leaf)
        private_state.sync_directory(receipts)
        private_state.sync_directory(store)
    _remove_session_payloads(store, record["id"])
    _release_credit(vault_root, record["id"])
    _forget(record["id"])


def _session_lock(store: Path, session_id: str, *, timeout: float = 0) -> FileLock:
    """Use one native lock for all writers and cleanup of a live session."""
    # A handler and its worker may use the same instance from different threads.
    return FileLock(store / f"{session_id}{_SESSION_LOCK_SUFFIX}", timeout=timeout, thread_local=False)


def _read_record_at(store: Path, session_id: str) -> dict | None:
    try:
        record = json.loads((store / f"{session_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("id") != session_id:
        return None
    if delegated_source(session_id) and not isinstance(record.get("source_handoff"), dict):
        return None
    return record


def _read_record(store: Path, session_id: str) -> dict | None:
    record = _read_record_at(store, session_id)
    if record is None and delegated_source(session_id):
        return _read_record_at(store / _RECEIPTS_DIRNAME, session_id)
    return record


def _part_size(part: Path) -> int:
    try:
        return part.stat().st_size
    except FileNotFoundError:
        return 0


def _drop(store: Path, session_id: str, vault_root: Path | None) -> None:
    """Remove the session under its lock. Never unlink the lock from its holder."""
    leaf = f"{session_id}.json"
    _remove_session_payloads(store, session_id)
    if vault_root is not None:
        media_capacity.release_commit_credit(vault_root, session_id)
    _remove(store / leaf)
    if delegated_source(session_id):
        _remove(store / _RECEIPTS_DIRNAME / leaf)
    _forget(session_id)


def _expiry_timestamp(record: dict | None) -> float | None:
    if record is None:
        return None
    try:
        expiry = float(record["expires"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return expiry if math.isfinite(expiry) else None


def _expired(record: dict, now: float) -> bool:
    expiry = _expiry_timestamp(record)
    return expiry is None or expiry <= now


def _expire(store: Path, session_id: str, vault_root: Path | None, now: float) -> dict | None:
    """Clean up only when the part and commit are idle, after re-reading expiry."""
    with _session_lock(store, session_id):
        record = _read_record(store, session_id)
        expiry = _expiry_timestamp(record)
        if expiry is None:
            return None
        if expiry > now:
            return record
        _drop(store, session_id, vault_root)
    return None


def sweep(vault_root: Path) -> None:
    """Remove expired sessions with their bytes, and anything a crash left behind."""
    now = _now()
    for store, path in _sweep_entries(vault_root):
        _sweep_entry(vault_root, store, path, now)


def _sweep_entries(vault_root: Path) -> Generator[tuple[Path, Path], None, None]:
    for store in _existing_stores(vault_root):
        for directory in (store, store / _RECEIPTS_DIRNAME):
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        yield store, Path(entry.path)
            except FileNotFoundError:
                continue


def _absent(*paths: Path | str, dir_fd: int | None = None) -> bool:
    for path in paths:
        try:
            os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        return False
    return True


def _reap_unix_lock(store: Path, leaf: str, live_names: tuple[str, str]) -> None:
    import fcntl

    root = os.open(store, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        observed = os.stat(leaf, dir_fd=root, follow_symlinks=False)
        if not stat.S_ISREG(observed.st_mode):
            return
        held = os.open(leaf, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        try:
            opened = os.fstat(held)
            if not os.path.samestat(observed, opened):
                return
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if _absent(*live_names, dir_fd=root) and os.path.samestat(
                opened, os.stat(leaf, dir_fd=root, follow_symlinks=False)
            ):
                os.unlink(leaf, dir_fd=root)
        finally:
            os.close(held)
    finally:
        os.close(root)


def _reap_orphan_lock(store: Path, path: Path) -> None:
    sid = path.stem
    # Released workers may still use the legacy namespace. Only this protocol
    # owns the lock names of delegated sessions.
    if path.parent != store or not delegated_source(sid) or not valid_session_id(sid):
        return
    live_names = (f"{sid}.json", f"{sid}.part")
    # A Source receipt may outlive its lock. There is no live record or part to
    # mutate, and a retired ID cannot be used to create another session.
    try:
        if not _absent(*(store / name for name in live_names)):
            return
        if os.name == "nt":
            # Its native release attempts removal after closing the handle.
            if stat.S_ISREG(path.lstat().st_mode):
                with _session_lock(store, sid):
                    pass
        elif hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
            _reap_unix_lock(store, path.name, live_names)
    except (ImportError, OSError, Timeout):
        pass


def _sweep_entry(vault_root: Path, store: Path, path: Path, now: float) -> None:
    from .cli_ops import OpError

    if path.suffix == _SESSION_LOCK_SUFFIX:
        _reap_orphan_lock(store, path)
        return
    if path.suffix == ".json":
        if valid_session_id(path.stem):
            record = _read_record_at(path.parent, path.stem)
            expiry = _expiry_timestamp(record)
            if expiry is not None and expiry <= now:
                try:
                    _expire(store, path.stem, vault_root, now)
                except Timeout:
                    pass
                except (OpError, OSError, sqlite3.Error):
                    log.warning("expired upload session cleanup will retry", exc_info=True)
        return
    # These are the closed suffixes of session payloads and interrupted file writes.
    if path.suffix not in (".part", ".tmp", _COMMIT_WITNESS_SUFFIX, _PENDING_COMMIT_WITNESS_SUFFIX):
        return
    session_id = path.name.partition(".")[0]
    if not valid_session_id(session_id):
        return
    try:
        if path.stat().st_mtime + TTL_SECONDS > now:
            return
        with _session_lock(store, session_id):
            record = _read_record(store, session_id)
            if path.suffix == ".tmp" or (record is not None and record.get("state") in (COMMITTED, FAILED)):
                _remove(path)
            elif record is None:
                # An unreadable record is not proof that its bytes are abandoned.
                leaf = f"{session_id}.json"
                if _absent(store / leaf, store / _RECEIPTS_DIRNAME / leaf):
                    _remove(path)
    except (OSError, Timeout):
        pass


def run_expiry_sweep(vault_root: Path, stop: threading.Event) -> None:
    """Reclaim expired session files off the request path for the serving runtime."""
    while not stop.wait(_SWEEP_INTERVAL_SECONDS):
        try:
            # Keep the scan cursor across batches. Restarting at each pause would
            # repeatedly read the same names and strand older entries.
            with closing(_sweep_entries(vault_root)) as entries:
                for count, (store, path) in enumerate(entries, start=1):
                    if stop.is_set():
                        return
                    _sweep_entry(vault_root, store, path, _now())
                    if count % _SWEEP_BATCH_SIZE == 0 and stop.wait(_SWEEP_BATCH_PAUSE_SECONDS):
                        return
        except OSError:
            log.warning("upload session expiry sweep could not finish", exc_info=True)


@dataclass
class Session:
    """One authenticated session: its record as read, and its store."""

    store: Path
    record: dict
    vault_root: Path | None = None

    @property
    def id(self) -> str:
        return self.record["id"]

    @property
    def part(self) -> Path:
        return self.store / f"{self.id}.part"

    @property
    def length(self) -> int:
        return int(self.record["length"])

    @property
    def expires(self) -> float:
        return float(self.record["expires"])

    def offset(self) -> int:
        if self.record["state"] == RECEIVING:
            if delegated_source(self.id):
                return self.record.get("durable_offset", 0)
            return _part_size(self.part)
        return self.length

    def view(self) -> dict[str, Any]:
        """What `GET` answers: the state, and the receipt once committed."""
        out: dict[str, Any] = {
            "state": self.record["state"],
            "offset": self.offset(),
            "length": self.length,
        }
        if self.record.get("code"):
            out["code"] = self.record["code"]
            out["reason"] = self.record.get("reason")
        if self.record.get("receipt") is not None:
            out["receipt"] = self.record["receipt"]
        return out


def startup_sweep(vault_root: Path) -> list[Session]:
    """At the serving runtime's start: sweep an existing store; return the commits to resume."""
    sweep(vault_root)
    pending = []
    for store in _existing_stores(vault_root):
        for meta in store.glob("*.json"):
            record = _read_record_at(store, meta.stem)
            if record and _can_start(store, record):
                pending.append(Session(store, record, vault_root))
    return pending


def create(
    vault_root: Path,
    *,
    binding: str,
    length: int,
    sha256: str,
    target: dict[str, Any],
    max_bytes: int,
    source_handoff: dict[str, Any] | None = None,
) -> tuple[Session, str]:
    """Open a session for `length` bytes; return it with the secret only the client keeps.

    `target` is what the commit needs (destination, filename, flags); the route
    has validated it.
    """
    if length < 0:
        raise SessionError("INVALID_UPLOAD", "`Upload-Length` must be a non-negative integer", 400)
    if length > max_bytes:
        raise SessionError("TOO_LARGE", f"a session holds at most {max_bytes:,} bytes", 413)
    if not _SHA256.fullmatch(sha256):
        raise SessionError("INVALID_UPLOAD", "`sha256` must be 64 lowercase hex digits", 400)
    store = _store(vault_root)
    # The limit and the reservation count the sessions on disk, so creation is
    # serialised: two creates between the count and the write would both pass.
    try:
        with FileLock(store / ".create.lock", timeout=_LOCK_WAIT_SECONDS):
            if source_handoff is not None:
                store = _store(vault_root, delegated=True)
                private_state.sync_directory(store.parent)
            return _create_locked(vault_root, store, binding, length, sha256, target, source_handoff)
    except Timeout as exc:
        raise SessionError("UPLOAD_SESSION_BUSY", "another upload session is being opened", 409) from exc


def _create_locked(
    vault_root: Path, store: Path, binding: str, length: int, sha256: str, target: dict[str, Any],
    source_handoff: dict[str, Any] | None,
) -> tuple[Session, str]:
    now = _now()
    live = 0
    reserved = 0
    # The legacy flat directory stays readable by previous workers. Delegated
    # receipts live outside this scan, and cleanup stays outside the create lock.
    for existing_store in _existing_stores(vault_root):
        for meta in existing_store.glob("*.json"):
            record = _read_record_at(existing_store, meta.stem)
            if not record or record.get("state") not in _LIVE or _expired(record, now):
                continue
            if record.get("binding") == binding:
                live += 1
            if record.get("state") == RECEIVING:
                existing = Session(existing_store, record)
                reserved += max(int(record.get("length") or 0) - existing.offset(), 0)
    if live >= MAX_LIVE_PER_BINDING:
        raise SessionError(
            "UPLOAD_SESSION_QUOTA",
            f"at most {MAX_LIVE_PER_BINDING} upload sessions may be open at once; finish or cancel one",
            429,
        )
    with _vault_upload_room(vault_root) as room:
        if room is not None:
            same_volume = store.stat().st_dev == vault_root.stat().st_dev
            room.require(length + (reserved if same_volume else 0), upload_size=length)
    if reserved + length > shutil.disk_usage(store).free:
        raise SessionError("UPLOAD_SESSION_NO_SPACE", "not enough free disk space for this upload", 507)
    secret = secrets.token_urlsafe(32)
    prefix = SOURCE_ID_PREFIX if source_handoff is not None else ""
    session_id = prefix + secrets.token_hex(16)
    record = {
        "id": session_id,
        "binding": binding,
        "secret_sha256": _secret_hash(secret),
        "length": length,
        "sha256": sha256,
        "target": target,
        # An empty upload is complete when it is created (tus 1.0 creation).
        "state": RECEIVING if length else VERIFYING,
        "created": now,
        "expires": now + TTL_SECONDS,
        **({"source_handoff": source_handoff, "durable_offset": 0} if source_handoff is not None else {}),
    }
    part = store / f"{session_id}.part"
    # A startup sweep uses the same lock when it finds a part before its record.
    with _session_lock(store, session_id, timeout=_LOCK_WAIT_SECONDS):
        descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
        os.close(descriptor)
        try:
            _write_record(store, record)
        except BaseException:
            _remove(part)
            raise
        running = _running(session_id)
        running.size, running.digest = 0, hashlib.sha256()
    return Session(store, record, vault_root), secret


def open_session(vault_root: Path, session_id: str, secret: str | None) -> Session:
    """The session this id and secret name; anything else is one `NOT_FOUND`."""
    delegated = delegated_source(session_id)
    if not valid_session_id(session_id) or not valid_session_secret(secret or ""):
        raise _not_found()
    store = _store(vault_root, delegated=delegated)
    record = _read_record(store, session_id)
    if record is None or not hmac.compare_digest(
        str(record.get("secret_sha256")).encode(), _secret_hash(str(secret)).encode()
    ):
        raise _not_found()
    now = _now()
    expiry = _expiry_timestamp(record)
    if expiry is None:
        raise _not_found()
    if expiry <= now:
        try:
            record = _expire(store, session_id, vault_root, now)
        except Timeout:
            record = None
        if record is None:
            raise SessionError("UPLOAD_SESSION_EXPIRED", "this upload session has expired", 410)
    return Session(store, record, vault_root)


def _reconcile_source_part(session: Session) -> None:
    """Sync a recoverable tail before its offset becomes visible to other requests."""
    try:
        size = _part_size(session.part)
        offset = session.offset()
        if size == offset:
            return
        if not offset <= size <= session.length:
            raise SessionError("UPLOAD_SESSION_BUSY", "the held upload needs recovery", 503)
        with session.part.open("r+b") as source:
            os.fsync(source.fileno())
        record = {**session.record, "durable_offset": size, "expires": _now() + TTL_SECONDS}
        if size == session.length:
            record["state"] = VERIFYING
        _write_record(session.store, record)
    except OSError as exc:
        raise SessionError("UPLOAD_SESSION_BUSY", "the held upload could not be synced", 503) from exc
    session.record = record


def held_offset(session: Session) -> int:
    """The offset once any part still being written has landed, so a resume starts there."""
    if session.record["state"] != RECEIVING:
        return session.offset()
    try:
        with _session_lock(session.store, session.id, timeout=_LOCK_WAIT_SECONDS):
            current = _read_record(session.store, session.id)
            if current is None:
                raise _not_found()
            session.record = current
            if current["state"] == RECEIVING and delegated_source(session.id):
                _reconcile_source_part(session)
            return session.offset()
    except Timeout as exc:
        raise SessionError("UPLOAD_SESSION_BUSY", "a part of this upload is still being written", 409) from exc


def delete(session: Session) -> None:
    """Cancel a session and delete its bytes; a commit in flight is not cancelled."""
    committing = SessionError("UPLOAD_SESSION_COMMITTING", "this upload is being committed", 409)
    try:
        with _session_lock(session.store, session.id):
            current = _read_record(session.store, session.id)
            if current is not None and current.get("state") in (VERIFYING, COMMITTING):
                raise committing
            _drop(session.store, session.id, session.vault_root)
    except Timeout as exc:
        raise committing from exc


@contextmanager
def _vault_upload_room(vault_root: Path, *, reserve: bool = False):
    from .cli_ops import OpError

    try:
        with media_capacity.upload_room(vault_root, reserve=reserve) as room:
            yield room
    except OpError as error:
        raise SessionError(error.code, error.message, 503) from error


def commit_copy_count(vault_root: Path, target: dict) -> int:
    """Count the stages that coexist on the vault volume before a normal session installs."""
    from . import add, preserve, vault

    if target.get("archive") is not None:
        # A member upload meters the actual compressed output and can reuse existing blobs.
        return 0
    if target.get("lane") == "source":
        pooled = add.pools_named_original(vault_root, f"artifact{add.source_artifact_suffix(target['filename'])}")
    else:
        pooled = preserve.pools_evidence_original(vault_root, target["filename"])
    if pooled:
        return 2
    # Without a trustworthy generation, the workspace and final publication both copy.
    return 1 if vault.STAT_GENERATION_TRUSTED else 3


class Patch:
    """One part being appended: holds the session's writer lock until `close`."""

    def __init__(self, session: Session, offset: int, declared: int | None) -> None:
        if session.record["state"] != RECEIVING:
            raise SessionError("UPLOAD_SESSION_CLOSED", "this upload has all its bytes", 409)
        if declared is not None and declared > MAX_PATCH_BYTES:
            raise SessionError("TOO_LARGE", f"one request carries at most {MAX_PATCH_BYTES:,} bytes", 413)
        if declared is not None and offset + declared > session.length:
            raise SessionError("INVALID_UPLOAD", "the part runs past `Upload-Length`", 400)
        self.session = session
        self._lock = _session_lock(session.store, session.id, timeout=_LOCK_WAIT_SECONDS)
        try:
            self._lock.acquire()
        except Timeout as exc:
            raise SessionError("UPLOAD_SESSION_BUSY", "a part of this upload is still being written", 409) from exc
        self._running = _running(session.id)
        try:
            # Re-read under the lock: an earlier part may have ended or failed it.
            current = _read_record(session.store, session.id)
            if current is None or current.get("state") != RECEIVING or _expired(current, _now()):
                raise SessionError("UPLOAD_SESSION_CLOSED", "this upload has all its bytes", 409)
            session.record = current
            if delegated_source(session.id):
                _reconcile_source_part(session)
                if session.record["state"] == VERIFYING:
                    raise SessionError("UPLOAD_SESSION_CLOSED", "this upload has all its bytes", 409)
            size = session.offset()
            if offset != size:
                raise SessionError("UPLOAD_OFFSET_MISMATCH", f"the server holds {size} bytes", 409)
            if self._running.size != size or self._running.digest is None:
                # After a restart: one pass over the bytes already held.
                digest = hashlib.sha256()
                with session.part.open("rb") as source:
                    while chunk := source.read(_CHUNK):
                        digest.update(chunk)
                self._running.size, self._running.digest = size, digest
            self._sink = session.part.open("ab")
        except BaseException:
            self._lock.release()
            raise
        self.offset = offset
        self.written = 0

    def write(self, chunk: bytes) -> None:
        if self.written + len(chunk) > MAX_PATCH_BYTES:
            raise SessionError("TOO_LARGE", f"one request carries at most {MAX_PATCH_BYTES:,} bytes", 413)
        if self.offset + self.written + len(chunk) > self.session.length:
            raise SessionError("INVALID_UPLOAD", "the part runs past `Upload-Length`", 400)
        vault = self.session.vault_root
        offset = self.offset + self.written
        same_volume = vault is not None and self.session.store.stat().st_dev == vault.stat().st_dev
        reserve = (vault is not None and offset + len(chunk) == self.session.length
                   and media_capacity.managed_allocations(vault))
        copies = commit_copy_count(vault, self.session.record["target"]) if reserve and vault is not None else 0
        if vault is not None and (same_volume or copies):
            with media_capacity.commit_credit(self.session.id), _vault_upload_room(vault, reserve=bool(copies)) as room:
                if room is not None:
                    incoming = len(chunk) if same_volume else 0
                    if copies:
                        room.reserve_commit(self.session.length, copies, incoming=incoming, offset=offset)
                    else:
                        room.require(incoming, offset=offset, upload_size=self.session.length)
                self._sink.write(chunk)
                if same_volume:
                    # The next allocator must see these bytes in the volume's free-space accounting.
                    self._sink.flush()
        else:
            self._sink.write(chunk)
        self._running.digest.update(chunk)
        self.written += len(chunk)
        self._running.size = self.offset + self.written

    def close(self) -> tuple[int, bool]:
        """Make the received bytes durable; return the new offset and whether it is complete."""
        try:
            with self._sink:
                self._sink.flush()
                os.fsync(self._sink.fileno())
            record = dict(self.session.record)
            offset = self.offset + self.written
            complete = offset == self.session.length
            if self.written:
                record["expires"] = _now() + TTL_SECONDS
                if delegated_source(self.session.id):
                    record["durable_offset"] = offset
            if complete:
                record["state"] = VERIFYING
            if self.written or complete:
                _write_record(self.session.store, record)
            self.session.record = record
            return offset, complete
        finally:
            self._lock.release()


Committer = Callable[[Path, dict], dict]


def _fail(store: Path, record: dict, code: str, reason: str, vault_root: Path | None) -> None:
    _finish(store, {**record, "state": FAILED, "code": code, "reason": reason}, vault_root)


def _release_credit(vault_root: Path | None, session_id: str) -> None:
    if vault_root is None:
        return
    try:
        media_capacity.release_commit_credit(vault_root, session_id)
    except Exception:  # The terminal receipt survives and expiry can retire its credit again.
        log.warning("upload session credit cleanup will retry at expiry", exc_info=True)


def _commit(store: Path, session_id: str, committer: Committer, lock: FileLock, vault_root: Path | None) -> None:
    try:
        _commit_locked(store, session_id, committer, _running(session_id), vault_root)
    finally:
        lock.release()
    _schedule_reattempt(store, session_id, committer, vault_root)


def _schedule_reattempt(store: Path, session_id: str, committer: Committer, vault_root: Path | None) -> None:
    """Retry an eligible park and retire any parked session at its expiry.

    A client that sent every byte has nothing more to send, and the vault's contention is the
    server's to wait out. One timer owns the next retry or expiry. A park that needs a new
    client decision still releases its bytes and reserved room when its session expires.
    """
    record = _read_record(store, session_id)
    expiry = _expiry_timestamp(record)
    if record is None or record.get("state") != RETRYABLE or expiry is None:
        return
    with _RUNNING_LOCK:
        running = _RUNNING.get(session_id)
        if running is None:
            return
        wait = running.reattempt_wait
        remaining = expiry - _now()
        if remaining > 0:
            wait = min(wait, remaining) if record.get("code") in _REATTEMPT_CODES else remaining
        wake_at = time.monotonic() + wait
        if running.reattempt is not None:
            if running.reattempt_at <= wake_at:
                return
            running.reattempt.cancel()
        if record.get("code") in _REATTEMPT_CODES:
            running.reattempt_wait = min(running.reattempt_wait * 2, REATTEMPT_MAX_SECONDS)
        timer = threading.Timer(wait, lambda: _reattempt(store, session_id, committer, vault_root, timer))
        timer.name = f"upload-session-reattempt-{session_id[:8]}"
        timer.daemon = True
        running.reattempt = timer
        running.reattempt_at = wake_at
        timer.start()


def _reattempt(
    store: Path, session_id: str, committer: Committer, vault_root: Path | None, timer: threading.Timer,
) -> None:
    with _RUNNING_LOCK:
        running = _RUNNING.get(session_id)
        if running is None or running.reattempt is not timer:
            return
        running.reattempt = None
        running.reattempt_at = 0
    record = _read_record(store, session_id)
    expiry = _expiry_timestamp(record)
    if record is None or record.get("state") != RETRYABLE or expiry is None:
        return
    now = _now()
    if expiry <= now:
        from .cli_ops import OpError

        try:
            renewed = _expire(store, session_id, vault_root, now)
        except Timeout:
            renewed = record
        except (OpError, OSError, sqlite3.Error):
            log.warning("expired upload session cleanup will retry", exc_info=True)
            renewed = record
        if renewed is not None:
            _schedule_reattempt(store, session_id, committer, vault_root)
        return
    if record.get("code") not in _REATTEMPT_CODES:
        _schedule_reattempt(store, session_id, committer, vault_root)
        return
    # An empty context: nothing of the request that began the session, its principal included,
    # reaches the commit, which binds the transfer principal itself.
    contextvars.Context().run(start_commit, Session(store, record, vault_root), committer)


def _commit_locked(store: Path, session_id: str, committer: Committer, running: _Running, vault_root: Path | None) -> None:
    record = _read_record(store, session_id)
    if record is None or record.get("state") not in _COMMITTABLE:
        return
    part = store / f"{session_id}.part"
    size = _part_size(part)
    if running.size == size and running.digest is not None:
        digest = running.digest.hexdigest()
    else:
        hasher = hashlib.sha256()
        with part.open("rb") as source:
            while chunk := source.read(_CHUNK):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    if size != int(record["length"]) or not hmac.compare_digest(digest, record["sha256"]):
        _fail(store, record, "UPLOAD_SHA256_MISMATCH", "the bytes received do not match the declared SHA-256", vault_root)
        return
    from .mutation_journal import retrying_park
    from .writer_lease import caller_holds_no_request_worker

    try:
        # A refusal with the code of the park this retry started from adds no row and no count.
        # The commit thread is not a request worker: it waits in line without a waiter slot.
        with retrying_park(running.parked), caller_holds_no_request_worker():
            receipt = committer(part, record)
    except CommitFailed as exc:
        if not exc.retryable:
            _fail(store, record, exc.code, exc.reason, vault_root)
            return
        code, reason, refusal = exc.code, exc.reason, exc
    except Exception as exc:  # noqa: BLE001 - the bytes are sound, so a retry may succeed
        log.exception("upload session commit failed")
        code, reason = "UPLOAD_COMMIT_FAILED", "the upload could not be preserved yet"
        refusal = exc
    else:
        _finish(store, {**record, "state": COMMITTED, "receipt": receipt}, vault_root)
        return
    # The committer ran outside any request's dispatcher, so the park records itself. A poll retries a parked
    # session and parks it again for the same reason: that is one refusal, not a new one. A new process
    # starts without this memory, so it records one row for each park code.
    if running.parked != code:
        from .writer_lease import record_caller_refusal

        record_caller_refusal(refusal, tool="upload_session", code=code)
        running.parked = code
    _write_record(store, {**record, "state": RETRYABLE, "code": code, "reason": reason})


def _can_start(store: Path, record: dict) -> bool:
    if record.get("state") in _COMMITTABLE:
        return True
    if record.get("state") != RECEIVING or type(record.get("length")) is not int:
        return False
    part = store / f"{record['id']}.part"
    return _part_size(part) == record["length"]


def start_commit(session: Session, committer: Committer) -> None:
    """Admit one background commit after publishing its pending state.

    The thread runs in the starting request's context, as `/upload` runs its
    preserve. It is a daemon: a commit that a shutdown interrupts restarts the
    next time the session is read.
    """
    if not _can_start(session.store, session.record):
        return
    lock = _session_lock(session.store, session.id)
    try:
        try:
            lock.acquire(timeout=0)
        except Timeout:
            latest = _read_record(session.store, session.id)
            if latest is None or latest.get("state") != RETRYABLE:
                return
            # A failed worker can still hold its lock during cleanup; reuse the part wait budget.
            lock.acquire(timeout=_LOCK_WAIT_SECONDS)
    except Timeout:
        return
    try:
        record = _read_record(session.store, session.id)
        if record is None or _expired(record, _now()) or not _can_start(session.store, record):
            lock.release()
            if record is not None and record.get("state") == RETRYABLE and _expired(record, _now()):
                # A reattempt that read the clock just before expiry hands the park back to its timer,
                # which releases the bytes and reserved room. Nothing else would until the expiry sweep.
                _schedule_reattempt(session.store, session.id, committer, session.vault_root)
            return
        if record["state"] == RECEIVING:
            # A stopped PATCH may leave the last bytes before it publishes the verifying record.
            if delegated_source(session.id):
                session.record = record
                try:
                    _reconcile_source_part(session)
                except SessionError:
                    lock.release()
                    return
                record = session.record
            else:
                record = {**record, "state": VERIFYING}
                _write_record(session.store, record)
                session.record = record
        # Report a failure found under the lock even when GET captured the preceding attempt.
        if record["state"] == RETRYABLE:
            session.record = record
        accepted = {**record, "state": COMMITTING}
        accepted.pop("code", None)
        accepted.pop("reason", None)
        _write_record(session.store, accepted)
        # Keep the request snapshot: a GET that observed a failed attempt still reports it once.
        context = contextvars.copy_context()
        threading.Thread(
            target=context.run,
            args=(_commit, session.store, session.id, committer, lock, session.vault_root),
            name=f"upload-session-commit-{session.id[:8]}",
            daemon=True,
        ).start()
    except BaseException:
        lock.release()
        raise
