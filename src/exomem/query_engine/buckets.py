"""Source-local calendar buckets and exact reductions shared by rollups and base scans.

OpenSpec add-collection-query-engine §3 and §13. A row's day is the local day
its source recorded: an instant plus that record's own UTC offset (a declared
offset field when present, otherwise the instant's recorded offset), or a
date-only value as given. A row with neither is flagged, never bucketed.

Sums are exact and order-independent: integers in arbitrary precision and
floats as integer multiples of 2**-1074, rounded once when read. Extremes
break ties on item key, so the reduced value never depends on visit order.
Recency is the basis instant in UTC microseconds (a date's ordinal day for a
date basis), so ``latest`` is the value recorded last, whatever its offset.
"""

from __future__ import annotations

import datetime as dt
import re

from .scalars import ScalarValueError, parse_instant, scalar_key

# nosemgrep: ep-word-set -- The rollup grammar fixes these aggregate operators.
REDUCTIONS = ("count", "sum", "avg", "min", "max", "latest")
# nosemgrep: ep-word-set -- Operators whose result is one of the field's own values.
EXTREMES = frozenset({"min", "max", "latest"})
NUMERIC = frozenset({"integer", "number"})
#: Every finite binary64 value is an integer multiple of 2**-1074.
_SCALE = 1074
_OFFSET = re.compile(r"[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]")
_OFFSETS: dict[str, int] = {}
#: Offset shifts, which repeat across the rows of a base scan; keyed by valid offsets only,
#: so at most 2,879 entries.
_SHIFTS: dict[int, dt.timedelta] = {}
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_MICROSECOND = dt.timedelta(microseconds=1)
_ENUM_KINDS = {bool: "boolean", int: "number", float: "number", str: "string"}


def declared_kind(kind: str, enum=()) -> str:
    """The order an enum's values share (boolean, number or string), else the declared type."""
    if kind != "enum":
        return kind
    kinds = {_ENUM_KINDS.get(type(value)) for value in enum}
    return kinds.pop() if len(kinds) == 1 and None not in kinds else "enum"


def instant_order(instant: dt.datetime) -> int:
    """Microseconds since the Unix epoch of an aware instant, exactly."""
    return (instant - _EPOCH) // _MICROSECOND


def offset_minutes(value: object) -> int | None:
    """Minutes east of UTC for a ``±HH:MM`` source offset, else None.

    ``-00:00`` is RFC 3339's "local offset unknown", so it supplies none.
    """
    if type(value) is not str or value == "-00:00":
        return None
    minutes = _OFFSETS.get(value)
    if minutes is None and _OFFSET.fullmatch(value):
        minutes = (int(value[1:3]) * 60 + int(value[4:6])) * (-1 if value[0] == "-" else 1)
        if len(_OFFSETS) < 4096:
            _OFFSETS[value] = minutes
    return minutes


def bucket_key(day: dt.date, bucket: str) -> str:
    """The bucket's first local date: the day, its ISO-week Monday or the month's first day."""
    if bucket == "week":
        day -= dt.timedelta(days=day.weekday())
    elif bucket == "month":
        day = day.replace(day=1)
    return day.isoformat()


class Basis:
    """How a row's source-local day and recency order are read from its values."""

    __slots__ = ("field", "kind", "offset")

    def __init__(self, field: str, kind: str, offset: str | None = None) -> None:
        if kind not in {"instant", "date"} or (kind == "date" and offset is not None):
            raise ValueError("a day basis is an instant with an optional offset field, or a date")
        self.field, self.kind, self.offset = field, kind, offset

    def __eq__(self, other):
        return isinstance(other, Basis) and self.describe() == other.describe()

    def __hash__(self):
        return hash((self.field, self.kind, self.offset))

    def describe(self) -> dict:
        return {"field": self.field, "kind": self.kind, "offset": self.offset}

    def locate(self, values, *, ordered: bool = True) -> tuple[dt.date, int | None] | None:
        """The source-local day and an integer recency key, or None for a flagged time basis.

        A valid value in the declared offset field is the record's supplied UTC
        offset. Without one, only an explicit numeric offset in the instant's
        own text is supplied: where the basis declares an offset field, a ``Z``
        instant is UTC awaiting that field, not a local day. Unzoned or absent
        instants, invalid offsets and ``-00:00`` are flagged, never guessed.
        With ``ordered=False`` the recency key is None, for a reduction without ``latest``.
        """
        raw = values.get(self.field)
        if type(raw) is not str:
            return None
        if self.kind == "date":
            if len(raw) != 10:
                return None
            try:
                day = dt.date.fromisoformat(raw)
            except ValueError:
                return None
            return day, (day.toordinal() if ordered else None)
        try:
            instant = parse_instant(raw)
        except ScalarValueError:
            return None
        own = 0 if raw[-1] in "Zz" else offset_minutes(raw[-6:])
        if self.offset is not None and values.get(self.offset) is not None:
            minutes = offset_minutes(values[self.offset])
        elif raw[-1] in "Zz":
            minutes = None if self.offset is not None else 0
        else:
            minutes = own
        if minutes is None:
            return None
        order = instant_order(instant) if ordered else None
        if minutes == own:
            # The instant's own offset: its local day is the date written in its text.
            return dt.date.fromisoformat(raw[:10]), order
        shift = _SHIFTS.get(minutes)
        if shift is None:
            shift = _SHIFTS[minutes] = dt.timedelta(minutes=minutes)
        return (instant + shift).date(), order


