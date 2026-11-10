"""automate-cloud-releases D4: each cell's one-hour UTC upgrade window, and the due rule.

Pure functions, no I/O, beside rollout.py. Every number comes from WindowConfig,
which main.py builds from the chart's `cellctl.upgradeWindow.*` values.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction

from .backup_window import within_backup_window

# The window annotation's two sources, a closed set this module defines and writes.
HISTORY = "history"
DEFAULT = "default"
SOURCES = (HISTORY, DEFAULT)

HOUR_SECONDS = 3600


@dataclass(frozen=True)
class WindowConfig:
    enabled: bool
    lookback_days: int
    min_coverage: float
    default_hour_start: int
    default_hour_end: int
    idle_minutes: int
    force_minutes: int


@dataclass(frozen=True)
class TrafficHistory:
    """The cell's `/health/traffic-history` answer: bucket i covers the UTC hour
    `first_hour + i hours`. An hour with 0 observed seconds was not observed."""

    retention_hours: int
    first_hour: datetime
    requests: tuple[int, ...]
    observed_seconds: tuple[int, ...]


@dataclass(frozen=True)
class Window:
    hour: int
    source: str

    def annotation(self) -> str:
        return f"{self.hour} {self.source}"


@dataclass(frozen=True)
class Idle:
    """The two fields of the cell's readiness `traffic` block the due rule reads."""

    inflight: int
    # None until the cell has handled its first MCP request since it started.
    last_request_age_seconds: float | None


def parse_window(raw: str | None) -> Window | None:
    """The `exomem.io/upgrade-window` annotation, or None when it is absent or unreadable."""

    parts = (raw or "").split()
    if len(parts) != 2 or not parts[0].isdecimal() or parts[1] not in SOURCES:
        return None
    hour = int(parts[0])
    return Window(hour, parts[1]) if 0 <= hour < 24 else None


def _digest(cell_id: str) -> int:
    # SHA-256, never hash(): Python salts str hashes per process, so a window
    # would move at every cellctl restart.
    return int.from_bytes(hashlib.sha256(cell_id.encode("utf-8")).digest()[:8], "big")


def default_hours(config: WindowConfig) -> list[int]:
    """The hours from defaultHourStart up to defaultHourEnd, exclusive, wrapping past midnight."""

    span = (config.default_hour_start, config.default_hour_end)
    hours = ((config.default_hour_start + offset) % 24 for offset in range(24))
    return [hour for hour in hours if within_backup_window(hour, span)]


def check_default_range(config: WindowConfig, backup_window: tuple[int, int]) -> None:
    """cellctl refuses to start when the default range is empty or overlaps the nightly
    backup window: unmeasured cells would then roll during stopped backups."""

    hours = default_hours(config)
    if not hours:
        raise ValueError("cellctl.upgradeWindow default hour range is empty")
    overlap = [hour for hour in hours if within_backup_window(hour, backup_window)]
    if overlap:
        raise ValueError(f"cellctl.upgradeWindow default hour range overlaps the backup window at {overlap}")


def default_hour(cell_id: str, config: WindowConfig) -> int:
    hours = default_hours(config)
    return hours[_digest(cell_id) % len(hours)]


def _hour_start(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def compute_window(cell_id: str, history: TrafficHistory | None, now: datetime, config: WindowConfig) -> Window:
    """The hour of the day with the lowest request rate over the lookback, or the cell's
    default hour when the cell observed less than the minimum coverage of it."""

    fallback = Window(default_hour(cell_id, config), DEFAULT)
    if history is None:
        return fallback
    lookback_hours = min(config.lookback_days * 24, history.retention_hours)
    if lookback_hours <= 0:
        return fallback
    end = _hour_start(now)
    start = end - timedelta(hours=lookback_hours)
    requests = [0] * 24
    seconds = [0] * 24
    for index, (count, observed) in enumerate(zip(history.requests, history.observed_seconds, strict=True)):
        hour = history.first_hour + timedelta(hours=index)
        if start <= hour < end and observed > 0:
            requests[hour.hour] += count
            seconds[hour.hour] += observed
    if Fraction(sum(seconds), lookback_hours * HOUR_SECONDS) < Fraction(config.min_coverage):
        return fallback
    # Exact rates, so a tie is a tie and not a rounding accident.
    rates = {hour: Fraction(requests[hour], seconds[hour]) for hour in range(24) if seconds[hour] > 0}
    lowest = min(rates.values())
    tied = sorted(hour for hour, rate in rates.items() if rate == lowest)
    return Window(tied[_digest(cell_id) % len(tied)], HISTORY)


def window_open(hour: int, now: datetime) -> bool:
    return now.hour == hour


def is_due(hour: int, now: datetime, idle: Idle | None, config: WindowConfig) -> bool:
    """Inside its window, a cell is due once nothing is in flight and no request arrived
    for idleMinutes, or once fewer than forceMinutes of the window remain. `idle` is None
    when readiness could not be read, which counts as not idle."""

    if not window_open(hour, now):
        return False
    remaining = _hour_start(now) + timedelta(hours=1) - now
    if remaining < timedelta(minutes=config.force_minutes):
        return True
    if idle is None or idle.inflight > 0:
        return False
    # No request since the cell started says nothing about how long it has been
    # quiet, so it is not idle; the force minutes still roll it.
    if idle.last_request_age_seconds is None:
        return False
    return idle.last_request_age_seconds >= config.idle_minutes * 60
