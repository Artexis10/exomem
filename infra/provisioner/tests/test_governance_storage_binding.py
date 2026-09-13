from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_governance_recovery_adapters import CODEC, METADATA

from exomem_provisioner.driver import DriverRetryable, EffectContext, LostAcknowledgement
from exomem_provisioner.governance_migration_checkpoint import (
    MigrationCheckpoint,
    migration_binding,
)
from exomem_provisioner.governance_storage_binding import KubernetesGovernanceStorageBindingAdapter
from exomem_provisioner.governance_storage_init import KubernetesGovernanceStorageInitAdapter
from exomem_provisioner.lifecycle import MetadataConflict, RecordedVolume, VolumeRegistrationDriver
from exomem_provisioner.models import OperationAction, OperationState, ResourceKind
from exomem_provisioner.provider_identity import (
    ProviderRecoveryIdentityCodec,
    ProviderReference,
    cell_provider_recovery_envelopes,
    provider_operation_resource_name,
)
from exomem_provisioner.wire_protocol import WIRE_PROTOCOL_V2

IMAGE = "ghcr.io/artexis10/exomem@sha256:" + "a" * 64


def _envelope() -> str:
    name = METADATA.resource_name + "-init"
    return CODEC.seal(
        provider="kubernetes",
        provider_reference=ProviderReference.kubernetes(
            provider="kubernetes",
            api_version="batch/v1",
            kind="Job",
            namespace=METADATA.resource_name,
            name=name,
        ),
        tenant_id=METADATA.tenant_id,
        cell_id=METADATA.subject_id,
        operation_id=METADATA.operation_id,
        fence_generation=METADATA.fence_generation,
    )


def _adapter():
    return KubernetesGovernanceStorageBindingAdapter(
        core_v1=SimpleNamespace(),
        batch_v1=SimpleNamespace(),
        apps_v1=SimpleNamespace(),
        identity_verifier=CODEC.verifier(),
        runtime_image=IMAGE,
        cell=SimpleNamespace(),
    )


def test_binding_job_is_inert_unmounted_and_bounded_in_fixed_init_slot():
    adapter = _adapter()
    job = adapter._job_body(METADATA, _envelope())
    assert (job["apiVersion"], job["kind"]) == ("batch/v1", "Job")
    spec = job["spec"]
    pod = spec["template"]["spec"]
    container = pod["containers"][0]

    assert job["metadata"]["name"] == METADATA.resource_name + "-init"
    assert job["metadata"]["annotations"]["exomem.io/job-purpose"] == "storage-binding"
    assert (spec["activeDeadlineSeconds"], spec["ttlSecondsAfterFinished"]) == (90, 60)
    assert spec["backoffLimit"] == 0
    assert container["image"] == IMAGE and container["command"] == ["/bin/true"]
    assert not any(
        key in container for key in ("args", "env", "envFrom", "volumeMounts", "volumeDevices")
    )
    assert pod["volumes"] == [
        {"name": "data", "persistentVolumeClaim": {"claimName": METADATA.resource_name + "-data"}}
    ]
    assert not any(key in pod for key in ("nodeName", "initContainers", "imagePullSecrets"))
    assert pod["automountServiceAccountToken"] is False
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}


def test_binder_job_proof_accepts_only_kubernetes_default_replacement_policy():
    adapter = _adapter()
    job = adapter._job_body(METADATA, _envelope())
    job["metadata"].update(uid="binder-uid", resourceVersion="12")
    job["spec"]["podReplacementPolicy"] = "TerminatingOrFailed"
    assert adapter._prove_job(job, METADATA, _envelope(), allow_deleting=False) == (
        "binder-uid",
        "12",
        False,
    )
    for wrong_policy in ("Failed", "Unknown"):
        job["spec"]["podReplacementPolicy"] = wrong_policy
        with pytest.raises(MetadataConflict):
            adapter._prove_job(job, METADATA, _envelope(), allow_deleting=False)


async def test_lost_binding_create_ack_remains_retryable_not_absent():
    class Missing(Exception):
        status = 404

    class Batch:
        def read_namespaced_job(self, *_args):
            raise Missing()

        def create_namespaced_job(self, *_args, **_kwargs):
            raise LostAcknowledgement("create accepted, reply lost")

    class Core:
        def list_namespaced_pod(self, *_args):
            return {"metadata": {}, "items": []}

    class Cell:
        async def authenticated_volume_state(self, _metadata):
            return "pvc-alpha", "Pending"

    adapter = KubernetesGovernanceStorageBindingAdapter(
        core_v1=Core(),
        batch_v1=Batch(),
        apps_v1=SimpleNamespace(),
        identity_verifier=CODEC.verifier(),
        runtime_image=IMAGE,
        cell=Cell(),
    )

    async def guard():
        return None

    with pytest.raises(DriverRetryable):
        await adapter.reconcile(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )


