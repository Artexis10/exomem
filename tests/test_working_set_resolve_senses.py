"""Competing senses the turn's own words decide (close-memory-loop, activation quality).

Two resolver rules, exercised on invented names and topics that share nothing
with the context-activation benchmark corpus:

* A bare name that two or more distinct entities share is a question, not an
  unresolved turn. When nothing resolves and the only thing that reached two
  unlinked entities is one shared name word, and that word is spoken as a name
  (the entities are people, or a cased turn capitalises it somewhere other
  than a sentence start), the turn is `ambiguous` between them, and the
  retrieval carry is never asked to guess a page instead.
* A qualifier narrows competing senses. When two same-kind anchors resolve and
  the contiguous run of the turn that spells one's name words lies strictly
  inside the run that spells the other's, the turn's extra word chose the
  second; the narrower sense and any same-kind partial reached only inside that
  run are not listed. A word of the wider name said elsewhere in the turn
  narrows nothing.

Resolver facts, with one persisted-index check for an admitted alias.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from exomem import working_set_conversation as conversation_module
from exomem import working_set_resolve as resolve_module
from exomem.working_set_index import WorkingSetIndex


def _row(
    path: str,
    title: str,
    *,
    kind: str,
    terms: tuple[str, ...] = (),
    neighbourhood: tuple[str, ...] = (),
    entity_type: str = "",
) -> resolve_module.AnchorFacts:
    return resolve_module.AnchorFacts(
        anchor_id=path,
        path=path,
        ref=None,
        title=title,
        kind=kind,
        lifecycle="active",
        aliases=(),
        terms=terms or tuple(title.lower().split()),
        categories=(),
        neighbourhood=frozenset(neighbourhood),
        anchor_neighbourhood=frozenset(neighbourhood),
        entity_type=entity_type,
    )


def _resolve(
    turn: str,
    rows: tuple[resolve_module.AnchorFacts, ...],
    *,
    counts: dict[str, int] | None = None,
    retrieved: tuple[str, ...] = (),
) -> resolve_module.Resolution:
    analysis = resolve_module.analyze_turn(turn)
    candidates = resolve_module.candidates_for(
        analysis,
        rows,
        term_anchor_counts=counts or {},
        retrieval_paths=frozenset(retrieved),
    )
    candidates = resolve_module.add_graph_corroboration(candidates)
    return resolve_module.resolve(candidates, turn_tokens=analysis.tokens)


# --------------------------------------------------------------------------- #
# A bare shared name
# --------------------------------------------------------------------------- #

PRIYA_N = _row(
    "people/priya-nandakumar.md", "Priya Nandakumar", kind="entity", entity_type="person"
)
PRIYA_O = _row("people/priya-oduya.md", "Priya Oduya", kind="entity", entity_type="person")
NAME_COUNTS = {"priya": 2, "nandakumar": 1, "oduya": 1}


def test_a_bare_name_two_unlinked_entities_share_is_ambiguous_between_them() -> None:
    resolution = _resolve("Priya sent the invoice over.", (PRIYA_N, PRIYA_O), counts=NAME_COUNTS)

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {PRIYA_N.path, PRIYA_O.path}
    assert {anchor.status for anchor in resolution.anchors} == {"partial"}


def test_a_bare_name_one_entity_holds_stays_a_partial_lead() -> None:
    resolution = _resolve("Priya sent the invoice over.", (PRIYA_N,), counts={"priya": 1})

    assert resolution.status == "unresolved"
    assert resolution.ambiguity == ()
    assert [anchor.status for anchor in resolution.anchors] == ["partial"]


def test_a_full_name_still_resolves_the_one_person() -> None:
    resolution = _resolve(
        "Priya Oduya sent the invoice over.", (PRIYA_N, PRIYA_O), counts=NAME_COUNTS
    )

    assert resolution.status == "resolved"
    assert [a.path for a in resolution.resolved_anchors] == [PRIYA_O.path]


def test_a_shared_word_in_two_non_entity_names_is_not_a_bare_name() -> None:
    """Hubs, resources and collections are named by ordinary nouns. A word two
    hub titles share is not a person's name said on its own."""

    rows = (
        _row("hubs/orchard-pruning.md", "Orchard pruning hub", kind="hub"),
        _row("hubs/orchard-irrigation.md", "Orchard irrigation hub", kind="hub"),
    )
    resolution = _resolve("the orchard looked dry today", rows, counts={"orchard": 2})

    assert resolution.status == "unresolved"
    assert resolution.ambiguity == ()


