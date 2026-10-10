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

from . import cloud_cell, media_engines

log = logging.getLogger(__name__)

BUDGETS_ENV = "EXOMEM_MEDIA_BUDGETS"
MARGIN_ENV = "EXOMEM_MEDIA_VMDATA_MARGIN_MIB"
FRACTION_ENV = "EXOMEM_MEDIA_ADMISSION_FRACTION"
PRESSURE_ENV = "EXOMEM_MEDIA_PRESSURE_AVG10"
STOP_LIMIT_ENV = "EXOMEM_MEDIA_MEMORY_STOP_LIMIT"
JOB_TIMEOUT_ENV = "EXOMEM_MEDIA_JOB_TIMEOUT_SECONDS"
#: The budget entry that applies to media whose kind needs no engine.
DEFAULT_BUDGET_KEY = "default"

#: Stop kinds the job ledger records. Hard-limit failures can mark a file over the
#: memory budget and watchdog stops over the time budget; a pressure stop never marks
#: a file over budget. A watchdog stop is not a memory stop and counts apart.
HARD_LIMIT = "hard_limit"
PRESSURE = "pressure"
TIMEOUT = "timeout"
#: The key of a watchdog verdict's context: the job timeout, in seconds, it exceeded.
_TIMEOUT_CONTEXT_KEY = "job_timeout_seconds"

_MIB = 1024 * 1024
# Defaults until each engine's acceptance pins its own values (docs/runbooks/cloud-media.md).
# VmData: about 3x the largest measured need of a small sample (329 MiB, MarkItDown).
_DEFAULT_VMDATA_MIB = 1024
# The child and each Tesseract it starts have their own data limit. For an image the
# child closes the decoded image before Tesseract starts (extract._ocr_image), so the job
# holds one hard limit at a time and stays under a 3 GiB memory.max:
# 0.80 x 3072 - 640 + 1024 + 128 = 2970 MiB. A scanned PDF keeps its document and the
# rendered page open while Tesseract reads that page, so there the two can overlap.
_DEFAULT_ANON_MIB = 640
# Covers the allocator's own arenas above the engine's budget.
_DEFAULT_MARGIN_MIB = 128
# The service-v1 profile's 80% cgroup peak gate (add-cloud-service-resource-policy).
_DEFAULT_ADMISSION_FRACTION = 0.80
# Percent of the last 10 s that tasks stalled on memory; 10% is sustained reclaim,
# well above the idle cell's ~0%, and below the stalls that precede an OOM kill.
_DEFAULT_PRESSURE_AVG10 = 10.0
# Memory stops, and apart from them watchdog stops, before a job leaves the stop cycle.
_DEFAULT_STOP_LIMIT = 3
# A media child runs one job. Fifteen minutes is far above the small samples'
# sub-second times and leaves room for a long scanned PDF. A file that outruns it, or
# hangs, costs at most the stop limit times this before it is over budget.
_DEFAULT_JOB_TIMEOUT_SECONDS = 900.0

_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """Settings are re-read on every supervisor tick; name each bad value once."""
    if message not in _warned:
        _warned.add(message)
        log.warning("%s", message)


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
    job_timeout_seconds: float

    def budget_for(self, engine: str | None) -> Budget:
        return self.budgets.get(engine or "", self.default_budget)


@dataclass(frozen=True)
class CellMemory:
    """One reading of the cell's cgroup. A limit of None means unlimited."""

    anon: int | None
    max: int | None
    high: int | None
    pressure_avg10: float | None
    #: Why this reading cannot be trusted, or None. A Cloud cell always runs in a
    #: cgroup v2 group with a memory limit, so anything else is unexpected.
    problem: str | None = None


