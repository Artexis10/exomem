"""Build exact request-local selectors over immutable projection catalogs.

Audience, purpose, session, and grants are inputs to this module but never
outputs or persistent keys.  Each request evaluates the conservative decision
meet against the catalog snapshot and selects one already-materialized variant
or L0 for every item.  A missing variant is stale namespace state, never a cue
to project or search raw content on demand.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from . import projected_retrieval, projection_store, projections
from .decisions import Decision, decide
from .policy import Policy, StandingGrant

DecisionResolver = Callable[[str, str | None, Decision], Decision]


class ProjectionAuthorizationUnavailable(
    projected_retrieval.ProjectedRetrievalUnavailable
):
    """The active request cannot select an exact complete namespace variant."""


def _text(value: object, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise projections.ProjectionCanonicalizationError(
            f"{name} must be bounded non-empty text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise projections.ProjectionCanonicalizationError(
            f"{name} contains an invalid Unicode scalar"
        ) from error
    return value


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise projections.ProjectionCanonicalizationError(
            f"{name} must be one lowercase SHA-256 digest"
        )
    return value


def _effective_decision_for_variant(
    decision: Decision,
    variant: projections.ProjectionVariant,
) -> Decision:
    """Bind public decision metadata to the exact selected fixed variant."""

    if variant.decision_level == decision.level:
        return decision
    if variant.decision_level > decision.level:
        raise ProjectionAuthorizationUnavailable(
            "selected projection variant exceeds the authorization decision"
        )
    try:
        value = json.loads(variant.value_jcs)
        options = value["options"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise ProjectionAuthorizationUnavailable(
            "selected projection variant is unavailable from the active namespace"
        ) from None
    if not isinstance(options, dict):
        raise ProjectionAuthorizationUnavailable(
            "selected projection variant is unavailable from the active namespace"
        )
    level = variant.decision_level
    notice = options.get("notice")
    return Decision(
        level=level,
        scope_ids=decision.scope_ids,
        rule_ids=decision.rule_ids,
        default_deny_scope_ids=decision.default_deny_scope_ids,
        options=dict(options),
        notice=notice if isinstance(notice, str) else None,
        bridge_abstraction=(
            variant.search_fields.get("bridge") if level == 4 else None
        ),
        bridge=decision.bridge if level == 4 else None,
        release_reason=decision.release_reason,
        release_grant_id=decision.release_grant_id if level == 4 else None,
        release_strip=decision.release_strip,
        release_dependency_digest=(
            decision.release_dependency_digest if level == 4 else None
        ),
    )


@dataclass(frozen=True, slots=True)
class VerifiedProjectionGrant:
    """One already-verified session grant bound to one catalog snapshot row."""

    grant_id: str
    item_identity: str
    content_hash: str
    policy_fingerprint: str
    scope_ids: tuple[str, ...]
    audience: str
    purpose: str | None
    ceiling: int

    def __post_init__(self) -> None:
        _text(self.grant_id, "projection grant id")
        _text(self.item_identity, "projection grant item identity")
        _digest(self.content_hash, "projection grant content hash")
        _digest(self.policy_fingerprint, "projection grant policy fingerprint")
        if not isinstance(self.scope_ids, tuple):
            raise projections.ProjectionCanonicalizationError(
                "projection grant scope ids must be an immutable tuple"
            )
        scopes = tuple(
            sorted(
                {_text(scope, "projection grant scope id") for scope in self.scope_ids},
                key=lambda value: value.encode("utf-16-be"),
            )
        )
        if not scopes or len(scopes) != len(self.scope_ids):
            raise projections.ProjectionCanonicalizationError(
                "projection grant scope ids must be a non-empty canonical set"
            )
        _text(self.audience, "projection grant audience", maximum=1024)
        if self.purpose is not None:
            _text(self.purpose, "projection grant purpose", maximum=1024)
        if type(self.ceiling) is not int or not 0 <= self.ceiling <= 6:
            raise projections.ProjectionCanonicalizationError(
                "projection grant ceiling must be L0-L6"
            )
        object.__setattr__(self, "scope_ids", scopes)


@dataclass(frozen=True, slots=True)
class ProjectionAuthorizationItem:
    """Exact membership and search-field inputs for one catalog item."""

    item: projection_store.ProjectionItemVariants
    scope_ids: tuple[str, ...]
    full_search_fields: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.item, projection_store.ProjectionItemVariants):
            raise projections.ProjectionCanonicalizationError(
                "authorization item has an invalid catalog row"
            )
        if not isinstance(self.scope_ids, tuple):
            raise projections.ProjectionCanonicalizationError(
                "authorization item scope ids must be an immutable tuple"
            )
        scopes = tuple(
            sorted(
                {_text(scope, "scope id") for scope in self.scope_ids},
                key=lambda value: value.encode("utf-16-be"),
            )
        )
        if len(scopes) != len(self.scope_ids):
            raise projections.ProjectionCanonicalizationError(
                "authorization item contains duplicate scope ids"
            )
        if not isinstance(self.full_search_fields, Mapping):
            raise projections.ProjectionCanonicalizationError(
                "authorization item search fields must be a mapping"
            )
        fields: dict[str, str] = {}
        for key, value in self.full_search_fields.items():
            fields[_text(key, "search field name", maximum=128)] = _text(
                value,
                f"search field {key}",
                maximum=1_048_576,
            )
        if not fields:
            raise projections.ProjectionCanonicalizationError(
                "authorization item search fields must be non-empty"
            )
        projections.require_supported_capacity(
            searchable_bytes=sum(
                len(value.encode("utf-8")) for value in fields.values()
            )
        )
        object.__setattr__(self, "scope_ids", scopes)
        object.__setattr__(self, "full_search_fields", MappingProxyType(fields))


def build_authorization_map(
    namespace: projection_store.VerifiedProjectionNamespace,
    *,
    policy: Policy,
    audience: str,
    purpose: str | None = None,
    verified_session_grants: tuple[VerifiedProjectionGrant, ...] = (),
    resolve_decision: DecisionResolver | None = None,
    catalog: projected_retrieval.ProjectionCatalog | None = None,
) -> projected_retrieval.AuthorizationProjectionMap:
    """Select one prebuilt variant or L0 for every exact catalog artifact."""

    if not isinstance(namespace, projection_store.VerifiedProjectionNamespace):
        raise ProjectionAuthorizationUnavailable(
            "authorization requires a verified active projection namespace"
        )
    namespace_key = namespace.namespace_key
    if not isinstance(policy, Policy) or policy.empty or policy.blocked:
        raise ProjectionAuthorizationUnavailable(
            "authorization policy is not an active compiled generation"
        )
    if namespace_key.policy_fingerprint != policy.fingerprint:
        raise ProjectionAuthorizationUnavailable(
            "authorization policy fingerprint does not match namespace"
        )
    principal = _text(audience, "authorization audience", maximum=1024)
    if purpose is not None:
        _text(purpose, "authorization purpose", maximum=1024)
    if not isinstance(verified_session_grants, tuple) or any(
        not isinstance(grant, VerifiedProjectionGrant)
        for grant in verified_session_grants
    ):
        raise projections.ProjectionCanonicalizationError(
            "authorization projection grants must be an immutable typed tuple"
        )
    if resolve_decision is not None and not callable(resolve_decision):
        raise projections.ProjectionCanonicalizationError(
            "authorization decision resolver is invalid"
        )

    active_catalog = (
        projected_retrieval.ProjectionCatalog(namespace)
        if catalog is None
        else catalog
    )
    if (
        not isinstance(active_catalog, projected_retrieval.ProjectionCatalog)
        or active_catalog.namespace is not namespace
    ):
        raise ProjectionAuthorizationUnavailable(
            "authorization catalog does not match the active namespace"
        )
    by_identity = active_catalog.items
    for item in by_identity.values():
        unknown_scopes = set(item.scope_ids) - set(policy.scopes)
        if unknown_scopes:
            raise ProjectionAuthorizationUnavailable(
                "authorization item names an unknown scope"
            )
    grants_by_item: dict[str, list[VerifiedProjectionGrant]] = {}
    for grant in verified_session_grants:
        request_item = by_identity.get(grant.item_identity)
        if request_item is None:
            raise ProjectionAuthorizationUnavailable(
                "authorization projection grant names an unknown item"
            )
        if set(grant.scope_ids) - set(policy.scopes):
            raise ProjectionAuthorizationUnavailable(
                "authorization projection grant names an unknown scope"
            )
        if (
            grant.content_hash != request_item.content_hash
            or grant.policy_fingerprint != policy.fingerprint
            or grant.audience != principal
            or grant.purpose != purpose
            or set(grant.scope_ids) - set(request_item.scope_ids)
        ):
            raise ProjectionAuthorizationUnavailable(
                "authorization projection grant does not match the item snapshot"
            )
        grants_by_item.setdefault(grant.item_identity, []).append(grant)

    selections: list[projected_retrieval.ProjectionSelection] = []
    decision_cache: dict[
        tuple[tuple[str, ...], tuple[StandingGrant, ...]],
        Decision,
    ] = {}
    descriptor_cache: dict[
        tuple[tuple[str, ...], tuple[StandingGrant, ...]],
        tuple[bytes, ...],
    ] = {}
    for identity in sorted(by_identity, key=lambda value: value.encode("utf-16-be")):
        request_item = by_identity[identity]
        item_grants = tuple(
            StandingGrant(
                id=grant.grant_id,
                source="authorization-session",
                scope_ids=grant.scope_ids,
                audience=grant.audience,
                ceiling=grant.ceiling,
            )
            for grant in grants_by_item.get(identity, ())
        )
        decision_key = (request_item.scope_ids, item_grants)
        decision = decision_cache.get(decision_key)
        if decision is None:
            decision = decide(
                request_item.scope_ids,
                audience=principal,
                purpose=purpose,
                policy=policy,
                active_grants=(*policy.grants, *item_grants),
            )
            if resolve_decision is not None:
                decision = resolve_decision(principal, purpose, decision)
                if not isinstance(decision, Decision):
                    raise ProjectionAuthorizationUnavailable(
                        "authorization decision resolution is unavailable"
                    )
            decision_cache[decision_key] = decision
        if decision.level == 0:
            selections.append(
                projected_retrieval.ProjectionSelection(
                    item_identity=request_item.item_identity,
                    content_hash=request_item.content_hash,
                    projection_variant_id=None,
                    decision=decision,
                )
            )
            continue
        candidate_descriptors = descriptor_cache.get(decision_key)
        if candidate_descriptors is None:
            candidate_descriptors = (
                projections.projection_variant_descriptors_for_decision(
                    decision
                )
            )
            descriptor_cache[decision_key] = candidate_descriptors
        variant_id: str | None = None
        selected_variant: projections.ProjectionVariant | None = None
        for ordinal, descriptor in enumerate(candidate_descriptors):
            candidate = active_catalog.variant_for_descriptor(
                request_item.item_identity,
                descriptor,
            )
            if candidate is not None:
                variant_id = candidate.projection_variant_id
                selected_variant = candidate
                break
            # L5 can legitimately lower only when the fixed source body was
            # absent at namespace construction. Every other viable candidate
            # is exact; a missing row is stale/incomplete authority.
            if not (
                decision.level == 5
                and ordinal == 0
                and not any(
                    "body" in variant.search_fields
                    for variant in request_item.variants
                )
            ):
                raise ProjectionAuthorizationUnavailable(
                    "selected projection variant is unavailable from the active namespace"
                )
        selections.append(
            projected_retrieval.ProjectionSelection(
                item_identity=request_item.item_identity,
                content_hash=request_item.content_hash,
                projection_variant_id=variant_id,
                decision=(
                    _effective_decision_for_variant(decision, selected_variant)
                    if selected_variant is not None
                    else decision
                ),
            )
        )
    return projected_retrieval.AuthorizationProjectionMap(
        namespace_key,
        tuple(selections),
    )


__all__ = [
    "ProjectionAuthorizationItem",
    "ProjectionAuthorizationUnavailable",
    "VerifiedProjectionGrant",
    "build_authorization_map",
]
