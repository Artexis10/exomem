"""Task 3.1 -- one cue vocabulary: the role registry replaces `CUE_PATTERNS`.

`make-activation-conventions-vault-owned`, decision 1: `CUE_PATTERNS` and
`_CUE_CATEGORIES` are deleted from `working_set_resolve`; a role's own
`evidence_cues`/`evidence_categories` are the only cue-to-category mapping
the server holds. This file pins the equivalence the design promises --
shipped evidence never widens what the deleted table made eligible, with
exactly one intended, pinned difference (a bare `?`) -- plus the
`context-roles` spec's scenarios for the new evidence rule.

The frozen `_OLD_*` tables below are TEST DATA ONLY: a literal copy of the
deleted `CUE_PATTERNS`/`_CUE_CATEGORIES` constants, kept here so the
equivalence has something fixed to compare the live registry against after
the real tables are gone from production code.
"""

from __future__ import annotations

from pathlib import Path

from exomem import context_roles, working_set_resolve

# --------------------------------------------------------------------------- #
# Frozen test data: the deleted table, verbatim
# --------------------------------------------------------------------------- #

_OLD_CUE_PATTERNS: dict[str, tuple[str, ...]] = {
    "planning": ("i'm planning", "im planning", "planning to", "how should i", "should i"),
    "constraint": ("constraint", "limit", "allowed to", "can i", "am i able"),
    "preference": ("prefer", "i like", "i hate", "usually"),
    "current_state": ("right now", "currently", "at the moment", "how much", "how many"),
    "method": ("how do i", "how to", "what is the best way", "approach"),
    "question": ("?", "what about", "why does"),
    "recent_change": ("again", "still", "changed", "since"),
    "precedent": ("last time", "before", "previously"),
}

_OLD_CUE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "planning": ("action",),
    "constraint": ("constraint", "requirement"),
    "preference": ("preference",),
    "current_state": ("fact",),
    "method": ("technique", "design"),
    "question": ("question", "problem"),
    "recent_change": ("decision", "finding"),
    "precedent": ("decision", "insight"),
}


def _old_eligible_categories(text: str) -> frozenset[str]:
    """The deleted table's own rule, restated as test data: a cue GROUP
    fires if ANY of its patterns is a substring of the (already NFKC +
    casefolded) turn text, and a firing group contributes its whole
    category set."""
    fired = [
        name
        for name, patterns in _OLD_CUE_PATTERNS.items()
        if any(pattern in text for pattern in patterns)
    ]
    return frozenset(
        category for name in fired for category in _OLD_CUE_CATEGORIES.get(name, ())
    )


# The required adversarial set, plus enough extra turns to exercise every
# one of the eight deleted cue groups at least once.
ADVERSARIAL_TURNS: tuple[str, ...] = (
    "?",
    "how much is left",
    "what about",
    "what is the budget",
    "what is next",
    "we already decided",
    "i'm planning to ship this",
    "what is the best way to do this",
    "why does this keep happening",
    "it changed again since last week",
    "last time we discussed this",
    "i prefer the other one",
    "right now everything is fine",
    "am i allowed to do this",
    "nothing interesting here",
    "just checking in",
)


def _override(vault: Path, payload: object) -> None:
    path = context_roles.override_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    import yaml

    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    context_roles.clear_cache()


# --------------------------------------------------------------------------- #
# (b) no-new-category: shipped evidence never widens the deleted table
# --------------------------------------------------------------------------- #


def test_no_new_category_becomes_eligible_over_the_adversarial_set() -> None:
    registry = context_roles.load_roles()
    roles = registry.roles.values()
    for turn in ADVERSARIAL_TURNS:
        analysis = working_set_resolve.analyze_turn(turn)
        old = _old_eligible_categories(analysis.text)
        new = working_set_resolve.eligible_categories(analysis, roles)
        assert new <= old, (turn, old, new)


def test_no_new_category_becomes_eligible_on_the_shipped_registrys_own_cues() -> None:
    """The same property, but driven by the registry's OWN evidence cues
    rather than a hand-picked turn set -- every evidence cue, said alone,
    must not earn the turn a category the deleted table would not have."""
    registry = context_roles.load_roles()
    roles = registry.roles.values()
    for role in roles:
        for cue in role.evidence_cues:
            analysis = working_set_resolve.analyze_turn(cue)
            old = _old_eligible_categories(analysis.text)
            new = working_set_resolve.eligible_categories(analysis, roles)
            assert new <= old, (cue, old, new)


# --------------------------------------------------------------------------- #
# (c) no-new-trigger: no anchor becomes `resolved` on a turn the deleted
# table made no category eligible for
# --------------------------------------------------------------------------- #


