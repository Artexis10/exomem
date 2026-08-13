"""Pure order-free disclosure evaluator: truth table, purity, purpose semantics.

`decide()` computes `min(org_cap, max(grants, min(standing_rules)))`, default
OPEN (L6) unless a scope declares `default_deny` — see design decision D5.
min/max are commutative, so the result must never depend on
authoring/insertion order.
"""

from __future__ import annotations

import dataclasses
import itertools
import random
from pathlib import Path

import pytest

from exomem.governance import decisions
from exomem.governance.bridges import StripIdentity
from exomem.governance.decisions import Decision, decide
from exomem.governance.policy import (
    DISCLOSURE_MAX,
    DISCLOSURE_MIN,
    Policy,
    Rule,
    Scope,
    StandingGrant,
)
from exomem.governance.principal import OWNER_AUDIENCE

SCOPE = "scope-1"
OTHER_SCOPE = "scope-2"


def _rule(
    id_,
    ceiling,
    *,
    audience="ext",
    kind="standing",
    purpose=None,
    purpose_condition="matches",
    options=None,
):
    return Rule(
        id=id_,
        source="rules/x.yaml",
        scope_ids=(SCOPE,),
        audience=audience,
        ceiling=ceiling,
        purpose=purpose,
        purpose_condition=purpose_condition,
        kind=kind,
        options=dict(options) if options else {},
    )


def _grant(id_, ceiling, *, audience="ext"):
    return StandingGrant(id=id_, source="grants/x.yaml", scope_ids=(SCOPE,), audience=audience, ceiling=ceiling)


def _scope(id_=SCOPE, *, default_deny=False):
    return Scope(id=id_, source="scopes/x.yaml", default_deny=default_deny)


def _policy(rules=(), grants=(), scopes=()):
    return Policy(
        fingerprint="test",
        scopes={scope.id: scope for scope in scopes},
        rules=tuple(rules),
        grants=tuple(grants),
    )


def _change_contract(name: str) -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "openspec/changes/archive/2026-08-13-add-default-deny-scope-cap"
        / name
    ).read_text(encoding="utf-8")


def test_design_resolves_default_emptiness_per_declaring_scope() -> None:
    design = _change_contract("design.md")

    assert "if standing:" not in design
    assert "named_scope_ids" in design
    assert "default_deny_scope_ids" in design


def test_no_disclosure_scenario_excludes_a_matching_grant() -> None:
    spec = _change_contract("specs/governance-kernel/spec.md")
    scenario = spec.split(
        "#### Scenario: an audience no rule names receives nothing", 1
    )[1].split("#### Scenario:", 1)[0]

    assert "no matching grant applies" in scenario


def test_default_is_full_disclosure_when_nothing_matches() -> None:
    decision = decide([SCOPE], audience="ext", policy=_policy())
    assert decision.level == DISCLOSURE_MAX


def test_most_restrictive_standing_rule_wins() -> None:
    pol = _policy(rules=[_rule("r1", 4), _rule("r2", 2)])
    decision = decide([SCOPE], audience="ext", policy=pol)
    assert decision.level == 2


def test_grant_elevates_over_standing_minimum() -> None:
    pol = _policy(rules=[_rule("r1", 2)], grants=[_grant("g1", 5)])
    decision = decide([SCOPE], audience="ext", policy=pol)
    assert decision.level == 5


def test_org_cap_dominates_even_a_generous_grant() -> None:
    pol = _policy(
        rules=[_rule("r1", 4), _rule("r2", 2), _rule("cap", 3, kind="org_cap")],
        grants=[_grant("g1", 5)],
    )
    decision = decide([SCOPE], audience="ext", policy=pol)
    # min(org_cap=3, max(grant=5, min(standing=[4,2])=2)) = min(3, 5) = 3
    assert decision.level == 3


def test_order_independence_across_shuffled_rules_and_grants() -> None:
    rules = [_rule("r1", 4), _rule("r2", 2), _rule("cap", 3, kind="org_cap")]
    grants = [_grant("g1", 5), _grant("g2", 1)]
    rng = random.Random(1234)
    seen_levels = set()
    for _ in range(20):
        rng.shuffle(rules)
        rng.shuffle(grants)
        pol = _policy(rules=rules, grants=grants)
        seen_levels.add(decide([SCOPE], audience="ext", policy=pol).level)
    assert seen_levels == {min(3, max(5, min(4, 2)))}


