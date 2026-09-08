from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from kubernetes.client import ApiClient, V1ListMeta, V1PodList

from exomem_provisioner.adapters import (
    KubernetesCellAdapter,
    KubernetesMaintenanceLeaseAdapter,
)
from exomem_provisioner.driver import DriverRetryable
from exomem_provisioner.governance_stopped_cell import verify_stopped_cell
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata

METADATA = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7)
NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _model(value: dict[str, object], kind: str):
    return ApiClient().deserialize(SimpleNamespace(data=json.dumps(value)), kind)


def _pvc():
    resource = METADATA.resource_name
    return _model(
        {
            "metadata": {
                "name": resource + "-data",
                "namespace": resource,
                "uid": "pvc-alpha",
                "annotations": METADATA.kubernetes_annotations,
            },
            "spec": {"volumeName": "pv-alpha"},
            "status": {"phase": "Bound"},
        },
        "V1PersistentVolumeClaim",
    )


def _runtime(*, uid: object = "statefulset-alpha"):
    resource = METADATA.resource_name
    return _model(
        {
            "metadata": {"name": resource, "namespace": resource, "uid": uid},
            "spec": {
                "replicas": 0,
                "serviceName": resource,
                "selector": {"matchLabels": {"app": "example"}},
                "template": {
                    "metadata": {"labels": {"app": "example"}},
                    "spec": {"containers": [{"name": "exomem", "image": "example"}]},
                },
            },
        },
        "V1StatefulSet",
    )


def _runtime_pod(
    *, annotations: dict[str, str] | None = None, owner_uid: str = "statefulset-alpha"
):
    resource = METADATA.resource_name
    return _model(
        {
            "metadata": {
                "name": resource + "-0",
                "namespace": resource,
                "uid": "pod-alpha",
                "annotations": METADATA.kubernetes_annotations
                if annotations is None
                else annotations,
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "StatefulSet",
                        "name": resource,
                        "uid": owner_uid,
                        "controller": True,
                    }
                ],
            },
            "spec": {
                "containers": [{"name": "exomem", "image": "example"}],
                "volumes": [
                    {
                        "name": "data",
                        "persistentVolumeClaim": {"claimName": resource + "-data"},
                    }
                ],
            },
        },
        "V1Pod",
    )


def _foreign_pvc_pod():
    resource = METADATA.resource_name
    return _model(
        {
            "metadata": {"name": "foreign", "namespace": resource, "uid": "pod-foreign"},
            "spec": {
                "containers": [{"name": "other", "image": "example"}],
                "volumes": [
                    {
                        "name": "data",
                        "persistentVolumeClaim": {"claimName": resource + "-data"},
                    }
                ],
            },
        },
        "V1Pod",
    )


class Missing(Exception):
    status = 404


class Cluster:
    def __init__(self, *, runtime: object = None, pods: list[object] | None = None) -> None:
        self.pvc = _pvc()
        self.runtime = _runtime() if runtime is None else runtime
        self.pods = V1PodList(items=[] if pods is None else pods, metadata=V1ListMeta())

    def read_namespaced_persistent_volume_claim(self, name: str, namespace: str):
        assert (name, namespace) == (METADATA.resource_name + "-data", METADATA.resource_name)
        return self.pvc

    def read_namespaced_stateful_set(self, name: str, namespace: str):
        assert (name, namespace) == (METADATA.resource_name, METADATA.resource_name)
        if self.runtime is Missing:
            raise Missing
        return self.runtime

    def list_namespaced_pod(self, namespace: str):
        assert namespace == METADATA.resource_name
        return self.pods


async def test_wait_for_runtime_allows_only_an_authenticated_zero_target_pod_to_be_retryable() -> (
    None
):
    cluster = Cluster(pods=[_runtime_pod()])

    with pytest.raises(MetadataConflict):
        await verify_stopped_cell(cluster, cluster, metadata=METADATA, pvc_uid="pvc-alpha")

    with pytest.raises(DriverRetryable):
        await verify_stopped_cell(
            cluster,
            cluster,
            metadata=METADATA,
            pvc_uid="pvc-alpha",
            wait_for_runtime=True,
        )


@pytest.mark.parametrize(
    "runtime,pod",
    [
        (_runtime(uid=None), _runtime_pod()),
        (_runtime(), _runtime_pod(annotations={})),
        (_runtime(), _runtime_pod(owner_uid="replacement")),
        (Missing, _runtime_pod()),
    ],
)
async def test_wait_for_runtime_refuses_unauthenticated_runtime_evidence(
    runtime: object, pod: object
) -> None:
    cluster = Cluster(runtime=runtime, pods=[pod])

    with pytest.raises(MetadataConflict):
        await verify_stopped_cell(
            cluster,
            cluster,
            metadata=METADATA,
            pvc_uid="pvc-alpha",
            wait_for_runtime=True,
        )


async def test_wait_for_runtime_validates_all_pods_before_classifying_a_retry() -> None:
    cluster = Cluster(pods=[_runtime_pod(), _foreign_pvc_pod()])

    with pytest.raises(MetadataConflict):
        await verify_stopped_cell(
            cluster,
            cluster,
            metadata=METADATA,
            pvc_uid="pvc-alpha",
            wait_for_runtime=True,
        )


def _lease(*, holder: str = METADATA.operation_id, renew: datetime = NOW - timedelta(seconds=121)):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=METADATA.resource_name + "-maintenance",
            namespace=METADATA.resource_name,
            uid="lease-alpha",
            resource_version="1",
            annotations=METADATA.kubernetes_annotations,
            deletion_timestamp=None,
        ),
        spec=SimpleNamespace(holder_identity=holder, lease_duration_seconds=120, renew_time=renew),
    )


async def test_elapsed_lease_is_retryable_only_for_the_explicit_wait_path() -> None:
    class Coordination:
        def read_namespaced_lease(self, name: str, namespace: str):
            return _lease()

    adapter = KubernetesMaintenanceLeaseAdapter(coordination_v1=Coordination(), now=lambda: NOW)
    with pytest.raises(MetadataConflict):
        await adapter.assert_owned(METADATA, METADATA.operation_id)
    with pytest.raises(DriverRetryable):
        await adapter.assert_owned(METADATA, METADATA.operation_id, retry_elapsed=True)


async def test_elapsed_wait_path_refuses_missing_or_foreign_lease() -> None:
    class MissingCoordination:
        def read_namespaced_lease(self, name: str, namespace: str):
            raise Missing

    missing = KubernetesMaintenanceLeaseAdapter(
        coordination_v1=MissingCoordination(), now=lambda: NOW
    )
    with pytest.raises(DriverRetryable):
        await missing.assert_owned(METADATA, METADATA.operation_id, retry_elapsed=True)

    class ForeignCoordination:
        def read_namespaced_lease(self, name: str, namespace: str):
            return _lease(holder="foreign-operation")

    foreign = KubernetesMaintenanceLeaseAdapter(
        coordination_v1=ForeignCoordination(), now=lambda: NOW
    )
    with pytest.raises(MetadataConflict):
        await foreign.assert_owned(METADATA, METADATA.operation_id, retry_elapsed=True)


def test_recovery_wait_options_are_keyword_only_and_default_to_strict_proofs() -> None:
    for method, parameter_name in (
        (KubernetesMaintenanceLeaseAdapter.assert_owned, "retry_elapsed"),
        (KubernetesCellAdapter.verify_governance_stopped, "wait_for_runtime"),
        (verify_stopped_cell, "wait_for_runtime"),
    ):
        parameter = inspect.signature(method).parameters[parameter_name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is False
