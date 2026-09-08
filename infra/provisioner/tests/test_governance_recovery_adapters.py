from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from kubernetes.client import ApiClient, V1ListMeta, V1PodList

from exomem_provisioner.adapters import KubernetesCellAdapter, KubernetesMaintenanceLeaseAdapter
from exomem_provisioner.conflict_reason import ConflictReason
from exomem_provisioner.driver import DriverRetryable
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata
from exomem_provisioner.provider_identity import ProviderRecoveryIdentityCodec, ProviderReference
from exomem_provisioner.repository import ClaimConflict, StaleFence


def test_governance_recovery_adapters_expose_read_only_guards() -> None:
    assert hasattr(KubernetesCellAdapter, "authenticated_volume_uid")
    assert hasattr(KubernetesCellAdapter, "verify_governance_stopped")
    assert hasattr(KubernetesMaintenanceLeaseAdapter, "assert_owned")


NOW = datetime(2030, 1, 1, tzinfo=UTC)
METADATA = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7)
CODEC = ProviderRecoveryIdentityCodec.from_secret("governance-recovery-adapters")


def _model(value: dict[str, object], kind: str):
    return ApiClient().deserialize(SimpleNamespace(data=json.dumps(value)), kind)


def _envelope() -> str:
    resource = METADATA.resource_name
    return CODEC.seal(
        provider="kubernetes",
        provider_reference=ProviderReference.kubernetes(
            provider="kubernetes",
            api_version="v1",
            kind="PersistentVolumeClaim",
            namespace=resource,
            name=resource + "-data",
        ),
        tenant_id=METADATA.tenant_id,
        cell_id=METADATA.subject_id,
        operation_id=METADATA.operation_id,
        fence_generation=METADATA.fence_generation,
    )


def _pvc(**changes: object):
    resource = METADATA.resource_name
    value: dict[str, object] = {
        "metadata": {
            "name": resource + "-data",
            "namespace": resource,
            "uid": "pvc-alpha",
            "annotations": {
                **METADATA.kubernetes_annotations,
                "exomem.io/recovery-envelope": _envelope(),
            },
        },
        "spec": {"volumeName": "pv-alpha"},
        "status": {"phase": "Bound"},
    }
    for path, replacement in changes.items():
        parent = value
        *keys, key = path.split(".")
        for item in keys:
            parent = parent[item]  # type: ignore[assignment,index]
        parent[key] = replacement
    return _model(value, "V1PersistentVolumeClaim")


def _lease(**changes: object):
    resource = METADATA.resource_name
    value: dict[str, object] = {
        "metadata": {
            "name": resource + "-maintenance",
            "namespace": resource,
            "uid": "lease-alpha",
            "resourceVersion": "1",
            "annotations": dict(METADATA.kubernetes_annotations),
        },
        "spec": {
            "holderIdentity": METADATA.operation_id,
            "leaseDurationSeconds": 120,
            "renewTime": (NOW - timedelta(seconds=1)).isoformat(),
        },
    }
    for path, replacement in changes.items():
        parent = value
        *keys, key = path.split(".")
        for item in keys:
            parent = parent[item]  # type: ignore[assignment,index]
        parent[key] = replacement
    return _model(value, "V1Lease")


class Missing(Exception):
    status = 404


class Cluster:
    def __init__(self) -> None:
        self.pvc = _pvc()
        self.pvc_reads = 0

    def read_namespaced_persistent_volume_claim(self, name: str, namespace: str):
        assert (name, namespace) == (METADATA.resource_name + "-data", METADATA.resource_name)
        self.pvc_reads += 1
        return self.pvc

    def read_namespaced_stateful_set(self, name: str, namespace: str):
        assert (name, namespace) == (METADATA.resource_name, METADATA.resource_name)
        raise Missing

    def list_namespaced_pod(self, namespace: str):
        assert namespace == METADATA.resource_name
        return V1PodList(items=[], metadata=V1ListMeta())


async def test_authenticated_volume_uid_requires_the_signed_fixed_pvc() -> None:
    cluster = Cluster()
    adapter = KubernetesCellAdapter(
        core_v1=cluster,
        apps_v1=cluster,
        identity_verifier=CODEC.verifier(),
    )

    assert await adapter.authenticated_volume_uid(METADATA) == "pvc-alpha"


@pytest.mark.parametrize(
    "pvc",
    [
        lambda: _pvc(**{"metadata.uid": None}),
        lambda: _pvc(**{"metadata.deletionTimestamp": NOW.isoformat()}),
        lambda: _pvc(**{"status.phase": "Pending"}),
        lambda: _pvc(**{"spec.volumeName": None}),
        lambda: _pvc(**{"metadata.annotations": {}}),
    ],
)
async def test_authenticated_volume_uid_refuses_invalid_physical_or_identity_evidence(pvc) -> None:
    cluster = Cluster()
    cluster.pvc = pvc()
    adapter = KubernetesCellAdapter(
        core_v1=cluster,
        apps_v1=cluster,
        identity_verifier=CODEC.verifier(),
    )

    with pytest.raises(MetadataConflict):
        await adapter.authenticated_volume_uid(METADATA)


async def test_authenticated_volume_uid_never_allows_optional_identity_authentication() -> None:
    cluster = Cluster()
    adapter = KubernetesCellAdapter(core_v1=cluster, apps_v1=cluster)

    with pytest.raises(MetadataConflict) as raised:
        await adapter.authenticated_volume_uid(METADATA)
    assert raised.value.reason is ConflictReason.PVC_RECOVERY_IDENTITY_UNAUTHENTICATED