def test_permutation_purity_all_orderings_agree() -> None:
    rules = [_rule("a", 3), _rule("b", 1, kind="org_cap")]
    grants = [_grant("g", 6)]
    levels = set()
    for rule_perm in itertools.permutations(rules):
        for grant_perm in itertools.permutations(grants):
            pol = _policy(rules=rule_perm, grants=grant_perm)
            levels.add(decide([SCOPE], audience="ext", policy=pol).level)
    assert len(levels) == 1


def test_same_inputs_same_output_no_io() -> None:
    pol = _policy(rules=[_rule("r1", 3)], grants=[_grant("g1", 5)])
    first = decide([SCOPE], audience="ext", purpose=None, policy=pol)
    second = decide([SCOPE], audience="ext", purpose=None, policy=pol)
    assert first == second


def test_audience_mismatch_does_not_participate() -> None:
    pol = _policy(rules=[_rule("r1", 1, audience="internal")])
    decision = decide([SCOPE], audience="external", policy=pol)
    assert decision.level == DISCLOSURE_MAX


def test_scope_mismatch_does_not_participate() -> None:
    pol = _policy(rules=[_rule("r1", 1)])
    decision = decide(["some-other-scope"], audience="ext", policy=pol)
    assert decision.level == DISCLOSURE_MAX


def test_undeclared_purpose_allowance_does_not_fire() -> None:
    pol = _policy(rules=[_rule("allow", 5, purpose="due-diligence", purpose_condition="matches")])
    decision = decide([SCOPE], audience="ext", purpose=None, policy=pol)
    assert decision.level == DISCLOSURE_MAX  # rule never participates -> default open


def test_declared_matching_purpose_allowance_fires() -> None:
    pol = _policy(rules=[_rule("allow", 5, purpose="due-diligence", purpose_condition="matches")])
    decision = decide([SCOPE], audience="ext", purpose="due-diligence", policy=pol)
    assert decision.level == 5


def test_outside_purpose_restriction_fires_when_undeclared() -> None:
    pol = _policy(rules=[_rule("restrict", 1, purpose="due-diligence", purpose_condition="outside")])
    decision = decide([SCOPE], audience="ext", purpose=None, policy=pol)
    assert decision.level == 1


def test_declaring_the_matching_purpose_cannot_escape_an_outside_restriction() -> None:
    """Purpose may only narrow, never widen.

    The `outside` branch alone would let a caller escape the restriction by
    declaring the very purpose it names (undeclared -> ceiling 1; declared ->
    the rule does not fire -> ceiling 6). Since a declared purpose is a
    self-assertion by the party the rule constrains, the evaluator takes the
    minimum of the declared and undeclared branches, so the restriction holds
    either way and lying can never help.
    """
    pol = _policy(rules=[_rule("restrict", 1, purpose="due-diligence", purpose_condition="outside")])
    undeclared = decide([SCOPE], audience="ext", policy=pol)
    declared = decide([SCOPE], audience="ext", purpose="due-diligence", policy=pol)
    assert undeclared.level == 1
    assert declared.level == 1


def test_purpose_conditioned_grant_cannot_raise_a_ceiling() -> None:
    """Widening belongs to identity, not to a claim about intent."""
    pol = _policy(
        rules=[
            _rule("floor", 1),
            _rule("allow", DISCLOSURE_MAX, purpose="audit", purpose_condition="matches"),
        ]
    )
    assert decide([SCOPE], audience="ext", policy=pol).level == 1
    assert decide([SCOPE], audience="ext", purpose="audit", policy=pol).level == 1


