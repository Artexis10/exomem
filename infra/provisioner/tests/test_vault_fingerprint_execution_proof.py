from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from kubernetes.client import ApiClient, V1ListMeta, V1PodList

from exomem_provisioner.adapters import KubernetesVaultFingerprintAdapter
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata

METADATA = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7)
IMAGE = "registry.example/provisioner@sha256:" + "a" * 64
OPERATION = "rollforward-alpha"
ENVELOPE = "signed"
RECORD = json.dumps(
    {
        "artifact": "exomem-hosted-vault-fingerprint",
        "schemaVersion": 1,
        "sha256": "b" * 64,
    }
)


def _model(value: dict[str, object], kind: str):
    return ApiClient().deserialize(SimpleNamespace(data=json.dumps(value)), kind)


def _adapter() -> KubernetesVaultFingerprintAdapter:
    return KubernetesVaultFingerprintAdapter(
        core_v1=SimpleNamespace(), batch_v1=SimpleNamespace(), image=IMAGE, sleep=lambda _: None
    )


def _job(adapter: KubernetesVaultFingerprintAdapter, *, mutate=None):
    value = copy.deepcopy(
        adapter._body(METADATA, operation_id=OPERATION, phase="before", recovery_envelope=ENVELOPE)
    )
    value["metadata"].update({"uid": "fingerprint-job", "resourceVersion": "2"})  # type: ignore[index]
    value["status"] = {"succeeded": 1, "failed": 0}
    if mutate is not None:
        mutate(value)
    return _model(value, "V1Job")


def _pod(adapter: KubernetesVaultFingerprintAdapter, *, mutate=None):
    body = adapter._body(
        METADATA, operation_id=OPERATION, phase="before", recovery_envelope=ENVELOPE
    )
    value = copy.deepcopy(body["spec"]["template"])
    name = METADATA.resource_name + "-init"
    value["metadata"].update(  # type: ignore[index]
        {
            "name": name + "-abcde",
            "namespace": METADATA.resource_name,
            "uid": "fingerprint-pod",
            "labels": {**value["metadata"]["labels"], "job-name": name},
            "ownerReferences": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": name,
                    "uid": "fingerprint-job",
                    "controller": True,
                }
            ],
        }
    )
    value["status"] = {  # type: ignore[index]
        "phase": "Succeeded",
        "containerStatuses": [
            {
                "name": "exomem",
                "image": IMAGE,
                "imageID": "sha256:" + "a" * 64,
                "ready": False,
                "restartCount": 0,
                "state": {"terminated": {"exitCode": 0, "message": RECORD}},
            }
        ],
    }
    if mutate is not None:
        mutate(value)
    return _model(
        {"metadata": value["metadata"], "spec": value["spec"], "status": value["status"]}, "V1Pod"
    )


class Cluster:
    def __init__(
        self,
        adapter: KubernetesVaultFingerprintAdapter,
        *,
        job_mutate=None,
        pod_mutate=None,
        pods=None,
    ) -> None:
        self.adapter = adapter
        self.job = _job(adapter, mutate=job_mutate)
        self.pods = [_pod(adapter, mutate=pod_mutate)] if pods is None else pods
        adapter._batch.read_namespaced_job = self.read_namespaced_job
        adapter._batch.delete_namespaced_job = self.delete_namespaced_job
        adapter._core.list_namespaced_pod = self.list_namespaced_pod
        self.deleted = False

    def read_namespaced_job(self, name: str, namespace: str):
        if self.deleted:
            raise type("Missing", (Exception,), {"status": 404})()
        return self.job

    def delete_namespaced_job(self, *args: object, **kwargs: object) -> None:
        self.deleted = True

    def list_namespaced_pod(self, namespace: str, **kwargs: object):
        assert namespace == METADATA.resource_name
        return V1PodList(items=[] if self.deleted else self.pods, metadata=V1ListMeta())


async def _fingerprint(cluster: Cluster) -> str:
    return await cluster.adapter.fingerprint(
        METADATA, operation_id=OPERATION, phase="before", recovery_envelope=ENVELOPE
    )


async def test_accepts_a_canonical_fingerprint_job_and_result_pod() -> None:
    adapter = _adapter()
    assert await _fingerprint(Cluster(adapter)) == "b" * 64


