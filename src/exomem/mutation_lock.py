"""Process-safe, re-entrant serialization for one Exomem vault.

The lock file is runtime coordination state, not authority or durable vault
content.  A process-local ``RLock`` handles threads and nested command helpers;
an OS lock on the stable file handles separate processes.  The operating system
releases the latter when a process exits, so a leftover file is harmless.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .cli_ops import OpError

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_POLL_INTERVAL_SECONDS = 0.025
_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK})
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_DEFAULT_LONG_HOLDER_SECONDS = 30.0
logger = logging.getLogger(__name__)


def canonical_mutation_identity(vault_or_cell: os.PathLike[str] | str) -> str:
    """Return a stable, non-display identity for a vault path or opaque cell ID."""
    if isinstance(vault_or_cell, os.PathLike):
        resolved = Path(vault_or_cell).expanduser().resolve(strict=False)
        return f"vault:{os.path.normcase(str(resolved))}"
    value = str(vault_or_cell).strip()
    if not value:
        raise ValueError("mutation identity must not be empty")
    return f"cell:{value}"


@dataclass
class _LocalLockState:
    guard: threading.RLock = field(default_factory=threading.RLock)
    metadata_guard: threading.Lock = field(default_factory=threading.Lock)
    owner_thread: int | None = None
    depth: int = 0
    handle: BinaryIO | None = None
    request_id: str | None = None
    operation: str | None = None
    holder_kind: str | None = None
    acquired_at: float | None = None
    long_holder_seconds: float = _DEFAULT_LONG_HOLDER_SECONDS
    long_warning_emitted: bool = False


_LOCAL_STATES: dict[Path, _LocalLockState] = {}
_LOCAL_STATES_GUARD = threading.Lock()


def _state_for(lock_path: Path) -> _LocalLockState:
    with _LOCAL_STATES_GUARD:
        state = _LOCAL_STATES.get(lock_path)
        if state is None:
            state = _LocalLockState()
            _LOCAL_STATES[lock_path] = state
        return state


def _reset_in_forked_child() -> None:
    """Drop inherited thread state and close inherited lock descriptors.

    Closing an inherited descriptor in the child does not release the parent's
    descriptor.  It prevents a forked child from retaining the parent's OS lock
    indefinitely while giving the child fresh process-local ``RLock`` objects.
    """
    global _LOCAL_STATES, _LOCAL_STATES_GUARD
    for state in _LOCAL_STATES.values():
        if state.handle is not None:
            try:
                state.handle.close()
            except OSError:
                pass
    _LOCAL_STATES = {}
    _LOCAL_STATES_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_in_forked_child)


class VaultMutationCoordinator:
    """Serialize the complete read-plan-write boundary for one vault or cell."""

    def __init__(
        self,
        state_root: Path,
        vault_or_cell: os.PathLike[str] | str,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        long_holder_seconds: float = _DEFAULT_LONG_HOLDER_SECONDS,
    ) -> None:
        if timeout_seconds < 0:
            raise ValueError("mutation lock timeout must be non-negative")
        if poll_interval_seconds <= 0:
            raise ValueError("mutation lock poll interval must be positive")
        if long_holder_seconds <= 0:
            raise ValueError("mutation long-holder threshold must be positive")
        self.state_root = Path(state_root).expanduser().resolve(strict=False)
        self.identity = canonical_mutation_identity(vault_or_cell)
        digest = hashlib.sha256(self.identity.encode("utf-8")).hexdigest()
        self.lock_path = self.state_root / "mutation-locks" / f"{digest}.lock"
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.long_holder_seconds = long_holder_seconds

    @contextmanager
    def hold(
        self,
        *,
        timeout_seconds: float | None = None,
        request_id: str | None = None,
        operation: str | None = None,
        holder_kind: str = "unknown",
    ) -> Iterator[None]:
        """Hold both the local and OS mutation guards for the bounded interval."""
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout < 0:
            raise ValueError("mutation lock timeout must be non-negative")
        deadline = time.monotonic() + timeout
        state = _state_for(self.lock_path)
        remaining = max(0.0, deadline - time.monotonic())
        if not state.guard.acquire(timeout=remaining):
            raise _mutation_busy(self.snapshot())
        try:
            thread_id = threading.get_ident()
            if state.owner_thread == thread_id:
                state.depth += 1
                try:
                    yield
                finally:
                    state.depth -= 1
                return

            handle = self._open_lock_file()
            try:
                self._acquire_os_lock(handle, deadline)
            except Exception:
                handle.close()
                raise
            state.owner_thread = thread_id
            state.depth = 1
            state.handle = handle
            with state.metadata_guard:
                state.request_id = _safe_label(request_id, fallback="untracked")
                state.operation = _safe_label(operation, fallback="unknown")
                state.holder_kind = _safe_label(holder_kind, fallback="unknown")
                state.acquired_at = time.monotonic()
                state.long_holder_seconds = self.long_holder_seconds
                state.long_warning_emitted = False
            try:
                yield
            finally:
                with state.metadata_guard:
                    state.request_id = None
                    state.operation = None
                    state.holder_kind = None
                    state.acquired_at = None
                    state.long_warning_emitted = False
                state.depth = 0
                state.owner_thread = None
                state.handle = None
                try:
                    try:
                        _release_os_lock(handle)
                    except OSError:
                        # Closing the descriptor below releases OS ownership;
                        # do not mask a leaf exception or successful result.
                        pass
                finally:
                    handle.close()
        finally:
            state.guard.release()

    def snapshot(self) -> dict[str, object]:
        """Return bounded process-local holder metadata without vault identity/content."""
        state = _state_for(self.lock_path)
        return _snapshot_state(state, emit_warning=True)

    def _open_lock_file(self) -> BinaryIO:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.lock_path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            return handle
        except OSError as exc:
            raise OpError(
                "MUTATION_LOCK_UNAVAILABLE",
                f"vault mutation lock could not be opened (host error {exc.errno})",
                "Check that the configured runtime state root exists and is writable.",
            ) from None

    def _acquire_os_lock(self, handle: BinaryIO, deadline: float) -> None:
        while True:
            try:
                if _try_os_lock(handle):
                    return
            except OSError as exc:
                raise OpError(
                    "MUTATION_LOCK_UNAVAILABLE",
                    f"vault mutation authority could not be established (host error {exc.errno})",
                    "Check runtime state storage and the host locking implementation.",
                ) from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _mutation_busy()
            time.sleep(min(self.poll_interval_seconds, remaining))


def _try_os_lock(handle: BinaryIO) -> bool:
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno in _BUSY_ERRNOS:
            return False
        raise
    return True


def _release_os_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_label(value: object, *, fallback: str) -> str:
    candidate = str(value or "").strip()
    return candidate if _SAFE_LABEL.fullmatch(candidate) else fallback


def _snapshot_state(
    state: _LocalLockState, *, emit_warning: bool
) -> dict[str, object]:
    with state.metadata_guard:
        if state.owner_thread is None or state.acquired_at is None:
            return {"state": "free"}
        age = max(0.0, time.monotonic() - state.acquired_at)
        overdue = age >= state.long_holder_seconds
        snapshot: dict[str, object] = {
            "state": "held",
            "request_id": state.request_id or "untracked",
            "operation": state.operation or "unknown",
            "holder_kind": state.holder_kind or "unknown",
            "age_seconds": round(age, 3),
            "overdue": overdue,
        }
        if overdue and emit_warning and not state.long_warning_emitted:
            state.long_warning_emitted = True
            logger.warning(
                "vault mutation boundary held too long request_id=%s operation=%s "
                "holder_kind=%s age_seconds=%.3f",
                snapshot["request_id"],
                snapshot["operation"],
                snapshot["holder_kind"],
                age,
            )
        return snapshot


def active_mutation_snapshot() -> dict[str, object]:
    """Return the oldest process-local holder without exposing vault identity."""
    with _LOCAL_STATES_GUARD:
        states = tuple(_LOCAL_STATES.values())
    held = [
        snapshot
        for state in states
        if (snapshot := _snapshot_state(state, emit_warning=True))["state"] == "held"
    ]
    if not held:
        return {"state": "free"}
    return max(held, key=lambda item: float(item["age_seconds"]))


def _mutation_busy(snapshot: dict[str, object] | None = None) -> OpError:
    details: dict[str, object] = {
        "status": "retryable",
        "committed": False,
        "retry_after_ms": 750,
    }
    if snapshot and snapshot.get("state") == "held":
        details["holder"] = snapshot
    return OpError(
        "MUTATION_BUSY",
        "vault mutation boundary is busy",
        "Retry after the current mutation completes; inspect cell health if it remains busy.",
        details=details,
    )
