"""Request-local projection selection from exact catalog membership."""

from __future__ import annotations

from dataclasses import replace

import pytest
from governance_projection_support import verified_namespace

from exomem.governance import (
    projected_retrieval,
    projection_authorization,
    projection_store,
    projections,
)
from exomem.governance.decisions import Decision
from exomem.governance.policy import Policy, Rule, Scope


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
            scope_ids=scope_ids,
        ),
        scope_ids=scope_ids,
        full_search_fields=fields,
    )


def _grant(
    item: projection_authorization.ProjectionAuthorizationItem,
    policy: Policy,
    *,
    scope_ids: tuple[str, ...],
    audience: str = "stranger",
    ceiling: int = 6,
) -> projection_authorization.VerifiedProjectionGrant:
    return projection_authorization.VerifiedProjectionGrant(
        grant_id=f"grant-{item.item.item_identity}",
        item_identity=item.item.item_identity,
        content_hash=item.item.content_hash,
        policy_fingerprint=policy.fingerprint,
        scope_ids=scope_ids,
        audience=audience,
        purpose=None,
        ceiling=ceiling,
    )


def _namespace(
    policy: Policy,
    *items: projection_authorization.ProjectionAuthorizationItem,
) -> projection_store.VerifiedProjectionNamespace:
    return verified_namespace(_key(policy), tuple(item.item for item in items))


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
        _namespace(policy, item),
        policy=policy,
        audience="reader",
    )
    stranger = projection_authorization.build_authorization_map(
        _namespace(policy, item),
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


def test_selector_uses_bound_catalog_membership_without_raw_search_fields() -> None:
    policy = _policy()
    item = _input(
        policy,
        identity="catalog-member",
        content_hash="9" * 64,
        scope_ids=("closed",),
        body="full body never reopened at request time",
    )

    authorization = projection_authorization.build_authorization_map(
        _namespace(policy, item),
        policy=policy,
        audience="reader",
    )

    selected = next(
        variant
        for variant in item.item.variants
        if variant.projection_variant_id
        == authorization.selections[0].projection_variant_id
    )
    assert selected.search_fields == {"constraint": "Approved readers only"}


def test_exact_scope_session_grant_does_not_cross_over_dual_membership() -> None:
    policy = _policy()
    item = _input(
        policy,
        identity="dual",
        content_hash="2" * 64,
        scope_ids=("closed", "open"),
        body="full dual body",
    )
    grant = _grant(item, policy, scope_ids=("open",))

    authorization = projection_authorization.build_authorization_map(
        _namespace(policy, item),
        policy=policy,
        audience="stranger",
        verified_session_grants=(grant,),
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
    grant = _grant(item, policy, scope_ids=("closed",))

    authorization = projection_authorization.build_authorization_map(
        _namespace(policy, item),
        policy=policy,
        audience="stranger",
        verified_session_grants=(grant,),
    )
    selected = next(
        variant
        for variant in item.item.variants
        if variant.projection_variant_id
        == authorization.selections[0].projection_variant_id
    )

    assert selected.decision_level == 6
    assert selected.search_fields["body"] == "full granted body"


def test_session_grant_is_bound_to_one_exact_projection_item() -> None:
    policy = _policy()
    granted = _input(
        policy,
        identity="granted-item",
        content_hash="7" * 64,
        scope_ids=("closed",),
        body="granted body",
    )
    sibling = _input(
        policy,
        identity="closed-sibling",
        content_hash="8" * 64,
        scope_ids=("closed",),
        body="sibling body",
    )
    grant = _grant(granted, policy, scope_ids=("closed",))

    authorization = projection_authorization.build_authorization_map(
        _namespace(policy, granted, sibling),
        policy=policy,
        audience="stranger",
        verified_session_grants=(grant,),
    )

    by_identity = {
        selection.item_identity: selection for selection in authorization.selections
    }
    assert by_identity["granted-item"].projection_variant_id is not None
    assert by_identity["closed-sibling"].projection_variant_id is None


def test_identical_scope_items_reuse_one_request_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    first = _input(
        policy,
        identity="closed-first",
        content_hash="a" * 64,
        scope_ids=("closed",),
        body="first body",
    )
    second = _input(
        policy,
        identity="closed-second",
        content_hash="b" * 64,
        scope_ids=("closed",),
        body="second body",
    )
    original = projection_authorization.decide
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(projection_authorization, "decide", counted)

    authorization = projection_authorization.build_authorization_map(
        _namespace(policy, first, second),
        policy=policy,
        audience="stranger",
    )

    assert calls == 1
    assert all(
        selection.projection_variant_id is None
        for selection in authorization.selections
    )


def test_preverified_catalog_selects_without_request_variant_rehash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    item = _input(
        policy,
        identity="visible-reader",
        content_hash="c" * 64,
        scope_ids=("closed",),
        body="reader-visible body",
    )
    namespace = _namespace(policy, item)
    catalog = projected_retrieval.ProjectionCatalog(namespace)
    monkeypatch.setattr(
        projections,
        "projection_variant_ids_for_decision",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("request rehashed immutable projection variants")
        ),
    )

    authorization = projection_authorization.build_authorization_map(
        namespace,
        policy=policy,
        audience="reader",
        catalog=catalog,
    )

    assert authorization.selections[0].projection_variant_id is not None


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
            _namespace(policy, incomplete),
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
            verified_namespace(
                replace(_key(policy), policy_fingerprint="0" * 64),
                (first.item,),
            ),
            policy=policy,
            audience="reader",
        )
    with pytest.raises(
        projection_authorization.ProjectionAuthorizationUnavailable,
        match="unknown scope",
    ):
        projection_authorization.build_authorization_map(
            verified_namespace(
                _key(policy),
                (replace(first.item, scope_ids=("unknown",)),),
            ),
            policy=policy,
            audience="reader",
        )
    unknown_grant = projection_authorization.VerifiedProjectionGrant(
        grant_id="unknown-grant",
        item_identity=first.item.item_identity,
        content_hash=first.item.content_hash,
        policy_fingerprint=policy.fingerprint,
        scope_ids=("unknown",),
        audience="reader",
        purpose=None,
        ceiling=6,
    )
    with pytest.raises(
        projection_authorization.ProjectionAuthorizationUnavailable,
        match="unknown scope",
    ):
        projection_authorization.build_authorization_map(
            _namespace(policy, first),
            policy=policy,
            audience="reader",
            verified_session_grants=(unknown_grant,),
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
            (scope.id,),
        ),
        scope_ids=(scope.id,),
        full_search_fields=fields,
    )

    authorization = projection_authorization.build_authorization_map(
        _namespace(policy, item),
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


def test_unavailable_bridge_selects_a_sanitized_lower_decision() -> None:
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
                options={
                    "bridge": "bridge-alpha",
                    "abstract": "Safe fallback",
                },
            ),
        ),
    )
    fields = {"body": "hidden"}
    variants = projections.enumerate_projection_variants(
        item_identity="bridge-item",
        content_hash="6" * 64,
        scope_ids=(scope.id,),
        policy=policy,
        projector_schema_version=1,
        full_search_fields=fields,
    )
    item = projection_authorization.ProjectionAuthorizationItem(
        item=projection_store.ProjectionItemVariants(
            "bridge-item",
            "6" * 64,
            variants,
            (scope.id,),
        ),
        scope_ids=(scope.id,),
        full_search_fields=fields,
    )

    authorization = projection_authorization.build_authorization_map(
        _namespace(policy, item),
        policy=policy,
        audience="reader",
    )

    selection = authorization.selections[0]
    selected = next(
        variant
        for variant in variants
        if variant.projection_variant_id == selection.projection_variant_id
    )
    assert selected.decision_level == 3
    assert selection.decision is not None
    assert selection.decision.level == 3
    assert selection.decision.options == {"abstract": "Safe fallback"}
    assert selection.decision.bridge is None
    assert selection.decision.release_grant_id is None
    assert selection.decision.release_dependency_digest is None
