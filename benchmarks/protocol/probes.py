"""Known-answer probe definitions; providers execute these elsewhere."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable, Mapping
from typing import Literal


@dataclass(frozen=True)
class ProbeSpec:
    kind: Literal["lexical-rare-token", "semantic-zero-overlap", "update-current-state"]
    prompt: str
    allowed_outcomes: tuple[str, ...]


def known_answer_probe_specs() -> tuple[ProbeSpec, ...]:
    outcomes = ("pass", "fail", "inconclusive-by-design")
    return (
        ProbeSpec("lexical-rare-token", "Retrieve the unique rare token exactly.", outcomes),
        ProbeSpec("semantic-zero-overlap", "Retrieve the meaning-preserving zero-overlap paraphrase.", outcomes),
        ProbeSpec("update-current-state", "Return only the current revision after an update.", outcomes),
    )


def classify_update_outcome(hits: Iterable[object]) -> Literal["superseded", "both_returned", "stale_only", "unresolvable"]:
    labels: set[str] = set()
    for hit in hits:
        if isinstance(hit, Mapping):
            label = hit.get("state") or hit.get("revision") or hit.get("kind") or ""
        else:
            label = str(hit)
        folded = str(label).casefold()
        if "current" in folded or "new" in folded:
            labels.add("current")
        if "stale" in folded or "old" in folded or "superseded" in folded:
            labels.add("stale")
    if labels == {"current"}:
        return "superseded"
    if labels == {"current", "stale"}:
        return "both_returned"
    if labels == {"stale"}:
        return "stale_only"
    return "unresolvable"