async def test_accepts_only_the_known_api_and_job_controller_defaults() -> None:
    adapter = _adapter()
    cluster = Cluster(adapter)
    wire = adapter._wire(cluster.job)
    pod = adapter._wire(cluster.pods[0])
    labels = {
        "job-name": METADATA.resource_name + "-init",
        "batch.kubernetes.io/job-name": METADATA.resource_name + "-init",
        "controller-uid": "fingerprint-job",
        "batch.kubernetes.io/controller-uid": "fingerprint-job",
    }
    for meta in (wire["metadata"], wire["spec"]["template"]["metadata"], pod["metadata"]):
        meta["labels"].update(labels)
    wire["spec"].update(
        {
            "completionMode": "NonIndexed",
            "suspend": False,
            "manualSelector": False,
            "selector": {"matchLabels": {"batch.kubernetes.io/controller-uid": "fingerprint-job"}},
        }
    )
    for spec in (wire["spec"]["template"]["spec"], pod["spec"]):
        spec.update(
            {
                "dnsPolicy": "ClusterFirst",
                "schedulerName": "default-scheduler",
                "terminationGracePeriodSeconds": 30,
                "enableServiceLinks": True,
                "serviceAccount": METADATA.resource_name,
                "preemptionPolicy": "PreemptLowerPriority",
                "priority": 0,
            }
        )
    pod["spec"].update(
        {
            "nodeName": "node-alpha",
            "tolerations": [
                {
                    "key": "node.kubernetes.io/not-ready",
                    "operator": "Exists",
                    "effect": "NoExecute",
                    "tolerationSeconds": 300,
                }
            ],
        }
    )
    cluster.job = _model(wire, "V1Job")
    cluster.pods = [_model(pod, "V1Pod")]
    assert await _fingerprint(cluster) == "b" * 64


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["spec"]["template"]["spec"]["containers"][0].update(
            {"command": ["sh"]}
        ),
        lambda value: value["spec"]["template"]["spec"]["volumes"][0][
            "persistentVolumeClaim"
        ].update({"claimName": "foreign-data"}),
        lambda value: value["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0].update(
            {"subPath": "state"}
        ),
        lambda value: value["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0].update(
            {"readOnly": False}
        ),
        lambda value: value["spec"]["template"]["spec"].update({"serviceAccountName": "default"}),
        lambda value: value["spec"]["template"]["spec"].update(
            {"automountServiceAccountToken": True}
        ),
        lambda value: value["spec"]["template"]["spec"]["containers"][0]["securityContext"].update(
            {"readOnlyRootFilesystem": False}
        ),
        lambda value: value["spec"].update({"activeDeadlineSeconds": 601}),
        lambda value: value["spec"].update({"parallelism": 2}),
        lambda value: value["spec"].update({"manualSelector": True}),
        lambda value: value["spec"].update({"selector": {"matchLabels": {"app": "foreign"}}}),
        lambda value: value["spec"]["template"]["metadata"]["annotations"].update(
            {"exomem.io/cell-id": "foreign"}
        ),
    ],
)
async def test_refuses_executable_job_mutations(mutate) -> None:
    adapter = _adapter()
    with pytest.raises(MetadataConflict):
        await _fingerprint(Cluster(adapter, job_mutate=mutate))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["spec"]["containers"][0].update({"command": ["sh"]}),
        lambda value: value["spec"]["containers"][0]["volumeMounts"][0].update({"readOnly": False}),
        lambda value: value["metadata"].update({"name": METADATA.resource_name + "-foreign-copy"}),
        lambda value: value["metadata"].update({"uid": ""}),
        lambda value: value["metadata"].update({"namespace": "foreign"}),
        lambda value: value["metadata"]["annotations"].update({"exomem.io/cell-id": "foreign"}),
        lambda value: value["metadata"]["ownerReferences"][0].update({"controller": False}),
        lambda value: value["metadata"]["labels"].update({"controller-uid": "foreign"}),
    ],
)
async def test_refuses_result_pod_execution_or_identity_drift(mutate) -> None:
    adapter = _adapter()
    with pytest.raises(MetadataConflict):
        await _fingerprint(Cluster(adapter, pod_mutate=mutate))


async def test_refuses_a_label_stripped_duplicate_fingerprint_candidate() -> None:
    adapter = _adapter()
    duplicate = _pod(adapter)
    duplicate.metadata.labels = {}
    cluster = Cluster(adapter, pods=[_pod(adapter), duplicate])
    with pytest.raises(MetadataConflict):
        await _fingerprint(cluster)


async def test_rejects_a_boolean_terminal_schema_version() -> None:
    adapter = _adapter()
    cluster = Cluster(adapter)
    cluster.pods[0].status.container_statuses[0].state.terminated.message = json.dumps(
        {
            "artifact": "exomem-hosted-vault-fingerprint",
            "schemaVersion": True,
            "sha256": "b" * 64,
        }
    )
    with pytest.raises(MetadataConflict):
        await _fingerprint(cluster)


@pytest.mark.parametrize(
    "page_metadata",
    [
        {"continue": "", "remainingItemCount": 1},
        {"continue": False},
        {"remainingItemCount": True},
        {"remainingItemCount": -1},
    ],
)
async def test_incomplete_or_malformed_inventory_cannot_prove_the_result(page_metadata) -> None:
    adapter = _adapter()
    cluster = Cluster(adapter)
    adapter._core.list_namespaced_pod = lambda *args, **kwargs: {
        "metadata": page_metadata,
        "items": [adapter._wire(cluster.pods[0])],
    }
    with pytest.raises(MetadataConflict):
        await _fingerprint(cluster)
    assert not cluster.deleted


async def test_foreign_namespace_item_cannot_be_ignored_as_a_non_candidate() -> None:
    adapter = _adapter()
    cluster = Cluster(adapter)
    foreign = _pod(adapter)
    foreign.metadata.name = "unrelated"
    foreign.metadata.namespace = "foreign"
    foreign.metadata.labels = {}
    foreign.metadata.owner_references = []
    cluster.pods.append(foreign)
    with pytest.raises(MetadataConflict):
        await _fingerprint(cluster)


async def test_allows_the_normal_runtime_pod_to_share_the_bound_pvc() -> None:
    adapter = _adapter()
    runtime = _model(
        {
            "metadata": {
                "name": METADATA.resource_name + "-0",
                "namespace": METADATA.resource_name,
                "uid": "runtime",
            },
            "spec": {
                "containers": [{"name": "exomem", "image": "runtime"}],
                "volumes": [
                    {
                        "name": "data",
                        "persistentVolumeClaim": {"claimName": METADATA.resource_name + "-data"},
                    }
                ],
            },
        },
        "V1Pod",
    )
    assert await _fingerprint(Cluster(adapter, pods=[_pod(adapter), runtime])) == "b" * 64
