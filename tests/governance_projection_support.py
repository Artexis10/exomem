"""Shared exact active-tuple fixture for projected retrieval unit tests."""

from __future__ import annotations

from exomem.governance import projection_store, projections, schema_v4
from exomem.governance.policy import Policy


def verified_namespace(
    key: projections.ProjectionNamespaceKey,
    items: tuple[projection_store.ProjectionItemVariants, ...],
) -> projection_store.VerifiedProjectionNamespace:
    _material, manifest = projection_store._materialize(key, items)
    active = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id="fixture-vault",
        activation_store_id="fixture-store",
        activation_epoch=1,
        activation_state_digest="e" * 64,
        policy_generation_id="fixture-policy",
        policy_fingerprint=key.policy_fingerprint,
        projector_schema_version=key.projector_schema_version,
        catalog_generation=key.catalog_generation,
        projection_namespace_id=key.namespace_id,
    )
    snapshot = schema_v4.ActivePolicySnapshot(
        active=active,
        policy=Policy(fingerprint=key.policy_fingerprint),
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(key, items),
        projection_namespace_evidence=(
            projection_store.projection_namespace_evidence_bytes(manifest)
        ),
    )
    return projection_store.bind_active_projection_namespace(
        snapshot,
        manifest=manifest,
        items=items,
    )
