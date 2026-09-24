"""In-process hand-off of a route's write-advisory inputs to its component.

Under fast durable acknowledgement a `remember` or an `edit` does not sweep for
near-duplicates and overlaps on the request thread. Its committed batch's
`write_advisory` component runs the same sweep later, and to compute exactly
what the route would have returned it needs the route's own inputs: the draft
title and body the write swept, which the receipt deliberately does not carry.

They are handed over here, keyed by vault and batch, and live only in this
process -- the same boundary as the pending overlay's parsed pages. Nothing is
persisted. A process that restarts before the component runs has no inputs,
and the component then falls back to its generic sweep over the page's
published vectors.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

#: How many committed batches' inputs one process holds at a time. An entry is
#: dropped once its result publishes; the bound only stops batches that never
#: run (superseded, stranded) from accumulating.
ROUTE_INPUT_LIMIT = 256

_LOCK = threading.Lock()
_INPUTS: OrderedDict[tuple[str, str], Any] = OrderedDict()


def _key(vault_root: Path, batch_id: str) -> tuple[str, str]:
    return (os.path.realpath(vault_root), str(batch_id))


def register_route_inputs(vault_root: Path, batch_id: str, inputs: Any) -> None:
    """Hold a committed batch's exact sweep inputs for its advisory component."""
    key = _key(vault_root, batch_id)
    with _LOCK:
        _INPUTS[key] = inputs
        _INPUTS.move_to_end(key)
        while len(_INPUTS) > ROUTE_INPUT_LIMIT:
            _INPUTS.popitem(last=False)


def route_inputs(vault_root: Path, batch_id: str) -> Any | None:
    """The inputs this process holds for one batch, or None."""
    with _LOCK:
        return _INPUTS.get(_key(vault_root, batch_id))


def forget_route_inputs(vault_root: Path, batch_id: str) -> None:
    with _LOCK:
        _INPUTS.pop(_key(vault_root, batch_id), None)


def reset_route_inputs() -> None:
    """Drop every held input (tests). Durable custody is untouched."""
    with _LOCK:
        _INPUTS.clear()