def test_two_linked_entities_sharing_a_name_are_not_competing() -> None:
    """The same connectivity rule every competing group uses: two people who
    link each other are one neighbourhood, not two senses."""

    linked_n = _row(
        PRIYA_N.path,
        PRIYA_N.title,
        kind="entity",
        neighbourhood=(PRIYA_O.path,),
        entity_type="person",
    )
    resolution = _resolve("Priya sent the invoice over.", (linked_n, PRIYA_O), counts=NAME_COUNTS)

    assert resolution.status == "unresolved"
    assert resolution.ambiguity == ()


def test_a_bare_name_beside_a_resolved_anchor_does_not_abstain_the_turn() -> None:
    ledger = _row("systems/kiln-ledger.md", "Kiln ledger", kind="resource")
    resolution = _resolve(
        "Priya updated the kiln ledger.",
        (PRIYA_N, PRIYA_O, ledger),
        counts={**NAME_COUNTS, "kiln": 1, "ledger": 1},
        retrieved=(ledger.path,),
    )

    assert resolution.status == "resolved"
    assert [a.path for a in resolution.resolved_anchors] == [ledger.path]


def test_a_lower_case_first_name_two_people_share_still_asks() -> None:
    resolution = _resolve("priya sent the invoice over.", (PRIYA_N, PRIYA_O), counts=NAME_COUNTS)

    assert resolution.status == "ambiguous"


BAKERY = _row("orgs/harbour-bakery.md", "Harbour Bakery", kind="entity", entity_type="organization")
CLINIC = _row("orgs/harbour-clinic.md", "Harbour Clinic", kind="entity", entity_type="organization")
HARBOUR_COUNTS = {"harbour": 2, "bakery": 1, "clinic": 1}


def test_an_ordinary_noun_two_business_names_share_is_not_a_bare_name() -> None:
    """Reviewer probe: a harbour busy with ferries is not a question about two
    businesses that happen to be named after it."""

    resolution = _resolve(
        "the harbour was busy this morning, ferries everywhere",
        (BAKERY, CLINIC),
        counts=HARBOUR_COUNTS,
    )

    assert resolution.status == "unresolved"
    assert resolution.ambiguity == ()


def test_a_capital_only_at_the_sentence_start_is_not_a_name() -> None:
    resolution = _resolve(
        "Harbour traffic was heavy again. Ferries everywhere.",
        (BAKERY, CLINIC),
        counts=HARBOUR_COUNTS,
    )

    assert resolution.status == "unresolved"


