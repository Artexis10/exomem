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

Every dispatched command, read or write, is a foreground request, except one
that a maintenance job dispatches from its own thread (`background_job()`): that
is bulk work. Every wait for and hold of a mutation boundary is a foreground
request: a drain or publication that holds the boundary keeps every writer
queued behind it waiting.
A store-mode warm-up's semantic step is foreground work too: writes are
refused until it ends (`warmup.warm_all`).

The rule that keeps this sound: no foreground thread ever waits on a parked
bulk pass. Two mechanisms keep it.

* A thread that holds a mutation boundary never parks (`_holds_a_boundary`).
* A foreground thread that waits on work a bulk pass may own -- a
  single-flight build, a lock a rebuild holds across its walk -- waits inside
  `awaiting_bulk()`. Every bulk pass runs until that wait ends, so the work
  finishes at its own speed instead of at the pass's progress floor.
* A foreground thread that joins a registered rebuild holds no boundary (the
  rebuild publishes under it), so it only leaves the in-flight count for the
  join (`outside_foreground()`). Other requests keep their priority.

A new wait on bulk-owned work must take one of the two, and a new yield in a
bulk pass must hold no other lock that a foreground thread can wait on.

A scheduler that starts bulk work from outside a request, such as a maintenance
tick, asks `bulk_free_since()` and starts only after bulk work has been free for
a gap. This is the same rule as above, read as a start condition.

Scheduling only: process-local counters, no content, nothing persisted.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

_T = TypeVar("_T")

#: Longest one yield may hold a bulk loop. An activation is 0.3-1 s settled and
#: bounded at 6 s by its own door budget; this covers the ordinary request and
#: caps what a stuck one can cost the bulk pass per unit of work.
MAX_YIELD_SECONDS = 2.0

#: Default progress floor for a `bulk()` pass. Overlapping requests can keep
#: `_in_flight` above zero indefinitely, and every unit then waited the full cap:
#: three units took 6 s. A pass may wait at most `wait_share()` of its elapsed
#: time, beyond one grace wait of `MAX_YIELD_SECONDS` so a request that arrives
#: early still gets priority. `EXOMEM_BULK_WAIT_SHARE` replaces it.
MAX_WAIT_SHARE = 0.5
WAIT_SHARE_ENV = "EXOMEM_BULK_WAIT_SHARE"

#: Bound each progress turn by elapsed time, not an estimated number of units:
#: cheap directory walks and expensive page parsing share the same pass.
#: Checked between complete units; an individual unit remains indivisible.
MAX_BULK_WORK_SECONDS = 0.05

_condition = threading.Condition()
#: Threads running foreground work now. A thread counts once however deeply its
#: marks nest: a write's dispatcher mark encloses its boundary mark.
_in_flight = 0
#: Foreground threads waiting on work a bulk pass may own (`awaiting_bulk`).
#: While any wait, no bulk pass parks.
_awaiting_bulk = 0
#: When `_bulk_may_run()` last became true, or None while it is false. Only a
#: scheduling input (`bulk_free_since`); every change to the two counters updates it.
_free_since: float | None = time.monotonic()
#: Per-thread foreground depth: a request that runs bulk work inline must never
#: wait on itself.
_local = threading.local()


def _bulk_may_run() -> bool:
    """No foreground thread is running, or one is waiting on bulk-owned work."""
    return _in_flight == 0 or _awaiting_bulk > 0


def _counted() -> None:
    """Record whether bulk work may run now. Call under `_condition` after a counter changes."""
    global _free_since
    if not _bulk_may_run():
        _free_since = None
    elif _free_since is None:
        _free_since = time.monotonic()


def bulk_free_since() -> float | None:
    """The monotonic time `_bulk_may_run()` last became true, or None while it is false.

    The store thread starts maintenance only after a quiet gap, so it asks how long bulk
    work has been free. A scheduling counter: it holds no content and leaves nothing persisted.
    """
    with _condition:
        return _free_since


