"""Request-local projection selection from exact catalog membership."""

from __future__ import annotations

from dataclasses import replace

import pytest

from exomem.governance import projection_authorization, projection_store, projections
from exomem.governance.decisions import Decision
from exomem.governance.policy import Policy, Rule, Scope, StandingGrant


def _key(policy: Policy) -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey(
        policy_fingerprint=policy.fingerprint,
        projector_schema_version=1,
        catalog_generation=21,
    )


def _policy() -> Policy:
    closed = Scope(
        id="closed",
        source="scopes/closed.yaml",
        default_deny=True,
        constraint="Approved readers only",
    )
    open_scope = Scope(id="open", source="scopes/open.yaml")
    return Policy(
        fingerprint="f" * 64,
        scopes={closed.id: closed, open_scope.id: open_scope},
        rules=(
            Rule(
                id="closed-reader",
                source="rules/closed-reader.yaml",
                scope_ids=(closed.id,),
                audience="reader",
                ceiling=2,
                options={
                    "notice": "Restricted",
                    "constraint": "Approved readers only",
                },
            ),
        ),
    )


def _input(
    policy: Policy,
    *,
    identity: str,
    content_hash: str,
    scope_ids: tuple[str, ...],
    body: str,
) -> projection_authorization.ProjectionAuthorizationItem:
    fields = {"title": identity, "body": body}
    variants = projections.enumerate_projection_variants(
        item_identity=identity,
        content_hash=content_hash,
        scope_ids=scope_ids,
        policy=policy,
        projector_schema_version=1,
        full_search_fields=fields,
    )
    return projection_authorization.ProjectionAuthorizationItem(
        item=projection_store.ProjectionItemVariants(
            item_identity=identity,
            content_hash=content_hash,
            variants=variants,
        ),
        scope_ids=scope_ids,
        full_search_fields=fields,
    )


def test_distinct_principals_select_variants_without_entering_persistent_rows() -> None:
    policy = _policy()
    item = _input(
        policy,
        identity="item",
        content_hash="1" * 64,
        scope_ids=("closed",),
        body="full body",
    )

    reader = projection_authorization.build_authorization_map(
        _key(policy),
        (item,),
        policy=policy,
        audience="reader",
    )
    stranger = projection_authorization.build_authorization_map(
        _key(policy),
        (item,),
        policy=policy,
        audience="stranger",
    )

    reader_variant = next(
        variant
        for variant in item.item.variants
        if variant.projection_variant_id == reader.selections[0].projection_variant_id
    )
    assert reader_variant.search_fields == {
        "constraint": "Approved readers only"
    }
    assert stranger.selections[0].projection_variant_id is None
    assert not hasattr(reader, "audience")
    assert not hasattr(reader, "purpose")
    assert not hasattr(reader, "session")
    assert not hasattr(reader, "grants")


def test_exact_scope_session_grant_does_not_cross_over_dual_membership() -> None:
    policy = _policy()
    item = _input(
        policy,
        identity="dual",
        content_hash="2" * 64,
        scope_ids=("closed", "open"),
        body="full dual body",
    )
    grant = StandingGrant(
        id="grant-open",
        source="authorization-session",
        scope_ids=("open",),
        audience="stranger",
        ceiling=6,
    )

    authorization = projection_authorization.build_authorization_map(
        _key(policy),
        (item,),
        policy=policy,
        audience="stranger",
        session_grants=(grant,),
    )

    assert authorization.selections[0].projection_variant_id is None


def test_exact_scope_session_grant_selects_prebuilt_variant() -> None:
    policy = _policy()
    item = _input(
        policy,
        identity="closed-only",
        content_hash="3" * 64,
        scope_ids=("closed",),
        body="full granted body",
    )
    grant = StandingGrant(
        id="grant-closed",
        source="authorization-session",
        scope_ids=("closed",),
        audience="stranger",
        ceiling=6,
    )

    authorization = projection_authorization.build_authorization_map(
        _key(policy),
        (item,),
        policy=policy,
        audience="stranger",
        session_grants=(grant,),
    )
    selected = next(
        variant
        for variant in item.item.variants
        if variant.projection_variant_id
        == authorization.selections[0].projection_variant_id
    )

    assert selected.decision_level == 6
    assert selected.search_fields["body"] == "full granted body"


