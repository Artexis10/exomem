"""The days on which a served unit's currency changes, for a cache that keeps the unit until then.

Stance moves onto this after the domain-profiles change merges; until then it keeps its own copy of the rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any


def boundaries(provenance: Mapping[str, Any], *, today: date) -> list[date]:
    """The days after `today` on which the authored `until` or `check_by` of a unit changes its currency."""
    found: list[date] = []
    for field in ("until", "check_by"):  # Closed typed date fields; each changes currency the following day.
        if value := provenance.get(field):
            authored = date.fromisoformat(value)
            # The last day of the calendar has no following day, so that date never ends.
            if authored < date.max:
                boundary = authored + timedelta(days=1)
                if boundary > today:
                    found.append(boundary)
    return found