def test_purpose_still_narrows() -> None:
    """The direction that remains available: a purpose-conditioned rule that
    LOWERS the ceiling still applies when that purpose is declared."""
    pol = _policy(
        rules=[_rule("narrow", 0, purpose="marketing", purpose_condition="matches")]
    )
    assert decide([SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MAX
    assert decide([SCOPE], audience="ext", purpose="marketing", policy=pol).level == 0


def test_purpose_never_widens_over_every_fixture_rule() -> None:
    """The mandated invariant, exhaustively over both purpose-conditions."""
    purposes = ("audit", "due-diligence", "marketing", "unrelated")
    for condition in ("matches", "outside"):
        for rule_purpose in purposes:
            for ceiling in range(DISCLOSURE_MAX + 1):
                pol = _policy(
                    rules=[
                        _rule("floor", 3),
                        _rule(
                            "conditioned",
                            ceiling,
                            purpose=rule_purpose,
                            purpose_condition=condition,
                        ),
                    ]
                )
                baseline = decide([SCOPE], audience="ext", policy=pol).level
                for declared in purposes:
                    got = decide(
                        [SCOPE], audience="ext", purpose=declared, policy=pol
                    ).level
                    assert got <= baseline, (
                        f"purpose={declared!r} widened {baseline} -> {got} "
                        f"(rule purpose={rule_purpose!r} condition={condition} "
                        f"ceiling={ceiling})"
                    )


def test_purpose_never_widens_under_random_declarations() -> None:
    """Property test: a client may declare ANY string, including ones no rule
    mentions. None of them may raise the ceiling."""
    rng = random.Random(20260725)
    alphabet = "abcdefghijklmnopqrstuvwxyz-"
    for _ in range(300):
        rule_purpose = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8)))
        pol = _policy(
            rules=[
                _rule("floor", rng.randint(0, DISCLOSURE_MAX)),
                _rule(
                    "conditioned",
                    rng.randint(0, DISCLOSURE_MAX),
                    purpose=rule_purpose,
                    purpose_condition=rng.choice(("matches", "outside")),
                ),
            ],
            grants=[_grant("g", rng.randint(0, DISCLOSURE_MAX))],
        )
        baseline = decide([SCOPE], audience="ext", policy=pol).level
        for _probe in range(4):
            declared = rng.choice(
                [rule_purpose, "".join(rng.choice(alphabet) for _ in range(6))]
            )
            got = decide([SCOPE], audience="ext", purpose=declared, policy=pol).level
            assert got <= baseline


def test_active_grants_override_policy_grants_when_supplied() -> None:
    pol = _policy(rules=[_rule("r1", 2)], grants=[_grant("g1", 5)])
    decision = decide([SCOPE], audience="ext", policy=pol, active_grants=[])
    assert decision.level == 2  # no active grant supplied -> standing min stands


def test_decision_carries_scope_and_rule_ids() -> None:
    pol = _policy(rules=[_rule("r1", 2)])
    decision = decide([SCOPE], audience="ext", policy=pol)
    assert decision.scope_ids == (SCOPE,)
    assert decision.rule_ids == ("r1",)


def test_options_meet_does_not_select_a_rule_id_winner() -> None:
    """Equal contributors must agree; authoring order cannot select content."""
    rule_a = _rule("aaa", 4, options={"notice": "from-aaa"})
    rule_b = _rule("bbb", 4, options={"notice": "from-bbb"})

    first = decide([SCOPE], audience="ext", policy=_policy(rules=(rule_a, rule_b)))
    decision = decide([SCOPE], audience="ext", policy=_policy(rules=(rule_b, rule_a)))

    assert decision == first
    assert decision.level == 0
    assert decision.options == {}
    assert decision.notice is None
    assert decision.bridge is None


# ---------------------------------------------------------------------------
# A scope may deny audiences it does not name (add-default-deny-scope-cap)
#
# The change is ONE default: `standing_min`'s fallback when the standing set is
# empty. Everything below pins that it is a DEFAULT and not a synthetic
# ceiling-0 rule — a synthetic rule would enter the `min` and silently override
# an authored allowance, which is the failure mode design.md records.
# ---------------------------------------------------------------------------