def test_selector_refuses_stale_or_incomplete_catalog_variant() -> None:
    policy = _policy()
    item = _input(
        policy,
        identity="stale",
        content_hash="4" * 64,
        scope_ids=("closed",),
        body="full body",
    )
    incomplete = replace(
        item,
        item=projection_store.ProjectionItemVariants(
            item_identity=item.item.item_identity,
            content_hash=item.item.content_hash,
            variants=tuple(
                variant
                for variant in item.item.variants
                if variant.decision_level != 2
            ),
        ),
    )

    with pytest.raises(
        projection_authorization.ProjectionAuthorizationUnavailable,
        match="variant",
    ):
        projection_authorization.build_authorization_map(
            _key(policy),
            (incomplete,),
            policy=policy,
            audience="reader",
        )


def test_selector_requires_exact_namespace_and_total_canonical_inputs() -> None:
    policy = _policy()
    first = _input(
        policy,
        identity="first",
        content_hash="5" * 64,
        scope_ids=("closed",),
        body="first",
    )
    with pytest.raises(
        projection_authorization.ProjectionAuthorizationUnavailable,
        match="fingerprint",
    ):
        projection_authorization.build_authorization_map(
            replace(_key(policy), policy_fingerprint="0" * 64),
            (first,),
            policy=policy,
            audience="reader",
        )
    with pytest.raises(
        projection_authorization.ProjectionAuthorizationUnavailable,
        match="unknown scope",
    ):
        projection_authorization.build_authorization_map(
            _key(policy),
            (replace(first, scope_ids=("unknown",)),),
            policy=policy,
            audience="reader",
        )
    unknown_grant = StandingGrant(
        id="unknown-grant",
        source="authorization-session",
        scope_ids=("unknown",),
        audience="reader",
        ceiling=6,
    )
    with pytest.raises(
        projection_authorization.ProjectionAuthorizationUnavailable,
        match="unknown scope",
    ):
        projection_authorization.build_authorization_map(
            _key(policy),
            (first,),
            policy=policy,
            audience="reader",
            session_grants=(unknown_grant,),
        )


def test_bridge_resolution_must_match_the_prebuilt_variant_identity() -> None:
    scope = Scope(id="bridge", source="scopes/bridge.yaml")
    policy = Policy(
        fingerprint="a" * 64,
        scopes={scope.id: scope},
        rules=(
            Rule(
                id="bridge-rule",
                source="rules/bridge.yaml",
                scope_ids=(scope.id,),
                audience="reader",
                ceiling=4,
                options={"bridge": "bridge-alpha"},
            ),
        ),
    )

    def resolve(_audience: str, _purpose: str | None, decision: Decision) -> Decision:
        if decision.level == 4:
            return replace(
                decision,
                bridge_abstraction="Approved bridge",
                release_grant_id="release-alpha",
                release_dependency_digest="b" * 64,
            )
        return decision

    fields = {"body": "hidden"}
    variants = projections.enumerate_projection_variants(
        item_identity="bridge-item",
        content_hash="6" * 64,
        scope_ids=(scope.id,),
        policy=policy,
        projector_schema_version=1,
        full_search_fields=fields,
        resolve_decision=resolve,
    )
    item = projection_authorization.ProjectionAuthorizationItem(
        item=projection_store.ProjectionItemVariants(
            "bridge-item",
            "6" * 64,
            variants,
        ),
        scope_ids=(scope.id,),
        full_search_fields=fields,
    )

    authorization = projection_authorization.build_authorization_map(
        _key(policy),
        (item,),
        policy=policy,
        audience="reader",
        resolve_decision=resolve,
    )
    selected = next(
        variant
        for variant in variants
        if variant.projection_variant_id
        == authorization.selections[0].projection_variant_id
    )
    assert selected.search_fields == {"bridge": "Approved bridge"}
