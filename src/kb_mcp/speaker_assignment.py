"""Max-overlap assignment of transcript segments to diarization turns.

Each transcript unit (a Whisper segment) is assigned to the turn it overlaps most; ties go to
the earliest turn for determinism. Replaces naive midpoint assignment, which loses short turns
and mis-attributes boundary-straddling units.

Pure / torch-free (stdlib only) so it unit-tests without a GPU. Ported from a production
`speaker_assignment` module; consumed by `extract._diarize` to map ASR segments → resolved
speaker labels.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    label: str  # diarization cluster id or resolved speaker label


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Length of the temporal intersection of [a0,a1] and [b0,b1] (0 if disjoint)."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def assign_span(start: float, end: float, turns: Sequence[Turn]) -> Optional[str]:
    """Label of the turn with the greatest overlap with [start, end]; None if none overlaps.

    Ties (equal overlap) are broken toward the earliest-starting turn for determinism.
    """
    best_key: Optional[tuple[float, float]] = None
    best_label: Optional[str] = None
    for t in turns:
        ov = overlap(start, end, t.start, t.end)
        if ov <= 0.0:
            continue
        key = (ov, -t.start)  # maximize overlap, then prefer the earliest-starting turn
        if best_key is None or key > best_key:
            best_key = key
            best_label = t.label
    return best_label


def assign_spans(
    spans: Sequence[tuple[float, float]], turns: Sequence[Turn]
) -> list[Optional[str]]:
    """Assign a label to each (start, end) span by max overlap."""
    return [assign_span(s, e, turns) for (s, e) in spans]
