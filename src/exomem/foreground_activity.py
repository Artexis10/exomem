"""Small process-local hint for background scans sharing a foreground vault."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_PAUSE_BUDGET_SECONDS = 0.050
_PAUSE_INCREMENT_SECONDS = 0.005
_LOCK = threading.RLock()
_LOCAL = threading.local()
_FOREGROUND: dict[Path, dict[int, int]] = {}


@dataclass
class _BackgroundScope:
    canonical: Path
    spellings: frozenset[str]
    waiter_bypass: Callable[[], bool] | None
    suppressed: int = 0


def _canonical(vault_root: os.PathLike[str] | str) -> Path | None:
    try:
        return Path(vault_root).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _background_stack() -> list[_BackgroundScope]:
    stack = getattr(_LOCAL, "background_stack", None)
    if stack is None:
        stack = []
        _LOCAL.background_stack = stack
    return stack


def _foreground_depth() -> int:
    return int(getattr(_LOCAL, "foreground_depth", 0))


@contextmanager
def foreground_scope(vault_root: os.PathLike[str] | str) -> Iterator[None]:
    """Mark a complete foreground invocation and suppress this thread's scan scope."""
    canonical = _canonical(vault_root)
    if canonical is None:
        yield
        return
    stack = _background_stack()
    _LOCAL.foreground_depth = _foreground_depth() + 1
    for scope in stack:
        scope.suppressed += 1
    thread_id = threading.get_ident()
    with _LOCK:
        holders = _FOREGROUND.setdefault(canonical, {})
        holders[thread_id] = holders.get(thread_id, 0) + 1
    try:
        yield
    finally:
        with _LOCK:
            current_holders = _FOREGROUND.get(canonical)
            if current_holders is not None:
                remaining = current_holders.get(thread_id, 0) - 1
                if remaining > 0:
                    current_holders[thread_id] = remaining
                else:
                    current_holders.pop(thread_id, None)
                if not current_holders:
                    _FOREGROUND.pop(canonical, None)
        for scope in stack:
            scope.suppressed -= 1
        _LOCAL.foreground_depth = _foreground_depth() - 1


@contextmanager
def background_scope(
    vault_root: os.PathLike[str] | str,
    *,
    waiter_bypass: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Bind one explicitly background scan to its vault without hot-path resolution."""
    canonical = _canonical(vault_root)
    if canonical is None:
        yield
        return
    scope = _BackgroundScope(
        canonical,
        frozenset({os.fspath(vault_root), str(canonical)}),
        waiter_bypass,
        _foreground_depth(),
    )
    stack = _background_stack()
    stack.append(scope)
    try:
        yield
    finally:
        stack.pop()


def foreground_active(vault_root: os.PathLike[str] | str) -> bool:
    """Test seam for a live foreground holder of one canonical vault."""
    canonical = _canonical(vault_root)
    if canonical is None:
        return False
    with _LOCK:
        return bool(_FOREGROUND.get(canonical))


def background_active(vault_root: os.PathLike[str] | str) -> bool:
    """Test seam for the active thread's unsuppressed matching scan scope."""
    stack = _background_stack()
    if not stack:
        return False
    spelling = os.fspath(vault_root)
    return any(
        not scope.suppressed and spelling in scope.spellings for scope in stack
    )


def _other_foreground_active(scope: _BackgroundScope) -> bool:
    thread_id = threading.get_ident()
    with _LOCK:
        return any(holder != thread_id and count for holder, count in _FOREGROUND.get(scope.canonical, {}).items())


def checkpoint(vault_root: os.PathLike[str] | str) -> None:
    """Yield a bounded amount only from an explicit scan to another foreground thread."""
    stack = _background_stack()
    if not stack:
        return
    spelling = os.fspath(vault_root)
    scope = next(
        (
            item
            for item in reversed(stack)
            if not item.suppressed and spelling in item.spellings
        ),
        None,
    )
    if scope is None:
        return
    deadline = time.monotonic() + _PAUSE_BUDGET_SECONDS
    while True:
        if scope.waiter_bypass is not None and scope.waiter_bypass():
            return
        if not _other_foreground_active(scope):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(_PAUSE_INCREMENT_SECONDS, remaining))