async def test_final_original_binder_wait_preserves_unrelated_valid_runtime_pod():
    class Batch:
        def __init__(self):
            self.job = None
            self.deletes = 0

        def read_namespaced_job(self, *_args):
            return self.job

        def delete_namespaced_job(self, *_args, **_kwargs):
            self.deletes += 1

    class Core:
        def list_namespaced_pod(self, *_args):
            return {
                "metadata": {},
                "items": [
                    {
                        "metadata": {
                            "name": METADATA.resource_name + "-0",
                            "namespace": METADATA.resource_name,
                        },
                        "spec": {
                            "volumes": [
                                {
                                    "persistentVolumeClaim": {
                                        "claimName": METADATA.resource_name + "-data"
                                    }
                                }
                            ]
                        },
                    }
                ],
            }

    class Cell:
        async def authenticated_volume_uid(self, _metadata):
            return "pvc-alpha"

    batch = Batch()
    adapter = KubernetesGovernanceStorageBindingAdapter(
        core_v1=Core(),
        batch_v1=batch,
        apps_v1=SimpleNamespace(),
        identity_verifier=CODEC.verifier(),
        runtime_image=IMAGE,
        cell=Cell(),
    )
    batch.job = adapter._job_body(METADATA, _envelope())
    batch.job["metadata"].update(uid="job-original", resourceVersion="1")

    async def guard():
        return None

    assert (
        await adapter.wait_for_original(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )
        is True
    )
    assert batch.deletes == 0
    batch.job["status"] = {"failed": 1, "conditions": [{"type": "Failed", "status": "True"}]}
    assert (
        await adapter.wait_for_original(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )
        is True
    )
    assert batch.deletes == 0
    batch.job = copy.deepcopy(batch.job)
    batch.job["spec"]["template"]["spec"]["containers"][0]["command"] = ["/bin/sh"]
    with pytest.raises(MetadataConflict):
        await adapter.wait_for_original(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )


async def test_pending_ttl_replay_recreates_binder_but_bound_replay_advances_without_it():
    class Missing(Exception):
        status = 404

    class Batch:
        def __init__(self):
            self.created = []

        def read_namespaced_job(self, *_args):
            raise Missing()

        def create_namespaced_job(self, *_args, body):
            self.created.append(body)

    class Core:
        def list_namespaced_pod(self, *_args):
            return {"metadata": {}, "items": []}

    class Cell:
        phase = "Pending"

        async def authenticated_volume_state(self, _metadata):
            return "pvc-alpha", self.phase

    batch, cell = Batch(), Cell()
    adapter = KubernetesGovernanceStorageBindingAdapter(
        core_v1=Core(),
        batch_v1=batch,
        apps_v1=SimpleNamespace(),
        identity_verifier=CODEC.verifier(),
        runtime_image=IMAGE,
        cell=cell,
    )

    async def guard():
        return None

    assert (
        await adapter.reconcile(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )
        is False
    )
    assert len(batch.created) == 1
    cell.phase = "Bound"
    assert (
        await adapter.reconcile(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )
        is True
    )
    assert len(batch.created) == 1


