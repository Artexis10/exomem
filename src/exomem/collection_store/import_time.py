"""Zone rules and local wall-clock resolution for import time bases (OpenSpec bring-in-large-exports §3).

Zone rules come from the pinned ``tzdata`` package, never the host's zone database,
so every platform resolves the same local time to the same instant; a job binding
records :data:`VERSION`. A local time in a repeated hour resolves by the mapping's
declared fold rule, and one in a gap resolves to nothing: the importer refuses it.
"""

from __future__ import annotations

import datetime as dt
import functools
import importlib.resources
import zoneinfo
from collections.abc import Mapping
from typing import Any

import tzdata

#: The IANA release of the pinned zone rules.
VERSION: str = tzdata.IANA_VERSION
_MAX_NAME_BYTES = 64


@functools.cache
def _names() -> frozenset[str]:
    # The package ships its own list of the zone names it holds.
    listing = importlib.resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8")
    return frozenset(listing.split())


@functools.lru_cache(maxsize=32)
def zone(name: Any) -> zoneinfo.ZoneInfo | None:
    """The named zone from the pinned rules, or None when they hold no such zone."""
    if type(name) is not str or len(name.encode()) > _MAX_NAME_BYTES or name not in _names():
        return None
    resource = importlib.resources.files("tzdata.zoneinfo").joinpath(*name.split("/"))
    with resource.open("rb") as handle:
        return zoneinfo.ZoneInfo.from_file(handle, key=name)


class Fold:
    """The ``order`` fold rule's progress through one innermost array.

    A repeated local hour takes the earlier offset until the wall clock steps
    backwards inside it, then the later one. The state travels in the job's
    checkpoint, so a resumed job resolves the rest of an array as an uninterrupted
    one would.
    """

    __slots__ = ("array", "later", "wall")

    def __init__(self, state: Mapping[str, Any] | None = None) -> None:
        state = state or {}
        self.array = state.get("array")
        self.wall = dt.datetime.fromisoformat(state["wall"]) if state.get("wall") else None
        self.later = bool(state.get("later"))

    def state(self) -> dict[str, Any]:
        return {
            "array": self.array,
            "wall": None if self.wall is None else self.wall.isoformat(),
            "later": self.later,
        }

    def takes_later(self, array: Any, wall: dt.datetime) -> bool:
        if array != self.array:
            self.array, self.wall, self.later = array, None, False
        if self.wall is not None and wall < self.wall:
            self.later = True
        self.wall = wall
        return self.later


def resolve(
    wall: dt.datetime, where: zoneinfo.ZoneInfo, fold: str, order: Fold, array: Any
) -> dt.datetime | None:
    """The aware instant of naive ``wall`` in ``where``, or None inside a gap."""
    first, second = wall.replace(tzinfo=where, fold=0), wall.replace(tzinfo=where, fold=1)
    before, after = first.utcoffset(), second.utcoffset()
    if before == after:
        return first
    if before < after:
        return None  # PEP 495: a skipped wall time maps fold 0 to the earlier, smaller offset
    if fold == "later" or (fold == "order" and order.takes_later(array, wall)):
        return second
    return first
