"""automate-cloud-releases D4: each cell's upgrade window and the due rule (task 1.6)."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cellctl.backup_window import DEFAULT_BACKUP_WINDOW, within_backup_window
from cellctl.upgrade_window import (
    DEFAULT,
    HISTORY,
    Idle,
    TrafficHistory,
    Window,
    WindowConfig,
    check_default_range,
    compute_window,
    default_hour,
    is_due,
)

CONFIG = WindowConfig(
    enabled=True,
    lookback_days=14,
    min_coverage=0.5,
    default_hour_start=5,
    default_hour_end=2,
    idle_minutes=5,
    force_minutes=10,
)
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _history(rate_by_hour: dict[int, int], *, days: int = 14, observed: int = 3600,
             retention_hours: int = 672, down_hours: frozenset[int] = frozenset()) -> TrafficHistory:
    """`days` full days of buckets ending at NOW, with `rate_by_hour[h]` requests in each hour h
    (default 100) and no observed seconds in `down_hours`."""

    first = NOW - timedelta(hours=days * 24)
    requests, seconds = [], []
    for index in range(days * 24):
        hour = (first + timedelta(hours=index)).hour
        if hour in down_hours:
            requests.append(0)
            seconds.append(0)
        else:
            requests.append(rate_by_hour.get(hour, 100))
            seconds.append(observed)
    return TrafficHistory(retention_hours=retention_hours, first_hour=first,
                          requests=tuple(requests), observed_seconds=tuple(seconds))


def test_the_quietest_hour_is_the_window() -> None:
    # Catches a window that picks the busiest hour.
    history = _history({4: 3, 13: 900})

    assert compute_window("cell-a", history, NOW, CONFIG) == Window(4, HISTORY)


def test_coverage_below_the_minimum_gives_the_default_hour() -> None:
    # Catches a window computed from one day of data: one day of 14 is below 0.5 coverage.
    history = _history({4: 3}, days=1)

    window = compute_window("cell-a", history, NOW, CONFIG)

    assert window == Window(default_hour("cell-a", CONFIG), DEFAULT)


def test_no_history_gives_the_default_hour() -> None:
    assert compute_window("cell-a", None, NOW, CONFIG) == Window(default_hour("cell-a", CONFIG), DEFAULT)


def test_unmeasured_cells_spread_across_the_default_range_and_avoid_the_backup_window() -> None:
    # Catches a fleet-wide default hour.
    hours = [default_hour(f"cell-{index:04d}", CONFIG) for index in range(1000)]
    allowed = {hour for hour in range(24) if within_backup_window(hour, (5, 2))}

    assert set(hours) == allowed
    assert not any(within_backup_window(hour, DEFAULT_BACKUP_WINDOW) for hour in hours)
    # Spread, not merely covered: no hour takes more than twice its fair share.
    assert max(hours.count(hour) for hour in allowed) < 2 * 1000 / len(allowed)


def test_default_hours_are_fixed_by_sha256_in_a_fresh_process() -> None:
    # Catches a per-process hash() that moves windows at every restart. Each cell is
    # computed in its own interpreter, with hash randomisation on, and compared with
    # the value SHA-256 fixes.
    expected = {"cell-alpha": 6, "cell-bravo": 5, "cell-charlie": 8}
    src = Path(__file__).resolve().parents[1] / "src"
    for cell_id, hour in expected.items():
        code = (
            "from cellctl.upgrade_window import WindowConfig, default_hour;"
            f"print(default_hour({cell_id!r}, WindowConfig(True, 14, 0.5, 5, 2, 5, 10)))"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                                env={"PYTHONPATH": str(src), "PYTHONHASHSEED": "random"})
        assert int(result.stdout) == hour, cell_id


def test_a_default_range_that_overlaps_the_backup_window_is_refused() -> None:
    # Catches a configuration that rolls cells during stopped backups.
    with pytest.raises(ValueError, match="backup window"):
        check_default_range(WindowConfig(True, 14, 0.5, 1, 6, 5, 10), DEFAULT_BACKUP_WINDOW)
    check_default_range(CONFIG, DEFAULT_BACKUP_WINDOW)


def test_an_hour_with_no_observed_seconds_never_wins() -> None:
    # Catches a down hour chosen as the quietest: hour 3 has no traffic because the cell was down.
    history = _history({4: 30}, down_hours=frozenset({3}))

    assert compute_window("cell-a", history, NOW, CONFIG) == Window(4, HISTORY)


def test_tied_hours_follow_the_cell_digest_and_stay_the_same() -> None:
    # Catches every tied cell choosing hour 0: equal rates in every hour.
    history = _history({})
    windows = {cell: compute_window(cell, history, NOW, CONFIG) for cell in ("cell-a", "cell-b", "cell-c", "cell-d")}

    assert len({window.hour for window in windows.values()}) > 1
    for cell, window in windows.items():
        assert compute_window(cell, history, NOW, CONFIG) == window
        assert window.source == HISTORY


def test_a_lookback_longer_than_the_retention_is_clamped() -> None:
    # Catches a coverage computed over hours the cell never kept: the cell keeps 3 days,
    # a 14-day lookback would read 3/14 coverage and fall back to the default.
    history = _history({4: 3}, days=3, retention_hours=72)

    assert compute_window("cell-a", history, NOW, CONFIG) == Window(4, HISTORY)


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 10, hour, minute, tzinfo=UTC)


def test_due_rule() -> None:
    # Catches a busy cell that never rolls, or one that rolls mid-burst.
    idle = Idle(inflight=0, last_request_age_seconds=600)
    busy = Idle(inflight=1, last_request_age_seconds=0)
    recent = Idle(inflight=0, last_request_age_seconds=60)

    assert is_due(4, _at(4, 10), idle, CONFIG)
    assert not is_due(4, _at(4, 10), busy, CONFIG)
    assert not is_due(4, _at(4, 10), recent, CONFIG)
    assert is_due(4, _at(4, 51), busy, CONFIG)
    assert not is_due(4, _at(5, 0), idle, CONFIG)
    assert not is_due(4, _at(3, 59), idle, CONFIG)
    # An unreadable readiness block is not idle.
    assert not is_due(4, _at(4, 10), None, CONFIG)
    assert is_due(4, _at(4, 55), None, CONFIG)
    # A window that ends at midnight.
    assert is_due(23, _at(23, 55), busy, CONFIG)
    assert not is_due(23, datetime(2026, 11, 11, 0, 1, tzinfo=UTC), idle, CONFIG)


def test_a_fresh_cell_with_no_request_is_not_idle() -> None:
    # Catches a fresh cell that reads as idle for years and rolls at once.
    assert not is_due(4, _at(4, 10), Idle(inflight=0, last_request_age_seconds=None), CONFIG)