@contextmanager
def foreground() -> Iterator[None]:
    """Hold bulk loops at their next yield for the duration of this block."""
    global _in_flight
    # Counted up and down, never restored from a saved value: marks on one
    # thread need not close in the order they opened. Reserved-state
    # coordination releases its boundaries out of order.
    _local.depth = getattr(_local, "depth", 0) + 1
    if _local.depth == 1:
        with _condition:
            _in_flight += 1
            _counted()
    try:
        yield
    finally:
        _local.depth -= 1
        if _local.depth == 0:
            with _condition:
                _in_flight -= 1
                _counted()
                if _bulk_may_run():
                    _condition.notify_all()


@contextmanager
def background_job() -> Iterator[None]:
    """Mark this thread as running a governed maintenance job, off the request path.

    A job's dispatch is minutes of bulk work, not a latency-bound request, so
    `request_foreground` leaves it unmarked and `yield_to_foreground` can park it
    for a real request. Waits for and holds of a mutation boundary stay
    foreground: they mark themselves (`mutation_lock`), and a writer queued
    behind a job's chunk hold must not be parked.
    """
    _local.job = getattr(_local, "job", 0) + 1
    try:
        yield
    finally:
        _local.job -= 1


@contextmanager
def request_foreground() -> Iterator[None]:
    """`foreground()` for a dispatched command, except inside `background_job()`."""
    if getattr(_local, "job", 0):
        yield
        return
    with foreground():
        yield


@contextmanager
def awaiting_bulk() -> Iterator[None]:
    """Let every bulk pass run while this foreground thread waits on its work.

    For a short wait on work a bulk pass may own, by a thread that may hold a
    boundary: joining a single-flight build, or a lock a rebuild holds across
    its walk. Without it the owner parks for this very request and the wait
    lasts the pass's progress floor (`wait_share()`) instead of the work.
    Every pass is released rather than only the owner, because another
    foreground thread can hold the owner too: a writer queued on the boundary
    this thread holds. No request has priority meanwhile; a long join by a
    thread that holds no boundary takes `outside_foreground()` instead. A
    thread outside `foreground()` claims nothing.
    """
    global _awaiting_bulk
    if not getattr(_local, "depth", 0):
        yield
        return
    with _condition:
        _awaiting_bulk += 1
        _counted()
        _condition.notify_all()
    try:
        yield
    finally:
        with _condition:
            _awaiting_bulk -= 1
            _counted()


@contextmanager
def outside_foreground() -> Iterator[None]:
    """Leave the in-flight count while this thread joins a bulk pass's work.

    The pass runs unless another request holds it, and that request keeps its
    priority. Only for a thread that holds nothing a foreground request waits
    on; a boundary holder takes `awaiting_bulk()` instead, because a writer
    queued on its boundary would keep the pass parked. Nothing may open or
    close a mark inside the block.
    """
    global _in_flight
    depth = getattr(_local, "depth", 0)
    if not depth:
        yield
        return
    _local.depth = 0
    with _condition:
        _in_flight -= 1
        _counted()
        if _bulk_may_run():
            _condition.notify_all()
    try:
        yield
    finally:
        with _condition:
            _in_flight += 1
            _counted()
        _local.depth = depth


def in_flight() -> int:
    """How many threads are running foreground work now."""
    with _condition:
        return _in_flight


def foreground_elsewhere() -> bool:
    """Whether another thread's foreground work would hold a bulk pass at its next yield.

    For a bulk unit that holds a boundary, so never yields, but can end between its
    parts: ending there brings its next yield, outside the boundary, forward.
    """
    mine = 1 if getattr(_local, "depth", 0) else 0  # a thread counts once in `_in_flight`
    return _in_flight > mine and not _awaiting_bulk


class BulkCancelled(BaseException):
    """Owned bulk work stopped; ordinary proof failures must not swallow it."""


def cancellation_event() -> threading.Event | None:
    """Return this scope's stop signal for a guarded operation on another thread."""
    return getattr(_local, "stop", None)


def check_cancelled() -> None:
    """Cancel this thread's owned bulk scope between complete work units."""
    from . import request_budget

    request_budget.checkpoint()
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


