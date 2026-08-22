"""Build exact request-local selectors over immutable projection catalogs.

Audience, purpose, session, and grants are inputs to this module but never
outputs or persistent keys.  Each request evaluates the conservative decision
meet against the catalog snapshot and selects one already-materialized variant
or L0 for every item.  A missing variant is stale namespace state, never a cue
to project or search raw content on demand.
"""

from __future__ import annotations

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
        object.__setattr__(self, "scope_ids", scopes)
        object.__setattr__(self, "full_search_fields", MappingProxyType(fields))


def build_authorization_map(
    namespace_key: projections.ProjectionNamespaceKey,
    items: tuple[ProjectionAuthorizationItem, ...],
    *,
    policy: Policy,
    audience: str,
    purpose: str | None = None,
    session_grants: tuple[StandingGrant, ...] = (),
    resolve_decision: DecisionResolver | None = None,
) -> projected_retrieval.AuthorizationProjectionMap:
    """Select one prebuilt variant or L0 for every exact catalog artifact."""

    if not isinstance(namespace_key, projections.ProjectionNamespaceKey):
        raise projections.ProjectionCanonicalizationError(
            "authorization namespace key is invalid"
        )
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
    if not isinstance(items, tuple):
        raise projections.ProjectionCanonicalizationError(
            "authorization items must be an immutable tuple"
        )
    if not isinstance(session_grants, tuple) or any(
        not isinstance(grant, StandingGrant) for grant in session_grants
    ):
        raise projections.ProjectionCanonicalizationError(
            "authorization session grants must be an immutable typed tuple"
        )
    if any(
        set(grant.scope_ids) - set(policy.scopes) for grant in session_grants
    ):
        raise ProjectionAuthorizationUnavailable(
            "authorization session grant names an unknown scope"
        )
    if resolve_decision is not None and not callable(resolve_decision):
        raise projections.ProjectionCanonicalizationError(
            "authorization decision resolver is invalid"
        )

    by_identity: dict[str, ProjectionAuthorizationItem] = {}
    for item in items:
        if not isinstance(item, ProjectionAuthorizationItem):
            raise projections.ProjectionCanonicalizationError(
                "authorization item has an invalid type"
            )
        identity = item.item.item_identity
        if identity in by_identity:
            raise projections.ProjectionCanonicalizationError(
                "authorization catalog contains a duplicate item identity"
            )
        unknown_scopes = set(item.scope_ids) - set(policy.scopes)
        if unknown_scopes:
            raise ProjectionAuthorizationUnavailable(
                "authorization item names an unknown scope"
            )
        by_identity[identity] = item

    # Constructing the catalog verifies namespace/schema/variant identity before
    # any principal-dependent decision is evaluated.
    projected_retrieval.ProjectionCatalog(
        namespace_key,
        tuple(item.item for item in by_identity.values()),
    )
    active_grants = (*policy.grants, *session_grants)
    selections: list[projected_retrieval.ProjectionSelection] = []
    for identity in sorted(by_identity, key=lambda value: value.encode("utf-16-be")):
        request_item = by_identity[identity]
        decision = decide(
            request_item.scope_ids,
            audience=principal,
            purpose=purpose,
            policy=policy,
            active_grants=active_grants,
        )
        if resolve_decision is not None:
            decision = resolve_decision(principal, purpose, decision)
            if not isinstance(decision, Decision):
                raise ProjectionAuthorizationUnavailable(
                    "authorization decision resolution is unavailable"
                )
        variant = projections.build_projection_variant(
            item_identity=request_item.item.item_identity,
            content_hash=request_item.item.content_hash,
            decision=decision,
            projector_schema_version=namespace_key.projector_schema_version,
            full_search_fields=request_item.full_search_fields,
        )
        variant_id = None if variant is None else variant.projection_variant_id
        if variant_id is not None and variant_id not in {
            candidate.projection_variant_id
            for candidate in request_item.item.variants
        }:
            raise ProjectionAuthorizationUnavailable(
                "selected projection variant is unavailable from the active namespace"
            )
        selections.append(
            projected_retrieval.ProjectionSelection(
                item_identity=request_item.item.item_identity,
                content_hash=request_item.item.content_hash,
                projection_variant_id=variant_id,
            )
        )
    return projected_retrieval.AuthorizationProjectionMap(
        namespace_key,
        tuple(selections),
    )


__all__ = [
    "ProjectionAuthorizationItem",
    "ProjectionAuthorizationUnavailable",
    "build_authorization_map",
]
