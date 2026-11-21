"""Concurrent contexts: a turn that names two domains serves both.

Competing senses are two anchors the turn's SAME words reach. A turn that
spells two different domains by their own names, each in words the other does
not use, reached two topics, not one ambiguous sense. Invented names and
topics throughout; nothing here shares vocabulary with the benchmark corpus.

Pure logic over facts: no vault, no index.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_working_set_resolve import _facts
from test_working_set_resolve_senses import _resolve, _row

from exomem import working_set_conversation as conversation_module
from exomem import working_set_resolve as resolve_module

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


def test_a_spelled_domain_carries_the_packet_past_unnamed_senses() -> None:
    """Residual words can retain leads without blocking the named domain."""

    left = _row("hubs/tide-model-research.md", "Tide model research", kind="hub")
    right = _row("hubs/tide-model-rollout.md", "Tide model rollout", kind="hub")
    resolution = _resolve(
        "can you look into the tide model and the pottery course schedule?",
        (left, right, COURSE),
        retrieved=(left.path, right.path),
    )

    assert resolution.status == "resolved"
    assert {anchor.path for anchor in resolution.resolved_anchors} == {COURSE.path}
    assert {anchor.path for anchor in resolution.anchors if anchor.status == "partial"} == {
        left.path, right.path,
    }


@pytest.mark.parametrize(
    ("turn", "contact"),
    [
        ("Will Maple Assembly's workshop tickets cover venue hire?", "lexical_overlap"),
        ("What is Maple Assembly's workshop sequence?", "rare_term"),
    ],
)
def test_a_current_name_demotes_an_unrelated_retrieved_subject(turn: str, contact: str) -> None:
    named = _row("entities/assembly.md", "Maple Assembly", kind="entity")
    weak = _row("entities/venue.md", "Workshop venue decision", kind="entity")

    analysis = resolve_module.analyze_turn(turn)
    candidates = resolve_module.candidates_for(
        analysis, (named, weak), retrieval_paths=frozenset({weak.path}),
        term_anchor_counts={"workshop": 1},
        retrieval_without=lambda pages, _excluded_terms: pages,
    )
    assert {contact, "retrieval"} <= next(item.evidence for item in candidates if item.path == weak.path)

    resolution = resolve_module.resolve(candidates, turn_tokens=analysis.tokens)

    assert resolution.status == "resolved", resolution.ambiguity
    assert {anchor.path for anchor in resolution.resolved_anchors} == {named.path}
    lead = next(anchor for anchor in resolution.anchors if anchor.path == weak.path)
    assert lead.status == "partial"
    assert {contact, "retrieval"} <= set(lead.evidence)


def test_a_weak_interpretation_of_the_spoken_name_still_competes() -> None:
    named = _row("entities/assembly.md", "Maple Assembly", kind="entity")
    interpretation = _row("entities/other.md", "Assembly workshop decision", kind="entity")

    resolution = _resolve(
        "Review Maple Assembly's workshop", (named, interpretation),
        retrieved=(interpretation.path,),
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {named.path, interpretation.path}


@pytest.mark.parametrize("connection", ["direct", "shared", "same_page"])
def test_a_named_subject_keeps_weak_structural_complements(connection: str) -> None:
    named = _facts("named.md", evidence=("exact_alias",), exact_alias_phrases=("silver beacon",))
    complement = _facts("related.md", evidence=("lexical_overlap", "retrieval"))
    if connection == "direct":
        named = replace(named, anchor_neighbourhood=frozenset({complement.path}))
    elif connection == "shared":
        named = replace(named, anchor_neighbourhood=frozenset({"shared.md"}))
        complement = replace(complement, anchor_neighbourhood=frozenset({"shared.md"}))
    else:
        complement = replace(complement, path=named.path)
    unrelated = replace(
        _facts("unrelated.md", evidence=("rare_term", "retrieval"), name_contact=("workshop",)),
        name_span=(2, 3),
    )

    resolution = resolve_module.resolve(
        (named, complement, unrelated), turn_tokens=("silver", "beacon", "workshop")
    )

    assert resolution.status == "resolved", resolution.ambiguity
    assert {anchor.anchor_id for anchor in resolution.resolved_anchors} == {
        named.anchor_id, complement.anchor_id,
    }
    assert next(anchor for anchor in resolution.anchors if anchor.anchor_id == unrelated.anchor_id).status == "partial"


def test_a_same_name_ambiguity_survives_beside_a_separately_named_topic() -> None:
    left = _row("resources/beacon-a.md", "Silver Beacon", kind="resource")
    right = _row("resources/beacon-b.md", "Silver Beacon", kind="resource")
    other = _row("resources/ledger.md", "Copper Ledger", kind="resource")

    resolution = _resolve("Compare Silver Beacon with Copper Ledger", (left, right, other))

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {left.path, right.path}


def test_possessive_spellings_of_the_same_turn_name_keep_competing() -> None:
    left = _facts("silver-a.md", evidence=("exact_alias",), exact_alias_phrases=("silver",))
    right = _facts("silver-b.md", evidence=("exact_alias",), exact_alias_phrases=("silver's",))

    resolution = resolve_module.resolve((left, right), turn_tokens=("silver's",))

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {left.path, right.path}


def test_cross_topic_links_cannot_hide_two_separate_name_ambiguities() -> None:
    left = _row("resources/beacon-a.md", "Silver Beacon", kind="resource", neighbourhood=("resources/ledger-a.md",))
    right = _row("resources/beacon-b.md", "Silver Beacon", kind="resource", neighbourhood=("resources/ledger-b.md",))
    other_left = _row("resources/ledger-a.md", "Copper Ledger", kind="resource", neighbourhood=(right.path,))
    other_right = _row("resources/ledger-b.md", "Copper Ledger", kind="resource")

    resolution = _resolve(
        "Compare Silver Beacon with Copper Ledger", (left, right, other_left, other_right)
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {
        left.path, right.path, other_left.path, other_right.path,
    }


@pytest.mark.parametrize("same_words", [False, True])
def test_an_unnamed_transitive_bridge_cannot_hide_a_same_name_ambiguity(same_words: bool) -> None:
    left = _facts("beacon-a.md", evidence=("exact_alias",), exact_alias_phrases=("silver beacon",), neighbourhood=("venue.md",))
    right = _facts("beacon-b.md", evidence=("exact_alias",), exact_alias_phrases=("silver beacon",))
    bridge = _facts("venue.md", evidence=("lexical_overlap", "retrieval"), neighbourhood=(right.path,))
    bridge = replace(bridge, name_span=(2, 3), name_contact=frozenset({"workshop"}))
    if same_words:
        bridge = replace(bridge, name_span=(0, 2), name_contact=frozenset({"silver", "beacon"}))

    resolution = resolve_module.resolve(
        (left, right, bridge), turn_tokens=("silver", "beacon", "workshop"),
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {
        left.path, right.path, *([bridge.path] if same_words else []),
    }


@pytest.mark.parametrize(
    ("tokens", "spans", "contact", "ambiguous"),
    [
        (("silver", "beacon", "workshop", "silver", "beacon"), ((2, 3),), ("workshop",), False),
        (("silver", "beacon", "silver", "workshop"), ((2, 4),), ("silver", "workshop"), False),
        (("silver", "beacon", "workshop", "silver"), ((1, 3), (3, 4)), ("beacon", "workshop", "silver"), True),
        (("silver", "beacon", "workshop"), None, (), True),
    ],
    ids=("gap-between-names", "same-word-outside", "one-overlap", "unknown-contact"),
)
def test_competition_uses_name_occurrences_and_preserves_unknown_contact(
    tokens: tuple[str, ...], spans: tuple[tuple[int, int], ...] | None,
    contact: tuple[str, ...], ambiguous: bool,
) -> None:
    named = _facts("named.md", evidence=("exact_alias",), exact_alias_phrases=("silver beacon",))
    weak = replace(
        _facts("weak.md", evidence=("lexical_overlap", "retrieval"), name_contact=contact),
        name_spans=spans,
    )

    resolution = resolve_module.resolve((named, weak), turn_tokens=tokens)

    assert resolution.status == ("ambiguous" if ambiguous else "resolved")
    assert next(anchor for anchor in resolution.anchors if anchor.anchor_id == weak.anchor_id).status == (
        "resolved" if ambiguous else "partial"
    )


def test_an_unspoken_focus_alias_cannot_join_separately_named_topics() -> None:
    left = replace(_row("resources/beacon.md", "Silver Beacon", kind="resource"), aliases=("ledger clerk",))
    right = _row("resources/ledger.md", "Copper Ledger", kind="resource")
    analysis = resolve_module.analyze_turn("Compare Silver Beacon with Copper Ledger")
    conversation = conversation_module.bound({"focus": "Ledger Clerk"})
    candidates, _origins, _entries = conversation_module.apply(
        resolve_module.candidates_for(analysis, (left, right)),
        conversation_module.analyze(conversation), conversation, rows=(left, right),
    )
    assert next(item for item in candidates if item.anchor_id == left.anchor_id).exact_alias_phrases == frozenset({"silver beacon", "ledger clerk"})

    resolution = resolve_module.resolve(candidates, turn_tokens=analysis.tokens)

    assert resolution.status == "resolved", resolution.ambiguity
    assert {anchor.path for anchor in resolution.resolved_anchors} == {left.path, right.path}


@pytest.mark.parametrize("origin", ["missing_tokens", "focus_segment", "focus_alias", "unwitnessed"])
def test_without_a_current_turn_name_the_existing_competition_remains(origin: str) -> None:
    named = _facts("named.md", evidence=("exact_alias",), exact_alias_phrases=("silver beacon",))
    tokens = ("silver", "beacon")
    if origin == "focus_segment":
        named = replace(named, name_span_segment="focus")
    elif origin == "focus_alias":
        named = replace(named, exact_alias_from_focus=True)
    elif origin == "missing_tokens":
        tokens = ()
    else:
        tokens = ("review", "workshop")
    weak = _facts("weak.md", evidence=("lexical_overlap", "retrieval"))

    resolution = resolve_module.resolve((named, weak), turn_tokens=tokens)

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {named.path, weak.path}


def test_an_agent_choice_keeps_its_existing_resolution_and_competition() -> None:
    row = _row("resources/chosen.md", "Chosen page", kind="resource")
    choice = resolve_module.override_candidates((row,), row.path)
    assert resolve_module.resolve(choice, turn_tokens=("chosen", "page")).status == "resolved"
    named = _facts("named.md", evidence=("exact_alias",), exact_alias_phrases=("silver beacon",))

    resolution = resolve_module.resolve((*choice, named), turn_tokens=("silver", "beacon"))

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {row.path, named.path}
