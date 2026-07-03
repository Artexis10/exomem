"""Warm-phase readiness registry (OpenSpec: add-instant-start-boot).

Boot no longer blocks on model preloads or cache warm-up: `warmup.start_background`
runs everything on a daemon thread while the transport serves requests. This module
is the coordination point between that thread and the request paths.

The hazard it exists to prevent is LOCK-BLOCKING, not exceptions: the model
singletons in `embeddings` use double-checked locking, so a hybrid `find` that
calls `get_model()` while the warm thread is inside it would block for the full
load (~30s warm, minutes on a first-ever download) — and the existing
ImportError soft-degrade would never fire. Request paths therefore ask
`should_defer(component)` BEFORE touching a model getter and skip the lane while
the warm is in flight.

Semantics are deliberately narrow: `should_defer` is True only while a warm is
active and the component unready. Once `finish_warm` runs — success or failure —
it is False forever, so a failed preload falls back to today's inline lazy-load
+ soft-degrade behavior exactly. No warm ever begun (e.g. EXOMEM_DISABLE_WARMUP)
means nothing defers, which is also today's behavior.

Writers use `defer(component, item)` to park re-embed work during the warm;
`mark_ready` sets the component's event and drains those items under one lock,
so an item is either embedded inline (defer returned False) or drained exactly
once — never lost in the set-event/drain window.

Pure substrate: process telemetry only. Nothing here reasons over notes.
"""

from __future__ import annotations

import threading
import time

COMPONENTS = ("lexical", "embeddings", "reranker", "clip")

_lock = threading.Lock()
_events: dict[str, threading.Event] = {c: threading.Event() for c in COMPONENTS}
_deferred: dict[str, list] = {c: [] for c in COMPONENTS}
_warm_active = False
_warm_finished = False
_started_at: float | None = None


def begin_warm() -> None:
    """Mark a warm in-flight. Resets per-component events and deferred items."""
    global _warm_active, _warm_finished, _started_at
    with _lock:
        for c in COMPONENTS:
            _events[c].clear()
            _deferred[c].clear()
        _warm_active = True
        _warm_finished = False
        _started_at = time.monotonic()


def finish_warm() -> None:
    """End the warm window permanently (until the next `begin_warm`).

    Components whose events are still unset (failed/skipped preloads) stop
    deferring — request paths return to inline lazy-load semantics.
    """
    global _warm_finished
    with _lock:
        _warm_finished = True


def mark_ready(component: str) -> list:
    """Set `component`'s event; atomically drain and return its deferred items."""
    _check(component)
    with _lock:
        _events[component].set()
        drained = _deferred[component]
        _deferred[component] = []
        return drained


def drain_deferred(component: str) -> list:
    """Atomically drain and return `component`'s deferred items WITHOUT marking
    it ready.

    For the FAILED-preload path: a model whose load raised must stay not-ready
    (so request paths keep their inline lazy-load + soft-degrade fallback for the
    rest of the warm), but the write-embed work parked during the warm must not
    be stranded in the deferred queue forever. This empties the queue so the
    caller can replay (or discard) it, leaving the readiness event untouched.
    Shares `_lock` with `defer`/`mark_ready` so a racing `defer` can't be lost.
    """
    _check(component)
    with _lock:
        drained = _deferred[component]
        _deferred[component] = []
        return drained


def is_ready(component: str) -> bool:
    _check(component)
    return _events[component].is_set()


def is_warming() -> bool:
    with _lock:
        return _warm_active and not _warm_finished


def should_defer(component: str) -> bool:
    """True IFF a warm is active, unfinished, and `component` isn't ready yet."""
    _check(component)
    with _lock:
        return _warm_active and not _warm_finished and not _events[component].is_set()


def defer(component: str, item) -> bool:
    """Atomically record `item` for the post-warm drain when deferring.

    Returns True when recorded (caller must skip the work), False when the
    caller should proceed inline. Shares `_lock` with `mark_ready` so a racing
    drain can't lose the item.
    """
    _check(component)
    with _lock:
        if _warm_active and not _warm_finished and not _events[component].is_set():
            _deferred[component].append(item)
            return True
        return False


def warming_info() -> dict | None:
    """{"components": [unready names], "since_s": seconds} while warming, else None."""
    with _lock:
        if not (_warm_active and not _warm_finished):
            return None
        since = 0.0 if _started_at is None else time.monotonic() - _started_at
        return {
            "components": [c for c in COMPONENTS if not _events[c].is_set()],
            "since_s": round(since, 1),
        }


def wait(component: str, timeout: float | None = None) -> bool:
    _check(component)
    return _events[component].wait(timeout)


def snapshot() -> dict:
    with _lock:
        return {
            "warming": _warm_active and not _warm_finished,
            "ready": {c: _events[c].is_set() for c in COMPONENTS},
            "deferred_counts": {c: len(_deferred[c]) for c in COMPONENTS},
        }


def reset() -> None:
    """Test hook: return to the never-warmed state (mirrors find.clear_cache)."""
    global _warm_active, _warm_finished, _started_at
    with _lock:
        for c in COMPONENTS:
            _events[c].clear()
            _deferred[c].clear()
        _warm_active = False
        _warm_finished = False
        _started_at = None


def _check(component: str) -> None:
    if component not in _events:
        raise ValueError(f"unknown readiness component: {component!r}")
