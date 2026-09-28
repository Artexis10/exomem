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


def test_the_same_pair_is_served_when_only_one_is_spelled_apart_from_a_third_sense() -> None:
    """A third same-kind sense that shares the trip's spelled words still
    competes with it; the pottery domain, named apart from both, is not
    dragged into the question."""

    other_trip = _row("hubs/autumn-orchard-harvest.md", "Autumn orchard harvest", kind="hub")
    resolution = _resolve(
        "should I book the autumn orchard visit given the pottery course schedule?",
        (TRIP, other_trip, COURSE),
    )

    if resolution.status == "ambiguous":
        assert {item["ref"] for item in resolution.ambiguity} <= {TRIP.path, other_trip.path}


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


def test_a_domain_named_apart_is_not_pulled_into_anothers_sense_question() -> None:
    """The pottery domain is spelled and shares no word with the two tide
    hubs, so it is never listed as a third sense of them. Under the existing
    named-anchor rule it carries the packet and the weak tide pair is demoted
    to a partial menu."""

    left = _row("hubs/tide-model-research.md", "Tide model research", kind="hub")
    right = _row("hubs/tide-model-rollout.md", "Tide model rollout", kind="hub")
    resolution = _resolve(
        "can you look into the tide model and the pottery course schedule?",
        (left, right, COURSE),
        retrieved=(left.path, right.path),
    )

    assert COURSE.path not in {item["ref"] for item in resolution.ambiguity}
    assert [a.path for a in resolution.resolved_anchors] == [COURSE.path]
