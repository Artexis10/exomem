"""Dependency-free process-memory metric selection for diagnostics."""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

_RUSAGE_INFO_V0 = 0
_RUSAGE_INFO_V0_SIZE = 96


class _RusageInfoV0(ctypes.Structure):
    """The complete stable Darwin ``rusage_info_v0`` ABI (96 bytes)."""

    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
    ]


def _darwin_physical_footprint_bytes(pid: int) -> int | None:
    if sys.platform != "darwin" or ctypes.sizeof(_RusageInfoV0) != _RUSAGE_INFO_V0_SIZE:
        return None
    try:
        proc_pid_rusage = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pid_rusage
        proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        proc_pid_rusage.restype = ctypes.c_int
        usage = _RusageInfoV0()
        if proc_pid_rusage(pid, _RUSAGE_INFO_V0, ctypes.byref(usage)) != 0:
            return None
        return int(usage.ri_phys_footprint) or None
    except Exception:  # noqa: BLE001 - native sampling must always fall back to RSS
        return None


_UNRESOLVED: Any = object()
_MALLOC_TRIM: Any = _UNRESOLVED
#: A trim walks every arena, so reap ticks and back-to-back drain passes share
#: one allowance: at most one trim per interval per process.
TRIM_INTERVAL_SECONDS = 60.0
SERVICE_TRIM_INTERVAL_SECONDS = 5.0
_LAST_TRIM: float | None = None
#: A trim was asked for inside the interval and skipped; the reaper's next tick
#: retries it, so freed memory is still returned once the interval passes.
_TRIM_PENDING = False
_TRIM_LOCK = threading.Lock()
_clock: Callable[[], float] = time.monotonic


def _resolve_malloc_trim() -> Callable[[int], int] | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        trim = ctypes.CDLL("libc.so.6").malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return trim
    except Exception:  # noqa: BLE001 - musl, a stripped libc, or no libc: nothing to trim
        return None


def trim_pending() -> bool:
    """Whether a trim was skipped by the throttle and has not run since."""
    return _TRIM_PENDING


def trim_allocator(*, clock: Callable[[], float] | None = None) -> bool:
    """Ask glibc to return freed heap to the OS: ``malloc_trim(0)``.

    Freed heap otherwise stays in the allocator's arenas as resident high-water
    after a model reap or a drain batch. The validated Cloud service profile
    shares a five-second allowance; local/legacy callers retain sixty seconds.
    A call inside the interval is skipped and marked
    pending (see `trim_pending`) for the reaper to retry. Off glibc this is a
    no-op, and it never raises: its callers are background threads that must
    keep running. Returns whether glibc reported releasing memory.
    """
    global _MALLOC_TRIM, _LAST_TRIM, _TRIM_PENDING
    try:
        from .cloud_cell import resource_policy

        interval = (
            SERVICE_TRIM_INTERVAL_SECONDS
            if resource_policy() == "service-v1"
            else TRIM_INTERVAL_SECONDS
        )
        now = (clock or _clock)()
        with _TRIM_LOCK:
            if _LAST_TRIM is not None and now - _LAST_TRIM < interval:
                _TRIM_PENDING = True
                return False
            _LAST_TRIM = now
            _TRIM_PENDING = False
        if _MALLOC_TRIM is _UNRESOLVED:
            _MALLOC_TRIM = _resolve_malloc_trim()
        trim = _MALLOC_TRIM
        return trim is not None and bool(trim(0))
    except Exception:  # noqa: BLE001 - trimming is an optimisation, never a failure
        return False


def enrich_process_memory(pid: int, rss_mb: float) -> dict[str, float | str | None]:
    """Choose Darwin physical footprint when obtainable, otherwise labelled RSS."""
    footprint = _darwin_physical_footprint_bytes(pid)
    if footprint is None:
        return {"memory_mb": rss_mb, "memory_metric": "rss", "physical_footprint_mb": None}
    footprint_mb = round(footprint / (1024 * 1024), 1)
    return {
        "memory_mb": footprint_mb,
        "memory_metric": "physical_footprint",
        "physical_footprint_mb": footprint_mb,
    }


def aggregate_memory(rows: list[dict[str, Any]]) -> dict[str, float | str]:
    """Aggregate only comparable selected metrics; mixed Darwin samples stay separate."""
    rss_total = round(sum(float(row.get("rss_mb") or 0.0) for row in rows), 1)
    physical_total = round(
        sum(float(row.get("memory_mb") or 0.0) for row in rows if row.get("memory_metric") == "physical_footprint"),
        1,
    )
    rss_fallback_total = round(
        sum(float(row.get("memory_mb") or row.get("rss_mb") or 0.0) for row in rows if row.get("memory_metric", "rss") == "rss"),
        1,
    )
    if physical_total and rss_fallback_total:
        return {
            "memory_metric": "mixed",
            "rss_mb_total": rss_total,
            "physical_footprint_mb_total": physical_total,
            "rss_fallback_mb_total": rss_fallback_total,
        }
    if physical_total:
        return {
            "memory_metric": "physical_footprint",
            "memory_mb_total": physical_total,
            "rss_mb_total": rss_total,
            "physical_footprint_mb_total": physical_total,
            "rss_fallback_mb_total": 0.0,
        }
    return {
        "memory_metric": "rss",
        "memory_mb_total": rss_fallback_total,
        "rss_mb_total": rss_total,
        "physical_footprint_mb_total": 0.0,
        "rss_fallback_mb_total": rss_fallback_total,
    }
