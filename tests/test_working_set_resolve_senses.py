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

Pure logic over facts: no vault, no index.
"""

from __future__ import annotations

import pytest

from exomem import working_set_resolve as resolve_module


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