def test_a_capitalised_shared_name_mid_sentence_asks_between_businesses() -> None:
    resolution = _resolve(
        "We ordered the rolls from Harbour again.", (BAKERY, CLINIC), counts=HARBOUR_COUNTS
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {BAKERY.path, CLINIC.path}


YAMADA_T = _row("people/yamada-taro.md", "山田 太郎", kind="entity", entity_type="person")
YAMADA_H = _row("people/yamada-hanako.md", "山田 花子", kind="entity", entity_type="person")
YAMADA_ORG_A = _row("orgs/yamada-a.md", "山田 商店", kind="entity", entity_type="organization")
YAMADA_ORG_B = _row("orgs/yamada-b.md", "山田 工務店", kind="entity", entity_type="organization")
YAMADA_COUNTS = {"山田": 2, "太郎": 1, "花子": 1, "商店": 1, "工務店": 1}


def test_an_uncased_shared_name_asks_between_people() -> None:
    resolution = _resolve("山田 から 連絡 が ありました", (YAMADA_T, YAMADA_H), counts=YAMADA_COUNTS)

    assert resolution.status == "ambiguous"


def test_an_uncased_shared_word_never_asks_between_non_people() -> None:
    resolution = _resolve(
        "山田 から 連絡 が ありました", (YAMADA_ORG_A, YAMADA_ORG_B), counts=YAMADA_COUNTS
    )

    assert resolution.status == "unresolved"


# --------------------------------------------------------------------------- #
# A qualifier narrows competing senses
# --------------------------------------------------------------------------- #

ROLLOUT = _row("hubs/tide-model-rollout.md", "Tide model rollout hub", kind="hub")
RESEARCH = _row("hubs/tide-model-research.md", "Tide model research hub", kind="hub")
REGISTRY = _row("hubs/model-registry.md", "Model registry hub", kind="hub")
HUB_COUNTS = {"tide": 2, "model": 3, "rollout": 1, "research": 1, "registry": 1, "hub": 3}
HUBS = (ROLLOUT, RESEARCH, REGISTRY)
BOTH_RETRIEVED = (ROLLOUT.path, RESEARCH.path)


def test_the_bare_shared_name_of_two_hubs_stays_ambiguous_with_every_sense_listed() -> None:
    resolution = _resolve(
        "Could you check the tide model for me?", HUBS, counts=HUB_COUNTS, retrieved=BOTH_RETRIEVED
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {ROLLOUT.path, RESEARCH.path}
    # The menu keeps the weaker sense the turn's words also reached.
    assert REGISTRY.path in {anchor.path for anchor in resolution.anchors}


def test_a_qualifier_that_names_one_sense_resolves_it_and_drops_the_others() -> None:
    resolution = _resolve(
        "Could you check the tide model rollout for me?",
        HUBS,
        counts=HUB_COUNTS,
        retrieved=BOTH_RETRIEVED,
    )

    assert resolution.status == "resolved"
    assert [anchor.path for anchor in resolution.anchors] == [ROLLOUT.path]


def test_two_qualifiers_naming_both_senses_stay_ambiguous() -> None:
    resolution = _resolve(
        "Compare the tide model rollout with the tide model research.",
        HUBS,
        counts=HUB_COUNTS,
        retrieved=BOTH_RETRIEVED,
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {ROLLOUT.path, RESEARCH.path}


def test_a_qualifier_never_narrows_across_kinds() -> None:
    """A resource and a hub the turn reached are complementary, not senses of
    one another, however their name words nest."""

    board = _row("systems/tide-model-board.md", "Tide model board", kind="resource")
    resolution = _resolve(
        "Could you check the tide model rollout board for me?",
        (ROLLOUT, board),
        counts={**HUB_COUNTS, "board": 1},
        retrieved=(ROLLOUT.path, board.path),
    )

    assert resolution.status == "resolved"
    assert {anchor.path for anchor in resolution.resolved_anchors} == {ROLLOUT.path, board.path}


KITCHEN = _row("hubs/kitchen-renovation.md", "Kitchen renovation hub", kind="hub")
KITCHEN_BUDGET = _row(
    "hubs/kitchen-renovation-budget.md", "Kitchen renovation budget hub", kind="hub"
)
KITCHEN_COUNTS = {"kitchen": 2, "renovation": 2, "budget": 1, "hub": 2}


def test_a_word_of_the_wider_name_said_elsewhere_narrows_nothing() -> None:
    """Reviewer probe: "budget" belongs to the groceries, not to the kitchen."""

    resolution = _resolve(
        "I blew my grocery budget this week, and the kitchen renovation is stalled again",
        (KITCHEN, KITCHEN_BUDGET),
        counts=KITCHEN_COUNTS,
        retrieved=(KITCHEN.path, KITCHEN_BUDGET.path),
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {KITCHEN.path, KITCHEN_BUDGET.path}


def test_the_qualifier_inside_the_name_still_narrows() -> None:
    resolution = _resolve(
        "Where does the kitchen renovation budget stand?",
        (KITCHEN, KITCHEN_BUDGET),
        counts=KITCHEN_COUNTS,
        retrieved=(KITCHEN.path, KITCHEN_BUDGET.path),
    )

    assert resolution.status == "resolved"
    assert [anchor.path for anchor in resolution.anchors] == [KITCHEN_BUDGET.path]


SOLAR = _row("hubs/solar-array.md", "Solar array hub", kind="hub")
SOLAR_MONITORING = _row("hubs/solar-array-monitoring.md", "Solar array monitoring hub", kind="hub")


def test_a_turn_naming_both_hubs_keeps_both() -> None:
    """Reviewer probe: the monitoring question and the array question are two."""

    resolution = _resolve(
        "Is the monitoring wired up yet, and did the solar array pass inspection?",
        (SOLAR, SOLAR_MONITORING),
        counts={"solar": 2, "array": 2, "monitoring": 1, "hub": 2},
        retrieved=(SOLAR.path, SOLAR_MONITORING.path),
    )

    assert {anchor.path for anchor in resolution.anchors} == {SOLAR.path, SOLAR_MONITORING.path}
    assert resolution.status == "ambiguous"


SOLAR_COUNTS = {"solar": 2, "array": 2, "monitoring": 1, "hub": 2}
SOLAR_RETRIEVED = (SOLAR.path, SOLAR_MONITORING.path)


@pytest.mark.parametrize(
    "turn",
    [
        "Check the solar array, monitoring can wait.",
        "The solar array. Monitoring is next week.",
        "The solar array: monitoring is next week.",
        "The solar array - monitoring is next week.",
        "The solar array — monitoring is next week.",
        "The solar array and monitoring are both late.",
        "The solar array or monitoring, whichever is first.",
        "Check the solar array (monitoring can wait).",
        "Check the solar array [monitoring can wait].",
    ],
)
def test_clause_punctuation_and_coordinators_end_a_contiguous_run(turn: str) -> None:
    """Recheck probes: a comma, a full stop, a colon, a dash or "and"/"or" between
    the shared words and the qualifier makes two things, not one name."""

    resolution = _resolve(turn, (SOLAR, SOLAR_MONITORING), counts=SOLAR_COUNTS, retrieved=SOLAR_RETRIEVED)

    assert resolution.status == "ambiguous"
    assert {anchor.path for anchor in resolution.anchors} == {SOLAR.path, SOLAR_MONITORING.path}


@pytest.mark.parametrize(
    "turn",
    [
        "The kitchen renovation? Budget talk can wait.",
        "Update the kitchen renovation, budget is fine.",
        "the kitchen renovation and the budget for it",
    ],
)
def test_kitchen_punctuation_and_coordinator_controls_keep_both_hubs(turn: str) -> None:
    resolution = _resolve(
        turn, (KITCHEN, KITCHEN_BUDGET), counts=KITCHEN_COUNTS, retrieved=(KITCHEN.path, KITCHEN_BUDGET.path)
    )

    assert resolution.status == "ambiguous"
    assert {anchor.path for anchor in resolution.anchors} == {KITCHEN.path, KITCHEN_BUDGET.path}


def test_an_unpunctuated_run_with_a_hyphenated_word_still_narrows() -> None:
    resolution = _resolve(
        "Where does the kitchen-renovation budget stand?",
        (KITCHEN, KITCHEN_BUDGET),
        counts=KITCHEN_COUNTS,
        retrieved=(KITCHEN.path, KITCHEN_BUDGET.path),
    )

    assert resolution.status == "resolved"


@pytest.mark.parametrize("separator", [" and ", ", "])
def test_a_complete_authored_name_keeps_its_internal_separator(separator: str) -> None:
    """An authored compound name is one mention, not two competing hubs."""
    title = f"Tide model{separator}weather hub"
    compound = _row("hubs/tide-model-weather.md", title, kind="hub")
    resolution = _resolve(
        f"Could you check the {title} for me?",
        (ROLLOUT, compound),
        counts={"tide": 2, "model": 2, "rollout": 1, "weather": 1, "hub": 2},
        retrieved=(ROLLOUT.path, compound.path),
    )

    assert resolution.status == "resolved"
    assert [anchor.path for anchor in resolution.resolved_anchors] == [compound.path]


@pytest.mark.parametrize("separator", [" and ", ", "])
def test_a_compound_name_does_not_consume_a_later_separate_mention(separator: str) -> None:
    compound = _row("hubs/tide-model-weather.md", f"Tide model{separator}weather hub", kind="hub")
    resolution = _resolve(
        f"Check the {compound.title}. Separately check the tide model.",
        (ROLLOUT, compound),
        counts={"tide": 2, "model": 2, "rollout": 1, "weather": 1, "hub": 2},
        retrieved=(ROLLOUT.path, compound.path),
    )

    assert resolution.status == "ambiguous"
    assert {anchor.path for anchor in resolution.anchors} == {ROLLOUT.path, compound.path}


def test_a_generic_word_after_a_compound_name_is_not_a_separate_name() -> None:
    compound = _row("hubs/tide-model-weather.md", "Tide model and weather hub", kind="hub")
    resolution = _resolve(
        "Check the tide model and weather hub. That hub is ready.",
        (ROLLOUT, compound),
        counts={"tide": 2, "model": 2, "rollout": 1, "weather": 1, "hub": 2},
        retrieved=(ROLLOUT.path, compound.path),
    )

    assert resolution.status == "resolved"
    assert [anchor.path for anchor in resolution.resolved_anchors] == [compound.path]


def test_repeating_a_compound_name_does_not_name_its_weaker_competitor() -> None:
    compound = _row("hubs/tide-model-weather.md", "Tide model and weather hub", kind="hub")
    resolution = _resolve(
        "Check the tide model and weather hub. Again, check the tide model and weather hub.",
        (ROLLOUT, compound),
        counts={"tide": 2, "model": 2, "rollout": 1, "weather": 1, "hub": 2},
        retrieved=(ROLLOUT.path, compound.path),
    )

    assert resolution.status == "resolved"
    assert [anchor.path for anchor in resolution.resolved_anchors] == [compound.path]


@pytest.mark.parametrize("focus", [
    "Check the copper crane harbour ship.",
    "Check copper crane harbour ship.",
])
def test_turn_name_narrowing_preserves_a_disjoint_focus_only_lead(focus: str) -> None:
    """Segment-local coordinates cannot make an unrelated focus lead a free rider."""
    compound = _row("hubs/tide-model-weather.md", "Tide model and weather hub", kind="hub")
    other = _row("hubs/copper-crane.md", "Copper crane harbour ship archive", kind="hub")
    rows = (ROLLOUT, compound, other)
    counts = {"tide": 2, "model": 2, "rollout": 1, "weather": 1, "hub": 2}
    analysis = resolve_module.analyze_turn("Check the tide model and weather hub.")
    conversation = conversation_module.bound({"focus": focus})
    segments = conversation_module.analyze(conversation)
    candidates, origins, _ = conversation_module.apply(
        resolve_module.candidates_for(
            analysis, rows, term_anchor_counts=counts,
            retrieval_paths=frozenset({ROLLOUT.path, compound.path}),
        ),
        segments, conversation, rows=rows, term_anchor_counts=counts,
    )
    resolution = resolve_module.resolve(candidates, turn_tokens=segments.turn_tokens(analysis))

    assert resolution.status == "resolved"
    assert [anchor.path for anchor in resolution.resolved_anchors] == [compound.path]
    leads = {anchor.path: anchor for anchor in resolution.partial_anchors}
    assert leads[other.path].status == "partial"
    assert origins[other.path] == "focus"


def test_an_authored_alias_survives_its_derived_spelling_in_the_persisted_index(
    tmp_path: Path,
) -> None:
    hub_dir = tmp_path / "Knowledge Base" / "Notes" / "Insights"
    hub_dir.mkdir(parents=True)
    for stem, title, alias in (
        ("harbour-workstream", "Harbour and wind (workstream)", "harbour and wind"),
        ("harbour-research", "Harbour wind research", ""),
    ):
        (hub_dir / f"{stem}.md").write_text(
            f"---\ntype: insight\nstatus: active\ntags: [hub]\naliases: [{alias}]\n---\n\n# {title}\n",
            encoding="utf-8",
        )
    index = WorkingSetIndex(tmp_path)
    index.rebuild()
    rows = resolve_module.facts_from_rows(index.anchors())
    resolution = _resolve(
        "Could you check harbour and wind please?",
        rows,
        counts=index.term_anchor_counts(),
        retrieved=tuple(row.path for row in rows),
    )

    assert resolution.status == "resolved"
    assert [anchor.title for anchor in resolution.resolved_anchors] == [
        "Harbour and wind (workstream)"
    ]


def test_an_authored_comma_is_not_a_sentence_boundary() -> None:
    compound = _row("hubs/tide-model-weather.md", "Tide model, weather hub", kind="hub")
    resolution = _resolve(
        "Could you check the tide model. Weather hub can wait.",
        (ROLLOUT, compound),
        counts={"tide": 2, "model": 2, "rollout": 1, "weather": 1, "hub": 2},
        retrieved=(ROLLOUT.path, compound.path),
    )

    assert resolution.status == "ambiguous"


def test_a_complete_authored_alias_keeps_its_coordinator() -> None:
    compound = replace(
        _row(
            "hubs/tide-weather.md",
            "Estuary weather review",
            kind="hub",
            terms=("estuary", "weather", "review", "tide", "model", "hub"),
        ),
        aliases=("tide model and weather hub",),
    )
    resolution = _resolve(
        "Could you check the tide model and weather hub for me?",
        (ROLLOUT, compound),
        counts={"tide": 2, "model": 2, "rollout": 1, "weather": 1, "hub": 2},
        retrieved=(ROLLOUT.path, compound.path),
    )

    assert resolution.status == "resolved"
    assert [anchor.path for anchor in resolution.resolved_anchors] == [compound.path]


MARK_E = _row("people/mark-ellison.md", "Mark Ellison", kind="entity", entity_type="person")
MARK_F = _row("people/mark-fenwick.md", "Mark Fenwick", kind="entity", entity_type="person")
MARK_COUNTS = {"mark": 2, "ellison": 1, "fenwick": 1}


@pytest.mark.parametrize(
    "turn",
    [
        "Please mark the task done.",
        "I left a pencil mark on the draft plan.",
    ],
)
def test_a_lower_case_person_word_in_a_cased_turn_is_not_a_name(turn: str) -> None:
    """Ruling: once the turn's casing says something, a lower-case word is a word."""

    resolution = _resolve(turn, (MARK_E, MARK_F), counts=MARK_COUNTS)

    assert resolution.status != "ambiguous"
    assert resolution.ambiguity == ()


@pytest.mark.parametrize(
    "turn",
    [
        "Mark called about the invoice.",
        "Please ask Mark about the invoice.",
        "mark sent the invoice over.",
    ],
)
def test_a_capitalised_or_all_lower_case_person_word_still_asks(turn: str) -> None:
    resolution = _resolve(turn, (MARK_E, MARK_F), counts=MARK_COUNTS)

    assert resolution.status == "ambiguous"


@pytest.mark.parametrize(
    "turn",
    [
        "Notes From The Harbour Walk",
        "WE ORDERED THE ROLLS FROM HARBOUR AGAIN",
    ],
)
def test_a_headline_or_all_caps_turn_carries_no_casing_signal(turn: str) -> None:
    resolution = _resolve(turn, (BAKERY, CLINIC), counts=HARBOUR_COUNTS)

    assert resolution.status == "unresolved"
    assert resolution.ambiguity == ()


def test_a_headline_turn_still_asks_between_people() -> None:
    resolution = _resolve("Notes From Mark About The Invoice", (MARK_E, MARK_F), counts=MARK_COUNTS)

    assert resolution.status == "ambiguous"


def test_an_uncased_person_name_in_a_mixed_script_turn_still_asks() -> None:
    resolution = _resolve(
        "Please tell 山田 about the invoice.", (YAMADA_T, YAMADA_H), counts=YAMADA_COUNTS
    )

    assert resolution.status == "ambiguous"


def test_a_slash_does_not_end_a_run() -> None:
    """A slash is left alone: "kitchen/renovation" stays one run of name words."""

    resolution = _resolve(
        "Where does the kitchen/renovation budget stand?",
        (KITCHEN, KITCHEN_BUDGET),
        counts=KITCHEN_COUNTS,
        retrieved=(KITCHEN.path, KITCHEN_BUDGET.path),
    )

    assert resolution.status == "resolved"


# --------------------------------------------------------------------------- #
# A CJK word glued to a Latin one is still a word of the run
# --------------------------------------------------------------------------- #
#
# `name_contact` counts the words a token holds without a space around them
# (`analysis.words`), so the name spans that decide narrowing have to see them
# too. Otherwise "rolloutの計画" leaves the rollout hub's run at "tide model",
# strictly inside the other hub's "alpha tide model", and narrowing drops the
# hub the turn named.

TIDE_ROLLOUT = _row("hubs/tide-model-rollout.md", "Tide model rollout", kind="hub")
ALPHA_TIDE = _row("hubs/alpha-tide-model.md", "Alpha tide model", kind="hub")
TIDE_HUBS = (TIDE_ROLLOUT, ALPHA_TIDE)
TIDE_COUNTS = {"tide": 2, "model": 2, "rollout": 1, "alpha": 1}
TIDE_RETRIEVED = (TIDE_ROLLOUT.path, ALPHA_TIDE.path)


def _tide(turn: str):
    return _resolve(turn, TIDE_HUBS, counts=TIDE_COUNTS, retrieved=TIDE_RETRIEVED)


def test_a_latin_twin_of_two_overlapping_hub_names_stays_ambiguous() -> None:
    resolution = _tide("check alpha tide model rollout please")

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {
        TIDE_ROLLOUT.path,
        ALPHA_TIDE.path,
    }


def test_a_cjk_word_glued_to_the_last_latin_word_does_not_narrow_the_turn() -> None:
    resolution = _tide("check alpha tide model rolloutの計画")

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {
        TIDE_ROLLOUT.path,
        ALPHA_TIDE.path,
    }


def test_the_qualifier_still_narrows_when_no_word_is_glued() -> None:
    resolution = _tide("Could you check the tide model rollout for me?")

    assert resolution.status == "resolved"
    assert [anchor.path for anchor in resolution.anchors] == [TIDE_ROLLOUT.path]
