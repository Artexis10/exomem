"""Task 2.1 — categorical anchor evidence and the referents resolution rule.

Everything here is pure logic over facts: no vault, no sqlite, no model. The
point of the module under test is that a turn either reaches an anchor on
categorical evidence or the operation abstains, and that no float ever leaves
it — the constitution forbids a confidence number on a note, and a packet that
carried one would be exactly that.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from exomem import working_set_resolve as resolve_module


def _facts(
    anchor_id: str,
    *,
    kind: str = "resource",
    title: str | None = None,
    evidence: tuple[str, ...] = (),
    neighbourhood: tuple[str, ...] = (),
    anchor_neighbourhood: tuple[str, ...] | None = None,
    exact_alias_phrases: tuple[str, ...] = (),
) -> resolve_module.CandidateFacts:
    """One candidate. Listed neighbours are anchors unless stated otherwise.

    `anchor_neighbourhood` defaults to the whole neighbourhood because that is
    the common case in the vault, so a test that wants a neighbour which is NOT
    an anchor — a boilerplate page two hubs both link — says so explicitly.
    """
    return resolve_module.CandidateFacts(
        anchor_id=anchor_id,
        path=anchor_id,
        ref=None,
        title=title or anchor_id,
        kind=kind,
        lifecycle="active",
        categories=(),
        neighbourhood=frozenset(neighbourhood),
        anchor_neighbourhood=frozenset(
            neighbourhood if anchor_neighbourhood is None else anchor_neighbourhood
        ),
        evidence=frozenset(evidence),
        exact_alias_phrases=frozenset(exact_alias_phrases),
    )


def _row(
    path: str,
    title: str,
    *,
    kind: str = "hub",
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
        terms=terms,
        categories=(),
        neighbourhood=frozenset(neighbourhood),
        anchor_neighbourhood=frozenset(neighbourhood),
    )


def _floats(value: object) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, float):
        return [value]
    if isinstance(value, dict):
        return [f for item in value.values() for f in _floats(item)]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [f for item in value for f in _floats(item)]
    return []


# --------------------------------------------------------------------------- #
# Turn analysis
# --------------------------------------------------------------------------- #


def test_turn_analysis_is_nfkc_casefolded_with_ngrams() -> None:
    analysis = resolve_module.analyze_turn("I'm planning to tow the Ｃargo Sled north")

    assert "cargo" in analysis.tokens
    assert "sled" in analysis.tokens
    # NFKC folds the fullwidth Ｃ; casefold lowers it.
    assert "cargo sled" in analysis.ngrams
    assert analysis.text == analysis.text.casefold()


def test_planning_cues_are_detected_deterministically() -> None:
    first = resolve_module.analyze_turn("I'm planning to cook this")
    second = resolve_module.analyze_turn("I'm planning to cook this")

    assert first.cues == second.cues
    assert "planning" in first.cues


# --------------------------------------------------------------------------- #
# Evidence kinds per source
# --------------------------------------------------------------------------- #


def test_exact_alias_comes_from_a_title_or_alias_hit() -> None:
    rows = (
        resolve_module.AnchorFacts(
            anchor_id="Products/Cargo Sled.md",
            path="Products/Cargo Sled.md",
            ref=None,
            title="Cargo Sled",
            kind="resource",
            lifecycle="active",
            aliases=("sled",),
            terms=("cargo", "sled", "tow"),
            categories=("constraint",),
            neighbourhood=frozenset(),
        ),
    )
    analysis = resolve_module.analyze_turn("can the cargo sled take this load")
    candidates = resolve_module.candidates_for(analysis, rows)

    assert len(candidates) == 1
    assert "exact_alias" in candidates[0].evidence


def test_lexical_overlap_needs_the_pinned_term_band() -> None:
    from exomem.ranking_config import DEFAULT_RANKING

    rows = (
        resolve_module.AnchorFacts(
            anchor_id="a",
            path="a",
            ref=None,
            title="Northern freight corridor planning",
            kind="hub",
            lifecycle="active",
            aliases=(),
            terms=("northern", "freight", "corridor", "planning"),
            categories=(),
            neighbourhood=frozenset(),
        ),
        resolve_module.AnchorFacts(
            anchor_id="b",
            path="b",
            ref=None,
            title="Kitchen inventory",
            kind="hub",
            lifecycle="active",
            aliases=(),
            terms=("kitchen", "inventory"),
            categories=(),
            neighbourhood=frozenset(),
        ),
    )
    analysis = resolve_module.analyze_turn("the freight corridor is jammed")
    by_id = {c.anchor_id: c for c in resolve_module.candidates_for(analysis, rows)}

    assert DEFAULT_RANKING.working_set_lexical_min_terms == 2
    assert "lexical_overlap" in by_id["a"].evidence
    assert "b" not in by_id


def test_claims_match_delegates_to_collection_claims_route(monkeypatch) -> None:
    from exomem import collection_claims

    calls: list[tuple] = []
    real_route = collection_claims.route

    def spy(terms, targets):
        calls.append((tuple(terms), tuple(targets)))
        return real_route(terms, targets)

    monkeypatch.setattr(collection_claims, "route", spy)

    rows = (
        resolve_module.AnchorFacts(
            anchor_id="Knowledge Base/Records/Depot Stock/_collection.md",
            path="Knowledge Base/Records/Depot Stock/_collection.md",
            ref=None,
            title="Depot stock",
            kind="collection",
            lifecycle="active",
            aliases=(),
            terms=("depot", "stock", "sled"),
            categories=("fact",),
            neighbourhood=frozenset(),
        ),
    )
    target = collection_claims.RoutingTarget(
        collection="Knowledge Base/Records/Depot Stock/_collection.md",
        title="Depot stock",
        claims=frozenset({"depot", "stock", "sled"}),
        natural_key=("observed_on", "asset"),
    )
    analysis = resolve_module.analyze_turn("how much depot stock is left for the sled")
    candidates = resolve_module.candidates_for(analysis, rows, routing_targets=(target,))

    assert calls, "claims_match must be computed by collection_claims.route, not re-implemented"
    assert "claims_match" in candidates[0].evidence


def test_retrieval_evidence_comes_from_the_anchors_own_page_only() -> None:
    """A recall hit near an anchor is not contact (canonical spec scenario).

    The neighbourhood clause is gone: `retrieval` is granted only when the
    anchor's OWN page is among recall's hits. On a dense vault a hub links
    dozens of pages, and hybrid recall returns hits for every turn, so the
    removed clause reached every hub for every turn — the mechanism behind
    the real-vault false activation this change fixes.
    """
    rows = (
        resolve_module.AnchorFacts(
            anchor_id="hub",
            path="hub.md",
            ref=None,
            title="Corridor",
            kind="hub",
            lifecycle="active",
            aliases=(),
            terms=("corridor",),
            categories=(),
            neighbourhood=frozenset({"note.md"}),
        ),
    )
    analysis = resolve_module.analyze_turn("something else entirely")
    direct = resolve_module.candidates_for(analysis, rows, retrieval_paths=frozenset({"hub.md"}))
    via_link = resolve_module.candidates_for(
        analysis, rows, retrieval_paths=frozenset({"note.md"})
    )

    assert "retrieval" in direct[0].evidence
    # The hub is not a candidate at all on account of a neighbour's hit: it
    # carries no contact kind, so it never enters the candidate list.
    assert via_link == ()


def test_a_project_key_anchor_never_carries_retrieval() -> None:
    """MINOR 12 (review round 3): a project-key anchor comes from a project
    key rather than from a page, so `row.path` is `""`. `row.path and
    row.path in retrieval_paths` is already false whenever `row.path` is
    falsy, so this was already correct -- this test only pins it down.
    Even the degenerate case where `retrieval_paths` itself holds the empty
    string must not grant `retrieval`.
    """
    rows = (
        resolve_module.AnchorFacts(
            anchor_id="proj:orchard",
            path="",
            ref=None,
            title="Orchard",
            kind="project",
            lifecycle="active",
            aliases=(),
            terms=("orchard",),
            categories=(),
            neighbourhood=frozenset(),
        ),
    )
    analysis = resolve_module.analyze_turn("orchard project status")
    candidates = resolve_module.candidates_for(analysis, rows, retrieval_paths=frozenset({""}))

    assert len(candidates) == 1
    assert "retrieval" not in candidates[0].evidence


def test_category_match_comes_from_turn_cues() -> None:
    rows = (
        resolve_module.AnchorFacts(
            anchor_id="sled",
            path="sled.md",
            ref=None,
            title="Cargo Sled",
            kind="resource",
            lifecycle="active",
            aliases=(),
            terms=("cargo", "sled"),
            categories=("constraint",),
            neighbourhood=frozenset(),
        ),
    )
    analysis = resolve_module.analyze_turn("what are the constraints on the cargo sled")
    candidates = resolve_module.candidates_for(analysis, rows)

    assert "category_match" in candidates[0].evidence


def test_vector_band_is_absent_without_vectors() -> None:
    rows = (
        resolve_module.AnchorFacts(
            anchor_id="sled",
            path="sled.md",
            ref=None,
            title="Cargo Sled",
            kind="resource",
            lifecycle="active",
            aliases=(),
            terms=("cargo", "sled"),
            categories=(),
            neighbourhood=frozenset(),
        ),
    )
    analysis = resolve_module.analyze_turn("cargo sled")
    candidates = resolve_module.candidates_for(analysis, rows)

    assert "vector_band" not in candidates[0].evidence


def test_graph_corroboration_counts_an_edge_between_two_candidates() -> None:
    """Counted even when both candidates already appear in ordinary recall.

    The find lane discards corroboration for pages already in the primary set;
    the compiler deliberately does not inherit that discard.
    """
    left = _facts("left.md", evidence=("lexical_overlap",), neighbourhood=("right.md",))
    right = _facts("right.md", evidence=("lexical_overlap",), neighbourhood=("left.md",))

    corroborated = resolve_module.add_graph_corroboration(
        (left, right), retrieval_paths=frozenset({"left.md", "right.md"})
    )

    assert all("graph_corroboration" in item.evidence for item in corroborated)


def test_corroboration_needs_an_independently_reached_partner() -> None:
    """Two candidates admitted through retrieved contact only never corroborate.

    They are linked by construction whenever they share a neighbourhood a
    hybrid recall run would surface, so a link between them restates the same
    ranking-engine fact rather than adding a second one.
    """
    left = _facts("left.md", evidence=("retrieval",), neighbourhood=("right.md",))
    right = _facts("right.md", evidence=("vector_band",), neighbourhood=("left.md",))

    corroborated = resolve_module.add_graph_corroboration((left, right))

    assert all("graph_corroboration" not in item.evidence for item in corroborated)


def test_corroboration_from_a_worded_partner_is_granted_but_does_not_alone_resolve() -> None:
    """A retrieval-only candidate linked to a worded partner gets the qualifier,
    but two non-worded kinds still never resolve it (canonical spec: "Retrieved
    evidence alone never resolves").
    """
    worded = _facts("worded.md", evidence=("lexical_overlap",), neighbourhood=("retrieved.md",))
    retrieved_only = _facts(
        "retrieved.md", evidence=("retrieval", "vector_band"), neighbourhood=("worded.md",)
    )

    corroborated = resolve_module.add_graph_corroboration((worded, retrieved_only))
    by_id = {item.anchor_id: item for item in corroborated}

    assert "graph_corroboration" in by_id["retrieved.md"].evidence
    resolution = resolve_module.resolve(corroborated)
    retrieved_anchor = next(a for a in resolution.anchors if a.anchor_id == "retrieved.md")
    assert retrieved_anchor.status == "partial"


def test_retrieved_evidence_alone_never_resolves_however_many_kinds_stack() -> None:
    """Canonical spec scenario "Retrieved evidence alone never resolves": own
    page a recall hit, vector band, typed-linked (so `graph_corroboration` from
    a worded partner), and a matching turn cue -- still `partial`.
    """
    resolution = resolve_module.resolve(
        (
            _facts(
                "a",
                evidence=("retrieval", "vector_band", "category_match", "graph_corroboration"),
            ),
        )
    )

    assert resolution.anchors[0].status == "partial"
    assert resolution.status == "unresolved"


# --------------------------------------------------------------------------- #
# rare_term and folded lexical comparison (task 3)
# --------------------------------------------------------------------------- #


def _term_row(
    path: str,
    title: str,
    *,
    terms: tuple[str, ...],
    aliases: tuple[str, ...] = (),
) -> resolve_module.AnchorFacts:
    return resolve_module.AnchorFacts(
        anchor_id=path,
        path=path,
        ref=None,
        title=title,
        kind="resource",
        lifecycle="active",
        aliases=aliases,
        terms=terms,
        categories=("constraint",),
        neighbourhood=frozenset(),
    )


def test_a_rare_shared_term_is_a_weak_worded_contact() -> None:
    row = _term_row("bench.md", "Workshop bench", terms=("workshop", "bench"))
    analysis = resolve_module.analyze_turn("is the bench free")
    candidates = resolve_module.candidates_for(
        analysis, (row,), term_anchor_counts={"bench": 1}
    )

    assert len(candidates) == 1
    assert candidates[0].evidence == frozenset({"rare_term"})


def test_a_common_shared_term_is_neither_rare_term_nor_lexical_overlap() -> None:
    row = _term_row("bench.md", "Workshop bench", terms=("workshop", "bench"))
    analysis = resolve_module.analyze_turn("is the bench free")
    candidates = resolve_module.candidates_for(
        analysis, (row,), term_anchor_counts={"bench": 4}
    )

    assert candidates == ()


def test_a_two_letter_shared_term_never_earns_rare_term() -> None:
    """D4: a term shorter than three characters is never a lead.

    "so should i go with the first option" is ordinary English, not a
    reference to a page whose title happens to contain the word "Go" — but
    "go" is no stopword, it names few anchors in a small catalogue, and it
    was therefore a perfectly rare single shared name term. Length is the
    only thing that separates that accident from a real short name, so a
    two-character term earns no contact kind at all and the anchor is not a
    candidate.
    """
    row = _term_row("release-go-checklist.md", "Release Go Checklist", terms=("release", "go"))
    analysis = resolve_module.analyze_turn("so should i go with the first option")
    candidates = resolve_module.candidates_for(
        analysis, (row,), term_anchor_counts={"go": 1}
    )

    assert candidates == ()


def test_a_three_letter_shared_term_still_earns_rare_term() -> None:
    """The floor is exactly three characters: a genuinely short NAME still leads."""
    row = _term_row("hob-service-log.md", "Hob Service Log", terms=("hob", "service"))
    analysis = resolve_module.analyze_turn("is the hob booked in yet")
    candidates = resolve_module.candidates_for(
        analysis, (row,), term_anchor_counts={"hob": 1}
    )

    assert len(candidates) == 1
    assert candidates[0].evidence == frozenset({"rare_term"})


# --------------------------------------------------------------------------- #
# Correction round 1 (`close-memory-loop`): rarity among ANCHOR NAMES is not
# rarity of the WORD. A single shared name term never earns `lexical_overlap`,
# however broad the overlap or how rare `term_anchor_counts` shows the term --
# only `rare_term` is available to a single shared name term, and it still
# needs a second, independent CONTACT kind (not merely a qualifier) to resolve.
# --------------------------------------------------------------------------- #


def test_a_rare_single_name_term_with_broad_overlap_and_only_a_qualifier_stays_partial() -> None:
    """The orchestrator's repro: a single shared name term that IS rare by
    `term_anchor_counts`, completed to the broad-term band by a section/tag
    word, plus a turn-cue qualifier (`category_match`) and NO retrieved
    contact. Before this fix `len(shared_name) >= 2 or
    single_shared_name_term_is_rare` treated "named by <= RARE_TERM_MAX_ANCHORS
    anchors" as proof the WORD itself is rare, granting `lexical_overlap` and
    letting `_status_for` resolve it off a mere qualifier. Only `rare_term` is
    available to one shared name term, and `rare_term` plus a qualifier alone
    stays `partial` (unchanged existing rule, `test_rare_term_with_a_
    qualifier_only_is_partial`).
    """
    row = _term_row("bench.md", "Workshop bench", terms=("workshop", "bench", "limit"))
    analysis = resolve_module.analyze_turn("is the bench past its limit")
    candidates = resolve_module.candidates_for(
        analysis, (row,), term_anchor_counts={"bench": 1}
    )

    assert len(candidates) == 1
    assert candidates[0].evidence == frozenset({"rare_term", "category_match"})
    resolution = resolve_module.resolve(candidates)
    assert resolution.anchors[0].status == "partial"
    assert resolution.status == "unresolved"


def test_a_rare_single_name_term_with_retrieval_resolves_via_rare_term() -> None:
    """Companion to the test above: the SAME anchor, same broad overlap and
    qualifier, but now with the anchor's own page in retrieval (a genuine
    CONTACT kind, not a qualifier) resolves -- via `rare_term`, never
    `lexical_overlap`."""
    row = _term_row("bench.md", "Workshop bench", terms=("workshop", "bench", "limit"))
    analysis = resolve_module.analyze_turn("is the bench past its limit")
    candidates = resolve_module.candidates_for(
        analysis,
        (row,),
        term_anchor_counts={"bench": 1},
        retrieval_paths=frozenset({"bench.md"}),
    )

    assert len(candidates) == 1
    assert "lexical_overlap" not in candidates[0].evidence
    assert candidates[0].evidence == frozenset({"rare_term", "category_match", "retrieval"})
    resolution = resolve_module.resolve(candidates)
    assert resolution.anchors[0].status == "resolved"
    assert "rare_term" in resolution.anchors[0].evidence


def test_rare_term_alone_is_partial() -> None:
    resolution = resolve_module.resolve((_facts("a", evidence=("rare_term",)),))

    assert resolution.anchors[0].status == "partial"
    assert resolution.status == "unresolved"


def test_rare_term_with_a_qualifier_only_is_partial() -> None:
    """Canonical spec: "A rare word and a turn cue are not enough."""
    resolution = resolve_module.resolve(
        (_facts("a", evidence=("rare_term", "category_match")),)
    )

    assert resolution.anchors[0].status == "partial"
    assert resolution.status == "unresolved"