def test_no_anchor_resolves_via_category_match_on_a_turn_with_no_old_eligible_category() -> None:
    """A `lexical_overlap`-only candidate is `partial` by itself (one
    deciding kind); the deleted table's category_match could promote it to
    `resolved` (two deciding kinds). For every adversarial turn where the
    OLD table made NOTHING eligible, the anchor must stay `partial` under
    the new registry too -- category_match must not newly fire.
    """
    registry = context_roles.load_roles()
    roles = registry.roles.values()
    row = working_set_resolve.AnchorFacts(
        anchor_id="a",
        path="a.md",
        ref=None,
        # Five tokens: longer than `MAX_NGRAM` (4), so the turn below can
        # never reproduce the whole title as one contiguous n-gram and earn
        # `exact_alias` by accident -- this test wants a `lexical_overlap`-
        # ONLY candidate, the case `category_match` alone can promote.
        title="Northern Freight Corridor Bypass Plan",
        kind="hub",
        lifecycle="active",
        aliases=(),
        terms=("northern", "freight", "corridor", "bypass", "plan"),
        # Every category any of the eight evidence-bearing roles could ever
        # supply -- the worst case for a spurious category_match.
        categories=(
            "fact",
            "constraint",
            "requirement",
            "preference",
            "technique",
            "design",
            "question",
            "problem",
            "decision",
            "finding",
            "action",
            "insight",
        ),
        neighbourhood=frozenset(),
    )
    checked_at_least_one = False
    for prefix in ADVERSARIAL_TURNS:
        # Only TWO of the title's five words -- enough for `lexical_overlap`
        # (the floor is two shared, authored terms), never enough to also
        # reproduce the title as a phrase.
        turn = f"{prefix} northern freight"
        analysis = working_set_resolve.analyze_turn(turn)
        old = _old_eligible_categories(analysis.text)
        if old:
            continue  # in scope for the OTHER equivalence test, not this one
        checked_at_least_one = True
        new = working_set_resolve.eligible_categories(analysis, roles)
        assert new == frozenset()
        candidates = working_set_resolve.candidates_for(
            analysis, (row,), eligible_categories=new
        )
        assert len(candidates) == 1
        assert candidates[0].evidence == frozenset({"lexical_overlap"})
        resolution = working_set_resolve.resolve(candidates)
        assert resolution.anchors[0].status == "partial"
    assert checked_at_least_one, "the adversarial set must include a no-old-signal turn"


# --------------------------------------------------------------------------- #
# (d) the named difference: a bare `?` and nothing else
# --------------------------------------------------------------------------- #


def test_named_difference_a_bare_question_mark_no_longer_makes_question_eligible() -> None:
    registry = context_roles.load_roles()
    roles = registry.roles.values()
    analysis = working_set_resolve.analyze_turn("is this ready?")

    old = _old_eligible_categories(analysis.text)
    assert old == frozenset({"question", "problem"})

    new = working_set_resolve.eligible_categories(analysis, roles)
    assert new == frozenset()

    # The role is still SELECTABLE on the bare "?" -- only evidence is lost.
    selected = context_roles.select_roles(registry, anchor_kinds=("hub",), analysis=analysis)
    assert any(item["id"] == "open_questions" for item in selected)


def test_named_difference_is_exactly_the_question_mark_loss_and_nothing_else() -> None:
    """Same turn, but with an UNRELATED evidence cue ("still") also present:
    that category must survive even though "?" alone is lost -- pinning
    that the difference is scoped to "?" and does not spill onto other
    cues in the same turn."""
    registry = context_roles.load_roles()
    roles = registry.roles.values()
    analysis = working_set_resolve.analyze_turn("does this still work?")

    old = _old_eligible_categories(analysis.text)
    assert old == frozenset({"question", "problem", "decision", "finding"})

    new = working_set_resolve.eligible_categories(analysis, roles)
    assert new == frozenset({"decision", "finding"})
    assert not ({"question", "problem"} & new)


# --------------------------------------------------------------------------- #
# (a) context-roles spec scenarios
# --------------------------------------------------------------------------- #


def test_an_owners_cue_reaches_anchor_evidence(vault: Path) -> None:
    """Scenario: An owner's cue reaches anchor evidence."""
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"active_plans": {"evidence_cues": ["ich plane"]}},
        },
    )
    registry = context_roles.load_roles(vault)
    assert "action" in registry.roles["active_plans"].evidence_categories

    row = working_set_resolve.AnchorFacts(
        anchor_id="a",
        path="a.md",
        ref=None,
        title="Norderweiterung Projekt",
        kind="project",
        lifecycle="active",
        aliases=(),
        terms=("norderweiterung", "projekt"),
        categories=("action",),
        neighbourhood=frozenset(),
    )
    analysis = working_set_resolve.analyze_turn("ich plane die norderweiterung projekt bald")
    eligible = working_set_resolve.eligible_categories(analysis, registry.roles.values())
    candidates = working_set_resolve.candidates_for(analysis, (row,), eligible_categories=eligible)

    assert "category_match" in candidates[0].evidence
    # `hub` (not `plan`/`project`) so `active_plans` can only be selected by
    # its cue, never by `anchor_defaults` -- isolating the "source" this
    # scenario is actually about.
    selected = context_roles.select_roles(registry, anchor_kinds=("hub",), analysis=analysis)
    sources = {item["id"]: item["source"] for item in selected}
    assert sources.get("active_plans") == "turn_cue"


