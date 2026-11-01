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
file lock lets one process at a time commit a session, and a store-wide one
serialises creation, so its limits hold across processes.

A session expires `TTL_SECONDS` after its last accepted part, and a sweep at
creation and at the serving runtime's start removes expired sessions with
their bytes.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from . import private_state

log = logging.getLogger(__name__)

STORE_DIRNAME = "upload-sessions"
#: A session lives this long after its last accepted part.
TTL_SECONDS = 24 * 3600
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
# The codes a busy or still-warming vault gives; they are `OpError` codes that the vault's own
# lock, store and warm-up fix, so this set is closed by them. Other retryable codes (a full
# disk, an unknown outcome) need the client or an operator, so the server leaves them alone.
# nosemgrep: ep-word-set -- these are the vault's documented transient error codes.
_REATTEMPT_CODES = frozenset({"MUTATION_BUSY", "COLLECTION_STORE_BUSY", "MUTATION_WARMING"})
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
    """Per-session process state: the writer lock and the running digest."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    size: int = -1
    digest: Any = None
    parked: str | None = None  # the code of the park last recorded for a caller
    # The one pending re-attempt, and the wait that the next one gets.
    reattempt: threading.Timer | None = None
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


def _store(vault_root: Path) -> Path:
    return private_state.store(vault_root, STORE_DIRNAME)


def _secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


_remove = private_state.remove


def _write_record(store: Path, record: dict) -> None:
    # `sweep` owns the `.tmp` temp; `.part` here is a session's bytes.
    private_state.write_record(store, record["id"], record, temp_suffix=".tmp")


def _commit_lock(store: Path, session_id: str) -> FileLock:
    """The lock a process holds while it commits this session; any other process skips it."""
    # Admission and the daemon share this instance across threads.
    return FileLock(store / f"{session_id}.lock", timeout=0, thread_local=False)


def _read_record(store: Path, session_id: str) -> dict | None:
    try:
        record = json.loads((store / f"{session_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _part_size(part: Path) -> int:
    try:
        return part.stat().st_size
    except FileNotFoundError:
        return 0


def _drop(store: Path, session_id: str) -> None:
    # A released lock removes its own file; this one is what a killed commit left.
    _remove(store / f"{session_id}.part", store / f"{session_id}.json", store / f"{session_id}.lock")
    _forget(session_id)


def sweep(vault_root: Path) -> None:
    """Remove expired sessions with their bytes, and anything a crash left behind."""
    store = _store(vault_root)
    now = _now()
    for meta in store.glob("*.json"):
        record = _read_record(store, meta.stem)
        try:
            expires = float(record["expires"]) if record else 0.0
        except (KeyError, TypeError, ValueError):
            expires = 0.0
        if expires <= now:
            _drop(store, meta.stem)
    for leftover in (*store.glob("*.part"), *store.glob("*.tmp")):
        session_id = leftover.name.split(".", 1)[0]
        if leftover.suffix == ".part" and (store / f"{session_id}.json").exists():
            continue
        try:
            stale = leftover.stat().st_mtime + TTL_SECONDS <= now
        except OSError:
            continue
        if stale:
            _remove(leftover)


@dataclass
class Session:
    """One authenticated session: its record as read, and its store."""

    store: Path
    record: dict

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
    from .state_paths import vault_state_dir

    if not (vault_state_dir(vault_root) / STORE_DIRNAME).is_dir():
        return []
    sweep(vault_root)
    store = _store(vault_root)
    pending = []
    for meta in store.glob("*.json"):
        record = _read_record(store, meta.stem)
        if record and record.get("state") in _COMMITTABLE:
            pending.append(Session(store, record))
    return pending


def create(
    vault_root: Path,
    *,
    binding: str,
    length: int,
    sha256: str,
    target: dict[str, Any],
    max_bytes: int,
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
            return _create_locked(vault_root, store, binding, length, sha256, target)
    except Timeout as exc:
        raise SessionError("UPLOAD_SESSION_BUSY", "another upload session is being opened", 409) from exc


def _create_locked(
    vault_root: Path, store: Path, binding: str, length: int, sha256: str, target: dict[str, Any]
) -> tuple[Session, str]:
    sweep(vault_root)
    live = 0
    reserved = 0
    for meta in store.glob("*.json"):
        record = _read_record(store, meta.stem)
        if not record or record.get("state") not in _LIVE:
            continue
        if record.get("binding") == binding:
            live += 1
        if record.get("state") == RECEIVING:
            reserved += max(int(record.get("length") or 0) - _part_size(store / f"{meta.stem}.part"), 0)
    if live >= MAX_LIVE_PER_BINDING:
        raise SessionError(
            "UPLOAD_SESSION_QUOTA",
            f"at most {MAX_LIVE_PER_BINDING} upload sessions may be open at once; finish or cancel one",
            429,
        )
    if reserved + length > shutil.disk_usage(store).free:
        raise SessionError("UPLOAD_SESSION_NO_SPACE", "not enough free disk space for this upload", 507)
    secret = secrets.token_urlsafe(32)
    session_id = secrets.token_hex(16)
    now = _now()
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
    }
    descriptor = os.open(
        store / f"{session_id}.part",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    os.close(descriptor)
    try:
        _write_record(store, record)
    except BaseException:
        _remove(store / f"{session_id}.part")
        raise
    running = _running(session_id)
    running.size, running.digest = 0, hashlib.sha256()
    return Session(store, record), secret


def open_session(vault_root: Path, session_id: str, secret: str | None) -> Session:
    """The session this id and secret name; anything else is one `NOT_FOUND`."""
    if not _SESSION_ID.fullmatch(session_id or "") or not _SECRET.fullmatch(secret or ""):
        raise _not_found()
    store = _store(vault_root)
    record = _read_record(store, session_id)
    if record is None or not hmac.compare_digest(
        str(record.get("secret_sha256")).encode(), _secret_hash(str(secret)).encode()
    ):
        raise _not_found()
    session = Session(store, record)
    try:
        expired = session.expires <= _now()
    except (KeyError, TypeError, ValueError):
        expired = True
    if expired:
        _drop(store, session_id)
        raise SessionError("UPLOAD_SESSION_EXPIRED", "this upload session has expired", 410)
    return session


def held_offset(session: Session) -> int:
    """The offset once any part still being written has landed, so a resume starts there."""
    running = _running(session.id)
    if not running.lock.acquire(timeout=_LOCK_WAIT_SECONDS):
        raise SessionError("UPLOAD_SESSION_BUSY", "a part of this upload is still being written", 409)
    try:
        return session.offset()
    finally:
        running.lock.release()


def delete(session: Session) -> None:
    """Cancel a session and delete its bytes; a commit in flight is not cancelled."""
    committing = SessionError("UPLOAD_SESSION_COMMITTING", "this upload is being committed", 409)
    running = _running(session.id)
    if not running.lock.acquire(timeout=_LOCK_WAIT_SECONDS):
        raise SessionError("UPLOAD_SESSION_BUSY", "a part of this upload is still being written", 409)
    try:
        # The commit lock keeps a retry in any process from starting on bytes being removed.
        with _commit_lock(session.store, session.id):
            # Re-read under both locks: the final part may have moved it to `verifying`.
            current = _read_record(session.store, session.id)
            if current is not None and current.get("state") in (VERIFYING, COMMITTING):
                raise committing
            _drop(session.store, session.id)
    except Timeout as exc:
        raise committing from exc
    finally:
        running.lock.release()


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
        self._running = _running(session.id)
        if not self._running.lock.acquire(timeout=_LOCK_WAIT_SECONDS):
            raise SessionError("UPLOAD_SESSION_BUSY", "a part of this upload is still being written", 409)
        try:
            # Re-read under the lock: an earlier part may have ended or failed it.
            current = _read_record(session.store, session.id)
            if current is None or current.get("state") != RECEIVING:
                raise SessionError("UPLOAD_SESSION_CLOSED", "this upload has all its bytes", 409)
            session.record = current
            size = _part_size(session.part)
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
            self._running.lock.release()
            raise
        self.offset = offset
        self.written = 0

    def write(self, chunk: bytes) -> None:
        if self.written + len(chunk) > MAX_PATCH_BYTES:
            raise SessionError("TOO_LARGE", f"one request carries at most {MAX_PATCH_BYTES:,} bytes", 413)
        if self.offset + self.written + len(chunk) > self.session.length:
            raise SessionError("INVALID_UPLOAD", "the part runs past `Upload-Length`", 400)
        self._sink.write(chunk)
        self._running.digest.update(chunk)
        self.written += len(chunk)
        self._running.size = self.offset + self.written

    def close(self) -> tuple[int, bool]:
        """Make the received bytes durable; return the new offset and whether it is complete."""
        try:
            self._sink.flush()
            os.fsync(self._sink.fileno())
            self._sink.close()
            record = dict(self.session.record)
            offset = self.offset + self.written
            complete = offset == self.session.length
            if self.written:
                record["expires"] = _now() + TTL_SECONDS
            if complete:
                record["state"] = VERIFYING
            if self.written or complete:
                _write_record(self.session.store, record)
            self.session.record = record
            return offset, complete
        finally:
            self._running.lock.release()


Committer = Callable[[Path, dict], dict]


def _fail(store: Path, record: dict, code: str, reason: str) -> None:
    _remove(store / f"{record['id']}.part")
    _write_record(store, {**record, "state": FAILED, "code": code, "reason": reason})
    _forget(record["id"])


def _commit(store: Path, session_id: str, committer: Committer, lock: FileLock) -> None:
    try:
        _commit_locked(store, session_id, committer, _running(session_id))
    finally:
        lock.release()
    _schedule_reattempt(store, session_id, committer)


def _schedule_reattempt(store: Path, session_id: str, committer: Committer) -> None:
    """After a commit that a busy vault left retryable, start it again later, with no client.

    A client that sent every byte has nothing more to send, and the vault's contention is the
    server's to wait out. One timer waits per session, so a client that also reads the session
    and starts a commit does not add a second one; the session's expiry ends the attempts.
    """
    record = _read_record(store, session_id)
    if record is None or record.get("state") != RETRYABLE or record.get("code") not in _REATTEMPT_CODES:
        return
    with _RUNNING_LOCK:
        running = _RUNNING.get(session_id)
        if running is None or running.reattempt is not None:
            return
        wait = running.reattempt_wait
        running.reattempt_wait = min(wait * 2, REATTEMPT_MAX_SECONDS)
        timer = threading.Timer(wait, _reattempt, args=(store, session_id, committer))
        timer.name = f"upload-session-reattempt-{session_id[:8]}"
        timer.daemon = True
        running.reattempt = timer
        timer.start()


def _reattempt(store: Path, session_id: str, committer: Committer) -> None:
    with _RUNNING_LOCK:
        running = _RUNNING.get(session_id)
        if running is None:
            return
        running.reattempt = None
    record = _read_record(store, session_id)
    try:
        expired = record is None or float(record["expires"]) <= _now()
    except (KeyError, TypeError, ValueError):
        expired = True
    if expired or record["state"] != RETRYABLE or record.get("code") not in _REATTEMPT_CODES:
        return
    # An empty context: nothing of the request that began the session, its principal included,
    # reaches the commit, which binds the transfer principal itself.
    contextvars.Context().run(start_commit, Session(store, record), committer)


def _commit_locked(store: Path, session_id: str, committer: Committer, running: _Running) -> None:
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
        _fail(store, record, "UPLOAD_SHA256_MISMATCH", "the bytes received do not match the declared SHA-256")
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
            _fail(store, record, exc.code, exc.reason)
            return
        code, reason, refusal = exc.code, exc.reason, exc
    except Exception as exc:  # noqa: BLE001 - the bytes are sound, so a retry may succeed
        log.exception("upload session commit failed")
        code, reason = "UPLOAD_COMMIT_FAILED", "the upload could not be preserved yet"
        refusal = exc
    else:
        _write_record(store, {**record, "state": COMMITTED, "receipt": receipt})
        _remove(part)
        _forget(session_id)
        return
    # The committer ran outside any request's dispatcher, so the park records itself. A poll retries a parked
    # session and parks it again for the same reason: that is one refusal, not a new one. A new process
    # starts without this memory, so it records one row for each park code.
    if running.parked != code:
        from .writer_lease import record_caller_refusal

        record_caller_refusal(refusal, tool="upload_session", code=code)
        running.parked = code
    _write_record(store, {**record, "state": RETRYABLE, "code": code, "reason": reason})


def start_commit(session: Session, committer: Committer) -> None:
    """Admit one background commit after publishing its pending state.

    The thread runs in the starting request's context, as `/upload` runs its
    preserve. It is a daemon: a commit that a shutdown interrupts restarts the
    next time the session is read.
    """
    if session.record["state"] not in _COMMITTABLE:
        return
    lock = _commit_lock(session.store, session.id)
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
        if record is None or record.get("state") not in _COMMITTABLE:
            lock.release()
            return
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
            args=(_commit, session.store, session.id, committer, lock),
            name=f"upload-session-commit-{session.id[:8]}",
            daemon=True,
        ).start()
    except BaseException:
        lock.release()
        raise
