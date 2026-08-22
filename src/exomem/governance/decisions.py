"""Pure, order-free disclosure-ceiling evaluator.

`decide()` resolves standing rules, exact-scope grants, and organisation caps
for every member scope independently before taking the conservative item meet.
It is a pure function over already-compiled tables: no file IO, no clock, no
randomness, and no rule-priority ordering. Purpose-absence is deterministic:
a purpose-conditioned allowance does not fire when purpose is undeclared,
while an "outside purpose" restriction does.

The standing default is OPEN — an audience no rule names receives full
disclosure — unless a scope the item belongs to sets `default_deny`, which
inverts that default for every audience but the owner. It is a default and
nothing more: it applies only where no standing rule named the audience FOR
THAT SCOPE, so it never lowers a ceiling authored on the scope it governs, and
a rule authored on one declared scope never suppresses another's declaration.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from .policy import DISCLOSURE_MAX, DISCLOSURE_MIN, Policy, Rule, StandingGrant
from .principal import OWNER_AUDIENCE

_PurposeBranch = Literal["neutral", "declared", "undeclared"]


@dataclass(frozen=True)
class _ScopeContribution:
    """Immutable owner-inspection evidence for one scope-local evaluation."""

    scope_id: str
    purpose_branch: _PurposeBranch
    standing_floor: int
    default_deny_supplied_floor: bool
    standing_rule_ids: tuple[str, ...]
    grant_ids: tuple[str, ...]
    grant_ceiling: int | None
    grant_contribution: int
    organization_cap_ids: tuple[str, ...]
    organization_cap: int
    option_values: tuple[tuple[str, Any], ...]
    option_ambiguities: tuple[str, ...]
    final_ceiling: int


@dataclass(frozen=True)
class Decision:
    level: int
    scope_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    #: The scopes whose `default_deny` supplied the standing floor because no
    #: standing rule named this audience for them. Non-empty only where the
    #: default rather than an authored rule set the floor, so `explain` can
    #: name the declaring scope without inventing a rule id.
    default_deny_scope_ids: tuple[str, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)
    notice: str | None = None
    #: Trusted rendered content from an exact current bridge approval.  The
    #: authored ``bridge`` option above remains an opaque approval id and is
    #: never itself suitable for a wire response.
    bridge_abstraction: str | None = None
    bridge: str | None = None
    release_reason: str | None = None
    release_grant_id: str | None = None
    release_strip: tuple[Any, ...] = ()
    release_dependency_digest: str | None = None
    option_values: dict[str, Any] = field(default_factory=dict, repr=False)
    option_ambiguities: frozenset[str] = field(default_factory=frozenset, repr=False)
    scope_contributions: tuple[_ScopeContribution, ...] = field(
        default_factory=tuple, repr=False
    )


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


def _grant_matches(grant: StandingGrant, scope_id: str, audience: str) -> bool:
    return grant.audience == audience and scope_id in grant.scope_ids


_OPTION_KEYS = frozenset({"notice", "constraint", "abstract", "bridge"})
_OPTION_LEVELS = {"notice": 1, "constraint": 2, "abstract": 3, "bridge": 4}
_MISSING = object()


def _release_strip_key(value: Any) -> tuple[str, ...]:
    value_type = type(value)
    type_key = (value_type.__module__, value_type.__qualname__)
    path = getattr(value, "path", None)
    ref = getattr(value, "ref", None)
    title = getattr(value, "title", None)
    if all(isinstance(item, str) for item in (path, ref, title)):
        return (*type_key, "identity", path, ref, title)
    if isinstance(value, str):
        return (*type_key, "string", value)
    return (*type_key, "repr", repr(value))


def _normalize_release_strip(values: Iterable[Any]) -> tuple[Any, ...]:
    by_key: dict[tuple[str, ...], Any] = {}
    for value in values:
        by_key.setdefault(_release_strip_key(value), value)
    return tuple(by_key[key] for key in sorted(by_key))


def _scope_contribution_key(contribution: _ScopeContribution) -> tuple[Any, ...]:
    return (
        contribution.scope_id,
        contribution.purpose_branch,
        contribution.standing_floor,
        contribution.default_deny_supplied_floor,
        contribution.standing_rule_ids,
        contribution.grant_ids,
        -1 if contribution.grant_ceiling is None else contribution.grant_ceiling,
        contribution.grant_contribution,
        contribution.organization_cap_ids,
        contribution.organization_cap,
        tuple((key, repr(value)) for key, value in contribution.option_values),
        contribution.option_ambiguities,
        contribution.final_ceiling,
    )


def _normalize_scope_contributions(
    contributions: Iterable[_ScopeContribution],
) -> tuple[_ScopeContribution, ...]:
    by_key: dict[tuple[Any, ...], _ScopeContribution] = {}
    for contribution in contributions:
        by_key.setdefault(_scope_contribution_key(contribution), contribution)
    return tuple(by_key[key] for key in sorted(by_key))


def _meet_options(option_sets: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], set[str]]:
    """Meet closed policy options without allowing a sibling to fill absence."""
    contributors = tuple(option_sets)
    if not contributors:
        return {}, set()
    result: dict[str, Any] = {}
    ambiguous: set[str] = set()
    for key in _OPTION_KEYS:
        values = tuple(options.get(key, _MISSING) for options in contributors)
        if all(value is _MISSING for value in values):
            continue
        if any(value is _MISSING or value != values[0] for value in values[1:]):
            ambiguous.add(key)
            continue
        result[key] = values[0]
    return result, ambiguous


def _decision(
    level: int,
    *,
    scope_ids: Iterable[str],
    rule_ids: Iterable[str],
    default_deny_scope_ids: Iterable[str],
    options: dict[str, Any],
    option_ambiguities: Iterable[str] = (),
    option_values: dict[str, Any] | None = None,
    release_reason: str | None = None,
    release_grant_id: str | None = None,
    release_strip: Iterable[Any] = (),
    release_dependency_digest: str | None = None,
    scope_contributions: Iterable[_ScopeContribution] = (),
) -> Decision:
    values = {
        key: value
        for key, value in (option_values if option_values is not None else options).items()
        if key in _OPTION_KEYS
    }
    ambiguities = frozenset(option_ambiguities)
    projected_level = level
    for key in ("bridge", "abstract", "constraint", "notice"):
        if key in ambiguities and projected_level >= _OPTION_LEVELS[key]:
            projected_level = _OPTION_LEVELS[key] - 1
    projected_options = {
        key: value
        for key, value in values.items()
        if _OPTION_LEVELS[key] <= projected_level and key not in ambiguities
    }
    if (
        "constraint" in projected_options
        and options.get("constraint_source") == "scope"
    ):
        projected_options["constraint_source"] = "scope"
    if "constraint" in ambiguities and projected_level == 1:
        projected_options["constraint_ambiguous"] = True
    return Decision(
        level=projected_level,
        scope_ids=tuple(sorted(set(scope_ids))),
        rule_ids=tuple(sorted(set(rule_ids))),
        default_deny_scope_ids=tuple(sorted(set(default_deny_scope_ids))),
        options=projected_options,
        notice=projected_options.get("notice"),
        bridge=projected_options.get("bridge"),
        release_reason=release_reason,
        release_grant_id=release_grant_id,
        release_strip=_normalize_release_strip(release_strip),
        release_dependency_digest=release_dependency_digest,
        option_values=values,
        option_ambiguities=ambiguities,
        scope_contributions=_normalize_scope_contributions(scope_contributions),
    )


def _meet_decisions(decisions: Iterable[Decision]) -> Decision:
    """Conservatively fold complete decisions at a shared disclosure level."""
    contributors = tuple(decisions)
    if not contributors:
        return _decision(
            DISCLOSURE_MAX,
            scope_ids=(),
            rule_ids=(),
            default_deny_scope_ids=(),
            options={},
        )
    level = min(decision.level for decision in contributors)
    values: dict[str, Any] = {}
    ambiguous: set[str] = set()
    for key in _OPTION_KEYS:
        states = []
        for decision in contributors:
            if key in decision.option_ambiguities:
                states.append(_MISSING)
                ambiguous.add(key)
            else:
                source = decision.option_values or decision.options
                states.append(source.get(key, _MISSING))
        if all(state is _MISSING for state in states):
            if key not in ambiguous:
                continue
        elif any(state is _MISSING or state != states[0] for state in states[1:]):
            ambiguous.add(key)
        else:
            values[key] = states[0]
    options = dict(values)
    if values.get("constraint") and all(
        decision.options.get("constraint_source") == "scope"
        for decision in contributors
    ):
        options["constraint_source"] = "scope"

    def singleton(name: str) -> Any:
        candidates = [getattr(decision, name) for decision in contributors]
        return candidates[0] if candidates[0] is not None and all(
            candidate == candidates[0] for candidate in candidates
        ) else None

    return _decision(
        level,
        scope_ids=(scope_id for decision in contributors for scope_id in decision.scope_ids),
        rule_ids=(rule_id for decision in contributors for rule_id in decision.rule_ids),
        default_deny_scope_ids=(
            scope_id
            for decision in contributors
            for scope_id in decision.default_deny_scope_ids
        ),
        options=options,
        option_ambiguities=ambiguous,
        option_values=values,
        release_reason=singleton("release_reason"),
        release_grant_id=singleton("release_grant_id"),
        release_strip=(
            strip for decision in contributors for strip in decision.release_strip
        ),
        release_dependency_digest=singleton("release_dependency_digest"),
        scope_contributions=(
            contribution
            for decision in contributors
            for contribution in decision.scope_contributions
        ),
    )


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
            purpose_branch="neutral",
            active_grants=active_grants,
        )
    declared = _decide_at(
        scope_ids, audience=audience, purpose=purpose, policy=policy,
        purpose_branch="declared",
        active_grants=active_grants,
    )
    undeclared = _decide_at(
        scope_ids, audience=audience, purpose=None, policy=policy,
        purpose_branch="undeclared",
        active_grants=active_grants,
    )
    return _meet_decisions((declared, undeclared))


def _decide_at(
    scope_ids: Iterable[str],
    *,
    audience: str,
    purpose: str | None,
    purpose_branch: _PurposeBranch,
    policy: Policy,
    active_grants: Iterable[StandingGrant] | None = None,
) -> Decision:
    """One evaluation of the lattice at a single purpose value."""
    scope_id_set = frozenset(scope_ids)
    grants = policy.grants if active_grants is None else tuple(active_grants)
    scope_decisions: list[Decision] = []
    for scope_id in sorted(scope_id_set):
        matched_rules = [
            rule
            for rule in policy.rules
            if _rule_matches(rule, frozenset((scope_id,)), audience, purpose)
        ]
        standing = [rule for rule in matched_rules if rule.kind != "org_cap"]
        org_caps = [rule for rule in matched_rules if rule.kind == "org_cap"]
        scope = policy.scopes.get(scope_id)
        default_denied = (
            audience != OWNER_AUDIENCE
            and not standing
            and scope is not None
            and scope.default_deny
        )
        standing_level = min(
            (rule.ceiling for rule in standing),
            default=DISCLOSURE_MIN if default_denied else DISCLOSURE_MAX,
        )
        matched_grants = [
            grant for grant in grants if _grant_matches(grant, scope_id, audience)
        ]
        grant_ceiling = max(
            (grant.ceiling for grant in matched_grants), default=None
        )
        grant_level = max(
            standing_level,
            grant_ceiling if grant_ceiling is not None else standing_level,
        )
        organization_cap = min(
            (rule.ceiling for rule in org_caps), default=DISCLOSURE_MAX
        )
        level = min(grant_level, organization_cap)
        options, ambiguous = _meet_options(rule.options for rule in matched_rules)
        if level >= 2 and scope is not None and scope.constraint:
            options["constraint"] = scope.constraint
            options["constraint_source"] = "scope"
        scope_decision = _decision(
            level,
            scope_ids=(scope_id,),
            rule_ids=(rule.id for rule in matched_rules),
            default_deny_scope_ids=(scope_id,) if default_denied else (),
            options=options,
            option_ambiguities=ambiguous,
        )
        contribution = _ScopeContribution(
            scope_id=scope_id,
            purpose_branch=purpose_branch,
            standing_floor=standing_level,
            default_deny_supplied_floor=default_denied,
            standing_rule_ids=tuple(sorted(rule.id for rule in standing)),
            grant_ids=tuple(sorted(grant.id for grant in matched_grants)),
            grant_ceiling=grant_ceiling,
            grant_contribution=grant_level,
            organization_cap_ids=tuple(sorted(rule.id for rule in org_caps)),
            organization_cap=organization_cap,
            option_values=tuple(sorted(scope_decision.option_values.items())),
            option_ambiguities=tuple(sorted(scope_decision.option_ambiguities)),
            final_ceiling=scope_decision.level,
        )
        scope_decisions.append(
            replace(scope_decision, scope_contributions=(contribution,))
        )
    return _meet_decisions(scope_decisions)
