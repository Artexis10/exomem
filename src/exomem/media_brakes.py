"""Memory brakes for media work in an Exomem Cloud cell (`cloud-multimodal-processing` D2).

Three brakes keep media from taking the serving process down. Each reads the cell's
own cgroup v2 files at run time:

- admission compares anonymous memory (`memory.stat` anon) plus the engine's
  anonymous-memory budget with the lower of `memory.high` and the admission
  fraction of `memory.max`;
- the pressure stop reads the `memory.pressure` 10-second averages, and falls back
  to the admission ceiling on anonymous memory where pressure is unreadable;
- the hard limit is a data-segment limit (`RLIMIT_DATA`) on the media child: the
  engine's VmData budget plus a margin, inherited by the native tools it starts.

Nothing here reads `memory.current`: it counts reclaimable page cache and model
weights charged to whichever cell faulted them first.

The budgets and thresholds are deployment configuration. Acceptance measures each
engine and pins its values; the defaults below only hold until then.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import cloud_cell

log = logging.getLogger(__name__)

BUDGETS_ENV = "EXOMEM_MEDIA_BUDGETS"
MARGIN_ENV = "EXOMEM_MEDIA_VMDATA_MARGIN_MIB"
FRACTION_ENV = "EXOMEM_MEDIA_ADMISSION_FRACTION"
PRESSURE_ENV = "EXOMEM_MEDIA_PRESSURE_AVG10"
STOP_LIMIT_ENV = "EXOMEM_MEDIA_MEMORY_STOP_LIMIT"

#: Stop kinds the job ledger records. A pressure stop never marks a file over budget.
HARD_LIMIT = "hard_limit"
PRESSURE = "pressure"

_MIB = 1024 * 1024
# Defaults until each engine's acceptance pins its own values (docs/runbooks/cloud-media.md).
# VmData: about 3x the largest measured need of a small sample (329 MiB, MarkItDown).
_DEFAULT_VMDATA_MIB = 1024
# Kept with the VmData limit so that a child admitted at the ceiling and grown to its hard
# limit still stays under a 3 GiB memory.max: 0.80 x 3072 - 640 + 1024 + 128 = 2970 MiB.
_DEFAULT_ANON_MIB = 640
# Covers the allocator's own arenas above the engine's budget.
_DEFAULT_MARGIN_MIB = 128
# The service-v1 profile's 80% cgroup peak gate (add-cloud-service-resource-policy).
_DEFAULT_ADMISSION_FRACTION = 0.80
# Percent of the last 10 s that tasks stalled on memory; 10% is sustained reclaim,
# well above the idle cell's ~0%, and below the stalls that precede an OOM kill.
_DEFAULT_PRESSURE_AVG10 = 10.0
# Consecutive memory stops before a job leaves the stop cycle.
_DEFAULT_STOP_LIMIT = 3
# The C and C++ allocation primitives that native libraries name when an allocation
# fails (MuPDF "malloc (N bytes) failed", Leptonica "malloc fail", std::bad_alloc).
# They are those libraries' error tokens, not prose, and the only native signal.
_ALLOCATOR_TOKENS = ("bad_alloc", "malloc", "calloc", "realloc")


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """The brakes bound Cloud cells only.

    A personal install has no cell limit to derive them from, and its GPU runtimes map
    data segments far larger than any CPU budget.
    """
    return cloud_cell.cloud_mode_enabled(env)


@dataclass(frozen=True)
class Budget:
    anon_bytes: int
    vmdata_bytes: int


@dataclass(frozen=True)
class Settings:
    budgets: Mapping[str, Budget]
    default_budget: Budget
    margin_bytes: int
    admission_fraction: float
    pressure_avg10: float
    stop_limit: int

    def budget_for(self, engine: str | None) -> Budget:
        return self.budgets.get(engine or "", self.default_budget)


def _number(values: Mapping[str, str], name: str, default: float, *, low: float, high: float) -> float:
    raw = (values.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not low <= value <= high:
        log.warning("%s=%r is not a number in [%s, %s]; using %s", name, raw, low, high, default)
        return default
    return value


def settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read the brake configuration. A malformed value falls back to its default.

    Falling back keeps media running under conservative values rather than stopping
    every job on an operator typo; the warning names the value.
    """
    values = os.environ if env is None else env
    default_budget = Budget(_DEFAULT_ANON_MIB * _MIB, _DEFAULT_VMDATA_MIB * _MIB)
    budgets: dict[str, Budget] = {}
    raw = (values.get(BUDGETS_ENV) or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            for engine, entry in parsed.items():
                budgets[str(engine)] = Budget(
                    int(entry["anon_mib"]) * _MIB, int(entry["vmdata_mib"]) * _MIB
                )
        except (ValueError, TypeError, KeyError, AttributeError):
            log.warning("%s is not a map of engine to anon_mib and vmdata_mib; using defaults", BUDGETS_ENV)
            budgets = {}
    return Settings(
        budgets=budgets,
        default_budget=budgets.pop("default", default_budget),
        margin_bytes=int(_number(values, MARGIN_ENV, _DEFAULT_MARGIN_MIB, low=0, high=65536)) * _MIB,
        admission_fraction=_number(values, FRACTION_ENV, _DEFAULT_ADMISSION_FRACTION, low=0.05, high=1.0),
        pressure_avg10=_number(values, PRESSURE_ENV, _DEFAULT_PRESSURE_AVG10, low=0.0, high=100.0),
        stop_limit=int(_number(values, STOP_LIMIT_ENV, _DEFAULT_STOP_LIMIT, low=1, high=100)),
    )


@dataclass(frozen=True)
class CellMemory:
    """One reading of the cell's cgroup. None means unreadable or unlimited."""

    anon: int | None
    max: int | None
    high: int | None
    pressure_avg10: float | None


def _cgroup_dir(proc: Path, sys_root: Path) -> Path | None:
    try:
        lines = (proc / "self/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        # cgroup v2 has the single hierarchy line `0::<path>` (cgroups(7)).
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if hierarchy == "0" and controllers == "":
            return sys_root / path.lstrip("/")
    return None


def _limit(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None  # `max` means unlimited


def read_cell(*, proc: Path = Path("/proc"), sys_root: Path = Path("/sys/fs/cgroup")) -> CellMemory:
    directory = _cgroup_dir(proc, sys_root)
    if directory is None:
        return CellMemory(None, None, None, None)
    anon = None
    try:
        for line in (directory / "memory.stat").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            if key == "anon":
                anon = int(value)
                break
    except (OSError, ValueError):
        anon = None
    pressure = None
    try:
        averages = []
        for line in (directory / "memory.pressure").read_text(encoding="utf-8").splitlines():
            # `some avg10=0.00 avg60=0.00 avg300=0.00 total=0`, then the `full` line.
            fields = dict(part.partition("=")[::2] for part in line.split()[1:])
            averages.append(float(fields["avg10"]))
        pressure = max(averages) if averages else None
    except (OSError, ValueError, KeyError):
        pressure = None
    return CellMemory(
        anon=anon,
        max=_limit(directory / "memory.max"),
        high=_limit(directory / "memory.high"),
        pressure_avg10=pressure,
    )


def ceiling(cell: CellMemory, config: Settings) -> int | None:
    """The lower of `memory.high` and the admission fraction of `memory.max`."""
    candidates = [cell.high] if cell.high is not None else []
    if cell.max is not None:
        candidates.append(int(cell.max * config.admission_fraction))
    return min(candidates) if candidates else None


def admits(cell: CellMemory, budget: Budget, config: Settings) -> bool:
    """Whether a job with `budget` fits under the ceiling now.

    An unreadable cgroup admits: there is no cell limit to protect, and refusing
    would stop media for good on a host that only lacks cgroup v2.
    """
    limit = ceiling(cell, config)
    if limit is None or cell.anon is None:
        return True
    return cell.anon + budget.anon_bytes < limit


def pressure_exceeded(cell: CellMemory, config: Settings) -> bool:
    """Whether the supervisor must stop the media child now."""
    if cell.pressure_avg10 is not None:
        return cell.pressure_avg10 > config.pressure_avg10
    limit = ceiling(cell, config)
    return limit is not None and cell.anon is not None and cell.anon >= limit


def context(cell: CellMemory, budget: Budget, config: Settings) -> str:
    """What a memory-blocked or over-budget verdict depended on.

    When the cell's limit or the engine's budget changes, the verdict no longer holds
    and the job returns to pending.
    """
    return json.dumps(
        {
            "max": cell.max,
            "high": cell.high,
            "fraction": config.admission_fraction,
            "anon_budget": budget.anon_bytes,
            "vmdata_budget": budget.vmdata_bytes,
            "margin": config.margin_bytes,
        },
        sort_keys=True,
    )


def own_vmdata(*, proc: Path = Path("/proc")) -> int | None:
    try:
        for line in (proc / "self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmData:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def apply_hard_limit(budget: Budget, config: Settings) -> None:
    """Cap this process's data segment for one job. Child processes inherit it."""
    import resource

    _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
    limit = budget.vmdata_bytes + config.margin_bytes
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_DATA, (limit, hard))


def lift_hard_limit() -> None:
    import resource

    _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
    resource.setrlimit(resource.RLIMIT_DATA, (hard, hard))


def names_allocation_failure(diagnostic: str) -> bool:
    """Whether a native engine's error text reports a failed allocation.

    Engines call this at their boundary and raise a `MemoryError`, so the worker
    classifies a hard-limit failure by type alone.
    """
    return any(token in diagnostic for token in _ALLOCATOR_TOKENS)