def group_key(values, names) -> tuple:
    """Tagged group values that keep a present null apart from a missing field."""
    return tuple(("m",) if name not in values else ("n",) if values[name] is None else ("v", values[name])
                 for name in names)


def rank(value, kind: str):
    """A value's position in its declared order, or None where it has none."""
    if kind in NUMERIC:
        return value if type(value) is int or type(value) is float else None
    if kind == "datetime":
        try:
            return instant_order(parse_instant(value)) if type(value) is str else None
        except ScalarValueError:
            return None
    # nosemgrep: ep-word-membership -- SCALAR_TYPES kinds whose canonical encoding is a string.
    if kind in {"string", "link", "date"}:
        return value if type(value) is str else None
    if kind == "enum":
        # Mixed enum domains order by their typed comparison key, kept as one string.
        try:
            tag, payload = scalar_key(value, "enum")
        except ScalarValueError:
            return None
        return f"{tag}:{payload}" if type(value) is not bool else None
    return None


def _scaled(value: float) -> int:
    numerator, denominator = value.as_integer_ratio()
    return numerator << (_SCALE + 1 - denominator.bit_length())


class Accumulator:
    """Exact reduction state of one value field within one bucket."""

    __slots__ = ("count", "numbers", "ints", "scaled", "floats", "low", "high", "last")

    def __init__(self, state=None) -> None:
        if state is None:
            self.count = self.numbers = self.ints = self.scaled = self.floats = 0
            self.low = self.high = self.last = None
        else:
            (self.count, self.numbers, self.ints, self.scaled, self.floats,
             self.low, self.high, self.last) = (*state[:5], *(None if item is None else tuple(item)
                                                             for item in state[5:]))

    def state(self) -> list:
        return [self.count, self.numbers, self.ints, self.scaled, self.floats,
                None if self.low is None else list(self.low), None if self.high is None else list(self.high),
                None if self.last is None else list(self.last)]

    def add(self, value, kind: str, key: str, order: int | None, extremes: bool) -> int:
        """Fold one present value in; returns the change in retained extreme bytes."""
        if value is None:
            return 0
        self.count += 1
        kind_of = type(value)
        if kind_of is int:
            self.numbers += 1
            self.ints += value
        elif kind_of is float:
            self.numbers += 1
            self.floats += 1
            self.scaled += _scaled(value)
        return self.extreme(value, kind, key, order) if extremes else 0

    def extreme(self, value, kind: str, key: str, order: int | None) -> int:
        """Consider one present value for min/max/latest only; returns the retained-byte change.

        Without a recency ``order`` (no time basis) only min and max are kept.
        """
        change = 0
        position = rank(value, kind)
        if position is not None:
            if self.low is None or (position, key) < self.low[:2]:
                change += _size(value) - (0 if self.low is None else _size(self.low[2]))
                self.low = (position, key, value)
            if self.high is None or (position, key) > self.high[:2]:
                change += _size(value) - (0 if self.high is None else _size(self.high[2]))
                self.high = (position, key, value)
        if order is not None and (self.last is None or (order, key) > self.last[:2]):
            change += _size(value) - (0 if self.last is None else _size(self.last[2]))
            self.last = (order, key, value)
        return change

    def remove(self, value, kind: str, key: str, order: int) -> bool:
        """Take one present value out; True when a held extreme left and must be recomputed."""
        if value is None:
            return False
        self.count -= 1
        kind_of = type(value)
        if kind_of is int:
            self.numbers -= 1
            self.ints -= value
        elif kind_of is float:
            self.numbers -= 1
            self.floats -= 1
            self.scaled -= _scaled(value)
        position = rank(value, kind)
        stale = position is not None and any(
            held is not None and held[:2] == (position, key) for held in (self.low, self.high))
        return stale or (self.last is not None and self.last[:2] == (order, key))

    def clear_extremes(self) -> None:
        self.low = self.high = self.last = None

    def result(self, op: str):
        if op == "count":
            return self.count
        if op in {"sum", "avg"}:
            if not self.numbers:
                return None
            if op == "sum" and not self.floats:
                return self.ints
            total = (self.ints << _SCALE) + self.scaled
            return total / ((self.numbers if op == "avg" else 1) << _SCALE)
        held = {"min": self.low, "max": self.high, "latest": self.last}[op]
        return None if held is None else held[2]


def _size(value) -> int:
    return len(value.encode()) if type(value) is str else 16
