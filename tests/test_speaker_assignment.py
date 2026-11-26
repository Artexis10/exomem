"""Unit tests for max-overlap segment→turn assignment (pure, torch-free)."""
from __future__ import annotations

from types import SimpleNamespace

from exomem.speaker_assignment import Turn, assign_span, assign_spans, label_transcript, overlap


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


def _segment(text, start, end, track=None):
    return SimpleNamespace(text=text, start=start, end=end, track=track)


def test_a_result_with_no_turns_and_no_tracks_renders_unlabelled_timed_lines():
    text, merged = label_transcript([_segment("one", 0.0, 2.0), _segment("two", 2.0, 4.0)], [], timed=True)

    assert text == "[0:00] one\n[0:02] two" and merged == []


def test_each_track_takes_its_own_turns_and_letters_and_names_its_lines_by_track():
    segments = [_segment("hello", 0.0, 2.0, "mic1"), _segment("hi", 0.0, 2.0, "mic2"), _segment("again", 3.0, 5.0, "mic1")]
    turns = [Turn(0.0, 5.0, "SPEAKER_00", "mic1"), Turn(0.0, 5.0, "SPEAKER_00", "mic2")]

    text, merged = label_transcript(segments, turns, timed=True)

    assert text.splitlines() == ["[0:00] [mic1: Speaker A]: hello", "[0:00] [mic2: Speaker A]: hi", "[0:03] [mic1: Speaker A]: again"]
    assert [item["speaker"] for item in merged] == ["mic1: Speaker A", "mic2: Speaker A", "mic1: Speaker A"]


def test_voices_past_the_twenty_sixth_continue_the_letters_in_one_closed_shape():
    segments = [_segment(f"line {n}", float(n), n + 1.0) for n in range(28)]
    turns = [Turn(float(n), n + 1.0, f"SPEAKER_{n:02d}") for n in range(28)]

    _text, merged = label_transcript(segments, turns, timed=True)

    labels = [item["speaker"] for item in merged]
    assert labels[24:] == ["Speaker Y", "Speaker Z", "Speaker AA", "Speaker AB"]
    assert len(set(labels)) == 28
