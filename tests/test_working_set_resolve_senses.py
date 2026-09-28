"""Competing senses the turn's own words decide (close-memory-loop, activation quality).

Two resolver rules, exercised on invented names and topics that share nothing
with the context-activation benchmark corpus:

* A bare name that two or more distinct entities share is a question, not an
  unresolved turn. When nothing resolves and the only thing that reached two
  unlinked entities is one shared name word, the turn is `ambiguous` between
  them, and the retrieval carry is never asked to guess a page instead.
* A qualifier narrows competing senses. When two same-kind anchors resolve and
  the name words the turn reached on one are a strict subset of those it
  reached on the other, the turn's extra word chose the second; the narrower
  sense and any same-kind partial reached only by words of the chosen name are
  not listed.

Pure logic over facts: no vault, no index.
"""

from __future__ import annotations

from exomem import working_set_resolve as resolve_module


def _row(
    path: str,
    title: str,
    *,
    kind: str,
    terms: tuple[str, ...] = (),
    neighbourhood: tuple[str, ...] = (),
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

PRIYA_N = _row("people/priya-nandakumar.md", "Priya Nandakumar", kind="entity")
PRIYA_O = _row("people/priya-oduya.md", "Priya Oduya", kind="entity")
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

    linked_n = _row(PRIYA_N.path, PRIYA_N.title, kind="entity", neighbourhood=(PRIYA_O.path,))
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