def test_a_shipped_short_cue_selects_a_role_without_counting_as_evidence() -> None:
    """Scenario: A short or embedded cue selects a role without counting
    as evidence (shipped half: `?`)."""
    registry = context_roles.load_roles()
    analysis = working_set_resolve.analyze_turn("does this still work?")
    selected = context_roles.select_roles(registry, anchor_kinds=("hub",), analysis=analysis)
    assert any(item["id"] == "open_questions" for item in selected)
    eligible = working_set_resolve.eligible_categories(analysis, registry.roles.values())
    assert not ({"question", "problem"} & eligible)


def test_an_embedded_override_cue_selects_its_role_but_never_counts_as_evidence(
    vault: Path,
) -> None:
    """Scenario: A short or embedded cue selects a role without counting
    as evidence (override half: `an` inside `plan`)."""
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {
                "active_plans": {"evidence_cues": ["an"], "evidence_categories": ["action"]}
            },
        },
    )
    registry = context_roles.load_roles(vault)
    assert any(finding["code"] == "evidence_cue_too_weak" for finding in registry.findings)

    analysis = working_set_resolve.analyze_turn("plan")
    selected = context_roles.select_roles(registry, anchor_kinds=("hub",), analysis=analysis)
    assert any(item["id"] == "active_plans" for item in selected)
    eligible = working_set_resolve.eligible_categories(analysis, registry.roles.values())
    assert "action" not in eligible


def test_a_punctuation_only_cue_is_a_finding_and_never_evidence(vault: Path) -> None:
    """Scenario: A cue made only of punctuation is never evidence."""
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {
                "constraints": {"evidence_cues": ["???"], "evidence_categories": ["constraint"]}
            },
        },
    )
    registry = context_roles.load_roles(vault)
    assert any(finding["code"] == "evidence_cue_too_weak" for finding in registry.findings)

    for turn in ("what about the budget???", "no punctuation here at all", "???"):
        analysis = working_set_resolve.analyze_turn(turn)
        eligible = working_set_resolve.eligible_categories(analysis, registry.roles.values())
        assert "constraint" not in eligible


def test_an_owner_promotes_a_shipped_selection_cue(vault: Path) -> None:
    """Scenario: An owner promotes a shipped selection cue."""
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {
                "constraints": {"evidence_cues": ["budget"], "evidence_categories": ["constraint"]}
            },
        },
    )
    registry = context_roles.load_roles(vault)
    assert "budget" in registry.roles["constraints"].cues
    assert "budget" in registry.roles["constraints"].evidence_cues

    row = working_set_resolve.AnchorFacts(
        anchor_id="a",
        path="a.md",
        ref=None,
        title="Fleet Rollout Budget",
        kind="project",
        lifecycle="active",
        aliases=(),
        terms=("fleet", "rollout", "budget"),
        categories=("constraint",),
        neighbourhood=frozenset(),
    )
    analysis = working_set_resolve.analyze_turn("what is the fleet rollout budget")
    eligible = working_set_resolve.eligible_categories(analysis, registry.roles.values())
    candidates = working_set_resolve.candidates_for(analysis, (row,), eligible_categories=eligible)
    assert "category_match" in candidates[0].evidence


def test_a_shipped_selection_cue_is_not_evidence_by_default() -> None:
    """Scenario: A shipped selection cue is not an evidence cue."""
    registry = context_roles.load_roles()
    assert "budget" in registry.roles["constraints"].cues
    assert "budget" not in registry.roles["constraints"].evidence_cues

    analysis = working_set_resolve.analyze_turn("what is the budget")
    eligible = working_set_resolve.eligible_categories(analysis, registry.roles.values())
    assert not ({"constraint", "requirement"} & eligible)

    selected = context_roles.select_roles(registry, anchor_kinds=("project",), analysis=analysis)
    assert any(item["id"] == "constraints" for item in selected)


def test_a_role_without_evidence_categories_never_qualifies_an_anchor() -> None:
    """Scenario: A role without evidence categories never qualifies an anchor."""
    registry = context_roles.load_roles()
    assert registry.roles["location"].evidence_categories == frozenset()

    analysis = working_set_resolve.analyze_turn("where is it")
    eligible = working_set_resolve.eligible_categories(analysis, registry.roles.values())
    assert eligible == frozenset()


def test_an_evidence_cue_alone_never_resolves_an_anchor() -> None:
    """Scenario: A cue alone never resolves an anchor."""
    registry = context_roles.load_roles()
    analysis = working_set_resolve.analyze_turn("i'm planning something obscure")
    eligible = working_set_resolve.eligible_categories(analysis, registry.roles.values())
    assert eligible  # "i'm planning" is an evidence cue -> {"action"} eligible

    row = working_set_resolve.AnchorFacts(
        anchor_id="a",
        path="a.md",
        ref=None,
        title="Completely Unrelated Topic",
        kind="hub",
        lifecycle="active",
        aliases=(),
        terms=("completely", "unrelated", "topic"),
        categories=("action",),
        neighbourhood=frozenset(),
    )
    candidates = working_set_resolve.candidates_for(analysis, (row,), eligible_categories=eligible)
    assert candidates == ()
