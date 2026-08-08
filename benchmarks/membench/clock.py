"""Fixed simulated timeline: a 12-week ingestion window with a pinned epoch.

Wall-clock time never enters generation; every timestamp derives from the
epoch so corpora are reproducible byte-for-byte.
"""

from __future__ import annotations

from datetime import date, timedelta

EPOCH = date(2025, 1, 6)  # Monday of week 0
WEEKS = 12


def week_date(week: int, day: int = 0) -> date:
    """Calendar date for ``day`` (0=Mon..6=Sun) of simulated ``week``."""

    if day < 0 or day > 6:
        raise ValueError(f"day must be 0..6, got {day}")
    return EPOCH + timedelta(weeks=week, days=day)


def week_iso(week: int, day: int = 0) -> str:
    return week_date(week, day).isoformat()


def end_of_window() -> date:
    """The knowledge horizon after the final ingestion week."""

    return week_date(WEEKS, 0)
