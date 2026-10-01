"""Concurrent contexts: a turn that names two domains serves both.

Competing senses are two anchors the turn's SAME words reach. A turn that
spells two different domains by their own names, each in words the other does
not use, reached two topics, not one ambiguous sense. Invented names and
topics throughout; nothing here shares vocabulary with the benchmark corpus.

Pure logic over facts: no vault, no index.
"""

from __future__ import annotations

from test_working_set_resolve_senses import _resolve, _row

TRIP = _row("hubs/autumn-orchard-trip.md", "Autumn orchard trip", kind="hub")
COURSE = _row("hubs/pottery-course-schedule.md", "Pottery course schedule", kind="hub")
ORCHARD_TURN = "should I book the autumn orchard trip given the pottery course schedule?"


def test_two_same_kind_domains_named_apart_are_both_served() -> None:
    resolution = _resolve(ORCHARD_TURN, (TRIP, COURSE))

    assert resolution.status == "resolved", resolution.ambiguity
    assert {a.path for a in resolution.resolved_anchors} == {TRIP.path, COURSE.path}
    assert resolution.ambiguity == ()


def test_a_third_sharing_the_trips_spelled_words_keeps_competing_with_it() -> None:
    """Two hubs spelled with a shared word still compete; the pottery domain,
    spelled apart from both, is not dragged into the question."""

    other_trip = _row("hubs/autumn-orchard-harvest.md", "Autumn orchard harvest", kind="hub")
    resolution = _resolve(
        "should I book the autumn orchard trip, or the autumn orchard harvest, "
        "given the pottery course schedule?",
        (TRIP, other_trip, COURSE),
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {TRIP.path, other_trip.path}


def test_two_senses_reached_by_the_same_words_still_compete() -> None:
    """The negative control: one spelled name two hubs both carry is a sense
    question, exactly as before."""

    left = _row("hubs/tide-model-research.md", "Tide model research", kind="hub")
    right = _row("hubs/tide-model-rollout.md", "Tide model rollout", kind="hub")
    resolution = _resolve(
        "can you look into the tide model?", (left, right), retrieved=(left.path, right.path)
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {left.path, right.path}


def test_a_spelled_domain_still_competes_with_a_sense_only_reached_by_shared_words() -> None:
    """The conservative edge: apart means every member was spelled. A hub the
    turn only reached by shared words and retrieval has no spelling to set
    it apart, so the spelled one is still part of the question."""

    left = _row("hubs/tide-model-research.md", "Tide model research", kind="hub")
    right = _row("hubs/tide-model-rollout.md", "Tide model rollout", kind="hub")
    resolution = _resolve(
        "can you look into the tide model and the pottery course schedule?",
        (left, right, COURSE),
        retrieved=(left.path, right.path),
    )

    assert resolution.status == "ambiguous"
    assert COURSE.path in {item["ref"] for item in resolution.ambiguity}
