"""Zone rules and local wall-clock resolution for import time bases (OpenSpec bring-in-large-exports §3).

Zone rules come from the pinned ``tzdata`` package, never the host's zone database,
so every platform resolves the same local time to the same instant; a job binding
records :func:`version`. A local time in a repeated hour resolves by the mapping's
declared fold rule, and one in a gap resolves to nothing: the importer refuses it.
"""

from __future__ import annotations

import datetime as dt
import functools
import importlib.resources
import re
import zoneinfo
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

_MAX_NAME_BYTES = 64

# nosemgrep: ep-word-set -- The time-basis grammar fixes these source-path keys.
BASIS_PATHS = ("date", "instant", "offset", "offset_minutes", "seconds", "milliseconds", "duration", "index")
# nosemgrep: ep-word-set -- The time-mapping grammar fixes these output targets, each a declared collection field.
TIME_TARGETS = ("instant", "offset", "local_date", "local_datetime")

# nosemgrep: ep-word-set -- ISO 8601 fixes these designators; a year or month is absent because its length depends on the calendar.
_UNIT_MICROSECONDS = {
    "W": 7 * 86_400_000_000,
    "D": 86_400_000_000,
    "H": 3_600_000_000,
    "M": 60_000_000,
    "S": 1_000_000,
}
# A regex fits here: it checks the shape of a field over a closed ISO 8601 profile, not what the field means.
_DURATION = re.compile(
    r"-?P(?:(?P<W>[0-9]+(?:[.,][0-9]+)?)W)?(?:(?P<D>[0-9]+(?:[.,][0-9]+)?)D)?"
    r"(?:T(?:(?P<H>[0-9]+(?:[.,][0-9]+)?)H)?(?:(?P<M>[0-9]+(?:[.,][0-9]+)?)M)?(?:(?P<S>[0-9]+(?:[.,][0-9]+)?)S)?)?"
)


def basis_paths(basis: Mapping[str, Any]) -> list[Any]:
    """The source paths a declared time basis reads, including interval paths."""
    every = basis.get("every")
    intervals = every.values() if isinstance(every, Mapping) else ()
    return [basis[key] for key in BASIS_PATHS if key in basis] + [
        value for value in intervals if type(value) is str
    ]


def duration(text: Any) -> dt.timedelta | None:
    """The ISO 8601 duration ``text`` as a timedelta, or None when it is outside the profile.

    The profile is an optional ``-``, ``P``, weeks and days, then ``T`` with hours, minutes and
    seconds. A fraction may follow the last unit only. Years and months are outside it.
    """
    found = _DURATION.fullmatch(text) if type(text) is str else None
    if found is None or text.endswith("T"):
        return None
    parts = [(unit, found[unit]) for unit in _UNIT_MICROSECONDS if found[unit] is not None]
    if not parts or any(not number.isdigit() for _, number in parts[:-1]):
        return None
    total = sum(Decimal(number.replace(",", ".")) * _UNIT_MICROSECONDS[unit] for unit, number in parts)
    try:
        return dt.timedelta(microseconds=-round(total) if text.startswith("-") else round(total))
    except OverflowError:
        return None


def version() -> str:
    """The IANA release of the pinned zone rules; loaded on use, so other routes never need them."""
    import tzdata

    return tzdata.IANA_VERSION


@functools.cache
def _names() -> frozenset[str]:
    # The package ships its own list of the zone names it holds.
    listing = importlib.resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8")
    return frozenset(listing.split())


def zone(name: Any) -> zoneinfo.ZoneInfo | None:
    """The named zone from the pinned rules, or None when they hold no such zone."""
    if type(name) is not str or len(name.encode()) > _MAX_NAME_BYTES or name not in _names():
        return None
    return _load(name)  # checked first: the cache hashes its argument, and a mapping may hold anything


@functools.lru_cache(maxsize=32)
def _load(name: str) -> zoneinfo.ZoneInfo:
    resource = importlib.resources.files("tzdata.zoneinfo").joinpath(*name.split("/"))
    with resource.open("rb") as handle:
        return zoneinfo.ZoneInfo.from_file(handle, key=name)


class Fold:
    """The ``order`` fold rule's progress through one innermost array.

    A repeated local hour takes the earlier offset until the wall clock steps
    backwards inside it, then the later one. A wall time outside a repeated hour
    starts the rule over, so the next repeated hour of the same array, a later
    year's or another series', begins on the earlier offset again. The state
    travels in the job's checkpoint, so a resumed job resolves the rest of an array
    as an uninterrupted one would.
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

    def takes_later(self, array: Any, wall: dt.datetime, repeated: bool) -> bool:
        """Whether ``wall`` takes the later offset; ``repeated`` says it falls in a repeated hour."""
        if array != self.array or not repeated:
            self.array, self.wall, self.later = array, None, False
        if not repeated:
            return False
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
    later = fold == "later" or (fold == "order" and order.takes_later(array, wall, before > after))
    if before == after:
        return first
    if before < after:
        return None  # PEP 495: a skipped wall time maps fold 0 to the earlier, smaller offset
    return second if later else first