class PassBudget:
    """How long one `bulk()` pass has run, waited, and may still run unyielded.

    ``wait_share``, in [0, 1), replaces `wait_share()` for this pass. A pass
    that may wait most of its time sets ``max_waited`` too: the total seconds it
    may wait, so it still finishes within that delay beyond its work.
    """

    __slots__ = ("started", "waited", "work_until", "wait_share", "max_waited")

    def __init__(self, *, wait_share: float | None = None, max_waited: float | None = None) -> None:
        self.started = time.monotonic()
        self.waited = 0.0
        self.work_until = 0.0
        self.wait_share = wait_share
        self.max_waited = max_waited

    def allowance(self, now: float) -> float:
        """Seconds this pass may still wait without breaking the floor."""
        work = max(0.0, now - self.started - self.waited)
        # Charge the initial grace once. Counting previous waits as elapsed
        # work would let those waits buy more waiting and double the grace.
        share = wait_share() if self.wait_share is None else self.wait_share
        wait_per_work = share / (1.0 - share)
        allowed = MAX_YIELD_SECONDS + wait_per_work * work
        if self.max_waited is not None:
            allowed = min(allowed, self.max_waited)
        return allowed - self.waited

    def after_capped_wait(self, now: float, waited: float) -> None:
        self.work_until = now + min(waited, MAX_BULK_WORK_SECONDS)


def wait_share() -> float:
    """The share of its elapsed time a `bulk()` pass may wait, in [0, 1).

    `EXOMEM_BULK_WAIT_SHARE`, read at each call; unset, malformed or out of range
    means `MAX_WAIT_SHARE`. A share of 1 or more would make the progress floor unbounded.
    """
    raw = os.environ.get(WAIT_SHARE_ENV)
    if raw is None:
        return MAX_WAIT_SHARE
    try:
        share = float(raw)
    except ValueError:
        return MAX_WAIT_SHARE
    return share if math.isfinite(share) and 0.0 <= share < 1.0 else MAX_WAIT_SHARE


def pass_waited() -> float:
    """Seconds this thread's current `bulk()` pass has waited at its yields; 0.0 outside one."""
    budget: PassBudget | None = getattr(_local, "budget", None)
    return budget.waited if budget is not None else 0.0


def _wait_limit(started: float, max_wait: float, budget: PassBudget | None) -> float:
    """Seconds a yield may wait now: `max_wait`, held to the pass's progress floor inside a `bulk()` pass."""
    limit = max(0.0, max_wait)
    if budget is None:
        return limit
    if started < budget.work_until:
        return 0.0
    return min(limit, max(0.0, budget.allowance(started)))


def _account_wait(started: float, budget: PassBudget | None, *, released: bool) -> float:
    """Charge a finished wait to the pass; return its length."""
    check_cancelled()
    now = time.monotonic()
    waited = now - started
    if budget is not None:
        budget.waited += waited
        if not released:
            budget.after_capped_wait(now, waited)
    return waited


def yield_to_foreground(*, max_wait: float = MAX_YIELD_SECONDS) -> float:
    """Wait while a foreground request runs, at most `max_wait`; return the wait.

    Inside a `bulk()` pass the wait is also held to the pass's progress floor
    (`wait_share()`, unless its `PassBudget` sets its own). The stop of an owned
    scope ends the wait and cancels the pass at once if it is a `StopEvent`.
    """
    check_cancelled()
    budget: PassBudget | None = getattr(_local, "budget", None)
    if _bulk_may_run():  # unlocked reads: the common case costs two loads
        if budget is not None:
            budget.work_until = 0.0
        return 0.0
    if getattr(_local, "depth", 0) or _holds_a_boundary():
        return 0.0
    started = time.monotonic()
    limit = _wait_limit(started, max_wait, budget)
    if limit <= 0.0:
        return 0.0
    released = _park(limit, getattr(_local, "stop", None))
    return _account_wait(started, budget, released=released)


def _park(limit: float, stop: threading.Event | None) -> bool:
    """Wait up to `limit` for bulk work to be free; whether it is, or the scope's stop is set."""
    with _condition:
        if stop is None:
            return _condition.wait_for(_bulk_may_run, timeout=limit)
        return _condition.wait_for(lambda: _bulk_may_run() or stop.is_set(), timeout=limit)


class StopEvent(threading.Event):
    """A stop flag that also wakes every yield parked behind a request when it is set.

    A pass parked at a yield waits on `_condition`, which a plain `threading.Event` cannot notify.
    Pass this as the stop of a `bulk(stop=...)` or `cancellable()` scope, so setting it ends the wait
    now. A stop that is a plain Event is still seen once the wait's cap runs out.
    """

    def set(self) -> None:
        super().set()
        with _condition:
            _condition.notify_all()


