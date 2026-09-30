"""The shared poll loop for the two routine workers.

Both entrypoints previously ran `while True: if not await worker.run_once(): sleep(poll)`
with `poll` fixed at one second. That is correct for latency and wrong for a serverless
database, which is billed for being awake rather than for answering: a query every second
never leaves the endpoint the idle gap its autosuspend timer needs, so an idle fleet costs
exactly as much as a busy one.

The escalation here is the whole fix. It keeps the original latency while work is flowing,
because any pass that does something resets the delay to the floor, and only a genuinely
idle fleet walks out to the ceiling. Latency for the first operation after a quiet period
rises to at most that ceiling, which is proportionate: the reconciler that submits most of
this work runs on a comparable cadence, and a cell provision takes minutes regardless.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol


class _Worker(Protocol):
    async def run_once(self) -> Any: ...


def next_idle_delay(consecutive_idle_passes: int, *, floor: float, ceiling: float) -> float:
    """Seconds to wait after `consecutive_idle_passes` empty passes in a row.

    Zero passes means work just happened, so there is nothing to wait for. The first
    empty pass waits the floor, and each further one doubles until the ceiling, which is
    never exceeded even when a caller supplies a ceiling below the floor.
    """
    if consecutive_idle_passes <= 0:
        return 0.0
    delay = floor * (2 ** (consecutive_idle_passes - 1))
    return min(delay, ceiling) if ceiling >= floor else ceiling


async def run_polling_loop(
    worker: _Worker,
    *,
    poll_seconds: float,
    idle_poll_seconds: float,
    sleep: Callable[[float], Awaitable[None] | None] = asyncio.sleep,
    passes: int | None = None,
) -> None:
    """Drive `worker.run_once()` forever, backing off while it reports nothing to do.

    `passes` bounds the loop for tests; production passes None and runs until cancelled.
    `sleep` is injectable for the same reason and may be synchronous.
    """
    idle = 0
    completed = 0
    while passes is None or completed < passes:
        completed += 1
        if await worker.run_once():
            idle = 0
            continue
        idle += 1
        outcome = sleep(next_idle_delay(idle, floor=poll_seconds, ceiling=idle_poll_seconds))
        if outcome is not None:
            await outcome