async def test_late_binder_cleanup_owns_only_original_job_and_waits_for_terminating_pod():
    class Missing(Exception):
        status = 404

    class Batch:
        def __init__(self):
            self.job = None
            self.deleted = []

        def read_namespaced_job(self, *_args):
            if self.job is None:
                raise Missing()
            return self.job

        def delete_namespaced_job(self, *_args, body):
            self.deleted.append(body)
            self.job = None

    class Core:
        pods = []

        def list_namespaced_pod(self, *_args):
            return {"metadata": {}, "items": self.pods}

    class Cell:
        async def authenticated_volume_state(self, _metadata):
            return "pvc-alpha", "Bound"

    batch, core = Batch(), Core()
    adapter = KubernetesGovernanceStorageBindingAdapter(
        core_v1=core,
        batch_v1=batch,
        apps_v1=SimpleNamespace(),
        identity_verifier=CODEC.verifier(),
        runtime_image=IMAGE,
        cell=Cell(),
    )
    batch.job = adapter._job_body(METADATA, _envelope())
    batch.job["metadata"].update(uid="binder-uid", resourceVersion="12")

    async def guard():
        return None

    assert (
        await adapter.cleanup_late(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )
        is False
    )
    assert batch.deleted == [
        {
            "propagationPolicy": "Foreground",
            "preconditions": {
                "uid": "binder-uid",
                "resourceVersion": "12",
            },
        }
    ]
    core.pods = [
        {
            "metadata": {
                "name": METADATA.resource_name + "-init-orphan",
                "namespace": METADATA.resource_name,
                "deletionTimestamp": "2026-01-01T00:00:00Z",
            },
            "spec": {"volumes": []},
        }
    ]
    assert (
        await adapter.cleanup_late(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )
        is False
    )
    core.pods = []
    assert (
        await adapter.cleanup_late(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )
        is True
    )
    replacement = KubernetesGovernanceStorageInitAdapter(
        core_v1=core,
        batch_v1=batch,
        apps_v1=SimpleNamespace(),
        identity_verifier=CODEC.verifier(),
        runtime_image=IMAGE,
    )._job_body(METADATA, _envelope())
    replacement["metadata"].update(uid="initializer-uid", resourceVersion="13")
    batch.job = replacement
    core.pods = [
        {
            "metadata": {
                "name": METADATA.resource_name + "-init-old",
                "namespace": METADATA.resource_name,
                "deletionTimestamp": "2026-01-01T00:00:00Z",
                "ownerReferences": [
                    {"name": METADATA.resource_name + "-init", "uid": "binder-uid"}
                ],
            },
            "spec": {"volumes": []},
        }
    ]
    assert (
        await adapter.cleanup_late(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )
        is False
    )
    core.pods = []
    assert (
        await adapter.cleanup_late(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )
        is True
    )
    assert len(batch.deleted) == 1
    with pytest.raises(MetadataConflict):
        await adapter.wait_for_original(
            METADATA,
            pvc_uid="pvc-alpha",
            recovery_envelope=_envelope(),
            effect_guard=guard,
        )


@pytest.mark.parametrize("after_delete", ["absent", "replacement", "remaining-pod"])
async def test_binding_delete_404_reobserves_slot_before_deciding(after_delete):
    class Missing(Exception):
        status = 404

    class Batch:
        reads = 0
        job = None

        def read_namespaced_job(self, *_args):
            self.reads += 1
            if self.job is None:
                raise Missing()
            return self.job

        def delete_namespaced_job(self, *_args, **_kwargs):
            self.job = replacement if after_delete == "replacement" else None
            if after_delete == "remaining-pod":
                core.pods = [
                    {
                        "metadata": {
                            "name": METADATA.resource_name + "-init-old",
                            "namespace": METADATA.resource_name,
                            "deletionTimestamp": "2026-01-01T00:00:00Z",
                        },
                        "spec": {"volumes": []},
                    }
                ]
            raise Missing()

    class Core:
        pods = []

        def list_namespaced_pod(self, *_args):
            return {"metadata": {}, "items": self.pods}

    class Cell:
        async def authenticated_volume_state(self, _metadata):
            return "pvc-alpha", "Bound"

    batch, core = Batch(), Core()
    adapter = KubernetesGovernanceStorageBindingAdapter(
        core_v1=core,
        batch_v1=batch,
        apps_v1=SimpleNamespace(),
        identity_verifier=CODEC.verifier(),
        runtime_image=IMAGE,
        cell=Cell(),
    )
    batch.job = adapter._job_body(METADATA, _envelope())
    batch.job["metadata"].update(uid="binder-uid", resourceVersion="12")
    replacement = KubernetesGovernanceStorageInitAdapter(
        core_v1=core,
        batch_v1=batch,
        apps_v1=SimpleNamespace(),
        identity_verifier=CODEC.verifier(),
        runtime_image=IMAGE,
    )._job_body(METADATA, _envelope())
    replacement["metadata"].update(uid="replacement-uid", resourceVersion="13")
    guards = 0

    async def guard():
        nonlocal guards
        guards += 1

    result = await adapter.reconcile(
        METADATA,
        pvc_uid="pvc-alpha",
        recovery_envelope=_envelope(),
        effect_guard=guard,
    )
    assert result is (after_delete == "absent")
    assert batch.reads >= 3
    assert guards >= 4