async def test_authenticated_volume_uid_maps_transient_provider_failure_without_payload() -> None:
    class ProviderError(Exception):
        status = 503

    cluster = Cluster()

    def unavailable(name: str, namespace: str):
        raise ProviderError("private-provider-payload")

    cluster.read_namespaced_persistent_volume_claim = unavailable  # type: ignore[method-assign]
    adapter = KubernetesCellAdapter(
        core_v1=cluster,
        apps_v1=cluster,
        identity_verifier=CODEC.verifier(),
    )

    with pytest.raises(DriverRetryable) as raised:
        await adapter.authenticated_volume_uid(METADATA)
    assert "private-provider-payload" not in str(raised.value)


async def test_stopped_wrapper_refuses_a_pvc_replacement_after_authenticated_read() -> None:
    cluster = Cluster()
    original = cluster.read_namespaced_persistent_volume_claim

    def changed(name: str, namespace: str):
        result = original(name, namespace)
        if cluster.pvc_reads == 1:
            cluster.pvc = _pvc(**{"metadata.uid": "pvc-replacement"})
        return result

    cluster.read_namespaced_persistent_volume_claim = changed  # type: ignore[method-assign]
    adapter = KubernetesCellAdapter(
        core_v1=cluster,
        apps_v1=cluster,
        identity_verifier=CODEC.verifier(),
    )

    with pytest.raises(MetadataConflict):
        await adapter.verify_governance_stopped(METADATA, pvc_uid="pvc-alpha")
    assert cluster.pvc_reads == 2


async def test_stopped_wrapper_refuses_a_caller_uid_different_from_fresh_evidence() -> None:
    cluster = Cluster()
    adapter = KubernetesCellAdapter(
        core_v1=cluster,
        apps_v1=cluster,
        identity_verifier=CODEC.verifier(),
    )

    with pytest.raises(MetadataConflict):
        await adapter.verify_governance_stopped(METADATA, pvc_uid="pvc-other")
    assert cluster.pvc_reads == 1


@pytest.mark.parametrize(
    "lease",
    [
        lambda: _lease(**{"metadata.uid": None}),
        lambda: _lease(**{"metadata.resourceVersion": None}),
        lambda: _lease(**{"metadata.deletionTimestamp": NOW.isoformat()}),
        lambda: _lease(**{"spec.holderIdentity": "foreign-operation"}),
        lambda: _lease(**{"spec.leaseDurationSeconds": True}),
        lambda: _lease(**{"spec.leaseDurationSeconds": 0}),
        lambda: _lease(**{"spec.renewTime": (NOW + timedelta(seconds=1)).isoformat()}),
        lambda: _lease(**{"spec.renewTime": (NOW - timedelta(seconds=120)).isoformat()}),
    ],
)
async def test_maintenance_assert_owned_refuses_unproven_or_expired_authority(lease) -> None:
    class Coordination:
        def read_namespaced_lease(self, name: str, namespace: str):
            assert (name, namespace) == (
                METADATA.resource_name + "-maintenance",
                METADATA.resource_name,
            )
            return lease()

    adapter = KubernetesMaintenanceLeaseAdapter(
        coordination_v1=Coordination(),
        now=lambda: NOW,
    )
    with pytest.raises(MetadataConflict) as raised:
        await adapter.assert_owned(METADATA, METADATA.operation_id)
    assert raised.value.reason is ConflictReason.GOVERNANCE_MIGRATION_JOB_UNAVAILABLE


async def test_maintenance_assert_owned_accepts_fresh_exact_current_lease() -> None:
    class Coordination:
        def read_namespaced_lease(self, name: str, namespace: str):
            return _lease()

    adapter = KubernetesMaintenanceLeaseAdapter(
        coordination_v1=Coordination(),
        now=lambda: NOW,
    )
    await adapter.assert_owned(METADATA, METADATA.operation_id)


async def test_maintenance_assert_owned_refuses_a_missing_lease() -> None:
    class Coordination:
        def read_namespaced_lease(self, name: str, namespace: str):
            raise Missing

    adapter = KubernetesMaintenanceLeaseAdapter(
        coordination_v1=Coordination(),
        now=lambda: NOW,
    )
    with pytest.raises(MetadataConflict) as raised:
        await adapter.assert_owned(METADATA, METADATA.operation_id)
    assert raised.value.reason is ConflictReason.GOVERNANCE_MIGRATION_JOB_UNAVAILABLE


@pytest.mark.parametrize("failure", [ClaimConflict, StaleFence])
async def test_recovery_adapter_guards_preserve_claim_and_fence_conflicts(failure) -> None:
    refused = failure("AUTHORITY_LOST")

    class Coordination:
        def read_namespaced_lease(self, name: str, namespace: str):
            raise refused

    adapter = KubernetesMaintenanceLeaseAdapter(
        coordination_v1=Coordination(),
        now=lambda: NOW,
    )
    with pytest.raises(failure) as raised:
        await adapter.assert_owned(METADATA, METADATA.operation_id)
    assert raised.value is refused


async def test_transient_lease_provider_failure_is_retryable_and_content_free() -> None:
    class ProviderError(Exception):
        status = 503

    class Coordination:
        def read_namespaced_lease(self, name: str, namespace: str):
            raise ProviderError("private-provider-payload")

    adapter = KubernetesMaintenanceLeaseAdapter(
        coordination_v1=Coordination(),
        now=lambda: NOW,
    )
    with pytest.raises(DriverRetryable) as raised:
        await adapter.assert_owned(METADATA, METADATA.operation_id)
    assert "private-provider-payload" not in str(raised.value)
