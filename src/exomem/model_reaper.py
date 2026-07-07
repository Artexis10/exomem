"""Idle-unload reaper — reclaim model singletons that sit resident beyond a threshold.

Only relevant for the GPU-opt-in path (`performance` mode) and `quiet` mode, where a
model may load lazily and then idle. Started from `server.build_server` ONLY when
`mode.release_when_idle()`. On a CPU-default normal-mode server it never runs — the
models are cheap CPU-RAM and there is no CUDA context to reclaim anyway (that's the
whole point of the CPU-default primary fix; this is the complement for GPU-opt-in).

One daemon thread; each tick it unloads any registered model that is loaded, not
in-flight, and idle past the threshold, coordinating with the warm thread via
`readiness.is_warming()`. Unloading under a concurrent encode is inefficiency, not a
use-after-free — see `embeddings._ModelGuard`. `stop()` lets a live mode switch (PR4)
or shutdown end it cleanly.

Pure substrate: process telemetry only. Nothing here reasons over notes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import accel, readiness

log = logging.getLogger(__name__)

DEFAULT_IDLE_SECONDS = 900.0  # 15 min
TICK_SECONDS = 60.0

_thread: threading.Thread | None = None
_stop = threading.Event()


@dataclass
class ResourceSlot:
    name: str
    is_loaded: Callable[[], bool]
    inflight: Callable[[], int]
    last_activity: Callable[[], float]
    unload: Callable[[], bool]


def idle_seconds() -> float:
    """Idle threshold in seconds from `EXOMEM_IDLE_MINUTES` (default 15 min)."""
    raw = os.environ.get("EXOMEM_IDLE_MINUTES")
    if not raw:
        return DEFAULT_IDLE_SECONDS
    try:
        minutes = float(raw)
    except ValueError:
        return DEFAULT_IDLE_SECONDS
    return minutes * 60.0 if minutes > 0 else DEFAULT_IDLE_SECONDS


ModelSlot = ResourceSlot


def _should_unload(slot: ResourceSlot, now: float, threshold: float) -> bool:
    """Pure decision: unload iff not warming, loaded, not in-flight, idle-window elapsed."""
    if readiness.is_warming():
        return False
    if not slot.is_loaded():
        return False
    if slot.inflight() > 0:
        return False
    return (now - slot.last_activity()) >= threshold


def _quiet_cache_slot(
    name: str,
    is_loaded: Callable[[], bool],
    unload: Callable[[], bool],
) -> ResourceSlot:
    """Cache slot with approximate idle tracking, active only when caches are evictable."""
    from . import mode

    state = {"last_empty": time.monotonic()}

    def loaded() -> bool:
        should_retain = mode.retain_cpu_caches()
        resident = (not should_retain) and is_loaded()
        if not resident:
            state["last_empty"] = time.monotonic()
        return resident

    return ResourceSlot(
        name=name,
        is_loaded=loaded,
        inflight=lambda: 0,
        last_activity=lambda: state["last_empty"],
        unload=unload,
    )


def default_slots() -> list[ResourceSlot]:
    """Default reclaimable resources: models always, CPU caches only in quiet mode."""
    from . import bm25, embeddings as e, find

    return [
        ResourceSlot(
            "embeddings", lambda: e._MODEL is not None,
            e.BGE_GUARD.inflight, e.BGE_GUARD.last_activity, e.unload_model,
        ),
        ResourceSlot(
            "reranker", lambda: e._RERANKER is not None,
            e.RERANKER_GUARD.inflight, e.RERANKER_GUARD.last_activity, e.unload_reranker,
        ),
        ResourceSlot(
            "clip", lambda: e._CLIP_MODEL is not None,
            e.CLIP_GUARD.inflight, e.CLIP_GUARD.last_activity, e.unload_clip_model,
        ),
        _quiet_cache_slot(
            "index-matrices",
            lambda: any(v.get("loaded", 0) for v in e.index_cache_status().values()),
            lambda: any(e.unload_index_caches().values()),
        ),
        _quiet_cache_slot(
            "bm25-cache",
            lambda: bool(bm25.cache_status().get("loaded")),
            bm25.unload_cache,
        ),
        _quiet_cache_slot(
            "find-ram-caches",
            lambda: any(
                section.get("entries", 0) for section in find.cache_status().values()
            ),
            lambda: any(find.unload_ram_caches().values()),
        ),
    ]


def _reap_once(slots: list[ResourceSlot], now: float, threshold: float) -> list[str]:
    """Unload every stale slot once; return the names actually reaped. Never raises."""
    reaped: list[str] = []
    for s in slots:
        try:
            if _should_unload(s, now, threshold):
                before = accel.gpu_mem()
                if s.unload():
                    reaped.append(s.name)
                    log.info("reaped idle model %s (gpu_mem %s -> %s)", s.name, before, accel.gpu_mem())
        except Exception:  # noqa: BLE001 — a reaper tick must never crash the thread
            log.warning("reaper tick failed for %s", s.name, exc_info=True)
    return reaped


def start(
    threshold: float | None = None,
    tick: float = TICK_SECONDS,
    slots: list[ResourceSlot] | None = None,
) -> threading.Thread:
    """Start the idle-unload daemon (idempotent — a second call returns the live thread)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop.clear()
    thr = threshold if threshold is not None else idle_seconds()
    the_slots = slots if slots is not None else default_slots()

    def _run() -> None:
        log.info("idle-unload reaper started (threshold=%.0fs, tick=%.0fs)", thr, tick)
        while not _stop.wait(tick):  # returns True when stopped, False on timeout
            _reap_once(the_slots, time.monotonic(), thr)

    t = threading.Thread(target=_run, name="exomem-reaper", daemon=True)
    _thread = t
    t.start()
    return t


def stop() -> None:
    """Signal the reaper to stop (live mode switch / shutdown). Idempotent."""
    _stop.set()


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()
