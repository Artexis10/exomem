"""Dependency-free process-memory metric selection for diagnostics."""

from __future__ import annotations

import ctypes
import sys
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
