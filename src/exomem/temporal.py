"""Frontmatter knowledge time: two precisions, one order.

Notes record `created`/`updated` — and `log.md` history headings — at two
precisions, permanently:

- a bare calendar date (`2026-08-05`) denotes *an unknown instant within that
  day*. Every note written before second granularity landed reads this way, and
  hand-authored frontmatter still may.
- a second-granularity UTC timestamp (`2026-08-05T09:12:33Z`) denotes a
  specific instant. This is what the write tools emit going forward.

Old values are never rewritten. Restamping them as midnight would assert a
precision that was never captured, so the mixed vault is the permanent state
and **precision is part of the data**. Two consequences follow, and they are
the reason this module exists rather than a bare `date.fromisoformat` at each
call site:

1. Ordering is not total. Two values that share a day cannot be ordered when
   either lacks a time, so `compare` is four-valued and reports
   `INDETERMINATE` rather than collapsing an unknown into a guess. Callers that
   need a total order for display use `sort_key`, which is stable but does not
   pretend the ambiguity is resolved.
2. Precision is carried by the Python type, on both sides. PyYAML already hands
   back `datetime.date` for a bare date and `datetime.datetime` for a
   timestamp; the write tools' injectable `today`/`now` seam accepts either and
   emits at whatever precision it was given. A caller passing a `date` gets
   date-only output, which is what keeps the existing day-granular tests honest
   instead of merely passing.

Timezone handling: one standard on disk. Stamps are written in UTC with a `Z`
suffix, matching `adoption_run._now_iso` and `review_state`, so values are
comparable across machines without consulting a timezone database. The author's
local zone is deliberately not stored — that is a different fact from when the
edit happened. `render_date`, by contrast, reads the day off the value *as
given*, because note paths (`YYYY-MM-<slug>`) have always used the local day
and folding to UTC would silently move a late-evening note into the previous
month.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum


class Order(Enum):
    """Result of comparing two `Moment`s.

    `INDETERMINATE` is a real answer, not an error: the values genuinely do not
    determine an order, and reporting that is more useful than a coin flip.
    """

    BEFORE = "before"
    AFTER = "after"
    SAME = "same"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class Moment:
    """A point in knowledge time, at whichever precision was actually recorded.

    `instant` is `None` when only the day is known. It is never inferred.
    """

    day: dt.date
    instant: dt.datetime | None = None

    @property
    def precise(self) -> bool:
        """True when the exact instant is known, not just the calendar day."""
        return self.instant is not None


def now() -> dt.datetime:
    """The current instant, timezone-aware and truncated to the second.

    Local offset rather than UTC so `render_date` keeps yielding the local day
    that note paths have always used; `stamp` folds it to UTC for storage.
    """
    return dt.datetime.now().astimezone().replace(microsecond=0)


def stamp(value: dt.date) -> str:
    """Render a `date` or `datetime` in canonical frontmatter form.

    A `datetime` becomes second-granularity UTC (`2026-08-05T09:12:33Z`); a
    bare `date` stays bare. Naive datetimes are read as UTC — the codebase has
    no local-wall-clock storage to preserve.
    """
    if isinstance(value, dt.datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
        utc = aware.astimezone(dt.UTC).replace(microsecond=0)
        return utc.isoformat().replace("+00:00", "Z")
    return value.isoformat()


def render_date(value: dt.date) -> str:
    """The bare `YYYY-MM-DD` day, for note paths and draft tokens.

    Reads the day off `value` without changing zone, so a note written late
    locally stays in the month its author wrote it in. Draft tokens require
    exactly this form (`semantic_writes.DraftToken.decode`).
    """
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    return value.isoformat()


def parse(value: object) -> Moment | None:
    """Coerce a frontmatter value to a `Moment`, or `None` if unusable.

    Accepts every shape the vault can produce: the `date`/`datetime` objects
    PyYAML auto-types from unquoted values, and the string forms that reach us
    from quoted frontmatter, templates, and `str()` of a loaded datetime (which
    is space-separated, not ISO-`T`).
    """
    if isinstance(value, dt.datetime):
        return _from_datetime(value)
    if isinstance(value, dt.date):
        return Moment(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Date first: `date.fromisoformat` rejects anything carrying a time, so it
    # cannot silently turn a bare date into midnight the way `datetime` would.
    try:
        return Moment(dt.date.fromisoformat(text))
    except ValueError:
        pass
    try:
        return _from_datetime(dt.datetime.fromisoformat(text))
    except ValueError:
        return None


def _from_datetime(value: dt.datetime) -> Moment:
    """Normalize an instant to UTC at second granularity.

    Sub-second precision is dropped rather than preserved: milliseconds are
    false precision on hand-authored notes, and keeping them would let two
    values compare unequal on a difference neither the writer nor the reader
    can account for.
    """
    aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    utc = aware.astimezone(dt.UTC).replace(microsecond=0)
    return Moment(utc.date(), utc)


def compare(a: Moment, b: Moment) -> Order:
    """Order two moments, reporting `INDETERMINATE` where none exists.

    Distinct days always order — a whole day precedes the next whatever the
    unknown times within them. Within one day, only two known instants can be
    ordered; anything else is genuinely undetermined.
    """
    if a.instant is not None and b.instant is not None:
        if a.instant < b.instant:
            return Order.BEFORE
        if a.instant > b.instant:
            return Order.AFTER
        return Order.SAME
    if a.day < b.day:
        return Order.BEFORE
    if a.day > b.day:
        return Order.AFTER
    return Order.INDETERMINATE


_UNKNOWN_TIME = dt.datetime.min.replace(tzinfo=dt.UTC)


def sort_key(moment: Moment) -> tuple[dt.date, bool, dt.datetime]:
    """Stable total order for display, where `compare` may be indeterminate.

    Day-only values sort ahead of same-day timestamped ones. This is a
    presentation fallback, not a claim about what actually happened first —
    surface the ambiguity separately rather than letting the sort imply it away.
    """
    return (moment.day, moment.instant is not None, moment.instant or _UNKNOWN_TIME)
