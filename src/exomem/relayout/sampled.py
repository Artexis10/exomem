"""A list that a report shows in part by default and in full on request, always saying how much it shows."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: How many entries a default report shows of a long list. `--full` shows all of them.
SAMPLE_LIMIT = 20


@dataclass(frozen=True, slots=True)
class Sampled:
    """A list of JSON-ready entries. `listing(full)` is the form a report prints."""

    items: Sequence[object]

    def listing(self, *, full: bool) -> dict[str, object]:
        shown = list(self.items if full else self.items[:SAMPLE_LIMIT])
        return {"showing": len(shown), "of": len(self.items), "items": shown}
