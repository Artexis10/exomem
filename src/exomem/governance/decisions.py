"""Pure, order-free disclosure-ceiling evaluator.

`decide()` computes `min(org_cap, max(grants, min(standing_rules)))` — the
kernel's only decision surface, and the only place a later enforcement change
will call into. It is a pure function over already-compiled tables: no file
IO, no clock, no randomness, and no rule-priority ordering — every matching
rule participates, and min/max are commutative so the result never depends on
authoring order (design decision D5). Purpose-absence is deterministic: a
purpose-conditioned allowance does not fire when purpose is undeclared, while
an "outside purpose" restriction does.

The standing default is OPEN — an audience no rule names receives full
disclosure — unless a scope the item belongs to sets `default_deny`, which
inverts that default for every audience but the owner. It is a default and
nothing more: it applies only when NO standing rule matched, so it never
lowers an authored ceiling.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .policy import DISCLOSURE_MAX, DISCLOSURE_MIN, Policy, Rule, StandingGrant
from .principal import OWNER_AUDIENCE


@dataclass(frozen=True)
class Decision:
    level: int
    scope_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    #: The scopes whose `default_deny` supplied the standing floor because NO
    #: standing rule matched. Non-empty only when the outcome was decided by
    #: the default rather than by an authored rule, so `explain` can name the
    #: declaring scope without inventing a rule id.
    default_deny_scope_ids: tuple[str, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)
    notice: str | None = None
    bridge: str | None = None
    release_reason: str | None = None
    release_grant_id: str | None = None
    release_strip: tuple[Any, ...] = ()
    release_dependency_digest: str | None = None


def _rule_matches(
    rule: Rule, scope_ids: frozenset[str], audience: str, purpose: str | None
) -> bool:
    if rule.options.get("suspended") is True:
        return False
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

    **Purpose may only narrow, never widen.** For every audience and every
    declared purpose X:

        decide(..., purpose=X).level <= decide(..., purpose=None).level

    enforced by evaluating the lattice twice — once with the declared purpose,
    once as undeclared — and returning whichever produced the lower ceiling.

    This is what makes `purpose` safe to accept from an untrusted client. A
    declared purpose is a self-assertion by the party the rules constrain, so
    the only sound reading is one where lying can never help:

    - An `outside purpose P` restriction can no longer be escaped by declaring
      P. Undeclared fires the restriction (ceiling 0); declaring P does not
      (ceiling 6); `min(6, 0) = 0`, so the restriction holds either way.
    - A purpose-conditioned *grant* can no longer raise a ceiling. Widening
      belongs to identity, not to a claim the caller makes about intent —
      audience-conditioned grants still raise exactly as before.

    `active_grants` defaults to every grant in `policy` — this change has no
    session-scoped grant tracking yet (a later change narrows "active" to a
    live session); the parameter exists so the evaluator itself never needs to
    change shape when that arrives.
    """
    if purpose is None:
        return _decide_at(
            scope_ids, audience=audience, purpose=None, policy=policy,
            active_grants=active_grants,
        )
    declared = _decide_at(
        scope_ids, audience=audience, purpose=purpose, policy=policy,
        active_grants=active_grants,
    )
    undeclared = _decide_at(
        scope_ids, audience=audience, purpose=None, policy=policy,
        active_grants=active_grants,
    )
    # Ties go to the declared branch so its rule_ids/scope_ids explain the
    # outcome when both branches agree on the level.
    return declared if declared.level <= undeclared.level else undeclared


def _decide_at(
    scope_ids: Iterable[str],
    *,
    audience: str,
    purpose: str | None,
    policy: Policy,
    active_grants: Iterable[StandingGrant] | None = None,
) -> Decision:
    """One evaluation of the lattice at a single purpose value."""
    scope_id_set = frozenset(scope_ids)
    grants = policy.grants if active_grants is None else tuple(active_grants)

    matched_rules = [r for r in policy.rules if _rule_matches(r, scope_id_set, audience, purpose)]
    matched_grants = [g for g in grants if _grant_matches(g, scope_id_set, audience)]

    standing = [r for r in matched_rules if r.kind != "org_cap"]
    org_caps = [r for r in matched_rules if r.kind == "org_cap"]

    # The one default this change inverts. It applies ONLY when the standing
    # set is empty. `standing_min` is a `min`, so injecting a synthetic
    # ceiling-0 rule for a declared scope instead would combine with an
    # authored `ceiling: 3` as `min(3, 0) = 0` and silently override the
    # allowance the owner wrote — a declaration must change the default, never
    # a rule's outcome.
    default_deny_scope_ids: tuple[str, ...] = ()
    if standing:
        standing_min = min(r.ceiling for r in standing)
    else:
        # The owner is exempt where the default is CHOSEN, not by a post-hoc
        # override, so a rule that deliberately restricts the owner still
        # takes effect through the branch above.
        if audience != OWNER_AUDIENCE:
            # ANY declared member closes the item: membership is a set, and a
            # declaration that could be defeated by authoring a broad
            # undeclared scope alongside it would be no control at all.
            default_deny_scope_ids = tuple(
                sorted(
                    scope_id
                    for scope_id in scope_id_set
                    if (scope := policy.scopes.get(scope_id)) is not None and scope.default_deny
                )
            )
        standing_min = DISCLOSURE_MIN if default_deny_scope_ids else DISCLOSURE_MAX
    # Unchanged: a grant still raises off the floor, so "unless a grant names
    # them" needs no special case.
    grant_max = max([g.ceiling for g in matched_grants] + [standing_min])
    org_cap_min = min((r.ceiling for r in org_caps), default=DISCLOSURE_MAX)
    level = min(org_cap_min, grant_max)

    options: dict[str, Any] = {}
    for rule in sorted(matched_rules, key=lambda r: r.id):
        options.update(rule.options)

    # Scope-registered L2 constraints are governed micro-bridges.  Resolve
    # them as a set, not by document/rule order: one distinct approved string
    # is deterministic; two different strings are ambiguous and therefore
    # cannot cross the boundary.  Legacy rule-option constraints remain the
    # fallback when no scope record applies.
    scope_constraints = {
        scope.constraint
        for scope_id in scope_id_set
        if (scope := policy.scopes.get(scope_id)) is not None and scope.constraint
    }
    if level >= 2 and len(scope_constraints) == 1:
        options["constraint"] = next(iter(scope_constraints))
        options["constraint_source"] = "scope"
    elif level >= 2 and len(scope_constraints) > 1:
        level = min(level, 1)
        options.pop("constraint", None)
        options["constraint_ambiguous"] = True

    return Decision(
        level=level,
        scope_ids=tuple(sorted(scope_id_set)),
        rule_ids=tuple(sorted(r.id for r in matched_rules)),
        default_deny_scope_ids=default_deny_scope_ids,
        options=options,
        notice=options.get("notice"),
        bridge=options.get("bridge"),
    )