def test_a_declared_scope_denies_an_audience_no_standing_rule_names() -> None:
    """Spec: an audience no rule names receives nothing."""
    pol = _policy(scopes=[_scope(default_deny=True)])
    assert decide([SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN


def test_an_undeclared_scope_keeps_todays_open_default() -> None:
    """Spec: an undeclared scope keeps today's default — the change is opt-in."""
    pol = _policy(scopes=[_scope(default_deny=False)])
    assert decide([SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MAX


def test_a_scope_absent_from_the_policy_table_keeps_the_open_default() -> None:
    """A scope id with no compiled record cannot carry a declaration, so the
    lookup must not be a fail-closed guess about scopes the evaluator was
    handed but the policy never defined."""
    assert decide([SCOPE], audience="ext", policy=_policy()).level == DISCLOSURE_MAX


def test_a_declared_scope_does_not_lower_an_authored_rule() -> None:
    """THE constraint that rules out the synthetic-rule implementation.

    Inject a ceiling-0 standing rule for a declared scope and this reads
    `min(3, 0) == 0` — the declaration silently overriding the allowance the
    owner authored. It must apply only when the standing set is EMPTY."""
    pol = _policy(rules=[_rule("r1", 3)], scopes=[_scope(default_deny=True)])
    assert decide([SCOPE], audience="ext", policy=pol).level == 3


def test_a_declared_scope_does_not_lower_an_authored_full_release_rule() -> None:
    """The same constraint at the top of the lattice: an owner who deliberately
    opens a declared scope to one named audience gets exactly that."""
    pol = _policy(rules=[_rule("r1", DISCLOSURE_MAX)], scopes=[_scope(default_deny=True)])
    assert decide([SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MAX


def test_a_grant_still_raises_above_the_declared_default() -> None:
    """Spec: "unless a grant names them" needs no special case — `grant_max`
    is `max(grants + [standing_min])`, so a grant raises off the floor."""
    pol = _policy(grants=[_grant("g1", 4)], scopes=[_scope(default_deny=True)])
    assert decide([SCOPE], audience="ext", policy=pol).level == 4


def test_a_grant_for_another_audience_does_not_lift_the_declared_default() -> None:
    pol = _policy(grants=[_grant("g1", 4, audience="someone-else")],
                  scopes=[_scope(default_deny=True)])
    assert decide([SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN


def test_an_org_cap_still_lowers_on_a_declared_scope() -> None:
    """Spec: an organisation cap still lowers, unchanged by the declaration."""
    pol = _policy(
        rules=[_rule("r1", 5), _rule("cap", 2, kind="org_cap")],
        scopes=[_scope(default_deny=True)],
    )
    assert decide([SCOPE], audience="ext", policy=pol).level == 2


def test_an_org_cap_alone_leaves_the_standing_set_empty_and_the_default_applies() -> None:
    """An org cap is not a standing rule, so a matching cap must not be read as
    "something matched" and re-open the scope."""
    pol = _policy(rules=[_rule("cap", 5, kind="org_cap")], scopes=[_scope(default_deny=True)])
    assert decide([SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN


def test_the_owner_is_never_subject_to_the_declaration() -> None:
    """Spec: the owner reads a declared scope at full release."""
    pol = _policy(scopes=[_scope(default_deny=True)])
    assert decide([SCOPE], audience=OWNER_AUDIENCE, policy=pol).level == DISCLOSURE_MAX


def test_an_authored_rule_still_restricts_the_owner_on_a_declared_scope() -> None:
    """The exemption is chosen at the DEFAULT site, not applied as a post-hoc
    override, so a rule that deliberately restricts the owner still holds."""
    pol = _policy(rules=[_rule("r1", 1, audience=OWNER_AUDIENCE)],
                  scopes=[_scope(default_deny=True)])
    assert decide([SCOPE], audience=OWNER_AUDIENCE, policy=pol).level == 1


def test_one_declared_scope_denies_across_an_overlapping_undeclared_scope() -> None:
    """Spec: a scope cannot be widened by authoring a broad undeclared scope
    alongside it. Membership is a set; ANY declared member closes the item."""
    pol = _policy(scopes=[_scope(default_deny=True), _scope(OTHER_SCOPE, default_deny=False)])
    assert decide([SCOPE, OTHER_SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN
    # Order-free, like the rest of the lattice.
    assert decide([OTHER_SCOPE, SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN


def test_a_rule_on_the_undeclared_sibling_does_not_unlock_the_declared_scope() -> None:
    """A rule authorising an audience on the UNDECLARED sibling says nothing
    about the declared scope, which still names nobody — so the item stays
    closed. The floor is resolved per declaring scope, not against the item's
    global matched-rule set."""
    rule = Rule(
        id="r1", source="rules/x.yaml", scope_ids=(OTHER_SCOPE,),
        audience="ext", ceiling=3,
    )
    pol = _policy(rules=[rule],
                  scopes=[_scope(default_deny=True), _scope(OTHER_SCOPE, default_deny=False)])
    assert decide([SCOPE, OTHER_SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN
    assert decide([OTHER_SCOPE, SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN


def test_a_rule_on_one_declared_scope_does_not_unlock_another_declared_scope() -> None:
    """Compartment crossover. `S1{default_deny}` with a rule opening it to an
    audience, alongside `S2{default_deny}` naming nobody: an item in BOTH is a
    member of a compartment that audience was never authorised for, so it is
    closed. Authorising someone on one compartment must not unlock another."""
    rule = Rule(
        id="r1", source="rules/x.yaml", scope_ids=(SCOPE,),
        audience="ext", ceiling=DISCLOSURE_MAX,
    )
    pol = _policy(rules=[rule],
                  scopes=[_scope(default_deny=True), _scope(OTHER_SCOPE, default_deny=True)])
    assert decide([SCOPE, OTHER_SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN
    # Commutative: the module promises order-independence.
    assert decide([OTHER_SCOPE, SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN
    # ...and the item stays fully open on the compartment that DID name them.
    assert decide([SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MAX


def test_the_unnamed_declared_scope_is_the_one_named_in_the_explanation() -> None:
    """The explanation must point at the compartment that closed the item, not
    at the one whose rule was overridden."""
    rule = Rule(
        id="r1", source="rules/x.yaml", scope_ids=(SCOPE,),
        audience="ext", ceiling=DISCLOSURE_MAX,
    )
    pol = _policy(rules=[rule],
                  scopes=[_scope(default_deny=True), _scope(OTHER_SCOPE, default_deny=True)])
    decision = decide([SCOPE, OTHER_SCOPE], audience="ext", policy=pol)
    assert decision.default_deny_scope_ids == (OTHER_SCOPE,)


def test_a_suspended_rule_on_a_second_declared_scope_recloses_it() -> None:
    """Suspending the rule that named the audience for one compartment returns
    that compartment to its declared default, even though another compartment's
    rule still matches the item."""
    live = Rule(
        id="r1", source="rules/x.yaml", scope_ids=(SCOPE,), audience="ext", ceiling=4,
    )
    suspended = Rule(
        id="r2", source="rules/x.yaml", scope_ids=(OTHER_SCOPE,), audience="ext",
        ceiling=4, options={"suspended": True},
    )
    pol = _policy(rules=[live, suspended],
                  scopes=[_scope(default_deny=True), _scope(OTHER_SCOPE, default_deny=True)])
    assert decide([SCOPE, OTHER_SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN


def test_an_org_cap_on_a_declared_scope_does_not_count_as_naming_the_audience() -> None:
    """An org cap only lowers; it never authorises. A cap naming the audience
    for a declared scope must not be read as the rule that opens it."""
    cap = Rule(
        id="cap", source="rules/x.yaml", scope_ids=(OTHER_SCOPE,), audience="ext",
        ceiling=5, kind="org_cap",
    )
    rule = Rule(
        id="r1", source="rules/x.yaml", scope_ids=(SCOPE,), audience="ext", ceiling=4,
    )
    pol = _policy(rules=[cap, rule],
                  scopes=[_scope(default_deny=True), _scope(OTHER_SCOPE, default_deny=True)])
    assert decide([SCOPE, OTHER_SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN


def test_a_grant_still_raises_off_a_per_scope_declared_floor() -> None:
    """The floor is still only a floor: a grant naming the audience raises off
    it exactly as it does when the whole standing set was empty."""
    rule = Rule(
        id="r1", source="rules/x.yaml", scope_ids=(SCOPE,), audience="ext", ceiling=4,
    )
    grant = StandingGrant(
        id="g1", source="grants/x.yaml", scope_ids=(OTHER_SCOPE,),
        audience="ext", ceiling=2,
    )
    pol = _policy(rules=[rule], grants=[grant],
                  scopes=[_scope(default_deny=True), _scope(OTHER_SCOPE, default_deny=True)])
    assert decide([SCOPE, OTHER_SCOPE], audience="ext", policy=pol).level == 2


def test_the_owner_is_exempt_from_a_per_scope_declared_floor() -> None:
    """The owner exemption is chosen where the default is chosen, so a second
    declared compartment naming nobody never locks the owner out."""
    rule = Rule(
        id="r1", source="rules/x.yaml", scope_ids=(SCOPE,),
        audience=OWNER_AUDIENCE, ceiling=4,
    )
    pol = _policy(rules=[rule],
                  scopes=[_scope(default_deny=True), _scope(OTHER_SCOPE, default_deny=True)])
    decision = decide([SCOPE, OTHER_SCOPE], audience=OWNER_AUDIENCE, policy=pol)
    assert decision.level == 4
    assert decision.default_deny_scope_ids == ()


def test_a_declared_purpose_still_only_narrows_on_a_declared_scope() -> None:
    """Spec: a declared purpose continues to only narrow. Declaring the purpose
    of a purpose-conditioned allowance must not lift a default denial, because
    purpose is a self-assertion by the party the rules constrain."""
    pol = _policy(rules=[_rule("r1", 5, purpose="research")],
                  scopes=[_scope(default_deny=True)])
    undeclared = decide([SCOPE], audience="ext", policy=pol).level
    declared = decide([SCOPE], audience="ext", purpose="research", policy=pol).level
    assert undeclared == DISCLOSURE_MIN
    assert declared <= undeclared


def test_a_suspended_rule_falls_back_to_the_declared_default() -> None:
    """Suspending the one rule that named an audience must close the scope for
    that audience, not re-open it."""
    pol = _policy(rules=[_rule("r1", 5, options={"suspended": True})],
                  scopes=[_scope(default_deny=True)])
    assert decide([SCOPE], audience="ext", policy=pol).level == DISCLOSURE_MIN


def test_the_decision_names_the_declaring_scope_for_a_default_denial() -> None:
    """`explain` must be able to say WHICH scope closed the item without
    inventing a rule id, so the evaluator records it where it made the choice."""
    pol = _policy(scopes=[_scope(default_deny=True), _scope(OTHER_SCOPE, default_deny=False)])
    decision = decide([SCOPE, OTHER_SCOPE], audience="ext", policy=pol)
    assert decision.default_deny_scope_ids == (SCOPE,)
    assert decision.rule_ids == ()


def test_no_declaring_scope_is_recorded_when_a_rule_decided_the_outcome() -> None:
    pol = _policy(rules=[_rule("r1", 3)], scopes=[_scope(default_deny=True)])
    assert decide([SCOPE], audience="ext", policy=pol).default_deny_scope_ids == ()


def test_no_declaring_scope_is_recorded_for_the_owner() -> None:
    pol = _policy(scopes=[_scope(default_deny=True)])
    assert decide([SCOPE], audience=OWNER_AUDIENCE, policy=pol).default_deny_scope_ids == ()


def test_a_grant_on_a_sibling_scope_cannot_raise_a_declared_scope() -> None:
    """A scope grant raises only the explicitly named compartment."""
    s1 = "01ARZ3NDEKTSV4RRFFQ69G5FA1"
    s2 = "01ARZ3NDEKTSV4RRFFQ69G5FA2"
    both = [s1, s2]
    grant = StandingGrant(
        id="01ARZ3NDEKTSV4RRFFQ69G5FB1",
        source="grants/g.yaml",
        scope_ids=(s2,),
        audience="partner-x",
        ceiling=6,
    )

    declared = Policy(
        fingerprint="p",
        scopes={
            s1: Scope(id=s1, source="scopes/s1.yaml", default_deny=True),
            s2: Scope(id=s2, source="scopes/s2.yaml"),
        },
    )
    assert decide(both, audience="partner-x", policy=declared).level == DISCLOSURE_MIN
    declared_granted = dataclasses.replace(declared, grants=(grant,))
    assert decide(both, audience="partner-x", policy=declared_granted).level == DISCLOSURE_MIN
    assert decide(list(reversed(both)), audience="partner-x", policy=declared_granted).level == DISCLOSURE_MIN


def test_a_grant_covering_each_scope_raises_each_scope_independently() -> None:
    s1, s2 = "scope-a", "scope-b"
    grant = StandingGrant(
        id="g", source="grants/g.yaml", scope_ids=(s1, s2), audience="ext", ceiling=5
    )
    policy = Policy(
        fingerprint="p",
        scopes={
            s1: Scope(id=s1, source="scopes/a.yaml", default_deny=True),
            s2: Scope(id=s2, source="scopes/b.yaml", default_deny=True),
        },
        grants=(grant,),
    )

    assert decide((s1, s2), audience="ext", policy=policy).level == 5
    assert decide((s2, s1), audience="ext", policy=policy).level == 5


def test_item_minimum_keeps_a_sibling_organisation_cap_and_owner_rule() -> None:
    s1, s2 = "scope-a", "scope-b"
    policy = Policy(
        fingerprint="p",
        scopes={
            s1: Scope(id=s1, source="scopes/a.yaml", default_deny=True),
            s2: Scope(id=s2, source="scopes/b.yaml", default_deny=True),
        },
        rules=(
            Rule("owner", "rules/owner.yaml", (s1,), OWNER_AUDIENCE, 5),
            Rule("cap", "rules/cap.yaml", (s2,), OWNER_AUDIENCE, 2, kind="org_cap"),
        ),
    )

    assert decide((s1, s2), audience=OWNER_AUDIENCE, policy=policy).level == 2


def test_equal_purpose_levels_meet_options_without_adding_declared_content() -> None:
    policy = _policy(
        rules=(
            _rule("plain", 3, options={"abstract": "undeclared abstraction"}),
            _rule(
                "declared", 3, purpose="audit", options={"notice": "declared notice"}
            ),
        )
    )

    decision = decide((SCOPE,), audience="ext", purpose="audit", policy=policy)

    assert decision.level == 0
    assert decision.options == {}
    assert decision.notice is None


def test_decision_meet_is_associative_commutative_and_idempotent() -> None:
    contributors = (
        Decision(level=2, options={"constraint": "one"}),
        Decision(level=2, options={"constraint": "two"}),
        Decision(level=2, options={"constraint": "two"}),
    )
    expected = decisions._meet_decisions(contributors)

    for permutation in itertools.permutations(contributors):
        folded = decisions._meet_decisions(permutation)
        left = decisions._meet_decisions(
            (decisions._meet_decisions(permutation[:2]), permutation[2])
        )
        right = decisions._meet_decisions(
            (permutation[0], decisions._meet_decisions(permutation[1:]))
        )
        assert folded == expected
        assert left == expected
        assert right == expected
    assert decisions._meet_decisions((expected, expected)) == expected


@pytest.mark.parametrize(
    ("level", "key", "left", "right", "expected_level"),
    [
        (1, "notice", "one", "two", 0),
        (2, "constraint", "one", "two", 1),
        (3, "abstract", "one", "two", 2),
        (4, "bridge", "one", "two", 3),
    ],
)
def test_same_scope_option_conflicts_lower_before_cross_scope_meet(
    level: int, key: str, left: str, right: str, expected_level: int
) -> None:
    rules = (
        _rule("a", level, options={key: left}),
        _rule("b", level, options={key: right}),
    )

    decision = decide((SCOPE,), audience="ext", policy=_policy(rules=rules))

    assert decision.level == expected_level
    assert key not in decision.options


def test_decision_meet_is_aci_with_absence_and_mixed_levels() -> None:
    contributors = (
        Decision(level=2, options={"constraint": "one"}),
        Decision(level=2, options={}),
        Decision(level=3, options={"abstract": "abstract"}),
    )
    expected = decisions._meet_decisions(contributors)

    assert expected.level == 1
    assert expected.options["constraint_ambiguous"] is True
    for permutation in itertools.permutations(contributors):
        assert decisions._meet_decisions(permutation) == expected
        assert decisions._meet_decisions(
            (decisions._meet_decisions(permutation[:2]), permutation[2])
        ) == expected
        assert decisions._meet_decisions(
            (permutation[0], decisions._meet_decisions(permutation[1:]))
        ) == expected


def test_decision_meet_preserves_equal_release_fields_and_unions_strip() -> None:
    first = Decision(
        level=4,
        release_reason="approved",
        release_grant_id="grant",
        release_dependency_digest="digest",
        release_strip=("a",),
    )
    second = dataclasses.replace(first, release_strip=("b", "a"))

    decision = decisions._meet_decisions((first, second))

    assert decision.release_reason == "approved"
    assert decision.release_grant_id == "grant"
    assert decision.release_dependency_digest == "digest"
    assert decision.release_strip == ("a", "b")


def test_decision_meet_drops_disagreed_or_absent_release_singletons() -> None:
    first = Decision(level=4, release_reason="approved", release_grant_id="grant")
    disagreed = Decision(level=4, release_reason="other", release_grant_id="other")
    absent = Decision(level=4)

    assert decisions._meet_decisions((first, disagreed)).release_reason is None
    assert decisions._meet_decisions((first, disagreed)).release_grant_id is None
    assert decisions._meet_decisions((first, absent)).release_reason is None
    assert decisions._meet_decisions((first, absent)).release_grant_id is None


def test_release_strip_unions_real_identities_across_orders_and_groupings() -> None:
    identities = (
        StripIdentity(path="Knowledge Base/Sources/a.md", ref="ref-a", title="A"),
        StripIdentity(path="Knowledge Base/Sources/b.md", ref="ref-b", title="B"),
        StripIdentity(path="Knowledge Base/Sources/c.md", ref="ref-c", title="C"),
    )
    contributors = (
        Decision(level=4, release_strip=(identities[1], identities[0])),
        Decision(level=4, release_strip=(identities[2], identities[0])),
        Decision(level=4, release_strip=(identities[1],)),
    )
    expected = tuple(sorted(identities, key=lambda item: (item.path, item.ref, item.title)))

    for permutation in itertools.permutations(contributors):
        assert decisions._meet_decisions(permutation).release_strip == expected
        assert decisions._meet_decisions(
            (decisions._meet_decisions(permutation[:2]), permutation[2])
        ).release_strip == expected
        assert decisions._meet_decisions(
            (permutation[0], decisions._meet_decisions(permutation[1:]))
        ).release_strip == expected


def test_decision_retains_deterministic_per_scope_contributions() -> None:
    s1, s2 = "scope-a", "scope-b"
    constraint = {"constraint": "reviewed constraint"}
    standing = Rule(
        "standing", "rules/standing.yaml", (s1,), "ext", 5, options=constraint
    )
    cap = Rule(
        "cap", "rules/cap.yaml", (s1,), "ext", 4, kind="org_cap", options=constraint
    )
    grant = StandingGrant("grant", "grants/grant.yaml", (s2,), "ext", 3)
    policy = Policy(
        fingerprint="p",
        scopes={
            s1: Scope(id=s1, source="scopes/a.yaml", default_deny=True),
            s2: Scope(id=s2, source="scopes/b.yaml", default_deny=True),
        },
        rules=(cap, standing),
        grants=(grant,),
    )

    decision = decide((s2, s1), audience="ext", policy=policy)

    # The scope-local constraint is absent from the sibling, so the complete
    # item meet lowers below L2 even though the numeric scope ceilings are 4/3.
    assert decision.level == 1
    assert [item.scope_id for item in decision.scope_contributions] == [s1, s2]
    first, second = decision.scope_contributions
    assert first.purpose_branch == "neutral"
    assert first.standing_floor == 5
    assert first.default_deny_supplied_floor is False
    assert first.standing_rule_ids == ("standing",)
    assert first.grant_ids == ()
    assert first.grant_ceiling is None
    assert first.grant_contribution == 5
    assert first.organization_cap_ids == ("cap",)
    assert first.organization_cap == 4
    assert first.option_values == (("constraint", "reviewed constraint"),)
    assert first.option_ambiguities == ()
    assert first.final_ceiling == 4
    assert second.standing_floor == 0
    assert second.default_deny_supplied_floor is True
    assert second.standing_rule_ids == ()
    assert second.grant_ids == ("grant",)
    assert second.grant_ceiling == 3
    assert second.grant_contribution == 3
    assert second.organization_cap_ids == ()
    assert second.organization_cap == DISCLOSURE_MAX
    assert second.option_values == ()
    assert second.option_ambiguities == ()
    assert second.final_ceiling == 3


def test_purpose_meet_labels_duplicate_scope_contributions_by_branch() -> None:
    declared = _rule(
        "declared",
        3,
        purpose="audit",
        purpose_condition="matches",
        options={"abstract": "declared abstract"},
    )
    undeclared = _rule(
        "undeclared",
        3,
        purpose="audit",
        purpose_condition="outside",
    )

    forward = decide(
        (SCOPE,),
        audience="ext",
        purpose="audit",
        policy=_policy(rules=(declared, undeclared)),
    )
    reverse = decide(
        (SCOPE,),
        audience="ext",
        purpose="audit",
        policy=_policy(rules=(undeclared, declared)),
    )

    assert forward == reverse
    assert forward.level == 2
    assert [item.scope_id for item in forward.scope_contributions] == [SCOPE, SCOPE]
    assert [item.purpose_branch for item in forward.scope_contributions] == [
        "declared",
        "undeclared",
    ]
    assert [item.final_ceiling for item in forward.scope_contributions] == [3, 3]
    assert forward.scope_contributions[0].option_values == (
        ("abstract", "declared abstract"),
    )
    assert forward.scope_contributions[1].option_values == ()
