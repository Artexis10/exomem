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


def _reset_in_forked_child() -> None:
    """Discard parent thread activity without acquiring its possibly held lock."""
    global _FOREGROUND, _LOCAL, _LOCK
    _FOREGROUND = {}
    _LOCAL = threading.local()
    _LOCK = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_in_forked_child)


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
    thread_id = threading.get_ident()
    prior_depth_present = hasattr(_LOCAL, "foreground_depth")
    prior_depth = _foreground_depth()
    prior_holder_count = 0
    registration_started = False
    depth_started = False
    suppressed_scopes: list[tuple[_BackgroundScope, int]] = []
    try:
        with _LOCK:
            holders = _FOREGROUND.get(canonical)
            prior_holder_count = 0 if holders is None else holders.get(thread_id, 0)
            registration_started = True
            if holders is None:
                _FOREGROUND[canonical] = {thread_id: 1}
            else:
                holders[thread_id] = prior_holder_count + 1
        depth_started = True
        _LOCAL.foreground_depth = prior_depth + 1
        for scope in stack:
            suppressed_scopes.append((scope, scope.suppressed))
            scope.suppressed += 1
        yield
    finally:
        for scope, prior_suppression in reversed(suppressed_scopes):
            scope.suppressed = prior_suppression
        if depth_started:
            if prior_depth_present:
                _LOCAL.foreground_depth = prior_depth
            else:
                try:
                    delattr(_LOCAL, "foreground_depth")
                except AttributeError:
                    pass
        if registration_started:
            with _LOCK:
                holders = _FOREGROUND.get(canonical)
                if holders is not None:
                    if prior_holder_count:
                        holders[thread_id] = prior_holder_count
                    else:
                        holders.pop(thread_id, None)
                    if not holders:
                        _FOREGROUND.pop(canonical, None)


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
