"""Unit tests for max-overlap segment→turn assignment (pure, torch-free)."""
from __future__ import annotations

from kb_mcp.speaker_assignment import Turn, assign_span, assign_spans, overlap


def test_overlap_basic_and_disjoint():
    assert overlap(0.0, 10.0, 5.0, 15.0) == 5.0
    assert overlap(0.0, 5.0, 10.0, 15.0) == 0.0  # disjoint
    assert overlap(0.0, 5.0, 5.0, 10.0) == 0.0  # touching, not overlapping


def test_assign_span_picks_max_overlap():
    turns = [Turn(0.0, 4.0, "A"), Turn(3.0, 10.0, "B")]
    # [2,6] overlaps A by 2.0 and B by 3.0 → B
    assert assign_span(2.0, 6.0, turns) == "B"
    # [0,3.5] overlaps A by 3.5, B by 0.5 → A
    assert assign_span(0.0, 3.5, turns) == "A"


def test_assign_span_no_overlap_returns_none():
    turns = [Turn(0.0, 1.0, "A")]
    assert assign_span(5.0, 6.0, turns) is None
    assert assign_span(0.0, 1.0, []) is None


def test_assign_span_tie_breaks_to_earliest_turn():
    # Equal overlap (1.0 each) → earliest-starting turn wins.
    turns = [Turn(0.0, 2.0, "EARLY"), Turn(2.0, 5.0, "LATE")]
    # [1,3]: overlaps EARLY by 1.0 (1→2) and LATE by 1.0 (2→3) → EARLY
    assert assign_span(1.0, 3.0, turns) == "EARLY"


def test_assign_spans_maps_each():
    turns = [Turn(0.0, 5.0, "A"), Turn(5.0, 10.0, "B")]
    out = assign_spans([(1.0, 2.0), (6.0, 7.0), (20.0, 21.0)], turns)
    assert out == ["A", "B", None]
