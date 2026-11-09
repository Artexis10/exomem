"""The doubling retry delay that every persistent retry loop shares.

A leaf module: the derived-receipt drain, the graph quarantine and the media
worker each count failures their own way, and all of them need the same
arithmetic, which overflows a float when written as an uncapped power.
"""

from __future__ import annotations

import math


def capped_doubling(base: float, maximum: float, doublings: int) -> float:
    """`base` doubled `doublings` times, never more than `maximum`.

    The exponent stops at the first doubling that reaches `maximum`: an uncapped
    power overflows the float multiply after about a thousand doublings, and a
    failure count that persists across restarts gets there. A caller that then
    cannot record its next failure retries on every pass.
    """
    reach = max(0, math.ceil(math.log2(maximum / base))) if base > 0 else 0
    return min(maximum, base * 2.0 ** min(max(0, doublings), reach))
