"""Serving projections bind to one verified active governance tuple."""

from __future__ import annotations

from dataclasses import replace

import pytest

from exomem.governance import (
    projected_retrieval,
    projection_store,
    projections,
    schema_v4,
)
from exomem.governance.decisions import Decision
from exomem.governance.policy import Policy


def _key(*, generation: int = 7) -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey(
        policy_fingerprint="a" * 64,
        projector_schema_version=1,
        catalog_generation=generation,
    )


def _items() -> tuple[projection_store.ProjectionItemVariants, ...]:
    variant = projections.build_projection_variant(
        item_identity="item-1",
        content_hash="b" * 64,
        decision=Decision(level=6),
        projector_schema_version=1,
        full_search_fields={"body": "permitted body"},
    )
    assert variant is not None
    return (
        projection_store.ProjectionItemVariants(
            item_identity="item-1",
            content_hash="b" * 64,
            variants=(variant,),
        ),
    )


def _snapshot(
    key: projections.ProjectionNamespaceKey,
    items: tuple[projection_store.ProjectionItemVariants, ...],
    manifest: projection_store.VariantStoreManifest,
) -> schema_v4.ActivePolicySnapshot:
    active = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id="vault-1",
        activation_store_id="store-1",
        activation_epoch=3,
        activation_state_digest="c" * 64,
        policy_generation_id="policy-1",
        policy_fingerprint=key.policy_fingerprint,
        projector_schema_version=key.projector_schema_version,
        catalog_generation=key.catalog_generation,
        projection_namespace_id=key.namespace_id,
    )
    return schema_v4.ActivePolicySnapshot(
        active=active,
        policy=Policy(fingerprint=key.policy_fingerprint),
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(key, items),
        projection_namespace_evidence=(
            projection_store.projection_namespace_evidence_bytes(manifest)
        ),
    )


def test_serving_namespace_binds_exact_active_tuple_catalog_and_rows() -> None:
    key = _key()
    items = _items()
    _material, manifest = projection_store._materialize(key, items)
    snapshot = _snapshot(key, items, manifest)

    bound = projection_store.bind_active_projection_namespace(
        snapshot,
        manifest=manifest,
        items=items,
    )

    assert bound.namespace_key == key
    assert bound.active_state_digest == snapshot.active.activation_state_digest
    assert bound.manifest == manifest
    assert bound.items == items


def test_generation_one_rows_cannot_be_relabelled_as_generation_two() -> None:
    old_key = _key(generation=1)
    old_items = _items()
    _material, old_manifest = projection_store._materialize(old_key, old_items)
    old_snapshot = _snapshot(old_key, old_items, old_manifest)
    new_active = replace(
        old_snapshot.active,
        activation_epoch=4,
        activation_state_digest="d" * 64,
        catalog_generation=2,
        projection_namespace_id=_key(generation=2).namespace_id,
    )
    relabelled = replace(old_snapshot, active=new_active)

    with pytest.raises(projection_store.ProjectionStoreMismatch, match="active tuple"):
        projection_store.bind_active_projection_namespace(
            relabelled,
            manifest=old_manifest,
            items=old_items,
        )


def test_serving_namespace_refuses_catalog_or_evidence_drift() -> None:
    key = _key()
    items = _items()
    _material, manifest = projection_store._materialize(key, items)
    snapshot = _snapshot(key, items, manifest)

    for drifted in (
        replace(snapshot, catalog_descriptor=b"{}"),
        replace(snapshot, projection_namespace_evidence=b"{}"),
    ):
        with pytest.raises(projection_store.ProjectionStoreMismatch):
            projection_store.bind_active_projection_namespace(
                drifted,
                manifest=manifest,
                items=items,
            )


def test_projected_index_requires_the_verified_namespace_binding() -> None:
    key = _key()
    items = _items()
    _material, manifest = projection_store._materialize(key, items)
    bound = projection_store.bind_active_projection_namespace(
        _snapshot(key, items, manifest),
        manifest=manifest,
        items=items,
    )

    index = projected_retrieval.ProjectedLexicalIndex(bound)
    assert index.namespace_key == key

    with pytest.raises(
        (TypeError, projected_retrieval.ProjectedRetrievalUnavailable),
    ):
        projected_retrieval.ProjectedLexicalIndex(key, items)