def test_rare_term_with_another_contact_kind_resolves() -> None:
    """Canonical spec: "A rare word and the anchor's own page in recall resolve"."""
    resolution = resolve_module.resolve(
        (_facts("a", evidence=("rare_term", "retrieval")),)
    )

    assert resolution.anchors[0].status == "resolved"
    assert resolution.anchors[0].evidence == ("rare_term", "retrieval")


# --------------------------------------------------------------------------- #
# Independent review, round 4: `lexical_overlap` and `rare_term` are
# mutually exclusive, and `lexical_overlap` needs at least one AUTHORED word.
# --------------------------------------------------------------------------- #


def test_a_single_shared_name_term_never_grants_lexical_overlap_even_tag_completed() -> None:
    """The reviewer's "wardrobe hub" repro (round 4, BLOCKER), now updated
    for correction round 1 (`close-memory-loop`): title "Wardrobe inventory"
    (authored terms wardrobe/inventory), a `## Notes` section and a `hub`
    tag. Turn "wardrobe hub" shares two BROAD terms (wardrobe, hub) -- but
    only ONE of them ("wardrobe") is among the anchor's own authored
    name terms; "hub" is a tag, never authored. A single shared name term
    never grants `lexical_overlap`, however broad the completed overlap or
    how rare `term_anchor_counts` shows it -- rarity among anchor NAMES is
    not rarity of the word itself, and treating it as such let an unnamed
    anchor resolve off a shared tag word plus one common name term. Only
    `rare_term` is available to a single shared name term, and `rare_term`
    alone (no second CONTACT kind) still stays `partial`.
    """
    row = _term_row(
        "wardrobe.md", "Wardrobe inventory", terms=("wardrobe", "inventory", "notes", "hub")
    )
    analysis = resolve_module.analyze_turn("wardrobe hub")
    candidates = resolve_module.candidates_for(
        analysis, (row,), term_anchor_counts={"wardrobe": 1}
    )

    assert len(candidates) == 1
    assert candidates[0].evidence == frozenset({"rare_term"})
    resolution = resolve_module.resolve(candidates)
    assert resolution.status == "unresolved"
    assert resolution.anchors[0].status == "partial"


