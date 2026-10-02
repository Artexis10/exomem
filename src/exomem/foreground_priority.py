"""Let a latency-bound request run ahead of in-process bulk work.

A whole-vault graph pass is CPU-bound Python in the serving process. Under the
GIL, a request thread that releases the lock around each short SQLite call has
to wait for the bulk thread to hand it back, once per call: measured on the
personal service, every activation stage ran 5-15x slower while a rebuild ran,
and a 20 ms synthetic request took 485 ms beside one CPU-bound thread.

A request marks itself with `foreground()`. A bulk loop calls
`yield_to_foreground()` between units of work; the call blocks, releasing the
GIL, while any foreground request is in flight. The wait is bounded, so a
stream of requests slows bulk work down but can never stop it.

Scheduling only: process-local counters, no content, nothing persisted. Mark
only work that never waits on bulk work itself (read-only requests), or the
two would wait on each other until the bound.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import TypeVar

_T = TypeVar("_T")

#: Longest one yield may hold a bulk loop. An activation is 0.3-1 s settled and
#: bounded at 6 s by its own door budget; this covers the ordinary request and
#: caps what a stuck one can cost the bulk pass per unit of work.
MAX_YIELD_SECONDS = 2.0

#: Progress floor for a `bulk()` pass. Overlapping requests can keep
#: `_in_flight` above zero indefinitely, and every unit then waited the full cap:
#: three units took 6 s. A pass may wait at most `MAX_WAIT_SHARE` of its elapsed
#: time, beyond one grace wait of `MAX_YIELD_SECONDS` so a request that arrives
#: early still gets priority. After a wait that ran into its cap, the pass runs
#: a batch of units without yielding, sized from its own measured unit cost to
#: take about as long as that wait.
MAX_WAIT_SHARE = 0.5

_condition = threading.Condition()
_in_flight = 0
#: Per-thread foreground depth: a request that runs bulk work inline must never
#: wait on itself.
_local = threading.local()


@contextmanager
def foreground() -> Iterator[None]:
    """Hold bulk loops at their next yield for the duration of this block."""
    global _in_flight
    with _condition:
        _in_flight += 1
    _local.depth = getattr(_local, "depth", 0) + 1
    try:
        yield
    finally:
        _local.depth -= 1
        with _condition:
            _in_flight -= 1
            if _in_flight == 0:
                _condition.notify_all()


def in_flight() -> int:
    """How many foreground requests are running now."""
    with _condition:
        return _in_flight


class BulkCancelled(BaseException):
    """Owned bulk work stopped; ordinary proof failures must not swallow it."""


def check_cancelled() -> None:
    """Cancel this thread's owned bulk scope between complete work units."""
    stop = getattr(_local, "stop", None)
    if stop is not None and stop.is_set():
        raise BulkCancelled()


def wait_for_retry(seconds: float) -> None:
    """Keep ordinary retry sleep; wake an owned bulk scope on its stop event."""
    stop = getattr(_local, "stop", None)
    if stop is None:
        time.sleep(seconds)
    elif stop.wait(seconds):
        raise BulkCancelled()


class _PassBudget:
    """How long one `bulk()` pass has run, waited, and may still run unyielded."""

    __slots__ = ("started", "waited", "units", "free_units")

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.waited = 0.0
        self.units = 0
        self.free_units = 0

    def allowance(self, now: float) -> float:
        """Seconds this pass may still wait without breaking the floor."""
        elapsed = now - self.started
        return MAX_YIELD_SECONDS + MAX_WAIT_SHARE * elapsed - self.waited

    def after_capped_wait(self, now: float, waited: float) -> None:
        worked = max(0.0, now - self.started - self.waited)
        per_unit = worked / self.units if self.units else 0.0
        if per_unit <= 0.0:
            self.free_units = 1
            return
        self.free_units = max(1, math.ceil(waited / per_unit))


def yield_to_foreground(*, max_wait: float = MAX_YIELD_SECONDS) -> float:
    """Wait while a foreground request runs, at most `max_wait`; return the wait.

    Inside a `bulk()` pass the wait is also held to the pass's progress floor
    (`MAX_WAIT_SHARE`).
    """
    check_cancelled()
    budget: _PassBudget | None = getattr(_local, "budget", None)
    if budget is not None:
        budget.units += 1
        if budget.free_units:
            budget.free_units -= 1
            return 0.0
    if not _in_flight:  # unlocked read: the common case costs one load
        return 0.0
    if getattr(_local, "depth", 0) or _holds_a_boundary():
        return 0.0
    started = time.monotonic()
    limit = max(0.0, max_wait)
    if budget is not None:
        limit = min(limit, max(0.0, budget.allowance(started)))
        if limit <= 0.0:
            return 0.0
    with _condition:
        released = _condition.wait_for(lambda: _in_flight == 0, timeout=limit)
    check_cancelled()
    now = time.monotonic()
    waited = now - started
    if budget is not None:
        budget.waited += waited
        if not released:
            budget.after_capped_wait(now, waited)
    return waited


def _holds_a_boundary() -> bool:
    """A mutation boundary or vault creation lock held by this thread.

    Pausing there would make every writer queued on it wait out the request
    too, so a yield under one returns at once whatever called it.
    """
    from . import mutation_lock, vault

    return bool(getattr(vault._HELD_LOCKS, "keys", None)) or (  # noqa: SLF001
        mutation_lock.current_thread_holds_boundary()
    )


def yielding(items: Iterable[_T], *, max_wait: float = MAX_YIELD_SECONDS) -> Iterator[_T]:
    """`items`, yielding to foreground requests before each one."""
    for item in items:
        yield_to_foreground(max_wait=max_wait)
        yield item


@contextmanager
def bulk(*, stop: threading.Event | None = None) -> Iterator[None]:
    """Mark this thread's work as an off-boundary bulk pass for `yielding_in_bulk`."""
    previous_stop = getattr(_local, "stop", None)
    if stop is not None:
        _local.stop = stop
    outermost = not getattr(_local, "bulk", 0)
    _local.bulk = getattr(_local, "bulk", 0) + 1
    if outermost:
        _local.budget = _PassBudget()
    try:
        check_cancelled()
        yield
    finally:
        _local.stop = previous_stop
        _local.bulk -= 1
        if outermost:
            _local.budget = None


def yielding_in_bulk(items: Iterable[_T]) -> Iterable[_T]:
    """`yielding(items)` inside `bulk()` on this thread; `items` untouched elsewhere.

    For helpers shared between a bulk pass and a caller holding a mutation
    boundary: only the bulk pass may pause, and even there never while this
    thread holds a boundary (see `_holds_a_boundary`).
    """
    if getattr(_local, "bulk", 0):
        return yielding(items)
    return items
