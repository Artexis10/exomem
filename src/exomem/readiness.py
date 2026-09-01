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
from pathlib import Path

COMPONENTS = (
    "retrieval_catalog",
    "lexical",
    "semantic_corpus",
    "embeddings",
    "reranker",
    "clip",
)

_lock = threading.Lock()
_events: dict[str, threading.Event] = {c: threading.Event() for c in COMPONENTS}
_deferred: dict[str, list] = {c: [] for c in COMPONENTS}
_warm_active = False
_warm_finished = False
_runtime_managed = False
_started_at: float | None = None
_retrieval_generation = 0


def manage_runtime() -> None:
    """Declare that this process serves requests through managed activation."""
    global _retrieval_generation, _runtime_managed
    with _lock:
        _runtime_managed = True
        _retrieval_generation += 1


def unmanage_runtime() -> None:
    """Return request admission to the walk-backed offline contract."""
    global _retrieval_generation, _runtime_managed
    with _lock:
        _runtime_managed = False
        _retrieval_generation += 1


def runtime_managed() -> bool:
    """Whether request admission is owned by the activated server runtime."""
    with _lock:
        return _runtime_managed


def retrieval_proof_generation() -> int:
    """Return the generation a catalog proof must still match to publish."""
    with _lock:
        return _retrieval_generation


def admit_retrieval_proof(generation: int) -> bool:
    """Publish one exact proof only if no newer invalidation raced it."""
    global _retrieval_generation
    with _lock:
        if generation != _retrieval_generation:
            return False
        _events["retrieval_catalog"].set()
        _retrieval_generation += 1
        return True


def begin_warm() -> None:
    """Mark a warm in-flight. Resets per-component events and deferred items."""
    global _retrieval_generation, _warm_active, _warm_finished, _started_at
    with _lock:
        for c in COMPONENTS:
            _events[c].clear()
            _deferred[c].clear()
        _warm_active = True
        _warm_finished = False
        _started_at = time.monotonic()
        _retrieval_generation += 1


def finish_warm() -> None:
    """End the warm window permanently (until the next `begin_warm`).

    Components whose events are still unset (failed/skipped preloads) stop
    deferring — request paths return to inline lazy-load semantics.
    """
    global _retrieval_generation, _warm_finished
    with _lock:
        _warm_finished = True
        _retrieval_generation += 1


def mark_ready(component: str) -> list:
    """Set `component`'s event; atomically drain and return its deferred items."""
    global _retrieval_generation
    _check(component)
    with _lock:
        _events[component].set()
        if component == "retrieval_catalog":
            _retrieval_generation += 1
        drained = _deferred[component]
        _deferred[component] = []
        return drained


def mark_unready(component: str) -> None:
    """Revoke a component whose live backing state was later proven unavailable."""
    global _retrieval_generation
    _check(component)
    with _lock:
        _events[component].clear()
        if component == "retrieval_catalog":
            # Every invalidation is authoritative, even when the event was
            # already clear.  A proof that began before it must not re-admit an
            # older generation after this call returns.
            _retrieval_generation += 1


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


def warm_started() -> bool:
    """Whether this process has entered a managed warm at least once."""
    with _lock:
        return _warm_active or _warm_finished


def retrieval_admission(vault_root: Path | None = None) -> dict[str, object]:
    """Admission state for ordinary maintained-catalog recall.

    Supplying the configured runtime vault adds a read-only projection proof.
    A ready bit cannot outlive either authoritative recall scope; loss or a
    policy-identity mismatch revokes it before health or a request can claim
    admission.  After managed warm-up has finished, the same exact proof may
    restore a late-published catalog whose one background promotion callback
    lost a race.  The check never walks or reprojects the corpus.
    """
    global _retrieval_generation
    with _lock:
        if _events["retrieval_catalog"].is_set():
            admission = {"state": "ready", "admitted": True}
        elif _warm_active and not _warm_finished:
            admission = {"state": "warming", "admitted": False}
        elif _warm_finished:
            admission = {"state": "unavailable", "admitted": False}
        elif _runtime_managed:
            admission = {"state": "warming", "admitted": False}
        else:
            admission = {"state": "unverified", "admitted": False}
        managed_recovery = _runtime_managed and _warm_finished
        proof_generation = _retrieval_generation
    if vault_root is None or not (admission["admitted"] or managed_recovery):
        return admission
    from . import freshness

    if not freshness.event_indexes_enabled():
        # Explicit rollback mode retains its historical request-time polling
        # fallback.  Startup catalog verification still happens off-thread.
        return admission
    try:
        from . import lexstore

        proof_current = lexstore.runtime_retrieval_catalog_current(
            vault_root,
            schedule_repair=False,
        )
    except Exception:  # noqa: BLE001 - readiness uncertainty fails closed
        proof_current = False
    with _lock:
        if proof_generation != _retrieval_generation:
            # A watcher, warm transition, or another proof changed admission
            # after this proof began.  Its newer decision wins.
            return _retrieval_admission_locked()
        if proof_current and not admission["admitted"]:
            if not (_runtime_managed and _warm_finished):
                return _retrieval_admission_locked()
            # Retrieval has no deferred payload queue: its event is pure
            # admission.  Publish the exact proof only if no newer invalidation
            # raced it (the generation comparison above is the CAS).
            _events["retrieval_catalog"].set()
            _retrieval_generation += 1
        elif not proof_current and admission["admitted"]:
            _events["retrieval_catalog"].clear()
            _retrieval_generation += 1
        return _retrieval_admission_locked()


def _retrieval_admission_locked() -> dict[str, object]:
    """Return the current retrieval state while the caller holds ``_lock``."""
    if _events["retrieval_catalog"].is_set():
        return {"state": "ready", "admitted": True}
    if _warm_active and not _warm_finished:
        return {"state": "warming", "admitted": False}
    if _warm_finished:
        return {"state": "unavailable", "admitted": False}
    if _runtime_managed:
        return {"state": "warming", "admitted": False}
    return {"state": "unverified", "admitted": False}


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


def graph_recovery_payload(vault_root) -> dict:
    """Stable, content-free graph-recovery telemetry for the readiness payload.

    Persistent `recovery_required` is the condition the 2026-08 incident ran on
    for days unalarmed, so ELAPSED time is the signal, not a boolean. This
    observes and records the condition, so whoever serves readiness keeps the
    durable clock advancing. It names no path and no note — an age and a flag.
    """
    from . import graph_sync

    try:
        age = graph_sync.observe_recovery_state(vault_root)
    except Exception:  # noqa: BLE001 - telemetry must never break readiness
        return {"recovery_required": False, "recovery_age_s": None}
    return {
        "recovery_required": age is not None,
        "recovery_age_s": None if age is None else round(age, 1),
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
    global _retrieval_generation, _runtime_managed, _warm_active, _warm_finished, _started_at
    with _lock:
        for c in COMPONENTS:
            _events[c].clear()
            _deferred[c].clear()
        _warm_active = False
        _warm_finished = False
        _runtime_managed = False
        _started_at = None
        _retrieval_generation += 1


def _check(component: str) -> None:
    if component not in _events:
        raise ValueError(f"unknown readiness component: {component!r}")
