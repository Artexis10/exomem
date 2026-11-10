"""Start a call on a fresh interpreter frame-stack chunk, so no call below it meets a chunk end.

CPython 3.11 and later keeps a thread's call frames in chunks of 16 KiB and unmaps a chunk when the
call that began it returns. A loop whose own frame is the last that fits in a chunk then maps,
faults in and unmaps one chunk for every call it makes. Where a stack meets a chunk end depends on
every frame below it, so a change that adds a few frames can put the end inside a hot loop with no
other sign: one recall request took 17,000 page faults and 190 ms more after a change that added 44
words of frames. A frame that needs more words than a chunk holds always begins a new chunk, sized
to hold it with room to spare, and that chunk lives until the frame returns. Every call below
that frame then sits in one chunk and meets no end.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import Any, TypeVar, cast

_Result = TypeVar("_Result")

#: Words that the frame reserves for its operand stack: more than one 16 KiB chunk holds (2045), so
#: the frame never fits the current chunk. CPython then maps a chunk of at least this many words
#: plus 1000, rounded up to a power of two times 16 KiB, and the calls below the frame use what
#: remains. Reserved words are not touched, so the cost is one map and one unmap per call.
_RESERVED_WORDS = 8192


def _call(call: Callable[[], Any]) -> Any:
    return call()


_call_on_fresh_chunk = types.FunctionType(
    _call.__code__.replace(co_stacksize=_RESERVED_WORDS), globals(), "call_on_fresh_chunk"
)


def call_on_fresh_frame_stack(call: Callable[[], _Result]) -> _Result:
    """Return ``call()``, run so that every call below it sits on one fresh frame-stack chunk."""
    return cast(_Result, _call_on_fresh_chunk(call))