async def test_final_original_wait_allows_current_migration_job_but_not_old_slot_pod():
    class Batch:
        def read_namespaced_job(self, *_args):
            return {
                "metadata": {
                    "name": METADATA.resource_name + "-init",
                    "namespace": METADATA.resource_name,
                    "uid": "current-migration",
                    "resourceVersion": "15",
                    "labels": {
                        "exomem.io/governance-migration": "true",
                        "app.kubernetes.io/name": "exomem-governance-migration",
                        "exomem.io/cell": METADATA.resource_name,
                    },
                    "annotations": METADATA.kubernetes_annotations
                    | {
                        "exomem.io/recovery-envelope": _envelope(),
                    },
                }
            }

    class Core:
        pods = []

        def list_namespaced_pod(self, *_args):
            return {"metadata": {}, "items": self.pods}

    class Cell:
        async def authenticated_volume_uid(self, _metadata):
            return "pvc-alpha"

    core = Core()
    adapter = KubernetesGovernanceStorageBindingAdapter(
        core_v1=core,
        batch_v1=Batch(),
        apps_v1=SimpleNamespace(),
        identity_verifier=CODEC.verifier(),
        runtime_image=IMAGE,
        cell=Cell(),
    )

    async def guard():
        return None

    assert (
        await adapter.wait_for_original(
            METADATA,
            recovery_envelope=_envelope(),
            effect_guard=guard,
            replacement_metadata=METADATA,
            replacement_envelope=_envelope(),
        )
        is False
    )
    core.pods = [
        {
            "metadata": {
                "name": METADATA.resource_name + "-init-old",
                "namespace": METADATA.resource_name,
                "ownerReferences": [
                    {"name": METADATA.resource_name + "-init", "uid": "old-binder"}
                ],
            },
            "spec": {"volumes": []},
        }
    ]
    assert (
        await adapter.wait_for_original(
            METADATA,
            recovery_envelope=_envelope(),
            effect_guard=guard,
            replacement_metadata=METADATA,
            replacement_envelope=_envelope(),
        )
        is True
    )
    with pytest.raises(MetadataConflict):
        await adapter.wait_for_original(
            METADATA,
            recovery_envelope=_envelope(),
            effect_guard=guard,
            replacement_metadata=METADATA,
            replacement_envelope="foreign-envelope",
        )


async def test_later_claim_waits_at_same_checkpoint_for_final_original_binder():
    from test_governance_live_readiness import ReadinessHarness

    from exomem_provisioner.driver import DriverPending, EffectContext

    h = ReadinessHarness()
    original = METADATA
    current = h.current
    codec = ProviderRecoveryIdentityCodec.from_secret("readiness-test-provider-root")
    current_envelopes = cell_provider_recovery_envelopes(
        codec,
        tenant_id=current.tenant_id,
        cell_id=current.subject_id,
        operation_id=current.operation_id,
        fence_generation=current.fence_generation,
        resource_name=current.resource_name,
        operation_resource_name=provider_operation_resource_name(current.operation_id),
    )
    original_request = h.plane._helm_requests[h.plane._key(current)]
    h.plane._repository = SimpleNamespace(
        list_resources=lambda **_kwargs: _async_value(
            [
                SimpleNamespace(
                    kind=ResourceKind.KUBERNETES_NAMESPACE,
                    operation_id="original-internal",
                    provider_operation_id=original.operation_id,
                    provider_fence_generation=original.fence_generation,
                )
            ]
        ),
        load_request=lambda _id: _async_value(original_request),
        get_by_id=lambda _id: _async_value(
            SimpleNamespace(
                id="original-internal",
                action=OperationAction.PROVISION,
                state=OperationState.FINAL,
                tenant_id=original.tenant_id,
                cell_id=original.subject_id,
                external_operation_id=original.operation_id,
                fence_generation=original.fence_generation,
                wire_protocol=WIRE_PROTOCOL_V2,
            )
        ),
    )
    h.plane._registry = SimpleNamespace(
        inspect=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("refresh before binder wait")
        )
    )

    async def uid(_metadata):
        return "pvc-alpha"

    h.plane._cell.authenticated_volume_uid = uid
    seen = []

    async def wait(metadata, **kwargs):
        seen.append((metadata, kwargs["recovery_envelope"]))
        await kwargs["effect_guard"]()
        return True

    h.plane._storage_binding = SimpleNamespace(wait_for_original=wait)
    context = EffectContext(
        "current-internal",
        current.operation_id,
        current.tenant_id,
        current.subject_id,
        current.fence_generation,
        checkpoint="gm1:i:" + "a" * 64,
        wire_protocol=WIRE_PROTOCOL_V2,
        effect_guard=lambda: _async_value(None),
    )
    result = await h.plane.observe_operation(
        context, h.request | {"_providerRecoveryEnvelopes": current_envelopes}
    )
    assert isinstance(result, DriverPending)
    assert result.checkpoint == context.checkpoint
    assert seen == [(original, original_request["_providerRecoveryEnvelopes"]["initJob"])]


