"""Active projection tuples bind and preactivate exact measurement families."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem.governance import (
    authorization_custody,
    projected_graph,
    projected_retrieval,
    projection_measurement_store,
    projection_runtime,
    projection_store,
    projections,
    schema_v4,
)
from exomem.governance.decisions import Decision
from exomem.governance.policy import Policy, Scope


def _fixture(tmp_path: Path):
    scope = Scope(id="visible", source="scopes/visible.yaml")
    policy = Policy(fingerprint="f" * 64, scopes={scope.id: scope})
    key = projections.ProjectionNamespaceKey(policy.fingerprint, 1, 7)
    variant = projections.build_projection_variant(
        item_identity="Knowledge Base/visible.md",
        content_hash="1" * 64,
        decision=Decision(level=6, scope_ids=(scope.id,)),
        projector_schema_version=key.projector_schema_version,
        full_search_fields={
            "title": "Visible",
            "body": "projected term",
            "media_type": "image",
        },
    )
    assert variant is not None
    items = (
        projection_store.ProjectionItemVariants(
            item_identity=variant.item_identity,
            content_hash=variant.content_hash,
            scope_ids=(scope.id,),
            variants=(variant,),
        ),
    )
    catalog_manifest = projection_store.stage_variant_store(
        tmp_path,
        key=key,
        items=items,
    )
    active = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id="fixture-vault",
        activation_store_id="fixture-store",
        activation_epoch=3,
        activation_state_digest="e" * 64,
        policy_generation_id="fixture-policy",
        policy_fingerprint=key.policy_fingerprint,
        projector_schema_version=key.projector_schema_version,
        catalog_generation=key.catalog_generation,
        projection_namespace_id=key.namespace_id,
    )
    lexical_snapshot = schema_v4.ActivePolicySnapshot(
        active=active,
        policy=policy,
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(key, items),
        projection_namespace_evidence=(
            projection_store.projection_namespace_evidence_bytes(catalog_manifest)
        ),
    )
    namespace = projection_store.bind_active_projection_namespace(
        lexical_snapshot,
        manifest=catalog_manifest,
        items=items,
    )
    families = {
        lane: projection_measurement_store.MeasurementFamilyKey(
            namespace_key=key,
            lane=lane,
            extractor_version="extractor-v1",
            model_version="model-v1",
        )
        for lane in ("vector", "clip", "graph")
    }
    def measurement_key(lane: str) -> projections.MeasurementKey:
        return projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane=lane,
            extractor_version="extractor-v1",
            model_version="model-v1",
        )
    measurements = {
        "vector": (
            projected_retrieval.ProjectionVectorMeasurement(
                measurement_key=measurement_key("vector"),
                vector=(1.0, 0.0),
            ),
        ),
        "clip": (
            projected_retrieval.ProjectionClipMeasurement(
                measurement_key=measurement_key("clip"),
                vector=(0.0, 1.0),
            ),
        ),
        "graph": (
            projected_graph.ProjectionGraphMeasurement(
                measurement_key=measurement_key("graph"),
                edges=(),
            ),
        ),
    }
    manifests = {
        lane: projection_measurement_store.stage_measurement_store(
            tmp_path,
            namespace=namespace,
            family=families[lane],
            measurements=measurements[lane],
        )
        for lane in families
    }
    roots = tuple(
        projection_measurement_store.measurement_root(manifests[lane])
        for lane in ("vector", "clip", "graph")
    )
    snapshot = replace(
        lexical_snapshot,
        projection_namespace_evidence=projection_store.projection_namespace_evidence_bytes(
            catalog_manifest,
            required_measurement_roots=roots,
        ),
    )
    return snapshot, catalog_manifest, items, roots


def test_v2_namespace_evidence_binds_exact_measurement_subkeys(tmp_path: Path) -> None:
    snapshot, catalog_manifest, _items, roots = _fixture(tmp_path)

    evidence = projection_store.namespace_evidence_from_snapshot(snapshot)
    wire = json.loads(snapshot.projection_namespace_evidence)

    assert evidence.manifest == catalog_manifest
    assert evidence.required_measurement_roots == roots
    assert wire["schema"] == "exomem.authorization-projection-namespace-evidence/v2"
    assert wire["required_lane_roots"]["lexical"] == catalog_manifest.rows_digest
    assert set(wire["required_lane_roots"]) == {
        "lexical",
        "vector",
        "clip",
        "graph",
    }
    assert all(root.namespace_key == catalog_manifest.namespace_key for root in roots)


def test_evidence_refuses_duplicate_lane_or_family_namespace(tmp_path: Path) -> None:
    snapshot, catalog_manifest, _items, roots = _fixture(tmp_path)
    del snapshot

    with pytest.raises(projection_store.ProjectionStoreMismatch):
        projection_store.projection_namespace_evidence_bytes(
            catalog_manifest,
            required_measurement_roots=(roots[0], roots[0]),
        )

    with pytest.raises(projection_store.ProjectionStoreMismatch):
        other_key = projections.ProjectionNamespaceKey(
            "a" * 64,
            catalog_manifest.namespace_key.projector_schema_version,
            catalog_manifest.namespace_key.catalog_generation,
        )
        projection_store.projection_namespace_evidence_bytes(
            catalog_manifest,
            required_measurement_roots=(
                replace(
                    roots[0],
                    namespace_key=other_key,
                    family_id=projection_store.projection_measurement_family_id(
                        other_key,
                        lane=roots[0].lane,
                        extractor_version=roots[0].extractor_version,
                        model_version=roots[0].model_version,
                    ),
                ),
            ),
        )


def test_v2_evidence_refuses_a_relabelled_measurement_family(tmp_path: Path) -> None:
    snapshot, _catalog_manifest, _items, _roots = _fixture(tmp_path)
    wire = json.loads(snapshot.projection_namespace_evidence)
    wire["required_lane_roots"]["vector"]["model_version"] = "model-v2"
    drifted = replace(
        snapshot,
        projection_namespace_evidence=projections.canonical_jcs(wire),
    )

    with pytest.raises(projection_store.ProjectionStoreMismatch):
        projection_store.namespace_evidence_from_snapshot(drifted)


def test_startup_preactivates_bound_vector_clip_and_graph_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot, _catalog_manifest, _items, roots = _fixture(tmp_path)
    control = authorization_custody.AuthorizationControlRecord(
        version=1,
        keyring_id="keyring-1",
        cell_id="cell-1",
        logical_vault_id=snapshot.active.logical_vault_id,
        registry_attachment_id="attachment-1",
        attachment_epoch=1,
        governance_enrolled=True,
        activation_store_id=snapshot.active.activation_store_id,
        activation_epoch=snapshot.active.activation_epoch,
        activation_state_digest=snapshot.active.activation_state_digest,
        serving_membership_epoch=1,
        serving_membership_digest="2" * 64,
        issued_at=1,
        expires_at=2_000_000_000,
        signing_key_id="key-1",
    )

    class Connection:
        in_transaction = False

        def execute(self, statement: str) -> None:
            assert statement == "BEGIN"
            self.in_transaction = True

        def close(self) -> None:
            self.in_transaction = False

    monkeypatch.setenv(authorization_custody.KEYRING_FILE_ENV, str(tmp_path / "keyring"))
    monkeypatch.setenv(authorization_custody.CONTROL_FILE_ENV, str(tmp_path / "control"))
    monkeypatch.setattr(
        authorization_custody,
        "load_authorization_custody",
        lambda *_args, **_kwargs: SimpleNamespace(control=control),
    )
    monkeypatch.setattr(
        projection_runtime.store,
        "open_active_governance_read_connection",
        lambda _root: Connection(),
    )
    monkeypatch.setattr(
        schema_v4,
        "load_active_policy",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        schema_v4,
        "load_active_tuple_pointer",
        lambda _connection: snapshot.active,
    )

    projection_runtime._clear_preactivated_runtimes_for_tests()
    runtime = projection_runtime.preactivate_projection_runtime(tmp_path)

    assert isinstance(runtime.vector_index, projected_retrieval.ProjectedVectorIndex)
    assert isinstance(runtime.clip_index, projected_retrieval.ProjectedClipIndex)
    assert isinstance(runtime.graph_index, projected_graph.ProjectedGraphIndex)
    assert runtime.warming_components == ()
    assert tuple(root.lane for root in runtime.measurement_roots) == (
        "vector",
        "clip",
        "graph",
    )
    assert runtime.measurement_roots == roots

    for name in ("load_vector_index", "load_clip_index", "load_graph_index"):
        monkeypatch.setattr(
            projection_measurement_store,
            name,
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("request reloaded a projected measurement family")
            ),
        )
    monkeypatch.setattr(projection_runtime, "_PROJECTED_SERVING_RELEASE_ACCEPTED", True)
    assert projection_runtime.load_active_projection_runtime(tmp_path) is runtime


def test_startup_refuses_a_wrong_required_measurement_root_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot, catalog_manifest, _items, roots = _fixture(tmp_path)
    drifted = replace(roots[0], rows_digest="a" * 64)
    snapshot = replace(
        snapshot,
        projection_namespace_evidence=projection_store.projection_namespace_evidence_bytes(
            catalog_manifest,
            required_measurement_roots=(drifted, *roots[1:]),
        ),
    )
    control = SimpleNamespace(
        governance_enrolled=True,
        cell_id="cell-1",
        logical_vault_id=snapshot.active.logical_vault_id,
        activation_store_id=snapshot.active.activation_store_id,
        activation_epoch=snapshot.active.activation_epoch,
        activation_state_digest=snapshot.active.activation_state_digest,
    )

    class Connection:
        def execute(self, _statement: str) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setenv(authorization_custody.KEYRING_FILE_ENV, str(tmp_path / "keyring"))
    monkeypatch.setenv(authorization_custody.CONTROL_FILE_ENV, str(tmp_path / "control"))
    monkeypatch.setattr(
        authorization_custody,
        "load_authorization_custody",
        lambda *_args, **_kwargs: SimpleNamespace(control=control),
    )
    monkeypatch.setattr(
        projection_runtime.store,
        "open_active_governance_read_connection",
        lambda _root: Connection(),
    )
    monkeypatch.setattr(
        schema_v4,
        "load_active_policy",
        lambda *_args, **_kwargs: snapshot,
    )

    projection_runtime._clear_preactivated_runtimes_for_tests()
    with pytest.raises(
        projection_runtime.ProjectionRuntimeUnavailable,
        match="governed projected retrieval is unavailable",
    ):
        projection_runtime.preactivate_projection_runtime(tmp_path)


def test_v1_lexical_evidence_remains_loadable_but_model_lanes_are_warming(
    tmp_path: Path,
) -> None:
    snapshot, catalog_manifest, items, _roots = _fixture(tmp_path)
    legacy = replace(
        snapshot,
        projection_namespace_evidence=projection_store.projection_namespace_evidence_bytes(
            catalog_manifest
        ),
    )
    namespace = projection_store.bind_active_projection_namespace(
        legacy,
        manifest=catalog_manifest,
        items=items,
    )

    runtime = projection_runtime.ActiveProjectionRuntime(legacy, namespace)

    assert runtime.measurement_roots == ()
    assert runtime.vector_index is None
    assert runtime.clip_index is None
    assert runtime.graph_index is None
    assert runtime.warming_components == ("vector", "clip", "graph")


def test_runtime_refuses_committed_roots_without_their_verified_indexes(
    tmp_path: Path,
) -> None:
    snapshot, catalog_manifest, items, roots = _fixture(tmp_path)
    namespace = projection_store.bind_active_projection_namespace(
        snapshot,
        manifest=catalog_manifest,
        items=items,
    )

    with pytest.raises(projection_runtime.ProjectionRuntimeUnavailable):
        projection_runtime.ActiveProjectionRuntime(
            snapshot,
            namespace,
            measurement_roots=roots,
        )