def test_tag_and_section_words_alone_never_constitute_an_overlap() -> None:
    """The reviewer's MAJOR (round 4): "hub notes" against two anchors tagged
    `hub` with a `## Notes` section, whose TITLES name neither word, both own
    pages in `retrieval_paths`. Tag/section words may COMPLETE an overlap,
    never CONSTITUTE one: neither anchor's authored title/alias terms share
    anything with the turn, so neither may carry `lexical_overlap` (nor
    `rare_term`, for the same reason) -- retrieval alone, and the packet
    abstains `unresolved`, never `ambiguous` (ambiguity is only ever computed
    over RESOLVED anchors, and neither resolves here).
    """
    row1 = _term_row(
        "h1.md", "Northern Circuit", terms=("northern", "circuit", "notes", "hub")
    )
    row2 = _term_row(
        "h2.md", "Southern Circuit", terms=("southern", "circuit", "notes", "hub")
    )
    analysis = resolve_module.analyze_turn("hub notes")
    candidates = resolve_module.candidates_for(
        analysis, (row1, row2), retrieval_paths=frozenset({"h1.md", "h2.md"})
    )

    for candidate in candidates:
        assert "lexical_overlap" not in candidate.evidence
        assert "rare_term" not in candidate.evidence
        assert candidate.evidence == frozenset({"retrieval"})

    resolution = resolve_module.resolve(candidates)
    assert resolution.status == "unresolved"


def test_one_rare_authored_word_plus_one_section_word_plus_retrieval_still_resolves_via_rare_term() -> None:
    """Correction round 1: one RARE authored word ("wardrobe") plus one
    section word ("notes", never authored) is still a real two-term BROAD
    overlap, but a single shared NAME term is never `lexical_overlap` --
    only `rare_term`, regardless of rarity (see `test_a_single_shared_name_
    term_never_grants_lexical_overlap_even_tag_completed`). With `retrieval`
    alongside it the anchor still resolves, via `rare_term` this time.
    Without the rarity count a single shared name term earns nothing at all
    -- see `test_a_common_authored_word_plus_one_section_word_is_no_longer_overlap`.
    """
    row = _term_row(
        "wardrobe2.md", "Wardrobe inventory", terms=("wardrobe", "inventory", "notes")
    )
    analysis = resolve_module.analyze_turn("wardrobe notes")
    candidates = resolve_module.candidates_for(
        analysis,
        (row,),
        retrieval_paths=frozenset({"wardrobe2.md"}),
        term_anchor_counts={"wardrobe": 1},
    )

    assert candidates[0].evidence == frozenset({"rare_term", "retrieval"})
    resolution = resolve_module.resolve(candidates)
    assert resolution.anchors[0].status == "resolved"


def test_a_common_authored_word_plus_one_section_word_is_no_longer_overlap() -> None:
    """`close-memory-loop` root cause 2, the exact repro: an unnamed hub whose
    long title shares exactly one COMMON word with the turn, completed by a
    broad (section) word, used to earn `lexical_overlap` on that single word
    alone -- and `_status_for` then resolved it with a mere qualifier, so an
    unnamed hub could take over a packet. Same shape as the test above, minus
    the rarity count: a single shared name term that cannot be shown rare is
    no longer name contact, so this candidate earns nothing from the lexical
    comparison and never resolves on words alone.
    """
    row = _term_row(
        "wardrobe2.md", "Wardrobe inventory", terms=("wardrobe", "inventory", "notes")
    )
    analysis = resolve_module.analyze_turn("wardrobe notes")
    candidates = resolve_module.candidates_for(analysis, (row,))

    assert candidates == ()


def test_two_authored_words_still_give_lexical_overlap_as_before() -> None:
    """Unchanged case: two shared AUTHORED words grant `lexical_overlap`
    exactly as they did before this fix.
    """
    row = _term_row("x.md", "Alpha Beta", terms=("alpha", "beta"))
    # Reordered so the turn never forms the "alpha beta" bigram itself --
    # this is testing `lexical_overlap`, not a second route to `exact_alias`.
    analysis = resolve_module.analyze_turn("beta versus alpha today")
    candidates = resolve_module.candidates_for(analysis, (row,))

    assert candidates[0].evidence == frozenset({"lexical_overlap"})


def test_lexical_comparison_folds_regular_plurals() -> None:
    """Canonical spec: "Plural and singular agree"."""
    row = _term_row(
        "posts.md", "Post collection", terms=("post", "collection")
    )
    analysis = resolve_module.analyze_turn("what about the old posts collection")
    candidates = resolve_module.candidates_for(analysis, (row,))

    assert "lexical_overlap" in candidates[0].evidence


def test_lexical_comparison_folds_ies_plurals() -> None:
    row = _term_row("batteries.md", "Spare battery box", terms=("battery", "box"))
    analysis = resolve_module.analyze_turn("where are the spare batteries box")
    candidates = resolve_module.candidates_for(analysis, (row,))

    assert "lexical_overlap" in candidates[0].evidence


# --------------------------------------------------------------------------- #
# The resolution rule
# --------------------------------------------------------------------------- #


def test_two_independent_kinds_resolve_an_anchor() -> None:
    resolution = resolve_module.resolve((_facts("a", evidence=("lexical_overlap", "claims_match")),))

    assert resolution.status == "resolved"
    assert resolution.anchors[0].status == "resolved"
    assert resolution.anchors[0].evidence == ("claims_match", "lexical_overlap")


def test_exact_alias_alone_resolves() -> None:
    resolution = resolve_module.resolve((_facts("a", evidence=("exact_alias",)),))

    assert resolution.anchors[0].status == "resolved"


def test_one_kind_with_a_competitor_is_partial() -> None:
    resolution = resolve_module.resolve(
        (
            _facts("a", evidence=("lexical_overlap",)),
            _facts("b", evidence=("lexical_overlap",)),
        )
    )

    assert {anchor.status for anchor in resolution.anchors} == {"partial"}
    # No anchor resolved, so the turn abstains rather than guessing between them.
    assert resolution.status == "unresolved"


def test_usage_prior_alone_never_resolves() -> None:
    resolution = resolve_module.resolve(
        (_facts("a", evidence=("usage_prior", "vector_band")),)
    )

    assert resolution.anchors[0].status == "partial"
    assert resolution.status == "unresolved"


def test_usage_prior_does_not_count_toward_the_two_kinds_rule() -> None:
    resolution = resolve_module.resolve(
        (_facts("a", evidence=("usage_prior", "lexical_overlap")),)
    )

    assert resolution.anchors[0].status != "resolved"


def test_usage_prior_breaks_a_tie_between_otherwise_equal_candidates() -> None:
    plain = _facts("b", evidence=("lexical_overlap", "retrieval"))
    used = _facts("a", evidence=("lexical_overlap", "retrieval", "usage_prior"))
    resolution = resolve_module.resolve((plain, used))

    assert [anchor.anchor_id for anchor in resolution.anchors][0] == "a"


def test_negative_twin_abstains_with_an_unresolved_reason() -> None:
    resolution = resolve_module.resolve(())

    assert resolution.status == "unresolved"
    assert resolution.anchors == ()
    assert resolution.abstained is True
    assert resolution.abstention == {"reason": "unresolved"}