async def test_original_gm1_claim_cleans_late_binder_before_migration_observation():
    from test_governance_live_readiness import ReadinessHarness

    from exomem_provisioner.driver import DriverPending, EffectContext

    h = ReadinessHarness()
    original = METADATA
    codec = ProviderRecoveryIdentityCodec.from_secret("readiness-test-provider-root")
    envelopes = cell_provider_recovery_envelopes(
        codec,
        tenant_id=original.tenant_id,
        cell_id=original.subject_id,
        operation_id=original.operation_id,
        fence_generation=original.fence_generation,
        resource_name=original.resource_name,
        operation_resource_name=provider_operation_resource_name(original.operation_id),
    )
    h.plane._repository = SimpleNamespace(list_resources=lambda **_kwargs: _async_value([]))
    h.plane._registry = SimpleNamespace(
        inspect=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("migration observed before binder cleanup")
        )
    )

    async def uid(_metadata):
        return "pvc-alpha"

    h.plane._cell.authenticated_volume_uid = uid
    seen = []

    async def cleanup(metadata, **kwargs):
        seen.append((metadata, kwargs["pvc_uid"]))
        await kwargs["effect_guard"]()
        return False

    h.plane._storage_binding = SimpleNamespace(cleanup_late=cleanup)
    context = EffectContext(
        "original-internal",
        original.operation_id,
        original.tenant_id,
        original.subject_id,
        original.fence_generation,
        wire_protocol=WIRE_PROTOCOL_V2,
        effect_guard=lambda: _async_value(None),
    )
    context = replace(
        context,
        checkpoint=MigrationCheckpoint(
            "inspect",
            "a" * 64,
            migration_binding(context, pvc_uid="pvc-alpha", runtime_image=h.config.image),
        ).encode(),
    )
    result = await h.plane.observe_operation(
        context,
        h.request | {"provisionMode": "serve", "_providerRecoveryEnvelopes": envelopes},
    )
    assert isinstance(result, DriverPending)
    assert result.checkpoint == context.checkpoint
    assert seen == [(original, "pvc-alpha")]


async def _async_value(value):
    return value


async def test_volume_worker_recomputes_prefixed_binding_from_bound_pvc_before_effect():
    events = []

    async def guard():
        events.append("claim")

    context = EffectContext(
        "internal",
        METADATA.operation_id,
        METADATA.tenant_id,
        METADATA.subject_id,
        METADATA.fence_generation,
        wire_protocol=WIRE_PROTOCOL_V2,
        effect_guard=guard,
    )
    binding = migration_binding(context, pvc_uid="pvc-alpha", runtime_image=IMAGE)
    context = replace(context, checkpoint="gpi1:registering:" + binding)
    envelopes = cell_provider_recovery_envelopes(
        CODEC,
        tenant_id=METADATA.tenant_id,
        cell_id=METADATA.subject_id,
        operation_id=METADATA.operation_id,
        fence_generation=METADATA.fence_generation,
        resource_name=METADATA.resource_name,
        operation_resource_name=provider_operation_resource_name(METADATA.operation_id),
    )

    class Observer:
        async def authenticated_volume_uid(self, metadata):
            events.append("pvc")
            return "pvc-alpha"

    class Worker:
        async def register_bound_volume(self, metadata, *, pvc_recovery_envelope, effect_guard):
            await effect_guard()
            events.append("register")
            return RecordedVolume(
                "volume-alpha",
                "pv-alpha",
                "fsn1",
                metadata,
                hcloud_recovery_envelope="h",
                pv_recovery_envelope="p",
                pvc_recovery_envelope=pvc_recovery_envelope,
            )

    driver = VolumeRegistrationDriver(
        Worker(),
        identity_verifier=CODEC.verifier(),
        binding_observer=Observer(),
        runtime_image=IMAGE,
    )

    result = await driver.execute("provision", {"_providerRecoveryEnvelopes": envelopes}, context)
    assert result.checkpoint == "gpi1:registered:" + binding
    assert events == ["claim", "pvc", "claim", "claim", "pvc", "claim", "register"]