#: How often `wait_while` asks again. The writer it waits for polls the boundary at this rate
#: (`mutation_lock`'s default poll interval), so a slower ask would outlast the wait it serves.
_WAIT_WHILE_POLL_SECONDS = 0.025


def wait_while(waiting: Callable[[], bool], *, max_wait: float = MAX_YIELD_SECONDS) -> float:
    """Wait while `waiting()` is true, at most `max_wait`; return the wait.

    For a wait that no foreground mark covers, such as a writer in another process that polls for the
    boundary this thread just released. The floors are `yield_to_foreground`'s: the cap, the pass's
    progress share, and a thread that holds a boundary never waits. A thread that runs foreground
    work does wait, since a CLI maintenance run is one and the cap bounds it. A stop of an owned bulk
    scope ends the wait. It holds no lock; `waiting` must hold none either.
    """
    check_cancelled()
    if _holds_a_boundary() or not waiting():
        return 0.0
    budget: PassBudget | None = getattr(_local, "budget", None)
    started = time.monotonic()
    limit = _wait_limit(started, max_wait, budget)
    if limit <= 0.0:
        return 0.0
    deadline = started + limit
    released = True
    while waiting():
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            released = False
            break
        wait_for_retry(min(_WAIT_WHILE_POLL_SECONDS, remaining))
    return _account_wait(started, budget, released=released)


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
def cancellable(stop: threading.Event | None) -> Iterator[None]:
    """Own cancellation without changing a nested pass's scheduling budget."""
    previous_stop = getattr(_local, "stop", None)
    if stop is not None:
        _local.stop = stop
    try:
        check_cancelled()
        yield
    finally:
        _local.stop = previous_stop


@contextmanager
def bulk(*, stop: threading.Event | None = None, budget: PassBudget | None = None) -> Iterator[None]:
    """Mark this thread's work as an off-boundary bulk pass for `yielding_in_bulk`.

    ``budget`` continues one pass across several scopes, for a pass whose units run
    between other work on the same thread; otherwise each outermost scope is a pass.
    """
    with cancellable(stop):
        outermost = not getattr(_local, "bulk", 0)
        _local.bulk = getattr(_local, "bulk", 0) + 1
        if outermost:
            _local.budget = budget if budget is not None else PassBudget()
        try:
            yield
        finally:
            _local.bulk -= 1
            if outermost:
                _local.budget = None


def yield_in_bulk() -> float:
    """`yield_to_foreground()` inside `bulk()` on this thread; no wait and no check elsewhere.

    For one yield point between the units of a loop that `yielding_in_bulk` cannot wrap,
    or that is too fine to ask at every unit. The caller holds no lock a request waits on.
    """
    if getattr(_local, "bulk", 0):
        return yield_to_foreground()
    return 0.0


def give_way(budget: PassBudget | None = None) -> None:
    """Wait for a request in flight, once, as a bulk pass between two units of other work.

    The bulk scope holds only this wait, so what follows it keeps its own fan-out chain
    unbroken: a helper below it that yields in a bulk pass never does here. ``budget``
    continues one pass across several of these waits.
    """
    with bulk(budget=budget):
        yield_to_foreground()


@contextmanager
def recording_waits(fields: dict[str, Any]) -> Iterator[None]:
    """Put what this thread's bulk pass waited at its yields in the block into `fields["waited_ms"]`.

    For a span or a log line that times a pass's work: its duration counts those waits, and this
    lets a later comparison take them apart. Nothing is recorded when the block did not wait.
    """
    before = pass_waited()
    try:
        yield
    finally:
        waited_ms = int((pass_waited() - before) * 1000.0)
        if waited_ms > 0:
            fields["waited_ms"] = waited_ms


def yielding_in_bulk(items: Iterable[_T]) -> Iterable[_T]:
    """`yielding(items)` inside `bulk()` on this thread; `items` untouched elsewhere.

    For helpers shared between a bulk pass and a caller holding a mutation
    boundary: only the bulk pass may pause, and even there never while this
    thread holds a boundary (see `_holds_a_boundary`).
    """
    if getattr(_local, "bulk", 0):
        return yielding(items)
    return items