def _number(values: Mapping[str, str], name: str, default: float, *, low: float, high: float) -> float:
    raw = (values.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not low <= value <= high:
        _warn_once(f"{name}={raw!r} is not a number in [{low}, {high}]; using {default}")
        return default
    return value


def _budgets(raw: str, cell_max: int | None) -> dict[str, Budget]:
    """Parse EXOMEM_MEDIA_BUDGETS; an entry that cannot hold keeps its default.

    A budget at or below zero admits nothing, and one above the cell's limit can
    never be met, so neither is an operator's real intent.
    """
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        _warn_once(f"{BUDGETS_ENV} is not a JSON object of engine to anon_mib and vmdata_mib; using defaults")
        return {}
    budgets: dict[str, Budget] = {}
    for key, entry in parsed.items():
        engine = str(key)
        if engine != DEFAULT_BUDGET_KEY and engine not in media_engines.ENGINES:
            _warn_once(f"{BUDGETS_ENV} names unknown engine {engine!r}; ignored")
            continue
        try:
            anon = int(entry["anon_mib"]) * _MIB
            vmdata = int(entry["vmdata_mib"]) * _MIB
        except (TypeError, KeyError, ValueError):
            _warn_once(f"{BUDGETS_ENV}[{engine!r}] needs integer anon_mib and vmdata_mib; using the default")
            continue
        if anon <= 0 or vmdata <= 0 or (cell_max is not None and max(anon, vmdata) > cell_max):
            _warn_once(
                f"{BUDGETS_ENV}[{engine!r}] must be above 0 and within the cell's memory.max; using the default"
            )
            continue
        budgets[engine] = Budget(anon, vmdata)
    return budgets


def settings(env: Mapping[str, str] | None = None, cell: CellMemory | None = None) -> Settings:
    """Read the brake configuration. A malformed value falls back to its default.

    Falling back keeps media running under conservative values rather than stopping
    every job on an operator typo; the warning names the value. `cell`, when given,
    bounds each budget by the cell's memory.max.
    """
    values = os.environ if env is None else env
    raw = (values.get(BUDGETS_ENV) or "").strip()
    budgets = _budgets(raw, cell.max if cell is not None else None) if raw else {}
    return Settings(
        budgets=budgets,
        default_budget=budgets.pop(
            DEFAULT_BUDGET_KEY, Budget(_DEFAULT_ANON_MIB * _MIB, _DEFAULT_VMDATA_MIB * _MIB)
        ),
        margin_bytes=int(_number(values, MARGIN_ENV, _DEFAULT_MARGIN_MIB, low=0, high=65536)) * _MIB,
        admission_fraction=_number(values, FRACTION_ENV, _DEFAULT_ADMISSION_FRACTION, low=0.05, high=1.0),
        pressure_avg10=_number(values, PRESSURE_ENV, _DEFAULT_PRESSURE_AVG10, low=0.0, high=100.0),
        stop_limit=int(_number(values, STOP_LIMIT_ENV, _DEFAULT_STOP_LIMIT, low=1, high=100)),
        job_timeout_seconds=_number(
            values, JOB_TIMEOUT_ENV, _DEFAULT_JOB_TIMEOUT_SECONDS, low=1.0, high=86400.0
        ),
    )


class _Unexpected(Exception):
    """The cgroup does not look like a Cloud cell's."""


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise _Unexpected(f"{path} is not UTF-8") from error
    except OSError as error:
        raise _Unexpected(f"cannot read {path}: {error.strerror or error}") from error


def _cgroup_dir(proc: Path, sys_root: Path) -> Path:
    for line in _text(proc / "self/cgroup").splitlines():
        # cgroup v2 has the single hierarchy line `0::<path>` (cgroups(7)).
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if hierarchy == "0" and controllers == "":
            return sys_root / path.lstrip("/")
    raise _Unexpected("the cell's process is not in a cgroup v2 group")


def _limit(path: Path) -> int | None:
    raw = _text(path).strip()
    if raw == "max":
        return None  # unlimited (cgroup-v2.rst)
    if not raw.isdigit():
        raise _Unexpected(f"{path.name} holds {raw[:40]!r}, not a byte count or max")
    return int(raw)


def _anon(path: Path) -> int:
    for line in _text(path).splitlines():
        key, _, value = line.partition(" ")
        if key == "anon":
            if not value.strip().isdigit():
                raise _Unexpected(f"{path.name} anon holds {value[:40]!r}")
            return int(value)
    raise _Unexpected(f"{path.name} has no anon entry")


def _pressure(path: Path) -> float | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise _Unexpected(f"{path} is not UTF-8") from error
    except OSError:
        return None  # PSI is off on this kernel: the stop falls back to the ceiling
    averages = []
    try:
        for line in raw.splitlines():
            # `some avg10=0.00 avg60=0.00 avg300=0.00 total=0`, then the `full` line.
            fields = dict(part.partition("=")[::2] for part in line.split()[1:])
            averages.append(float(fields["avg10"]))
    except (KeyError, ValueError) as error:
        raise _Unexpected(f"{path.name} is not in the PSI format") from error
    return max(averages) if averages else None


def read_cell(*, proc: Path = Path("/proc"), sys_root: Path = Path("/sys/fs/cgroup")) -> CellMemory:
    try:
        directory = _cgroup_dir(proc, sys_root)
        anon = _anon(directory / "memory.stat")
        limit = _limit(directory / "memory.max")
        high = _limit(directory / "memory.high")
        if limit is None and high is None:
            raise _Unexpected("the cell has no memory limit: memory.max and memory.high are max")
        pressure = _pressure(directory / "memory.pressure")
    except _Unexpected as error:
        return CellMemory(None, None, None, None, problem=str(error))
    return CellMemory(anon=anon, max=limit, high=high, pressure_avg10=pressure)


def ceiling(cell: CellMemory, config: Settings) -> int | None:
    """The lower of `memory.high` and the admission fraction of `memory.max`."""
    candidates = [cell.high] if cell.high is not None else []
    if cell.max is not None:
        candidates.append(int(cell.max * config.admission_fraction))
    return min(candidates) if candidates else None


def admits(cell: CellMemory, budget: Budget, config: Settings) -> bool:
    """Whether a job with `budget` fits under the ceiling now.

    Fails closed on an unexpected cgroup state: on a Cloud cell it means something is
    wrong, and a job admitted blind could take the serving process down. When that
    fires wrongly, media waits and doctor and runtime status say why: the tenant
    waits, and the operator sees it.
    """
    limit = ceiling(cell, config)
    if cell.problem is not None or limit is None or cell.anon is None:
        return False
    return cell.anon + budget.anon_bytes < limit


def pressure_exceeded(cell: CellMemory, config: Settings) -> bool:
    """Whether the supervisor must stop the media child now.

    An unexpected cgroup state counts as pressure, for the reason `admits` refuses.
    """
    if cell.problem is not None:
        return True
    if cell.pressure_avg10 is not None:
        return cell.pressure_avg10 > config.pressure_avg10
    limit = ceiling(cell, config)
    return limit is not None and cell.anon is not None and cell.anon >= limit


def status(env: Mapping[str, str] | None = None) -> dict[str, object]:
    """The brakes as runtime status and doctor report them.

    `off` on an install without brakes; `unavailable` with the reason when the cell's
    cgroup cannot be trusted; otherwise `on` with this reading's values in bytes.
    """
    if not enabled(env):
        return {"state": "off"}
    cell = read_cell()
    if cell.problem is not None:
        return {"state": "unavailable", "reason": cell.problem}
    config = settings(env, cell)
    return {
        "state": "on",
        "anon_bytes": cell.anon,
        "ceiling_bytes": ceiling(cell, config),
        "pressure_avg10": cell.pressure_avg10,
    }


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


def timeout_context(config: Settings) -> str:
    """What a watchdog over-budget verdict depended on: the job timeout it exceeded."""
    return json.dumps({_TIMEOUT_CONTEXT_KEY: config.job_timeout_seconds})


def verdict_lifted(recorded: str | None, *, current: str, config: Settings) -> bool:
    """Whether a memory or watchdog verdict recorded with context `recorded` has lifted.

    A memory verdict lifts when its `context` differs from `current`, this reading's:
    the cell's limit or the engine's budget changed. A watchdog verdict lifts only when
    the job timeout grew, since an equal or shorter one would stop the file again.
    """
    try:
        parsed = json.loads(recorded or "null")
    except ValueError:
        parsed = None
    exceeded = parsed.get(_TIMEOUT_CONTEXT_KEY) if isinstance(parsed, dict) else None
    if isinstance(exceeded, int | float):
        return config.job_timeout_seconds > exceeded
    return recorded != current


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
