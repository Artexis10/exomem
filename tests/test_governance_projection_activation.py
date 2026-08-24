"""Startup-only activation for the governed projection runtime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from governance_projection_support import verified_namespace

from exomem.governance import (
    authorization_custody,
    projection_runtime,
    projection_store,
    projections,
    schema_v4,
)
from exomem.governance.decisions import Decision
from exomem.governance.policy import Policy, Scope


def _active_runtime():
    scope = Scope(id="visible", source="scopes/visible.yaml")
    policy = Policy(fingerprint="f" * 64, scopes={scope.id: scope})
    key = projections.ProjectionNamespaceKey(policy.fingerprint, 1, 7)
    variant = projections.build_projection_variant(
        item_identity="Knowledge Base/visible.md",
        content_hash="1" * 64,
        decision=Decision(level=6, scope_ids=(scope.id,)),
        projector_schema_version=key.projector_schema_version,
        full_search_fields={"title": "Visible", "body": "projected term"},
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
    namespace = verified_namespace(key, items)
    snapshot = schema_v4.ActivePolicySnapshot(
        active=schema_v4.VerifiedActiveGovernanceState(
            logical_vault_id="fixture-vault",
            activation_store_id="fixture-store",
            activation_epoch=3,
            activation_state_digest=namespace.active_state_digest,
            policy_generation_id="fixture-policy",
            policy_fingerprint=key.policy_fingerprint,
            projector_schema_version=key.projector_schema_version,
            catalog_generation=key.catalog_generation,
            projection_namespace_id=key.namespace_id,
        ),
        policy=policy,
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(key, items),
        projection_namespace_evidence=projection_store.projection_namespace_evidence_bytes(
            namespace.manifest
        ),
    )
    runtime = projection_runtime.ActiveProjectionRuntime(snapshot, namespace)
    return runtime, items


def test_startup_preactivates_one_digest_and_serves_only_that_revalidated_runtime(
    monkeypatch,
    tmp_path,
) -> None:
    assert hasattr(projection_runtime, "preactivate_projection_runtime")
    assert hasattr(projection_runtime, "_clear_preactivated_runtimes_for_tests")

    runtime, items = _active_runtime()
    control = authorization_custody.AuthorizationControlRecord(
        version=1,
        keyring_id="keyring-1",
        cell_id="cell-1",
        logical_vault_id=runtime.snapshot.active.logical_vault_id,
        registry_attachment_id="attachment-1",
        attachment_epoch=1,
        governance_enrolled=True,
        activation_store_id=runtime.snapshot.active.activation_store_id,
        activation_epoch=runtime.snapshot.active.activation_epoch,
        activation_state_digest=runtime.snapshot.active.activation_state_digest,
        serving_membership_epoch=1,
        serving_membership_digest="2" * 64,
        issued_at=1,
        expires_at=2_000_000_000,
        signing_key_id="key-1",
    )
    events: list[str] = []
    current_control = [control]

    class Connection:
        in_transaction = False

        def execute(self, statement: str) -> None:
            assert statement == "BEGIN"
            assert not self.in_transaction
            self.in_transaction = True
            events.append("begin")

        def close(self) -> None:
            self.in_transaction = False

    connection = Connection()

    monkeypatch.setenv(authorization_custody.KEYRING_FILE_ENV, str(tmp_path / "keyring"))
    monkeypatch.setenv(authorization_custody.CONTROL_FILE_ENV, str(tmp_path / "control"))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    monkeypatch.setattr(
        authorization_custody,
        "load_authorization_custody",
        lambda *_args, **_kwargs: SimpleNamespace(control=current_control[0]),
    )
    monkeypatch.setattr(
        projection_runtime.store,
        "open_active_governance_read_connection",
        lambda _root: connection,
    )
    monkeypatch.setattr(
        schema_v4,
        "load_active_policy",
        lambda *_args, **kwargs: (
            events.append(f"policy:{kwargs['expected_activation_state_digest']}"),
            runtime.snapshot,
        )[1],
    )
    monkeypatch.setattr(
        projection_store,
        "load_projection_catalog",
        lambda *_args, **_kwargs: (
            events.append("catalog"),
            runtime.namespace.manifest,
            items,
        )[1:],
    )
    store_pointer = [runtime.snapshot.active]
    monkeypatch.setattr(
        schema_v4,
        "load_active_tuple_pointer",
        lambda _connection: store_pointer[0],
        raising=False,
    )

    projection_runtime._clear_preactivated_runtimes_for_tests()
    activated = projection_runtime.preactivate_projection_runtime(tmp_path)
    assert activated.snapshot is runtime.snapshot
    assert events == [
        "begin",
        f"policy:{control.activation_state_digest}",
        "catalog",
    ]

    monkeypatch.setattr(
        schema_v4,
        "load_active_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("request reloaded active policy")
        ),
    )
    monkeypatch.setattr(
        projection_store,
        "load_projection_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("request reloaded projection catalog")
        ),
    )
    assert projection_runtime._PROJECTED_SERVING_RELEASE_ACCEPTED is True
    assert projection_runtime.load_active_projection_runtime(tmp_path) is activated

    current_control[0] = replace(control, activation_epoch=control.activation_epoch + 1)
    with pytest.raises(
        projection_runtime.ProjectionRuntimeUnavailable,
        match="governed projected retrieval is unavailable",
    ):
        projection_runtime.load_active_projection_runtime(tmp_path)

    current_control[0] = control
    store_pointer[0] = replace(
        runtime.snapshot.active,
        activation_epoch=runtime.snapshot.active.activation_epoch + 1,
    )
    with pytest.raises(
        projection_runtime.ProjectionRuntimeUnavailable,
        match="governed projected retrieval is unavailable",
    ):
        projection_runtime.load_active_projection_runtime(tmp_path)


@pytest.mark.parametrize(
    "enabled_model_variable",
    [
        "EXOMEM_DISABLE_EMBEDDINGS",
        "EXOMEM_DISABLE_CLIP",
        "EXOMEM_DISABLE_RANKING",
    ],
)
def test_release_fence_refuses_every_uncertified_model_enabled_profile(
    monkeypatch,
    enabled_model_variable: str,
) -> None:
    runtime, _items = _active_runtime()
    for name in (
        "EXOMEM_DISABLE_EMBEDDINGS",
        "EXOMEM_DISABLE_CLIP",
        "EXOMEM_DISABLE_RANKING",
    ):
        monkeypatch.setenv(name, "1")
    monkeypatch.delenv(enabled_model_variable)
    monkeypatch.setattr(
        projection_runtime,
        "_preactivated_runtime",
        lambda _root: runtime,
    )

    with pytest.raises(
        projection_runtime.ProjectionRuntimeUnavailable,
        match="governed projected retrieval is unavailable",
    ):
        projection_runtime.load_active_projection_runtime(Path("/fixture"))
