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


def yield_to_foreground(*, max_wait: float = MAX_YIELD_SECONDS) -> float:
    """Wait while a foreground request runs, at most `max_wait`; return the wait."""
    if not _in_flight:  # unlocked read: the common case costs one load
        return 0.0
    if getattr(_local, "depth", 0) or _holds_a_boundary():
        return 0.0
    started = time.monotonic()
    with _condition:
        _condition.wait_for(lambda: _in_flight == 0, timeout=max(0.0, max_wait))
    return time.monotonic() - started


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
def bulk() -> Iterator[None]:
    """Mark this thread's work as an off-boundary bulk pass for `yielding_in_bulk`."""
    _local.bulk = getattr(_local, "bulk", 0) + 1
    try:
        yield
    finally:
        _local.bulk -= 1


def yielding_in_bulk(items: Iterable[_T]) -> Iterable[_T]:
    """`yielding(items)` inside `bulk()` on this thread; `items` untouched elsewhere.

    For helpers shared between a bulk pass and a caller holding a mutation
    boundary: only the bulk pass may pause, and even there never while this
    thread holds a boundary (see `_holds_a_boundary`).
    """
    if getattr(_local, "bulk", 0):
        return yielding(items)
    return items
