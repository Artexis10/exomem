"""Private benchmark instrumentation, loaded unchanged into both paired arms.

The adapter observes real collectors while original bodies keep class identity,
policy decisions and public fields. It needs no capture API on an older tag.
"""

from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Load the head's stdlib accounting leaf without importing head's application.
_SOURCE = Path(__file__).resolve().parents[1] / "src/exomem/recall_timing.py"
_SPEC = importlib.util.spec_from_file_location("_recall_benchmark_clock", _SOURCE)
_CLOCK = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CLOCK
_SPEC.loader.exec_module(_CLOCK)
Trace = _CLOCK.Trace


@contextmanager
def instrument():
    """Wrap constructor/span boundaries only; restore them even after a failure."""
    from exomem import call_spans, find_types

    original_init = find_types.FindTimings.__init__
    original_span = find_types.FindTimings.span
    original_parallel = getattr(find_types.FindTimings, "parallel", None)
    original_call_span = call_spans.span
    collectors: list[Any] = []
    # The supported release tags use the known synchronous serial producer.
    trace = Trace(known_serial=True)

    def initialize(collector, *args, **kwargs):
        original_init(collector, *args, **kwargs)
        if _CLOCK.CURRENT.get() is trace:
            collectors.append(collector)

    @contextmanager
    def find_span(collector, name, *args, **kwargs):
        # The real collector owns cross-thread calls even without copied context.
        if collector not in collectors:
            with original_span(collector, name, *args, **kwargs) as value:
                yield value
            return
        marker = _CLOCK.CURRENT.set(trace)
        try:
            with trace.span(name), original_span(collector, name, *args, **kwargs) as value:
                yield value
        finally:
            _CLOCK.CURRENT.reset(marker)

    @contextmanager
    def call_span(name, *args, **kwargs):
        # Prefix removal joins the product's due-state and find stage names.
        if _CLOCK.CURRENT.get() is trace:
            with trace.span(name.removeprefix("recall.")), original_call_span(name, *args, **kwargs) as value:
                yield value
        else:
            with original_call_span(name, *args, **kwargs) as value:
                yield value

    class MirroredParallel:
        def __init__(self, observed, original):
            self.observed, self.original = observed, original

        @contextmanager
        def branch(self):
            with self.observed.branch(), self.original.branch() as value:
                yield value

    @contextmanager
    def parallel(collector, *args, **kwargs):
        with original_parallel(collector, *args, **kwargs) as original:
            if collector not in collectors:
                yield original
            else:
                with trace.parallel() as observed:
                    yield MirroredParallel(observed, original)

    find_types.FindTimings.__init__ = initialize
    find_types.FindTimings.span = find_span
    call_spans.span = call_span
    if original_parallel is not None:
        find_types.FindTimings.parallel = parallel
    marker = _CLOCK.CURRENT.set(trace)
    try:
        yield trace, collectors
    finally:
        trace.finish()
        _CLOCK.CURRENT.reset(marker)
        find_types.FindTimings.__init__ = original_init
        find_types.FindTimings.span = original_span
        call_spans.span = original_call_span
        if original_parallel is not None:
            find_types.FindTimings.parallel = original_parallel


def clear_results() -> None:
    """Legacy-compatible result-only reset inside an isolated benchmark worker."""
    from exomem import find

    with find._FIND_CACHE_LOCK:
        find._FIND_CACHE.clear()
        find._FIND_CACHE_CHECKPOINTS.clear()
