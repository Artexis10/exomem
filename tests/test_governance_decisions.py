"""Pure order-free disclosure evaluator: truth table, purity, purpose semantics.

`decide()` computes `min(org_cap, max(grants, min(standing_rules)))`, default
OPEN (L6) — see design decision D5. min/max are commutative, so the result
must never depend on authoring/insertion order.
"""

from __future__ import annotations

import itertools
import random

from exomem.governance.decisions import decide
from exomem.governance.policy import (
    DISCLOSURE_MAX,
    Policy,
    Rule,
    StandingGrant,
)

SCOPE = "scope-1"


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


def _policy(rules=(), grants=()):
    return Policy(fingerprint="test", scopes={}, rules=tuple(rules), grants=tuple(grants))


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


def test_outside_purpose_restriction_does_not_fire_when_purpose_matches() -> None:
    pol = _policy(rules=[_rule("restrict", 1, purpose="due-diligence", purpose_condition="outside")])
    decision = decide([SCOPE], audience="ext", purpose="due-diligence", policy=pol)
    assert decision.level == DISCLOSURE_MAX


def test_active_grants_override_policy_grants_when_supplied() -> None:
    pol = _policy(rules=[_rule("r1", 2)], grants=[_grant("g1", 5)])
    decision = decide([SCOPE], audience="ext", policy=pol, active_grants=[])
    assert decision.level == 2  # no active grant supplied -> standing min stands


def test_decision_carries_scope_and_rule_ids() -> None:
    pol = _policy(rules=[_rule("r1", 2)])
    decision = decide([SCOPE], audience="ext", policy=pol)
    assert decision.scope_ids == (SCOPE,)
    assert decision.rule_ids == ("r1",)


def test_options_merge_pins_lexical_rule_id_order() -> None:
    """Pins the exact (documented) merge contract for overlapping option keys
    across matching rules, so `add-release-gate` inherits a tested behavior
    rather than an accidental one: rules merge in ascending `id` order, so
    for a shared key the lexically GREATEST rule id wins."""
    rule_a = _rule("aaa", 4, options={"notice": "from-aaa", "bridge": "bridge-a", "only_a": 1})
    rule_b = _rule("bbb", 4, options={"notice": "from-bbb", "only_b": 2})
    # Insertion order deliberately reversed — the merge must not depend on it.
    pol = _policy(rules=[rule_b, rule_a])

    decision = decide([SCOPE], audience="ext", policy=pol)

    assert decision.options == {
        "notice": "from-bbb",  # "bbb" > "aaa" lexically -> bbb wins the shared key
        "bridge": "bridge-a",
        "only_a": 1,
        "only_b": 2,
    }
    assert decision.notice == "from-bbb"
    assert decision.bridge == "bridge-a"