def test_disjoint_neighbourhoods_of_one_kind_report_ambiguity() -> None:
    resolution = resolve_module.resolve(
        (
            _facts(
                "north.md",
                kind="hub",
                evidence=("exact_alias",),
                neighbourhood=("a.md", "b.md"),
            ),
            _facts(
                "south.md",
                kind="hub",
                evidence=("exact_alias",),
                neighbourhood=("c.md",),
            ),
        )
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {"north.md", "south.md"}
    assert {item["neighbourhood_size"] for item in resolution.ambiguity} == {2, 1}


def test_overlapping_neighbourhoods_are_not_ambiguous() -> None:
    resolution = resolve_module.resolve(
        (
            _facts("north.md", kind="hub", evidence=("exact_alias",), neighbourhood=("shared.md",)),
            _facts("south.md", kind="hub", evidence=("exact_alias",), neighbourhood=("shared.md",)),
        )
    )

    assert resolution.status == "resolved"
    assert resolution.ambiguity == ()


def test_planning_items_in_one_canonical_collection_are_complementary() -> None:
    items = tuple(
        replace(
            _facts(identity, kind="plan", evidence=("lexical_overlap", "retrieval")),
            path="Knowledge Base/Planning/Expedition/_collection.md",
        )
        for identity in ("plan:outcome", "plan:next-action")
    )

    resolution = resolve_module.resolve(items)

    assert resolution.status == "resolved"
    assert len(resolution.resolved_anchors) == 2
    assert resolution.ambiguity == ()


def test_empty_paths_do_not_make_distinct_project_keys_complementary() -> None:
    items = tuple(
        replace(_facts(identity, kind="project", evidence=("exact_alias",)), path="")
        for identity in ("project:north", "project:south")
    )

    assert resolve_module.resolve(items).status == "ambiguous"


def test_complementary_collection_items_do_not_hide_a_competing_collection() -> None:
    items = tuple(
        replace(
            _facts(identity, kind="plan", evidence=("lexical_overlap", "retrieval")),
            path=path,
        )
        for identity, path in (
            ("plan:outcome", "Planning/A/_collection.md"),
            ("plan:action", "Planning/A/_collection.md"),
            ("plan:competitor", "Planning/B/_collection.md"),
        )
    )

    resolution = resolve_module.resolve(items)
    assert resolution.status == "ambiguous"
    assert len(resolution.ambiguity) == 2
    assert {item["ref"] for item in resolution.ambiguity} == {
        "Planning/A/_collection.md",
        "Planning/B/_collection.md",
    }


def test_canonical_path_override_keeps_all_complementary_items() -> None:
    rows = tuple(
        replace(_row(path, identity, kind="plan"), anchor_id=identity)
        for identity, path in (
            ("plan:outcome", "Planning/A/_collection.md"),
            ("plan:action", "Planning/A/_collection.md"),
            ("plan:competitor", "Planning/B/_collection.md"),
        )
    )
    chosen = resolve_module.override_candidates(rows, "Planning/A/_collection.md")
    assert {row.anchor_id for row in chosen} == {"plan:outcome", "plan:action"}
    assert resolve_module.resolve(chosen).status == "resolved"
    exact = resolve_module.override_candidates(rows, "plan:action")
    assert [row.anchor_id for row in exact] == ["plan:action"]


@pytest.mark.parametrize("shared", [False, True])
def test_linked_pair_with_disconnected_candidate_keeps_explicit_ref_choices(shared):
    first_neighbours = ("shared.md",) if shared else ("b.md",)
    second_neighbours = ("shared.md",) if shared else ()
    candidates = (
        _facts("a.md", kind="hub", evidence=("exact_alias",), neighbourhood=first_neighbours),
        _facts("b.md", kind="hub", evidence=("exact_alias",), neighbourhood=second_neighbours),
        _facts("c.md", kind="hub", evidence=("exact_alias",)),
    )
    result = resolve_module.resolve(candidates)
    assert result.status == "ambiguous"
    assert len(result.ambiguity) == 3
    assert {item["ref"] for item in result.ambiguity} == {"a.md", "b.md", "c.md"}
    rows = tuple(
        _row(c.path, c.title, neighbourhood=tuple(c.neighbourhood)) for c in candidates
    )
    chosen = resolve_module.override_candidates(rows, "a.md")
    assert [item.path for item in chosen] == ["a.md"]
    assert resolve_module.resolve(chosen).status == "resolved"


def test_two_anchors_linked_to_each_other_are_not_ambiguous() -> None:
    """A direct typed link in either direction is relatedness, not competition.

    Sharing a third anchor is one way two senses can be related; being each
    other's neighbour is a stronger one, and a predicate that only looks for the
    first abstains on the turn that names two hubs precisely BECAUSE they are
    connected.
    """
    resolution = resolve_module.resolve(
        (
            _facts(
                "north.md",
                kind="hub",
                title="Northern Programme",
                evidence=("exact_alias", "graph_corroboration"),
                neighbourhood=("south.md",),
            ),
            _facts(
                "south.md",
                kind="hub",
                title="Southern Venture",
                evidence=("exact_alias", "graph_corroboration"),
                neighbourhood=("north.md",),
            ),
        )
    )

    assert resolution.status == "resolved"
    assert resolution.ambiguity == ()


def test_one_way_link_between_two_anchors_is_also_relatedness() -> None:
    """Link direction is an authoring accident; only the edge's existence counts."""
    resolution = resolve_module.resolve(
        (
            _facts("north.md", kind="hub", evidence=("exact_alias",), neighbourhood=("south.md",)),
            _facts("south.md", kind="hub", evidence=("exact_alias",), neighbourhood=()),
        )
    )

    assert resolution.status == "resolved"
    assert resolution.ambiguity == ()


def test_a_shared_page_that_is_not_an_anchor_does_not_suppress_ambiguity() -> None:
    """Complementarity is a claim about structure, not about a shared page.

    Two hubs that both link one boilerplate page have overlapping raw
    neighbourhoods and nothing in common: the page is reached by an alias, a
    navigation stub or a house-style footer, not by belonging to either sense.
    Disjointness is therefore evaluated over the neighbours that are themselves
    anchors, and the reported sizes stay the full ones so the brain can see how
    big each neighbourhood really is.
    """
    resolution = resolve_module.resolve(
        (
            _facts(
                "north.md",
                kind="hub",
                evidence=("exact_alias",),
                neighbourhood=("ops.md",),
                anchor_neighbourhood=(),
            ),
            _facts(
                "south.md",
                kind="hub",
                evidence=("exact_alias",),
                neighbourhood=("ops.md",),
                anchor_neighbourhood=(),
            ),
        )
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {"north.md", "south.md"}
    assert {item["neighbourhood_size"] for item in resolution.ambiguity} == {1}


def test_anchors_of_different_kinds_are_complementary_not_ambiguous() -> None:
    resolution = resolve_module.resolve(
        (
            _facts("who.md", kind="entity", evidence=("exact_alias",), neighbourhood=("a.md",)),
            _facts("what.md", kind="resource", evidence=("exact_alias",), neighbourhood=("b.md",)),
        )
    )

    assert resolution.status == "resolved"


def test_no_float_leaves_the_module() -> None:
    resolution = resolve_module.resolve(
        (
            _facts("a", evidence=("exact_alias", "vector_band", "usage_prior")),
            _facts("b", kind="hub", evidence=("lexical_overlap",)),
        )
    )

    assert _floats(resolution.as_dict()) == []
    assert all(kind in resolve_module.EVIDENCE_KINDS for kind in resolution.anchors[0].evidence)


def test_evidence_vocabulary_is_closed() -> None:
    assert resolve_module.EVIDENCE_KINDS == (
        "exact_alias",
        "lexical_overlap",
        "rare_term",
        "vector_band",
        "category_match",
        "claims_match",
        "retrieval",
        "graph_corroboration",
        "usage_prior",
        # Added by `close-memory-loop` D2: the hot profile's own top, which
        # may supply the referent of a turn that names nothing and decides
        # nothing on any other turn.
        "recency",
        # Added by `activate-context-on-host-turns`: a client-carried token
        # qualifies an anchor the turn already reached, and an agent's own choice
        # of sense resolves one outright.
        "continuity",
        "agent_choice",
    )
    with pytest.raises(ValueError):
        resolve_module.resolve((_facts("a", evidence=("made_up_kind",)),))


def test_repeated_tokens_survive_into_the_ngrams() -> None:
    """A word repeated in one turn must still start an n-gram the second time.

    "Alpha Initiative and Beta Initiative" repeats `initiative`; deduplicating
    the tokens before the n-gram window slides over them destroys the phrase
    "beta initiative" entirely, so the second name can never match a title.
    """
    analysis = resolve_module.analyze_turn("Alpha Initiative and Beta Initiative")

    assert analysis.tokens == ("alpha", "initiative", "and", "beta", "initiative")
    assert "alpha initiative" in analysis.ngrams
    assert "beta initiative" in analysis.ngrams
    assert len(analysis.ngrams) == len(set(analysis.ngrams))


def test_two_names_sharing_a_word_both_reach_exact_alias() -> None:
    candidates = resolve_module.candidates_for(
        resolve_module.analyze_turn("Alpha Initiative and Beta Initiative"),
        (_row("alpha.md", "Alpha Initiative"), _row("beta.md", "Beta Initiative")),
    )

    assert {item.path: "exact_alias" in item.evidence for item in candidates} == {
        "alpha.md": True,
        "beta.md": True,
    }


def test_two_hubs_named_in_one_turn_with_no_shared_anchor_are_ambiguous() -> None:
    """The verdict the resolver defect was hiding: one turn, two competing hubs."""
    candidates = resolve_module.candidates_for(
        resolve_module.analyze_turn("Alpha Initiative and Beta Initiative"),
        (
            _row("alpha.md", "Alpha Initiative", neighbourhood=("alpha-note.md",)),
            _row("beta.md", "Beta Initiative", neighbourhood=("beta-note.md",)),
        ),
    )
    resolution = resolve_module.resolve(candidates)

    assert resolution.status == "ambiguous"
    assert {item["title"] for item in resolution.ambiguity} == {
        "Alpha Initiative",
        "Beta Initiative",
    }


# --------------------------------------------------------------------------- #
# fix/activation-competing-senses, R1: same-kind name subsumption. A shorter
# spelled name wholly inside a longer spelled name is a free rider on the
# longer mention, not a second competing sense.
# --------------------------------------------------------------------------- #


def test_r1_a_shorter_spelled_name_inside_a_longer_one_is_demoted_to_partial() -> None:
    """Problem 1: turn spells "Dana Whitfield"; both the full name and the
    bare first name resolve on `exact_alias`, same kind, no structural link --
    the shorter one is a free rider on the fuller mention, not a second
    competing sense, so the turn resolves instead of abstaining.
    """
    candidates = resolve_module.candidates_for(
        resolve_module.analyze_turn("Dana Whitfield mentioned this again"),
        (
            _row("full.md", "Dana Whitfield", kind="entity"),
            _row("short.md", "Dana", kind="entity"),
        ),
    )
    resolution = resolve_module.resolve(candidates)

    assert resolution.status == "resolved"
    by_id = {a.anchor_id: a for a in resolution.anchors}
    assert by_id["full.md"].status == "resolved"
    assert by_id["short.md"].status == "partial"


def test_r1_free_standing_mention_is_not_demoted() -> None:
    """"compare alpha hosted with alpha": "alpha" also occurs on its own,
    outside "alpha hosted"'s span, so it is a free-standing mention and stays
    resolved -- both anchors then genuinely compete (unlinked, same kind) and
    the turn abstains as ambiguous rather than silently dropping one.
    """
    analysis = resolve_module.analyze_turn("compare alpha hosted with alpha")
    candidates = resolve_module.candidates_for(
        analysis,
        (
            _row("hosted.md", "Alpha Hosted", kind="project"),
            _row("alpha.md", "Alpha", kind="project"),
        ),
    )
    resolution = resolve_module.resolve(candidates, turn_tokens=analysis.tokens)

    by_id = {a.anchor_id: a for a in resolution.anchors}
    assert by_id["alpha.md"].status == "resolved"
    assert by_id["hosted.md"].status == "resolved"
    assert resolution.status == "ambiguous"


def test_r1_without_turn_tokens_the_free_standing_exception_is_unavailable() -> None:
    """The documented default for every existing direct `resolve()` caller in
    this file, which never passes `turn_tokens`: the base subsumption rule
    applies with no free-standing exception, rather than silently changing
    behaviour for callers that supply no turn context.
    """
    resolution = resolve_module.resolve(
        (
            _facts(
                "hosted.md",
                kind="project",
                evidence=("exact_alias",),
                exact_alias_phrases=("alpha hosted",),
            ),
            _facts(
                "alpha.md",
                kind="project",
                evidence=("exact_alias",),
                exact_alias_phrases=("alpha",),
            ),
        )
    )

    by_id = {a.anchor_id: a for a in resolution.anchors}
    assert by_id["alpha.md"].status == "partial"
    assert by_id["hosted.md"].status == "resolved"
    assert resolution.status == "resolved"


def test_r1_cross_kind_pair_untouched() -> None:
    """A product and a page named after it are complementary, not competing --
    R1's subsumption rule only ever compares SAME-kind anchors."""
    candidates = resolve_module.candidates_for(
        resolve_module.analyze_turn("dana whitfield"),
        (
            _row("full.md", "Dana Whitfield", kind="entity"),
            _row("short.md", "Dana", kind="resource"),
        ),
    )
    resolution = resolve_module.resolve(candidates)

    by_id = {a.anchor_id: a for a in resolution.anchors}
    assert by_id["full.md"].status == "resolved"
    assert by_id["short.md"].status == "resolved"
    assert resolution.status == "resolved"


def test_r1_an_anchor_resolved_by_other_evidence_too_is_not_demoted() -> None:
    """`exact_alias` plus an independent second kind already resolves without
    it -- removing `exact_alias` and re-checking `_status_for` finds the
    anchor still resolved, so subsumption must leave it alone."""
    resolution = resolve_module.resolve(
        (
            _facts(
                "short.md",
                kind="entity",
                evidence=("exact_alias", "lexical_overlap", "claims_match"),
                exact_alias_phrases=("dana",),
            ),
            _facts(
                "full.md",
                kind="entity",
                evidence=("exact_alias",),
                exact_alias_phrases=("dana whitfield",),
            ),
        )
    )

    by_id = {a.anchor_id: a for a in resolution.anchors}
    assert by_id["short.md"].status == "resolved"
    assert by_id["full.md"].status == "resolved"


def test_r1_identical_exact_alias_phrase_pair_stays_ambiguous() -> None:
    """Two anchors sharing the IDENTICAL phrase are not a subsumption pair --
    "strict" containment excludes equality -- and remain genuinely competing.
    """
    resolution = resolve_module.resolve(
        (
            _facts(
                "one.md",
                kind="entity",
                evidence=("exact_alias",),
                exact_alias_phrases=("dana whitfield",),
                neighbourhood=("a",),
            ),
            _facts(
                "two.md",
                kind="entity",
                evidence=("exact_alias",),
                exact_alias_phrases=("dana whitfield",),
                neighbourhood=("b",),
            ),
        )
    )

    assert resolution.status == "ambiguous"
    by_id = {a.anchor_id: a for a in resolution.anchors}
    assert by_id["one.md"].status == "resolved"
    assert by_id["two.md"].status == "resolved"


# --------------------------------------------------------------------------- #
# fix/activation-competing-senses, R2: consumed words earn no `rare_term`. A
# turn term whose every occurrence lies inside a DIFFERENT anchor's own
# spelled-out multi-token name is consumed and cannot separately leak
# `rare_term` to a stranger that merely shares that one word.
# --------------------------------------------------------------------------- #


def test_r2_a_word_inside_a_spelled_multiword_name_earns_no_rare_term_for_a_different_anchor() -> None:
    """Problem 2: turn "alpha hosted" spells project "Alpha Hosted" in full
    (`exact_alias`, a MULTI-token phrase covering both "alpha" and "hosted").
    An unrelated hub sharing only the single name term "hosted" must not
    leak `rare_term` from a word the turn only ever used as part of a
    DIFFERENT anchor's own spelled-out name.
    """
    named = _term_row("hosted.md", "Alpha Hosted", terms=("alpha", "hosted"))
    stranger = _term_row("stranger.md", "Hosted Roster", terms=("hosted", "roster"))
    analysis = resolve_module.analyze_turn("alpha hosted")
    candidates = resolve_module.candidates_for(
        analysis, (named, stranger), term_anchor_counts={"hosted": 2}
    )

    by_id = {c.path: c for c in candidates}
    assert "exact_alias" in by_id["hosted.md"].evidence
    assert "stranger.md" not in by_id


def test_r2_the_consuming_anchor_keeps_its_own_evidence() -> None:
    """The anchor whose OWN multi-token alias covers a term's only turn
    position is not blocked from also earning `rare_term` for that same term
    -- R2 only ever blocks a DIFFERENT anchor. Here "the ridge" is the
    anchor's own alias (a leading stopword keeps it out of `shared_name`
    while still covering "ridge"'s only position via the exact-alias phrase).
    """
    row = _term_row(
        "ridge.md", "Ridge overview", terms=("ridge", "overview"), aliases=("the ridge",)
    )
    analysis = resolve_module.analyze_turn("the ridge is closed")
    candidates = resolve_module.candidates_for(
        analysis, (row,), term_anchor_counts={"ridge": 1}
    )

    assert len(candidates) == 1
    assert candidates[0].evidence == frozenset({"exact_alias", "rare_term"})


def test_r2_a_term_that_also_occurs_outside_the_covered_span_still_earns_rare_term() -> None:
    """"hosted" occurs twice: once inside "Alpha Hosted"'s own spelled name
    (covered) and once standing alone (not covered) -- not every occurrence
    is consumed, so the stranger still earns `rare_term` for it.
    """
    named = _term_row("hosted.md", "Alpha Hosted", terms=("alpha", "hosted"))
    stranger = _term_row("stranger.md", "Hosted Roster", terms=("hosted", "roster"))
    analysis = resolve_module.analyze_turn("compare alpha hosted with the hosted setup")
    candidates = resolve_module.candidates_for(
        analysis, (named, stranger), term_anchor_counts={"hosted": 2}
    )

    by_id = {c.path: c for c in candidates}
    assert "rare_term" in by_id["stranger.md"].evidence


def test_r2_single_token_aliases_consume_nothing() -> None:
    """A one-word exact-alias match (never multi-token) never covers any
    position at all, so it cannot block a different anchor's `rare_term` on
    that same word."""
    solo = _row("solo.md", "Alpha", kind="project", terms=("alpha",))
    other = _term_row("other.md", "Alpha Workshop", terms=("alpha", "workshop"))
    analysis = resolve_module.analyze_turn("alpha notes")
    candidates = resolve_module.candidates_for(
        analysis, (solo, other), term_anchor_counts={"alpha": 2}
    )

    by_id = {c.path: c for c in candidates}
    assert "exact_alias" in by_id["solo.md"].evidence
    assert "rare_term" in by_id["other.md"].evidence


# --------------------------------------------------------------------------- #
# fix/activation-competing-senses, R3: a named anchor carries the packet. When
# some RESOLVED anchor holds a deciding-alone kind, a competing group of same-
# kind anchors none of which holds one is demoted to `partial` (not an
# abstention); a competing group with such a member stays ambiguous exactly
# as today.
# --------------------------------------------------------------------------- #


def test_r3_named_anchor_carries_the_packet_past_a_weak_same_kind_competition() -> None:
    """Problem 3: "fix the flaky gate test in alpha". Project `alpha`
    resolves on `exact_alias`. Two unrelated entities each resolve on
    `rare_term` + `retrieval` (the word "gate"), same kind, unlinked -- their
    own competition must not abstain the whole turn; they are demoted to
    `partial` and the turn resolves on the named project.
    """
    resolution = resolve_module.resolve(
        (
            _facts("alpha", kind="project", evidence=("exact_alias",)),
            _facts("d1", kind="entity", evidence=("rare_term", "retrieval")),
            _facts("d2", kind="entity", evidence=("rare_term", "retrieval")),
        )
    )

    assert resolution.status == "resolved"
    by_id = {a.anchor_id: a for a in resolution.anchors}
    assert by_id["alpha"].status == "resolved"
    # Demoted, never vanished.
    assert by_id["d1"].status == "partial"
    assert by_id["d2"].status == "partial"
    assert resolution.ambiguity == ()


def test_r3_no_deciding_alone_anchor_anywhere_stays_ambiguous() -> None:
    """With no named anchor anywhere, behaviour is unchanged: the agent may
    still be asked to choose between two weak, competing senses."""
    resolution = resolve_module.resolve(
        (
            _facts(
                "north", kind="hub", evidence=("lexical_overlap", "claims_match"),
                neighbourhood=("a",),
            ),
            _facts(
                "south", kind="hub", evidence=("lexical_overlap", "claims_match"),
                neighbourhood=("b",),
            ),
        )
    )

    assert resolution.status == "ambiguous"


def test_r3_a_competing_group_with_a_named_member_stays_ambiguous() -> None:
    """A named anchor elsewhere does not rescue a DIFFERENT competing group
    that itself contains a named (deciding-alone) member -- that group is a
    genuine ambiguity exactly as today (two pages the turn really did both
    name)."""
    resolution = resolve_module.resolve(
        (
            _facts("solo", kind="project", evidence=("exact_alias",)),
            _facts("north", kind="hub", evidence=("exact_alias",), neighbourhood=("a",)),
            _facts("south", kind="hub", evidence=("exact_alias",), neighbourhood=("b",)),
        )
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {"north", "south"}
    by_id = {a.anchor_id: a for a in resolution.anchors}
    assert by_id["solo"].status == "resolved"


# --------------------------------------------------------------------------- #
# fix/activation-competing-senses, R4: possessive fold. A trailing `'s`/`'`
# is stripped from a turn token for phrase building and the lexical term
# sets, on both sides of the comparison, so "gamma's" reaches "Gamma" the
# way "gamma" already would.
# --------------------------------------------------------------------------- #


def test_r4_a_possessive_turn_token_resolves_a_single_word_name_via_exact_alias() -> None:
    """Orchestrator ruling: `candidates_for` builds `phrases = ngrams |
    tokens`, so a single turn token IS a phrase -- a de-possessived unigram
    must reach a plain single-word `exact_alias` exactly as a de-possessived
    multi-word phrase reaches a spelled-out one.
    """
    row = _row("dana.md", "Dana", kind="entity")
    analysis = resolve_module.analyze_turn("draft dana's email")
    candidates = resolve_module.candidates_for(analysis, (row,))
    resolution = resolve_module.resolve(candidates)

    assert len(candidates) == 1
    assert "exact_alias" in candidates[0].evidence
    assert resolution.anchors[0].status == "resolved"


def test_r4_a_possessive_turn_token_reaches_a_multiword_spelled_name_via_lexical_contact() -> None:
    """Problem 4: "should I replace the gamma's sensor" tokenises to
    "gamma's"; the hub named "Gamma Fleet" shares only the word "gamma" via
    the possessive-stripped form -- the de-possessived lexical term set lets
    it earn `rare_term`/`lexical_overlap` via ordinary name contact, where
    before it earned neither.
    """
    row = _term_row("gamma.md", "Gamma Fleet", terms=("gamma", "fleet"))
    analysis = resolve_module.analyze_turn("should i replace the gamma's sensor")
    candidates = resolve_module.candidates_for(
        analysis, (row,), term_anchor_counts={"gamma": 1}
    )

    assert len(candidates) == 1
    assert candidates[0].evidence & {"rare_term", "lexical_overlap"}


def test_r4_a_title_authored_with_a_possessive_still_matches_verbatim() -> None:
    """A title authored WITH a possessive must still match a turn spelling
    it out verbatim -- the de-possessived reading is ADDED, never a
    replacement for the literal one."""
    row = _row("plan.md", "Dana's Plan", kind="hub")
    analysis = resolve_module.analyze_turn("what about dana's plan")
    candidates = resolve_module.candidates_for(analysis, (row,))

    assert "exact_alias" in candidates[0].evidence
    assert "dana's plan" in candidates[0].exact_alias_phrases


def test_r4_a_multiword_name_is_reachable_via_the_de_possessived_phrase() -> None:
    """The de-possessived reading, not just the verbatim one: turn "the
    dana's plan is done" names hub "Dana Plan" (no possessive in the
    anchor's own title) via the turn's own de-possessived phrase "dana
    plan"."""
    row = _row("plan.md", "Dana Plan", kind="hub")
    analysis = resolve_module.analyze_turn("the dana's plan is done")
    candidates = resolve_module.candidates_for(analysis, (row,))

    assert "exact_alias" in candidates[0].evidence
    assert "dana plan" in candidates[0].exact_alias_phrases


def test_r4_lexical_term_sets_are_folded_symmetrically() -> None:
    """"decide by reading whether anchor-side name terms need the same fold
    for symmetry" (packet): a title authored "Dana's Plan" shares the name
    term "dana" with a turn saying plain "dana", on the ANCHOR side of the
    lexical comparison, not just the turn side.
    """
    row = _term_row(
        "plan2.md", "Dana's Plan", terms=("dana's", "plan")
    )
    analysis = resolve_module.analyze_turn("what happened with dana today")
    candidates = resolve_module.candidates_for(
        analysis, (row,), term_anchor_counts={"dana": 1}
    )

    assert len(candidates) == 1
    assert candidates[0].evidence & {"rare_term", "lexical_overlap"}


# --------------------------------------------------------------------------- #
# Correction round 1, C1 (BLOCKER): a de-possessived form that itself folds
# into a STOPWORD must never reach the single-token phrase set or the folded
# lexical term sets. "it's"/"let's"/... are ordinary contractions of common
# pronouns/verbs, never a turn naming an anchor literally called "It"/"Let".
# --------------------------------------------------------------------------- #


def test_c1_a_stopword_contraction_does_not_manufacture_exact_alias() -> None:
    """Reviewer's named case: "it's" (4 chars, clears the length floor)
    folds to "it" -- itself a stopword that would never appear in `phrases`
    or a turn term on its own. It must not reach an anchor literally titled
    "It" through the possessive fold.
    """
    row = _row("it.md", "It", kind="product")
    analysis = resolve_module.analyze_turn("it's finally back online after the outage")
    candidates = resolve_module.candidates_for(analysis, (row,))

    assert candidates == ()


def test_c1_lets_does_not_manufacture_exact_alias_against_an_anchor_named_let() -> None:
    """"let's" (5 chars) folds to "let" -- not itself in STOPWORDS as a bare
    word, but "let" IS one of the added function-word stopwords, so the same
    route must be closed for it too."""
    row = _row("let.md", "Let", kind="product")
    analysis = resolve_module.analyze_turn("let's ship the release today")
    candidates = resolve_module.candidates_for(analysis, (row,))

    assert candidates == ()


@pytest.mark.parametrize(
    ("contraction", "title"),
    [
        ("that's", "That"),
        ("who's", "Who"),
        ("what's", "What"),
        ("there's", "There"),
    ],
)
def test_c1_common_stopword_contractions_do_not_manufacture_exact_alias(
    contraction: str, title: str
) -> None:
    row = _row(f"{title.lower()}.md", title, kind="product")
    analysis = resolve_module.analyze_turn(f"{contraction} not going to work today")
    candidates = resolve_module.candidates_for(analysis, (row,))

    assert candidates == ()


def test_c1_the_legitimate_possessive_case_still_resolves() -> None:
    """"dana's email" must still reach a plain "Dana" via `exact_alias` --
    the fix drops only a fold that LANDS IN STOPWORDS, not possessive
    folding in general (canonical R4 case, re-pinned here)."""
    row = _row("dana.md", "Dana", kind="entity")
    analysis = resolve_module.analyze_turn("draft dana's email")
    candidates = resolve_module.candidates_for(analysis, (row,))
    resolution = resolve_module.resolve(candidates)

    assert len(candidates) == 1
    assert "exact_alias" in candidates[0].evidence
    assert resolution.anchors[0].status == "resolved"


def test_c1_a_title_authored_with_a_possessive_still_matches_verbatim() -> None:
    """A title itself authored with a possessive must still match a turn
    spelling it out verbatim (canonical R4 case, re-pinned here)."""
    row = _row("plan.md", "Dana's Plan", kind="hub")
    analysis = resolve_module.analyze_turn("what about dana's plan")
    candidates = resolve_module.candidates_for(analysis, (row,))

    assert "exact_alias" in candidates[0].evidence
    assert "dana's plan" in candidates[0].exact_alias_phrases


def test_c1_fold_lexical_term_drops_a_stopword_landing_possessive_fold() -> None:
    """The SAME helper folds BOTH sides of the lexical comparison
    (`turn_terms_folded` and `row_terms_folded`/`name_terms_folded`), so
    proving it here proves the anchor side cannot manufacture a stopword
    name term in reverse either -- a title "It's Complicated" cannot gain
    the name term "it", because `tokens_of("It's Complicated")` feeds "it's"
    through this exact function on the anchor side too.
    """
    assert resolve_module._fold_lexical_term("it's") is None
    assert resolve_module._fold_lexical_term("let's") is None
    assert resolve_module._fold_lexical_term("dana's") == "dana"
    # An ordinary plural fold is unaffected by the stopword guard.
    assert resolve_module._fold_lexical_term("posts") == "post"


# A true end-to-end anchor-side probe is not constructible: `turn_terms`
# already excludes a LITERAL "it" (a stopword), and the turn-side fix above
# drops any contraction that folds to one, so `turn_terms_folded` can never
# contain "it" regardless of what the anchor side does -- `shared_name`'s
# intersection would be identical whether or not `name_terms_folded` (a
# title "It's Complicated") carries a phantom "it". `_fold_lexical_term`
# is the ONE function both `turn_terms_folded` and `row_terms_folded`/
# `name_terms_folded` call, so the direct test above is the correct and
# only way to verify the anchor side is safe: proving the shared function
# is safe proves both call sites are.


# --------------------------------------------------------------------------- #
# fix/activation-competing-senses, R5: named anchors order first. An anchor
# holding a deciding-alone kind must survive MAX_ANCHORS truncation ahead of
# weaker multi-kind candidates.
# --------------------------------------------------------------------------- #


def test_r5_a_named_anchor_survives_max_anchors_truncation_ahead_of_weak_candidates() -> None:
    """Red test (packet): more than `MAX_ANCHORS` weak two-kind candidates
    plus one `exact_alias`-only anchor. Without R5's ordering, the weak
    candidates (two deciding kinds each) sort ahead of the named one (one
    kind) by the existing `-len(deciding_kinds)` key alone, and MAX_ANCHORS
    truncation can drop the one anchor the turn actually named.
    """
    weak = tuple(
        _facts(f"weak{i}", kind="hub", evidence=("lexical_overlap", "claims_match"))
        for i in range(resolve_module.MAX_ANCHORS + 1)
    )
    named = _facts("named", kind="project", evidence=("exact_alias",))

    resolution = resolve_module.resolve((*weak, named))

    assert len(resolution.anchors) == resolve_module.MAX_ANCHORS
    by_id = {a.anchor_id: a for a in resolution.anchors}
    assert "named" in by_id
    assert by_id["named"].status == "resolved"
    assert resolution.status == "resolved"


# --------------------------------------------------------------------------- #
# Correction round 1, C3 (REQUIRED, orchestrator): `STOPWORDS` was missing
# almost every preposition and conjunction, so a turn sharing only one such
# function word with an anchor's title earned `rare_term` -- a turn ending
# "... before the trip" resolving an unrelated "Before Launch Review" plan.
# --------------------------------------------------------------------------- #


def test_c3_a_function_word_alone_does_not_earn_rare_term() -> None:
    row = _term_row(
        "plan.md", "Before Launch Review", terms=("before", "launch", "review")
    )
    analysis = resolve_module.analyze_turn("please pack before the trip")
    candidates = resolve_module.candidates_for(
        analysis, (row,), term_anchor_counts={"before": 1}
    )

    assert candidates == ()


# --------------------------------------------------------------------------- #
# Referential turns (close-memory-loop D2, as narrowed): a turn that SAYS it
# points back at what the session was doing. A declared cue, matched on whole
# tokens, AND nothing else said (R-G); being short is not a signal.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "turn",
    [
        "continue",
        "ok continue",
        "carry on",
        "where were we",
        "where did we leave off",
        "what's next",
        "what\u2019s next?",
        "same as before",
        "as before",
        "pick up where we left off",
        "resume",
        "status",
        "status?",
    ],
)
def test_a_declared_referential_cue_makes_the_turn_referential(turn: str) -> None:
    analysis = resolve_module.analyze_turn(turn)

    assert "referential" in analysis.cues
    assert analysis.referential is True


@pytest.mark.parametrize(
    "turn",
    [
        "and the northern depot?",
        "zzz qqq unrelated gibberish",
        "What's a good name for a new houseplant?",
        "so should I go with the cheaper one?",
    ],
)
def test_a_short_turn_without_a_cue_is_not_referential(turn: str) -> None:
    """Being short is not a signal. A novel turn is short too, and a prior
    must never answer one with whatever was edited last."""
    analysis = resolve_module.analyze_turn(turn)

    assert "referential" not in analysis.cues
    assert analysis.referential is False


@pytest.mark.parametrize(
    "turn",
    [
        "discontinue the winter schedule",
        "list the statuses of every depot",
        "go online and check the depot",
        "go on the northern route",
        "pick up the parcel at the depot",
        "wherever we were going",
    ],
)
def test_a_cue_is_matched_on_whole_tokens_only(turn: str) -> None:
    """A cue inside a longer word, or a dropped cue's ordinary sense, is not
    a turn pointing back."""
    analysis = resolve_module.analyze_turn(turn)

    assert "referential" not in analysis.cues
    assert analysis.referential is False


def test_a_long_turn_with_no_cue_is_not_referential() -> None:
    analysis = resolve_module.analyze_turn(
        "I'm planning to tow the cargo sled north along the winter corridor "
        "and I need to know what the depot stock looks like"
    )

    assert analysis.referential is False


#: The turns that only point back: every one must stay referential (R-G;
#: the reviewer's keep list, p11, plus the aligned cues).
POINTING_BACK_TURNS = (
    "continue",
    "ok continue",
    "please continue",
    "where were we",
    "so where were we?",
    "status?",
    "status update",
    "status report?",
    "what's next",
    "what's next?",
    "let's continue the work, what's pending?",
    "Let's continue the work... what's pending?",
    "pick up where we left off",
    "continue from where we stopped yesterday",
    "okay, where did we leave off?",
    "resume",
    "carry on",
    "same as before",
    "what were we doing?",
)

#: Turns that speak a cue word in its ordinary sense, or name something
#: besides it: none may be referential (R-G; the reviewer's p3, p11 and p15
#: misfires and named turns).
CUE_WORD_BUT_NOT_POINTING_BACK_TURNS = (
    "update my resume",
    "carry on luggage limits for a short flight",
    "what's the status of the parcel?",
    "continue the recipe from the book",
    "can I resume my gym membership after the injury?",
    "check the status of my flight",
    "I want to continue learning Spanish",
    "what is next year's tax deadline",
    "is the status quo fine for the lease?",
    "what's next for the quillon vantry window",
    "status of the tarn rollover cadence",
    "summarize this article as before",
    "use the same format as before",
    "same as before but shorter",
    "translate it as before",
    "resume the download",
    "what's the status code for not found",
    "continue the story",
    "what's next in the tutorial?",
    "carry on with the essay",
    "should I continue with the cheaper cargo sled or wait for the dearer one",
)


@pytest.mark.parametrize("turn", POINTING_BACK_TURNS)
def test_a_turn_that_only_points_back_is_referential(turn: str) -> None:
    assert resolve_module.analyze_turn(turn).referential is True


@pytest.mark.parametrize("turn", CUE_WORD_BUT_NOT_POINTING_BACK_TURNS)
def test_a_cue_word_with_anything_else_said_is_not_referential(turn: str) -> None:
    """The cue was spoken — it is recorded — but the turn also says what it
    is about, so recency is not asked to supply a referent for it."""
    analysis = resolve_module.analyze_turn(turn)

    assert "referential" in analysis.cues
    assert analysis.referential is False


def test_the_filler_set_is_closed_and_declared() -> None:
    assert "work" in resolve_module.REFERENTIAL_FILLER
    assert "resume" not in resolve_module.REFERENTIAL_FILLER
    assert "report" not in resolve_module.REFERENTIAL_FILLER
    assert len(resolve_module.REFERENTIAL_FILLER) == 33


# --------------------------------------------------------------------------- #
# `recency`, the prior that may supply a referent (close-memory-loop D2)
# --------------------------------------------------------------------------- #


def test_recency_is_its_own_class_and_no_other() -> None:
    assert "recency" in resolve_module.EVIDENCE_KINDS
    assert resolve_module.PRIOR_CONTACT_KINDS == frozenset({"recency"})
    assert "recency" not in resolve_module.TIE_BREAK_KINDS
    assert "recency" not in resolve_module.WORDED_CONTACT_KINDS
    assert "recency" not in resolve_module.RETRIEVED_CONTACT_KINDS
    # Deliberately NOT a contact kind: `_status_for`'s third clause resolves
    # `rare_term` plus any other CONTACT kind, and admitting a prior there
    # would let one shared word plus a hot page resolve an anchor.
    assert "recency" not in resolve_module.CONTACT_KINDS


def test_a_referential_turn_resolves_to_the_one_hot_anchor() -> None:
    resolution = resolve_module.resolve(
        (_facts("hot.md", evidence=("recency",)),), referential=True
    )

    assert resolution.status == "resolved"
    assert resolution.anchors[0].status == "resolved"
    assert resolution.anchors[0].evidence == ("recency",)


def test_two_equally_hot_anchors_on_a_referential_turn_are_ambiguous() -> None:
    """Never a guess: the profile put two anchors of one kind at the top and
    nothing relates them, so the agent is handed both."""
    resolution = resolve_module.resolve(
        (
            _facts("first.md", kind="hub", evidence=("recency",)),
            _facts("second.md", kind="hub", evidence=("recency",)),
        ),
        referential=True,
    )

    assert resolution.status == "ambiguous"
    assert {item["ref"] for item in resolution.ambiguity} == {"first.md", "second.md"}


def test_a_worded_candidate_anywhere_stops_recency_resolving() -> None:
    """The named anchor carries the packet and the hot one decides nothing,
    even though the turn read as referential by cue."""
    resolution = resolve_module.resolve(
        (
            _facts("hot.md", kind="hub", evidence=("recency",)),
            _facts("named.md", kind="resource", evidence=("exact_alias",)),
        ),
        referential=True,
    )

    assert resolution.status == "resolved"
    assert [anchor.anchor_id for anchor in resolution.resolved_anchors] == ["named.md"]
    assert "hot.md" not in {anchor.anchor_id for anchor in resolution.anchors}


def test_recency_never_resolves_on_a_turn_that_is_not_referential() -> None:
    resolution = resolve_module.resolve((_facts("hot.md", evidence=("recency",)),))

    assert resolution.status == "unresolved"
    assert resolution.anchors == ()


def test_recency_never_completes_the_two_kinds_rule() -> None:
    """The whole of section 8 that survives: a prior cannot be the second
    kind that promotes another candidate, referential turn or not."""
    for referential in (False, True):
        resolution = resolve_module.resolve(
            (_facts("a.md", evidence=("lexical_overlap", "recency")),),
            referential=referential,
        )

        assert resolution.anchors[0].status == "partial", referential
        assert resolution.status == "unresolved", referential


def test_recency_does_not_promote_a_rare_term_candidate() -> None:
    resolution = resolve_module.resolve(
        (_facts("a.md", evidence=("rare_term", "recency")),), referential=True
    )

    assert resolution.anchors[0].status == "partial"


def test_a_hot_anchor_is_a_candidate_only_on_a_referential_turn() -> None:
    row = _row("Products/Hot Page.md", "Hot Page", kind="resource")
    hot = frozenset({"Products/Hot Page.md"})

    referential = resolve_module.candidates_for(
        resolve_module.analyze_turn("continue"), (row,), hot_paths=hot
    )
    ordinary = resolve_module.candidates_for(
        resolve_module.analyze_turn(
            "I am planning to tow the cargo sled north along the winter corridor "
            "and need the depot stock figures"
        ),
        (row,),
        hot_paths=hot,
    )

    assert [item.anchor_id for item in referential] == ["Products/Hot Page.md"]
    assert referential[0].evidence == frozenset({"recency"})
    assert ordinary == ()


def test_a_hot_anchor_the_turn_also_named_keeps_its_own_evidence() -> None:
    """`recency` joins the evidence of an anchor already in contact — it is
    reported, so a reader sees the page was hot, and it still decides
    nothing (the clause above)."""
    row = _term_row("Products/Hot Page.md", "Hot Page", terms=("hot", "page"))
    candidates = resolve_module.candidates_for(
        resolve_module.analyze_turn("the hot page"),
        (row,),
        hot_paths=frozenset({"Products/Hot Page.md"}),
    )

    assert candidates[0].evidence >= frozenset({"exact_alias", "recency"})


def test_a_candidate_only_the_prior_admitted_is_dropped_when_something_was_named() -> None:
    """A hot page the turn never reached rides in on the prior alone. Once
    the turn names something else the prior decides nothing, and a qualifier
    it happens to carry (`continuity`, `category_match`) must not turn it into
    a `partial` menu entry whose only claim is that somebody edited it."""
    for qualifier in ("continuity", "category_match"):
        resolution = resolve_module.resolve(
            (
                _facts("hot.md", kind="hub", evidence=("recency", qualifier)),
                _facts("named.md", kind="resource", evidence=("exact_alias",)),
            ),
            referential=True,
        )

        assert resolution.status == "resolved", qualifier
        assert [anchor.anchor_id for anchor in resolution.anchors] == ["named.md"], qualifier


def test_a_hot_anchor_with_continuity_resolves_on_a_referential_turn_naming_nothing() -> None:
    """The previous packet's anchor, hot, on "continue": the fifth clause
    resolves it, and both kinds are reported so a reader can see why."""
    resolution = resolve_module.resolve(
        (_facts("carried.md", evidence=("recency", "continuity")),), referential=True
    )

    assert resolution.status == "resolved"
    assert resolution.anchors[0].evidence == ("continuity", "recency")


# --------------------------------------------------------------------------- #
# R-E (amends D4): a one- or two-letter ASCII term clears the rare-term floor
# only when the turn spells it as an acronym, in upper case.
# --------------------------------------------------------------------------- #


def test_an_upper_case_two_letter_acronym_earns_rare_term() -> None:
    """"AI" in "my AI usage limits" is a name the turn spelled as one — the
    D4 floor exists for everyday words, and nobody writes "go" as "GO" in a
    sentence of ordinary case."""
    row = _term_row(
        "Records/AI Subscriptions/_collection.md", "AI Subscriptions", terms=("ai", "subscriptions")
    )
    analysis = resolve_module.analyze_turn("I keep hitting my AI usage limits again this week.")

    assert analysis.acronyms == frozenset({"ai"})
    candidates = resolve_module.candidates_for(analysis, (row,), term_anchor_counts={"ai": 1})

    assert len(candidates) == 1
    assert "rare_term" in candidates[0].evidence


@pytest.mark.parametrize(
    "turn",
    [
        "I keep hitting my ai usage limits again this week.",
        "I keep hitting my Ai usage limits again this week.",
    ],
)
def test_a_short_term_not_spelled_as_an_acronym_stays_refused(turn: str) -> None:
    """Lower or mixed case is how an everyday word is written, so D4 holds."""
    row = _term_row(
        "Records/AI Subscriptions/_collection.md", "AI Subscriptions", terms=("ai", "subscriptions")
    )
    analysis = resolve_module.analyze_turn(turn)

    assert analysis.acronyms == frozenset()
    assert resolve_module.candidates_for(analysis, (row,), term_anchor_counts={"ai": 1}) == ()


def test_an_all_capitals_turn_carries_no_casing_signal() -> None:
    """A turn typed with caps lock on spells every word in upper case, so
    upper case says nothing there: "GO" in it is still the verb."""
    row = _term_row("release-go-checklist.md", "Release Go Checklist", terms=("release", "go"))
    analysis = resolve_module.analyze_turn("SO SHOULD I GO WITH THE FIRST OPTION")

    assert analysis.acronyms == frozenset()
    assert resolve_module.candidates_for(analysis, (row,), term_anchor_counts={"go": 1}) == ()


def test_a_capitalised_short_word_is_not_an_acronym() -> None:
    """"Go" at the start of a sentence is the verb with a capital, not a name."""
    row = _term_row("release-go-checklist.md", "Release Go Checklist", terms=("release", "go"))
    analysis = resolve_module.analyze_turn("Go with the first option, I think")

    assert analysis.acronyms == frozenset()
    assert resolve_module.candidates_for(analysis, (row,), term_anchor_counts={"go": 1}) == ()
