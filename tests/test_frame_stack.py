"""A call on a fresh frame-stack chunk keeps the calls below it clear of a chunk end."""

from __future__ import annotations

import sys
import types

import pytest

from exomem.frame_stack import call_on_fresh_frame_stack

resource = pytest.importorskip("resource")

CALLS = 3000


def _narrow_leaf():
    return 1


# A wide frame: the loop's frame is then the last that fits a chunk at a few depths in a row, so
# the scan below cannot step over them.
_leaf = types.FunctionType(_narrow_leaf.__code__.replace(co_stacksize=60), globals(), "wide_leaf")


def _loop():
    for _ in range(CALLS):
        _leaf()


def _descend(depth, call):
    if depth:
        return _descend(depth - 1, call)
    return call()


def _faults_for_a_loop_started_at(depth):
    before = resource.getrusage(resource.RUSAGE_SELF).ru_minflt
    _descend(depth, lambda: call_on_fresh_frame_stack(_loop))
    return resource.getrusage(resource.RUSAGE_SELF).ru_minflt - before


@pytest.mark.skipif(sys.implementation.name != "cpython", reason="the frame stack is CPython's")
def test_a_loop_started_at_any_stack_depth_pays_no_page_fault_for_each_call():
    """Defect: one recall request took 17,000 page faults and 190 ms more once a change moved a
    chunk end into a hot loop. Where the end falls depends on the depth, so the test starts the same
    loop at every depth over more than one chunk and bounds the worst. Python 3.14 does not map a
    chunk for each call, so the test only has teeth on 3.11 to 3.13."""
    worst = max(_faults_for_a_loop_started_at(depth) for depth in range(400))

    assert worst < CALLS // 10


def test_the_call_returns_its_value_and_raises_its_error():
    def fail():
        raise ValueError("kept")

    assert call_on_fresh_frame_stack(lambda: 7) == 7
    with pytest.raises(ValueError, match="kept"):
        call_on_fresh_frame_stack(fail)
