"""Start a call on a fresh interpreter frame-stack chunk, so the calls below it meet no chunk end.

CPython 3.11 and later keeps a thread's call frames in chunks of 16 KiB and unmaps a chunk when the
call that began it returns. A loop whose own frame is the last that fits in a chunk then maps,
faults in and unmaps one chunk for every call it makes. Where a stack meets a chunk end depends on
every frame below it, so a change that adds a few frames can put the end inside a hot loop with no
other sign: one recall request took 17,000 page faults and 190 ms more after a change that added 44
words of frames.

A frame that needs more words than a chunk holds always begins a new chunk, sized to hold it, and
that chunk lives until the frame returns. `_RESERVED_WORDS` gives the chunk about 65,000 words of
room below the wrapper, and a recall request was measured to go about 10,000 words below it. A
call deeper than the room leaves the fresh chunk and meets chunk ends again.

CPython keeps one chunk cached per thread from 3.13.13, 3.14.4 and 3.15 (gh-142183), which removes
the cost. Delete this module when the project's oldest supported Python has that change; 3.11 and
3.12 never get it.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Any, TypeVar, cast

_Result = TypeVar("_Result")

#: Words that the wrapper's frame reserves for its operand stack, far more than the 2,045 words of
#: a chunk. CPython maps a chunk of 1 MiB for it: the wrapper holds 65,536 words and the calls
#: below it use the rest. Pages that nothing touches cost no memory, so the price is one map and
#: one unmap for each call that this module wraps.
_RESERVED_WORDS = 65536


def _call(call: Callable[[], Any]) -> Any:
    return call()


# Another interpreter keeps its frames elsewhere: it runs the call as it is.
_call_on_fresh_chunk = (
    types.FunctionType(
        _call.__code__.replace(co_stacksize=_RESERVED_WORDS), globals(), "call_on_fresh_chunk"
    )
    if sys.implementation.name == "cpython"
    else _call
)


def call_on_fresh_frame_stack(call: Callable[[], _Result]) -> _Result:
    """Return ``call()``, run so that the calls below it sit on one fresh frame-stack chunk."""
    return cast(_Result, _call_on_fresh_chunk(call))
