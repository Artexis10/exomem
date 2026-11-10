"""A Cloud cell's hourly traffic counts (automate-cloud-releases D4, task 1.5).

The server-level checks (probes never count, the readiness block, the
health-only path) live in test_cloud_cell.py beside the other health-port tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from cellctl_cell_readiness import cell_readiness

from exomem.cell_traffic import RETENTION_HOURS, CellTraffic

HOUR = 3600
# 2025-11-10T10:00:00Z, an hour boundary.
T0 = 1_762_768_800 // HOUR * HOUR


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _bucket(history: dict, epoch: float) -> tuple[int, int]:
    _, first, requests, seconds = cell_readiness.traffic_history(json.dumps(history).encode())
    index = int(epoch // HOUR) - int(first.timestamp() // HOUR)
    return requests[index], seconds[index]


def test_counts_survive_a_restart_on_the_same_volume(tmp_path: Path) -> None:
    # Catches counts kept only in memory that reset at each upgrade.
    path = tmp_path / "cell-traffic.json"
    clock = Clock(T0 + 600)
    first = CellTraffic(path, clock=clock)
    for _ in range(7):
        first.request_started()
        first.request_finished()
    clock.now = T0 + 1200
    first.flush()

    clock.now = T0 + 1260
    second = CellTraffic(path, clock=clock)

    requests, seconds = _bucket(second.history(), T0)
    assert requests == 7
    assert seconds >= 600


def test_an_hour_the_cell_was_down_reads_as_not_observed(tmp_path: Path) -> None:
    # Catches a down hour that looks quiet and draws a window onto it.
    path = tmp_path / "cell-traffic.json"
    clock = Clock(T0)
    first = CellTraffic(path, clock=clock)
    clock.now = T0 + HOUR - 1
    first.flush()

    clock.now = T0 + 2 * HOUR + 30
    second = CellTraffic(path, clock=clock)
    history = second.history()

    assert _bucket(history, T0)[1] > 0
    assert _bucket(history, T0 + HOUR) == (0, 0)


def test_a_corrupt_counts_file_starts_new_counts(tmp_path: Path) -> None:
    # Catches a counter that blocks the start.
    path = tmp_path / "cell-traffic.json"
    path.write_text('{"version": 1, "first_hour": "x", "requests": [[[[', encoding="utf-8")
    clock = Clock(T0 + 10)

    traffic = CellTraffic(path, clock=clock)
    traffic.request_started()

    assert _bucket(traffic.history(), T0)[0] == 1


def test_a_full_history_stays_below_the_controllers_read_cap(tmp_path: Path) -> None:
    # Catches an answer that cellctl's capped read cuts off: 672 busy hours.
    path = tmp_path / "cell-traffic.json"
    first_hour = T0 // HOUR - RETENTION_HOURS + 1
    path.write_text(json.dumps({
        "version": 1, "first_hour": first_hour,
        "requests": [123_456_789] * RETENTION_HOURS, "observed_seconds": [3600] * RETENTION_HOURS,
    }), encoding="utf-8")

    history = CellTraffic(path, clock=Clock(T0 + 5)).history()
    body = json.dumps(history).encode()

    assert len(history["requests"]) == RETENTION_HOURS
    assert len(body) < 64 * 1024
    assert cell_readiness.traffic_history(body) is not None


def test_an_hour_of_requests_less_than_a_second_apart_reads_as_observed(tmp_path: Path) -> None:
    # Catches seconds lost to rounding: the busiest hour must not look like a down hour.
    clock = Clock(T0)
    traffic = CellTraffic(tmp_path / "cell-traffic.json", clock=clock)
    for step in range(1, 9000):
        clock.now = T0 + step * 0.4
        traffic.request_started()
        traffic.request_finished()
    clock.now = T0 + HOUR

    requests, seconds = _bucket(traffic.history(), T0)

    assert requests == 8999
    assert seconds == HOUR
