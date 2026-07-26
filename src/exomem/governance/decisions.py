"""Pure, order-free disclosure-ceiling evaluator.

`decide()` computes `min(org_cap, max(grants, min(standing_rules)))` — the
kernel's only decision surface, and the only place a later enforcement change
will call into. It is a pure function over already-compiled tables: no file
IO, no clock, no randomness, and no rule-priority ordering — every matching
rule participates, and min/max are commutative so the result never depends on
authoring order (design decision D5). Purpose-absence is deterministic: a
purpose-conditioned allowance does not fire when purpose is undeclared, while
an "outside purpose" restriction does.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .policy import DISCLOSURE_MAX, Policy, Rule, StandingGrant


@dataclass(frozen=True)
class Decision:
    level: int
    scope_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)
    notice: str | None = None
    bridge: str | None = None


def _rule_matches(
    rule: Rule, scope_ids: frozenset[str], audience: str, purpose: str | None
) -> bool:
    if rule.audience != audience:
        return False
    if not (set(rule.scope_ids) & scope_ids):
        return False
    if rule.purpose is not None:
        if rule.purpose_condition == "outside":
            # An "outside purpose P" restriction fires when purpose is anything
            # other than P — including undeclared (purpose is None).
            return purpose != rule.purpose
        # A purpose-conditioned allowance only fires when the purpose is
        # explicitly declared and matches; undeclared purpose never fires it.
        return purpose is not None and purpose == rule.purpose
    return True


def _grant_matches(grant: StandingGrant, scope_ids: frozenset[str], audience: str) -> bool:
    return grant.audience == audience and bool(set(grant.scope_ids) & scope_ids)


def decide(
    scope_ids: Iterable[str],
    *,
    audience: str,
    purpose: str | None = None,
    policy: Policy,
    active_grants: Iterable[StandingGrant] | None = None,
) -> Decision:
    """Resolve the disclosure ceiling for one item already resolved to `scope_ids`.

    `active_grants` defaults to every grant in `policy` — this change has no
    session-scoped grant tracking yet (a later change narrows "active" to a
    live session); the parameter exists so the evaluator itself never needs to
    change shape when that arrives.
    """
    scope_id_set = frozenset(scope_ids)
    grants = policy.grants if active_grants is None else tuple(active_grants)

    matched_rules = [r for r in policy.rules if _rule_matches(r, scope_id_set, audience, purpose)]
    matched_grants = [g for g in grants if _grant_matches(g, scope_id_set, audience)]

    standing = [r for r in matched_rules if r.kind != "org_cap"]
    org_caps = [r for r in matched_rules if r.kind == "org_cap"]

    standing_min = min((r.ceiling for r in standing), default=DISCLOSURE_MAX)
    grant_max = max([g.ceiling for g in matched_grants] + [standing_min])
    org_cap_min = min((r.ceiling for r in org_caps), default=DISCLOSURE_MAX)
    level = min(org_cap_min, grant_max)

    options: dict[str, Any] = {}
    for rule in sorted(matched_rules, key=lambda r: r.id):
        options.update(rule.options)

    return Decision(
        level=level,
        scope_ids=tuple(sorted(scope_id_set)),
        rule_ids=tuple(sorted(r.id for r in matched_rules)),
        options=options,
        notice=options.get("notice"),
        bridge=options.get("bridge"),
    )
