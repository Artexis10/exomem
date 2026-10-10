"""Resumable upload sessions: one file sent in parts, preserved once its SHA-256 matches.

`/upload/sessions` speaks the tus 1.0 core protocol with its creation,
expiration and termination extensions (see `server_transfer`). This module is
the session store under the routes: a `.part` file and a JSON record per
session, in a private directory under the vault's state directory, the same
shape as `held_uploads`. The client declares the whole file's length and
SHA-256 at creation; the server keeps a running SHA-256 in memory and, after a
restart, re-hashes the `.part` file once before it accepts more bytes.

A session is addressed by its id and authenticated by a 256-bit secret that
only the client holds; the record keeps its SHA-256. An unknown id, a missing
secret and a wrong one get the same answer. The record moves through
`receiving`, `verifying`, `committing`, then `committed` or `failed` with a
stable code. The final part returns at once and a background thread verifies
and commits; a mismatch fails the session and deletes its bytes, and the bytes
are deleted after any commit. A session expires `TTL_SECONDS` after its last
accepted part, and a sweep at creation and at service start removes expired
sessions with their bytes.
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
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

RECEIVING = "receiving"
VERIFYING = "verifying"
COMMITTING = "committing"
COMMITTED = "committed"
FAILED = "failed"
_LIVE = (RECEIVING, VERIFYING, COMMITTING)

_SESSION_ID = re.compile(r"[0-9a-f]{32}")
_SECRET = re.compile(r"[A-Za-z0-9_-]{43}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1024 * 1024
#: How long a part waits for an earlier part of the same session to finish.
_LOCK_WAIT_SECONDS = 30.0
_now = time.time


class SessionError(Exception):
    """A content-free refusal with the HTTP status the route answers."""

    def __init__(self, code: str, reason: str, status: int) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.status = status


class CommitFailed(Exception):
    """A commit refused with a stable code; the session fails with it."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _not_found() -> SessionError:
    return SessionError("UPLOAD_SESSION_NOT_FOUND", "no upload session answers this request", 404)


def max_bytes_from_env() -> int:
    """The longest session; a malformed setting falls back to the default."""
    raw = os.environ.get(MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    if raw.isascii() and raw.isdigit() and int(raw) > 0:
        return int(raw)
    log.warning("%s is not a positive integer; using %d", MAX_BYTES_ENV, DEFAULT_MAX_BYTES)
    return DEFAULT_MAX_BYTES


@dataclass
class _Running:
    """Per-session process state: the writer lock and the running digest."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    size: int = -1
    digest: Any = None
    committing: bool = False


_RUNNING: dict[str, _Running] = {}
_RUNNING_LOCK = threading.Lock()


def _running(session_id: str) -> _Running:
    with _RUNNING_LOCK:
        return _RUNNING.setdefault(session_id, _Running())


def _forget(session_id: str) -> None:
    with _RUNNING_LOCK:
        _RUNNING.pop(session_id, None)


def _store(vault_root: Path) -> Path:
    from .state_paths import ensure_vault_state_dir

    store = ensure_vault_state_dir(vault_root) / STORE_DIRNAME
    store.mkdir(mode=0o700, exist_ok=True)
    info = os.lstat(store)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"upload-session store is not a directory: {store}")
    if os.name == "posix":
        if info.st_uid != os.geteuid():
            raise OSError(f"upload-session store is owned by another user: {store}")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(store, 0o700)
    return store


def _secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _remove(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _write_record(store: Path, record: dict) -> None:
    fd, raw = tempfile.mkstemp(prefix=f"{record['id']}.", suffix=".tmp", dir=store)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as sink:
            json.dump(record, sink)
        os.replace(raw, store / f"{record['id']}.json")
    except BaseException:
        _remove(Path(raw))
        raise


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
    _remove(store / f"{session_id}.part", store / f"{session_id}.json")
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
    """At service start: sweep an existing store; return the commits a stop interrupted."""
    from .state_paths import vault_state_dir

    if not (vault_state_dir(vault_root) / STORE_DIRNAME).is_dir():
        return []
    sweep(vault_root)
    store = _store(vault_root)
    pending = []
    for meta in store.glob("*.json"):
        record = _read_record(store, meta.stem)
        if record and record.get("state") in (VERIFYING, COMMITTING):
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
    if session.record["state"] in (VERIFYING, COMMITTING):
        raise SessionError("UPLOAD_SESSION_COMMITTING", "this upload is being committed", 409)
    running = _running(session.id)
    if not running.lock.acquire(timeout=_LOCK_WAIT_SECONDS):
        raise SessionError("UPLOAD_SESSION_BUSY", "a part of this upload is still being written", 409)
    try:
        _drop(session.store, session.id)
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


def _commit(store: Path, session_id: str, committer: Committer) -> None:
    running = _running(session_id)
    try:
        record = _read_record(store, session_id)
        if record is None or record.get("state") not in (VERIFYING, COMMITTING):
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
        record = {**record, "state": COMMITTING}
        _write_record(store, record)
        try:
            receipt = committer(part, record)
        except CommitFailed as exc:
            _fail(store, record, exc.code, exc.reason)
            return
        except Exception:  # noqa: BLE001 - a session must end in a terminal state
            log.exception("upload session commit failed")
            _fail(store, record, "UPLOAD_COMMIT_FAILED", "the upload could not be preserved")
            return
        _write_record(store, {**record, "state": COMMITTED, "receipt": receipt})
        _remove(part)
        _forget(session_id)
    finally:
        running.committing = False


def start_commit(session: Session, committer: Committer) -> None:
    """Verify and commit a complete session in the background, once per process.

    The thread runs in the starting request's context, as `/upload` runs its
    preserve. It is a daemon: a commit that a shutdown interrupts restarts the
    next time the session is read.
    """
    if session.record["state"] not in (VERIFYING, COMMITTING):
        return
    running = _running(session.id)
    with _RUNNING_LOCK:
        if running.committing:
            return
        running.committing = True
    context = contextvars.copy_context()
    threading.Thread(
        target=context.run,
        args=(_commit, session.store, session.id, committer),
        name=f"upload-session-commit-{session.id[:8]}",
        daemon=True,
    ).start()
