"""The routine workers must stop polling a sleeping database once the fleet is idle.

Both worker entrypoints ran `sleep(poll_seconds)` after every empty pass, so at the
shipped one-second setting they contacted PostgreSQL 86,400 times a day whether or not
anything was happening. On a serverless endpoint billed by wakefulness that holds the
compute open permanently. These tests pin the escalation and, just as importantly, the
reset: a busy fleet must keep the original latency.
"""

from __future__ import annotations

import pytest

from exomem_provisioner.worker_loop import next_idle_delay, run_polling_loop


def test_the_first_idle_pass_waits_the_configured_floor() -> None:
    assert next_idle_delay(1, floor=1.0, ceiling=120.0) == 1.0


def test_consecutive_idle_passes_escalate() -> None:
    delays = [next_idle_delay(n, floor=1.0, ceiling=120.0) for n in range(1, 9)]
    assert delays == sorted(delays)
    assert delays[0] < delays[-1]


def test_escalation_never_exceeds_the_ceiling() -> None:
    assert all(next_idle_delay(n, floor=1.0, ceiling=120.0) <= 120.0 for n in range(1, 1000))
    assert next_idle_delay(999, floor=1.0, ceiling=120.0) == 120.0


def test_a_ceiling_below_the_floor_still_yields_the_ceiling() -> None:
    # Configuration is validated elsewhere; the primitive must not invert the bound.
    assert next_idle_delay(1, floor=30.0, ceiling=5.0) == 5.0


def test_zero_idle_passes_is_not_a_wait() -> None:
    assert next_idle_delay(0, floor=1.0, ceiling=120.0) == 0.0


class _Worker:
    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def run_once(self) -> bool:
        self.calls += 1
        return self._outcomes.pop(0)


@pytest.mark.asyncio
async def test_an_idle_fleet_backs_off_towards_the_ceiling() -> None:
    slept: list[float] = []
    worker = _Worker([False] * 6)
    await run_polling_loop(
        worker,
        poll_seconds=1.0,
        idle_poll_seconds=8.0,
        sleep=lambda delay: slept.append(delay),
        passes=6,
    )
    assert worker.calls == 6
    assert slept == sorted(slept)
    assert slept[-1] == 8.0


@pytest.mark.asyncio
async def test_work_resets_the_backoff_to_the_floor() -> None:
    slept: list[float] = []
    # Four empty passes climb, then real work lands, then one more empty pass.
    worker = _Worker([False, False, False, False, True, False])
    await run_polling_loop(
        worker,
        poll_seconds=1.0,
        idle_poll_seconds=8.0,
        sleep=lambda delay: slept.append(delay),
        passes=6,
    )
    # The pass that did work does not sleep at all, and the next empty pass is back
    # at the floor rather than resuming the climb.
    assert slept[-1] == 1.0
    assert len(slept) == 5


@pytest.mark.asyncio
async def test_a_busy_fleet_never_sleeps() -> None:
    slept: list[float] = []
    worker = _Worker([True] * 5)
    await run_polling_loop(
        worker,
        poll_seconds=1.0,
        idle_poll_seconds=120.0,
        sleep=lambda delay: slept.append(delay),
        passes=5,
    )
    assert slept == []
