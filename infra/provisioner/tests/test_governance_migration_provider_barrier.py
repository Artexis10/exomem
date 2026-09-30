from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from kubernetes.client import ApiClient
from test_governance_migration_job import _request
from test_live_provider import IDENTITY_CODEC, _metadata, _NotFound

from exomem_provisioner.driver import DriverPending, DriverTerminal, EffectContext
from exomem_provisioner.governance_migration_checkpoint import MigrationCheckpoint
from exomem_provisioner.governance_migration_job import build_governance_migration_job
from exomem_provisioner.lifecycle import CellLifecycleDriver, MetadataConflict
from exomem_provisioner.live import KubernetesProviderRegistry, LiveLifecyclePlane
from exomem_provisioner.provider_identity import (
    cell_provider_recovery_envelopes,
    provider_operation_resource_name,
)
from exomem_provisioner.wire_protocol import WIRE_PROTOCOL_V2


def _envelopes(metadata):
    return cell_provider_recovery_envelopes(
        IDENTITY_CODEC,
        tenant_id=metadata.tenant_id,
        cell_id=metadata.subject_id,
        operation_id=metadata.operation_id,
        fence_generation=metadata.fence_generation,
        resource_name=metadata.resource_name,
        operation_resource_name=provider_operation_resource_name(metadata.operation_id),
    )


def _registry(job_owner=None, *, terminating=False):
    current = _metadata()
    owner = job_owner or current
    body = build_governance_migration_job(
        _request(metadata=owner, vault_id=owner.tenant_id),
        recovery_envelope=_envelopes(owner)["initJob"],
    )
    body["metadata"].update(uid="job-alpha", resourceVersion="7")
    if terminating:
        body["metadata"]["deletionTimestamp"] = "2030-01-01T00:00:00Z"
    job = ApiClient().deserialize(SimpleNamespace(data=json.dumps(body)), "V1Job")

    class Core:
        def read_namespace(self, name):
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    annotations={
                        **current.kubernetes_annotations,
                        "exomem.io/recovery-envelope": _envelopes(current)["namespace"],
                    }
                )
            )

        def read_namespaced_persistent_volume_claim(self, *args):
            raise _NotFound()

        def list_namespaced_config_map(self, *args, **kwargs):
            return SimpleNamespace(items=[])

        def list_namespaced_pod(self, *args, **kwargs):
            return SimpleNamespace(items=[])

    class Batch:
        def read_namespaced_job(self, *args):
            return job

    class Missing:
        def __getattr__(self, name):
            def missing(*args, **kwargs):
                raise _NotFound()

            return missing

    registry = KubernetesProviderRegistry(
        core_v1=Core(),
        apps_v1=Missing(),
        batch_v1=Batch(),
        custom_objects=Missing(),
        identity_verifier=IDENTITY_CODEC.verifier(),
    )
    return registry, job


@pytest.mark.parametrize("terminating", [False, True])
@pytest.mark.parametrize("status", ["active", "succeeded", "failed"])
async def test_registry_classifies_exact_current_migration_without_claiming_success(
    terminating, status
):
    registry, job = _registry(terminating=terminating)
    job.status = SimpleNamespace(**{status: 1})
    snapshot = await registry.inspect(_metadata(), _metadata())
    assert snapshot.governance_migration_job == "current"
    assert not snapshot.init_complete and not snapshot.init_failed


@pytest.mark.parametrize("terminating", [False, True])
async def test_registry_classifies_authenticated_foreign_job_as_barrier(terminating):
    registry, _job = _registry(
        replace(_metadata(), operation_id="prior-operation", fence_generation=6),
        terminating=terminating,
    )
    snapshot = await registry.inspect(_metadata(), _metadata())
    assert snapshot.governance_migration_job == "foreign"


