"""A call on a fresh frame-stack chunk keeps the calls below it clear of a chunk end."""

from __future__ import annotations

import sys
import types

import pytest

from exomem.frame_stack import call_on_fresh_frame_stack

resource = pytest.importorskip("resource")

CALLS = 3000
# Frames of about 210 words each: 120 of them walk 25,000 words down, past twice the 10,000 words
# that a recall request was measured to go below the wrapper.
DEPTHS = 120


def _widened(function, words):
    """``function`` with a frame of ``words`` more, so a few frames walk far down a chunk."""
    code = function.__code__.replace(co_stacksize=words)
    return types.FunctionType(code, globals(), function.__name__)


def _narrow_leaf():
    return 1


# The leaf frame is wider than a step down: the loop's frame is then the last that fits a chunk at
# one depth or more in each chunk, so the scan cannot step over a chunk end.
_leaf = _widened(_narrow_leaf, 300)


def _loop():
    for _ in range(CALLS):
        _leaf()


def _narrow_descend(depth, call):
    if depth:
        return _descend(depth - 1, call)
    return call()


_descend = _widened(_narrow_descend, 200)


def _faults_of_a_loop_at(depth, wrapped):
    before = resource.getrusage(resource.RUSAGE_SELF).ru_minflt
    if wrapped:
        call_on_fresh_frame_stack(lambda: _descend(depth, _loop))
    else:
        _descend(depth, _loop)
    return resource.getrusage(resource.RUSAGE_SELF).ru_minflt - before


@pytest.mark.skipif(sys.implementation.name != "cpython", reason="the frame stack is CPython's")
def test_a_loop_below_the_wrapper_pays_no_page_fault_for_each_call_at_any_depth():
    """Defect: one recall request took 17,000 page faults and 190 ms more once a change moved a
    chunk end into a hot loop, and a wrapper chunk that is too small leaves the request thrashing
    again below its end. The test starts the same loop at every depth below the wrapper, over twice
    the depth that a request reaches, and bounds the worst."""
    bound = CALLS // 10
    plain = max(_faults_of_a_loop_at(depth, wrapped=False) for depth in range(DEPTHS))
    if plain < bound:
        pytest.skip(
            "this interpreter maps no chunk for each call: it caches one per thread (gh-142183)"
        )

    below_the_wrapper = max(_faults_of_a_loop_at(depth, wrapped=True) for depth in range(DEPTHS))

    assert below_the_wrapper < bound


def test_the_call_returns_its_value_and_raises_its_error():
    def fail():
        raise ValueError("kept")

    assert call_on_fresh_frame_stack(lambda: 7) == 7
    with pytest.raises(ValueError, match="kept"):
        call_on_fresh_frame_stack(fail)