@pytest.mark.parametrize(
    "corruption", ["envelope", "identity", "namespace", "name", "label", "markers"]
)
async def test_registry_refuses_malformed_migration_identity(corruption):
    registry, job = _registry()
    if corruption == "envelope":
        job.metadata.annotations["exomem.io/recovery-envelope"] = "forged"
    elif corruption == "identity":
        job.metadata.annotations["exomem.io/operation-id"] = "foreign"
    elif corruption in {"label", "markers"}:
        job.metadata.labels.pop("exomem.io/governance-migration")
        if corruption == "markers":
            job.metadata.annotations = {
                key: value
                for key, value in job.metadata.annotations.items()
                if not key.startswith("exomem.io/governance-migration-")
            }
    else:
        setattr(job.metadata, corruption, "foreign")
    with pytest.raises(MetadataConflict):
        await registry.inspect(_metadata(), _metadata())


@pytest.mark.parametrize(
    "mode,checkpoint,foreign,allowed",
    [
        ("none", "effect-prepared", False, False),
        ("governance-v3-to-v4", "effect-prepared", False, False),
        ("governance-v3-to-v4", "migration", True, False),
        ("governance-v3-to-v4", "migration", False, True),
    ],
)
async def test_live_observation_blocks_before_recording_or_dispatching_effects(
    mode, checkpoint, foreign, allowed
):
    metadata = _metadata()
    owner = (
        replace(metadata, operation_id="prior-operation", fence_generation=6)
        if foreign
        else metadata
    )
    registry, _job = _registry(owner)
    effects = []

    async def authority():
        effects.append("claim-guard")

    async def record(*args, effect_guard=None):
        if effect_guard is not None:
            await effect_guard()
        effects.append("record-operation")

    async def resources(**kwargs):
        return []

    registry.record_operation = record
    plane = LiveLifecyclePlane(
        repository=SimpleNamespace(list_resources=resources),
        registry=registry,
        cell=None,
        helm=None,
        runtime=None,
        routes=None,
        maintenance=None,
        capacity=None,
        identity_verifier=IDENTITY_CODEC.verifier(),
        config=SimpleNamespace(migration_mode=mode),
    )
    if checkpoint == "migration":
        checkpoint = MigrationCheckpoint("inspect", "a" * 64, "A" * 43).encode()
    context = EffectContext(
        "internal-operation",
        metadata.operation_id,
        metadata.tenant_id,
        metadata.subject_id,
        metadata.fence_generation,
        checkpoint=checkpoint,
        wire_protocol=WIRE_PROTOCOL_V2,
        effect_guard=authority,
    )
    request = {"_providerRecoveryEnvelopes": _envelopes(metadata)}
    if allowed:
        await plane.observe_operation(context, request)
        assert effects == ["claim-guard", "record-operation"]
    else:
        pending = await plane.observe_operation(context, request)
        assert pending == DriverPending(context.checkpoint, 30)
        assert effects == []


async def test_lifecycle_propagates_pending_observation_before_dispatch():
    context = EffectContext("internal", "provider", "tenant-alpha", "cell-alpha", 7)
    pending = DriverPending(context.checkpoint, 30)

    async def observe(*args):
        return pending

    async def fence(*args):
        return 0

    driver = CellLifecycleDriver(
        plane=SimpleNamespace(observe_operation=observe, observed_fence=fence),
        volume_worker=None,
        config=SimpleNamespace(migration_mode="none"),
    )
    assert await driver.execute("resume", {}, context) is pending


@pytest.mark.parametrize("action", ["resume", "health", "destroy", "rotate-credential"])
async def test_unrelated_action_cannot_adopt_a_migration_checkpoint(action):
    effects = []

    async def fence(*args):
        return 0

    async def observe(*args):
        effects.append("observe")

    driver = CellLifecycleDriver(
        plane=SimpleNamespace(observed_fence=fence, observe_operation=observe),
        volume_worker=None,
        config=SimpleNamespace(migration_mode="none"),
    )
    context = EffectContext(
        "internal-operation",
        "provider-operation",
        "tenant-alpha",
        "cell-alpha",
        7,
        checkpoint=MigrationCheckpoint("inspect", "a" * 64, "A" * 43).encode(),
        wire_protocol=WIRE_PROTOCOL_V2,
    )
    with pytest.raises(DriverTerminal, match="^PROVISIONER_CHECKPOINT_INVALID$"):
        await driver.execute(action, {}, context)
    assert effects == []
