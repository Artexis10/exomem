from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from kubernetes.client import ApiClient, V1ListMeta, V1PodList

from exomem_provisioner.driver import DriverRetryable, LostAcknowledgement
from exomem_provisioner.governance_storage_init import (
    KubernetesGovernanceStorageInitAdapter,
)
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata
from exomem_provisioner.provider_identity import (
    ProviderRecoveryIdentityCodec,
    ProviderReference,
)
from exomem_provisioner.repository import ClaimConflict

METADATA = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7)
IMAGE = "registry.example/runtime@sha256:" + "a" * 64
CODEC = ProviderRecoveryIdentityCodec.from_secret("storage-init-test-identity")


class ApiMissing(Exception):
    status = 404


def _model(value: dict[str, object], kind: str):
    return ApiClient().deserialize(SimpleNamespace(data=json.dumps(value)), kind)


def _envelope(kind: str) -> str:
    name = METADATA.resource_name + ("-init" if kind == "Job" else "-init-request")
    return CODEC.seal(
        provider="kubernetes",
        provider_reference=ProviderReference.kubernetes(
            provider="kubernetes",
            api_version="batch/v1" if kind == "Job" else "v1",
            kind=kind,
            namespace=METADATA.resource_name,
            name=name,
        ),
        tenant_id=METADATA.tenant_id,
        cell_id=METADATA.subject_id,
        operation_id=METADATA.operation_id,
        fence_generation=METADATA.fence_generation,
    )


JOB_ENVELOPE = _envelope("Job")
CONFIG_ENVELOPE = _envelope("ConfigMap")
INIT_REQUEST = {
    "request_id": "12345678-1234-4234-8234-123456789abc",
    "operation_id": METADATA.operation_id,
    "cell_id": METADATA.subject_id,
    "vault_id": METADATA.tenant_id,
    "vault_root": "/var/lib/exomem/vault",
    "state_root": "/var/lib/exomem/state",
    "log_root": "/var/lib/exomem/logs",
    "expected_release": "0.75.0",
    "expected_protocol": "1",
    "runtime_uid": 10001,
    "runtime_gid": 10001,
    "active_credential_version": "credential-v1",
}


def _labels(*, template: bool = False, storage_init: bool = True) -> dict[str, str]:
    values = {
        "app.kubernetes.io/name": "exomem-cell",
        "exomem.io/cell": METADATA.resource_name,
    }
    if not template:
        values.update(
            {
                "app.kubernetes.io/instance": METADATA.resource_name,
                "app.kubernetes.io/part-of": "exomem-hosted",
            }
        )
    if storage_init:
        values["exomem.io/storage-init"] = "true"
    return values


def _pod_spec() -> dict[str, object]:
    return {
        "runtimeClassName": "exomem-storage-init",
        "serviceAccountName": METADATA.resource_name,
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [
            {
                "name": "exomem",
                "image": IMAGE,
                "imagePullPolicy": "IfNotPresent",
                "terminationMessagePath": "/dev/termination-log",
                "terminationMessagePolicy": "File",
                "args": [
                    "hosted",
                    "init",
                    "--contract-version",
                    "1",
                    "--request-file",
                    "/run/exomem/operator-requests/init.json",
                ],
                "env": [
                    {"name": "EXOMEM_LOG_DIR", "value": "/dev"},
                    {"name": "EXOMEM_HOSTED_OFFLINE_STATE_MIGRATION", "value": "1"},
                ],
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "1", "memory": "1Gi"},
                },
                "securityContext": {
                    "runAsUser": 0,
                    "runAsGroup": 0,
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {
                        "drop": ["ALL"],
                        "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
                    },
                },
                "volumeMounts": [
                    {"name": "data", "mountPath": "/var/lib/exomem"},
                    {
                        "name": "credentials",
                        "mountPath": "/run/exomem/credentials",
                        "readOnly": True,
                    },
                    {
                        "name": "init-request",
                        "mountPath": "/run/exomem/operator-requests/init.json",
                        "subPath": "init.json",
                        "readOnly": True,
                    },
                ],
            }
        ],
        "volumes": [
            {
                "name": "data",
                "persistentVolumeClaim": {"claimName": METADATA.resource_name + "-data"},
            },
            {
                "name": "credentials",
                "secret": {"secretName": "exomem-cell-credentials", "defaultMode": 292},
            },
            {
                "name": "init-request",
                "configMap": {
                    "name": METADATA.resource_name + "-init-request",
                    "defaultMode": 292,
                },
            },
        ],
    }


def _job(*, state: str = "complete", deleting: bool = False):
    if state == "complete":
        status: dict[str, object] = {
            "succeeded": 1,
            "failed": 0,
            "active": 0,
            "terminating": 0,
            "conditions": [{"type": "Complete", "status": "True"}],
        }
    elif state == "failed":
        status = {
            "succeeded": 0,
            "failed": 1,
            "active": 0,
            "terminating": 0,
            "conditions": [{"type": "Failed", "status": "True"}],
        }
    else:
        status = {"succeeded": 0, "failed": 0, "active": 1, "terminating": 0}
    metadata: dict[str, object] = {
        "name": METADATA.resource_name + "-init",
        "namespace": METADATA.resource_name,
        "uid": "init-job-uid",
        "resourceVersion": "17",
        "labels": _labels(),
        "annotations": {
            **METADATA.kubernetes_annotations,
            "exomem.io/recovery-envelope": JOB_ENVELOPE,
        },
    }
    if deleting:
        metadata["deletionTimestamp"] = "2026-09-08T12:00:00Z"
    return _model(
        {
            "metadata": metadata,
            "spec": {
                "backoffLimit": 1,
                "activeDeadlineSeconds": 120,
                "ttlSecondsAfterFinished": 300,
                "completionMode": "NonIndexed",
                "completions": 1,
                "parallelism": 1,
                "suspend": False,
                "manualSelector": False,
                "template": {
                    "metadata": {"labels": _labels(template=True)},
                    "spec": _pod_spec(),
                },
            },
            "status": status,
        },
        "V1Job",
    )


def _pod(
    *,
    name: str | None = None,
    phase: str = "Succeeded",
    exit_code: int = 0,
    owner_uid: str = "init-job-uid",
    labels: dict[str, str] | None = None,
    spec: dict[str, object] | None = None,
    deleting: bool = False,
):
    pod_name = name or METADATA.resource_name + "-init-abcde"
    metadata: dict[str, object] = {
        "name": pod_name,
        "namespace": METADATA.resource_name,
        "uid": "pod-uid-" + pod_name[-5:],
        "labels": {
            **_labels(template=True),
            "batch.kubernetes.io/job-name": METADATA.resource_name + "-init",
            "job-name": METADATA.resource_name + "-init",
            **(labels or {}),
        },
        "ownerReferences": [
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "name": METADATA.resource_name + "-init",
                "uid": owner_uid,
                "controller": True,
                "blockOwnerDeletion": True,
            }
        ],
    }
    if deleting:
        metadata["deletionTimestamp"] = "2026-09-08T12:00:00Z"
    scheduled = copy.deepcopy(spec or _pod_spec())
    scheduled.update(
        {
            "nodeName": "worker-alpha",
            "dnsPolicy": "ClusterFirst",
            "schedulerName": "default-scheduler",
            "terminationGracePeriodSeconds": 30,
            "enableServiceLinks": True,
            "serviceAccount": METADATA.resource_name,
            "preemptionPolicy": "PreemptLowerPriority",
            "priority": 0,
        }
    )
    return _model(
        {
            "metadata": metadata,
            "spec": scheduled,
            "status": {
                "phase": phase,
                "containerStatuses": [
                    {
                        "name": "exomem",
                        "image": IMAGE,
                        "imageID": "registry.example/runtime@sha256:" + "b" * 64,
                        "ready": False,
                        "restartCount": 0,
                        "state": {"terminated": {"exitCode": exit_code}},
                    }
                ]
                if phase in {"Succeeded", "Failed"}
                else [],
            },
        },
        "V1Pod",
    )


def _config_map(*, request: dict[str, object] = INIT_REQUEST, envelope: str = CONFIG_ENVELOPE):
    return _model(
        {
            "metadata": {
                "name": METADATA.resource_name + "-init-request",
                "namespace": METADATA.resource_name,
                "uid": "init-request-uid",
                "resourceVersion": "11",
                "labels": _labels(storage_init=False),
                "annotations": {
                    **METADATA.kubernetes_annotations,
                    "exomem.io/recovery-envelope": envelope,
                },
            },
            "data": {"init.json": json.dumps(request, sort_keys=True, separators=(",", ":"))},
        },
        "V1ConfigMap",
    )


def _pvc(*, uid: str = "pvc-uid"):
    return _model(
        {
            "metadata": {
                "name": METADATA.resource_name + "-data",
                "namespace": METADATA.resource_name,
                "uid": uid,
                "annotations": dict(METADATA.kubernetes_annotations),
            },
            "spec": {"volumeName": "pv-alpha"},
            "status": {"phase": "Bound"},
        },
        "V1PersistentVolumeClaim",
    )


def _runtime(replicas: object = 0):
    return _model(
        {
            "metadata": {"name": METADATA.resource_name, "namespace": METADATA.resource_name},
            "spec": {
                "replicas": replicas,
                "serviceName": METADATA.resource_name,
                "selector": {"matchLabels": {"app": "example"}},
                "template": {
                    "metadata": {"labels": {"app": "example"}},
                    "spec": {"containers": [{"name": "exomem", "image": IMAGE}]},
                },
            },
        },
        "V1StatefulSet",
    )


class Cluster:
    def __init__(self, *, state: str = "complete") -> None:
        self.job = _job(state=state)
        self.config_map = _config_map()
        self.pvc = _pvc()
        self.runtime: object = ApiMissing
        self.pods = V1PodList(items=[_pod()], metadata=V1ListMeta())
        self.deleted: list[dict[str, object]] = []
        self.reads = 0
        self.read_hook = lambda: None
        self.delete_hook = lambda: None

    def read_namespaced_job(self, name: str, namespace: str):
        assert (name, namespace) == (METADATA.resource_name + "-init", METADATA.resource_name)
        self.reads += 1
        self.read_hook()
        if self.job is None:
            raise ApiMissing
        return copy.deepcopy(self.job)

    def read_namespaced_config_map(self, name: str, namespace: str):
        assert (name, namespace) == (
            METADATA.resource_name + "-init-request",
            METADATA.resource_name,
        )
        return copy.deepcopy(self.config_map)

    def read_namespaced_persistent_volume_claim(self, name: str, namespace: str):
        assert (name, namespace) == (METADATA.resource_name + "-data", METADATA.resource_name)
        return copy.deepcopy(self.pvc)

    def read_namespaced_stateful_set(self, name: str, namespace: str):
        assert name == namespace == METADATA.resource_name
        if self.runtime is ApiMissing:
            raise ApiMissing
        return copy.deepcopy(self.runtime)

    def list_namespaced_pod(self, namespace: str):
        assert namespace == METADATA.resource_name
        return copy.deepcopy(self.pods)

    def delete_namespaced_job(self, name: str, namespace: str, body: dict[str, object]):
        assert (name, namespace) == (METADATA.resource_name + "-init", METADATA.resource_name)
        self.deleted.append(copy.deepcopy(body))
        self.job = None
        self.pods = V1PodList(items=[], metadata=V1ListMeta())
        self.delete_hook()


class Guard:
    def __init__(self) -> None:
        self.calls = 0
        self.fail_at: int | None = None

    async def __call__(self) -> None:
        self.calls += 1
        if self.calls == self.fail_at:
            raise ClaimConflict("AUTHORITY_LOST")


def _adapter(cluster: Cluster) -> KubernetesGovernanceStorageInitAdapter:
    return KubernetesGovernanceStorageInitAdapter(
        core_v1=cluster,
        batch_v1=cluster,
        apps_v1=cluster,
        identity_verifier=CODEC.verifier(),
        runtime_image=IMAGE,
    )


def test_adapter_requires_a_digest_pinned_runtime_image() -> None:
    cluster = Cluster()
    with pytest.raises(MetadataConflict):
        KubernetesGovernanceStorageInitAdapter(
            core_v1=cluster,
            batch_v1=cluster,
            apps_v1=cluster,
            identity_verifier=CODEC.verifier(),
            runtime_image="registry.example/runtime:latest",
        )


def _arguments(guard: Guard | None = None) -> dict[str, object]:
    return {
        "recovery_envelope": JOB_ENVELOPE,
        "init_request_recovery_envelope": CONFIG_ENVELOPE,
        "init_request": INIT_REQUEST,
        "pvc_uid": "pvc-uid",
        "effect_guard": guard or Guard(),
    }


async def test_completed_proves_signed_job_config_input_pod_and_stopped_volume() -> None:
    cluster = Cluster()
    guard = Guard()
    assert await _adapter(cluster).completed(METADATA, **_arguments(guard))
    assert guard.calls >= 2


@pytest.mark.parametrize("target", ["job", "template", "pod", "config_map"])
async def test_completed_refuses_unexpected_execution_annotations(target: str) -> None:
    cluster = Cluster()
    if target == "job":
        cluster.job.metadata.annotations["k8s.v1.cni.cncf.io/networks"] = "attacker-net"
    elif target == "template":
        cluster.job.spec.template.metadata.annotations = {
            "k8s.v1.cni.cncf.io/networks": "attacker-net"
        }
    elif target == "pod":
        cluster.pods.items[0].metadata.annotations = {"k8s.v1.cni.cncf.io/networks": "attacker-net"}
    else:
        cluster.config_map.metadata.annotations["k8s.v1.cni.cncf.io/networks"] = "attacker-net"
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).completed(METADATA, **_arguments())


@pytest.mark.parametrize("target", ["template", "pod"])
async def test_completed_refuses_network_policy_escape_label(target: str) -> None:
    cluster = Cluster()
    if target == "template":
        cluster.job.spec.template.metadata.labels["network-access"] = "allowed"
    else:
        cluster.pods.items[0].metadata.labels["network-access"] = "allowed"
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).completed(METADATA, **_arguments())


async def test_completed_accepts_bound_helm_ownership_metadata() -> None:
    cluster = Cluster()
    for resource in (cluster.job, cluster.config_map):
        resource.metadata.labels["app.kubernetes.io/managed-by"] = "Helm"
        resource.metadata.annotations.update(
            {
                "meta.helm.sh/release-name": METADATA.resource_name,
                "meta.helm.sh/release-namespace": METADATA.resource_name,
            }
        )
    assert await _adapter(cluster).completed(METADATA, **_arguments())


async def test_completed_accepts_bound_job_controller_labels() -> None:
    cluster = Cluster()
    controller_labels = {
        "batch.kubernetes.io/controller-uid": "init-job-uid",
        "batch.kubernetes.io/job-name": METADATA.resource_name + "-init",
        "controller-uid": "init-job-uid",
        "job-name": METADATA.resource_name + "-init",
    }
    cluster.job.spec.template.metadata.labels.update(controller_labels)
    cluster.pods.items[0].metadata.labels.update(controller_labels)
    assert await _adapter(cluster).completed(METADATA, **_arguments())


@pytest.mark.parametrize("state", ["absent", "active"])
async def test_absent_or_active_initializer_remains_pending(state: str) -> None:
    cluster = Cluster(state="active")
    if state == "absent":
        cluster.job = None
        cluster.pods = V1PodList(items=[], metadata=V1ListMeta())
    assert not await _adapter(cluster).completed(METADATA, **_arguments())


async def test_absent_initializer_requires_same_bound_pvc_and_empty_candidate_inventory() -> None:
    cluster = Cluster()
    cluster.job = None
    cluster.pods = V1PodList(items=[], metadata=V1ListMeta())
    cluster.pvc = _pvc(uid="replacement")
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).completed(METADATA, **_arguments())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda cluster: setattr(
            cluster.job.spec.template.spec.containers[0], "image", "foreign/image:latest"
        ),
        lambda cluster: setattr(
            cluster.job.spec.template.spec.containers[0], "args", ["hosted", "migrate"]
        ),
        lambda cluster: cluster.job.metadata.labels.update({"exomem.io/storage-init": "false"}),
        lambda cluster: cluster.job.metadata.annotations.update(
            {"exomem.io/recovery-envelope": CONFIG_ENVELOPE}
        ),
        lambda cluster: cluster.pods.items[0].metadata.labels.update(
            {"exomem.io/storage-init": "false"}
        ),
    ],
)
async def test_changed_image_command_labels_or_envelope_is_terminal(mutation) -> None:
    cluster = Cluster()
    mutation(cluster)
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).completed(METADATA, **_arguments())


@pytest.mark.parametrize("kind", ["request", "envelope"])
async def test_config_map_executable_input_is_exact_and_separately_authenticated(kind: str) -> None:
    cluster = Cluster()
    if kind == "request":
        cluster.config_map = _config_map(request={**INIT_REQUEST, "vault_id": "foreign"})
    else:
        cluster.config_map = _config_map(envelope=JOB_ENVELOPE)
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).completed(METADATA, **_arguments())


async def test_expected_init_request_must_be_the_closed_initialize_shape() -> None:
    cluster = Cluster()
    request = {**INIT_REQUEST, "artifact_reference": "restore://foreign"}
    cluster.config_map = _config_map(request=request)
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).completed(
            METADATA,
            **({**_arguments(), "init_request": request}),
        )


@pytest.mark.parametrize("pod_kind", ["unlabelled-pvc", "runtime"])
async def test_foreign_unlabelled_pvc_or_runtime_pod_is_terminal(pod_kind: str) -> None:
    cluster = Cluster()
    if pod_kind == "unlabelled-pvc":
        pod = _pod(name="foreign")
        pod.metadata.labels = {}
        pod.metadata.owner_references = []
    else:
        pod = _pod(name=METADATA.resource_name + "-0")
        pod.metadata.labels = {}
        pod.metadata.owner_references = []
        pod.spec.volumes = []
    cluster.pods.items.append(pod)
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).completed(METADATA, **_arguments())


async def test_incomplete_namespace_inventory_is_retryable_not_absence() -> None:
    cluster = Cluster()
    cluster.pods.metadata = V1ListMeta(_continue="next-page")
    with pytest.raises(DriverRetryable):
        await _adapter(cluster).completed(METADATA, **_arguments())


@pytest.mark.parametrize(
    "conditions",
    [
        [{"type": "Complete", "status": True}],
        [
            {"type": "Complete", "status": "True"},
            {"type": "Complete", "status": "True"},
        ],
        [
            {"type": "Complete", "status": "True"},
            {"type": "Failed", "status": "True"},
        ],
    ],
)
async def test_malformed_complete_condition_is_terminal(conditions) -> None:
    cluster = Cluster()
    cluster.job.status.conditions = conditions
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).completed(METADATA, **_arguments())


async def test_cleanup_rereads_exact_job_then_deletes_with_uid_and_resource_version() -> None:
    cluster = Cluster()
    guard = Guard()
    await _adapter(cluster).cleanup(METADATA, **_arguments(guard))
    assert guard.calls >= 3
    assert cluster.reads >= 3
    assert cluster.deleted == [
        {
            "propagationPolicy": "Foreground",
            "preconditions": {"uid": "init-job-uid", "resourceVersion": "17"},
        }
    ]


@pytest.mark.parametrize("field", ["uid", "resource_version"])
async def test_cleanup_refuses_job_replacement_between_proof_and_delete(field: str) -> None:
    cluster = Cluster()

    def replace_on_second_read() -> None:
        if cluster.reads == 2:
            setattr(cluster.job.metadata, field, "replacement")

    cluster.read_hook = replace_on_second_read
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).cleanup(METADATA, **_arguments())
    assert cluster.deleted == []


async def test_cleanup_guard_loss_before_delete_propagates_without_effect() -> None:
    cluster = Cluster()
    guard = Guard()
    guard.fail_at = 2
    with pytest.raises(ClaimConflict):
        await _adapter(cluster).cleanup(METADATA, **_arguments(guard))
    assert cluster.deleted == []


async def test_cleanup_rechecks_guard_immediately_after_final_provider_reads() -> None:
    cluster = Cluster()
    guard = Guard()
    guard.fail_at = 3
    with pytest.raises(ClaimConflict):
        await _adapter(cluster).cleanup(METADATA, **_arguments(guard))
    assert cluster.deleted == []


async def test_terminating_exact_job_waits_without_repeating_delete() -> None:
    cluster = Cluster()
    cluster.job = _job(deleting=True)
    cluster.pods = V1PodList(items=[_pod(deleting=True)], metadata=V1ListMeta())
    with pytest.raises(DriverRetryable):
        await _adapter(cluster).cleanup(METADATA, **_arguments())
    assert cluster.deleted == []


async def test_lost_delete_acknowledgement_succeeds_only_after_proven_absence() -> None:
    cluster = Cluster()

    def lost_after_delete() -> None:
        raise LostAcknowledgement("private uncertain acknowledgement")

    cluster.delete_hook = lost_after_delete
    await _adapter(cluster).cleanup(METADATA, **_arguments())
    assert len(cluster.deleted) == 1


async def test_lost_delete_acknowledgement_with_remaining_job_is_retryable() -> None:
    cluster = Cluster()

    def uncertain_delete(name, namespace, body):
        cluster.deleted.append(copy.deepcopy(body))
        raise LostAcknowledgement("private uncertain acknowledgement")

    cluster.delete_namespaced_job = uncertain_delete  # type: ignore[method-assign]
    with pytest.raises(DriverRetryable) as raised:
        await _adapter(cluster).cleanup(METADATA, **_arguments())
    assert "private uncertain acknowledgement" not in str(raised.value)


async def test_failed_job_is_never_cleaned_up() -> None:
    cluster = Cluster(state="failed")
    cluster.pods = V1PodList(items=[_pod(phase="Failed", exit_code=1)], metadata=V1ListMeta())
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).cleanup(METADATA, **_arguments())
    assert cluster.deleted == []


@pytest.mark.parametrize("kind", ["pvc", "runtime"])
async def test_completion_refuses_changed_bound_pvc_or_nonzero_runtime(kind: str) -> None:
    cluster = Cluster()
    if kind == "pvc":
        cluster.pvc = _pvc(uid="replacement")
    else:
        cluster.runtime = _runtime(1)
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).completed(METADATA, **_arguments())


async def test_cleanup_rechecks_same_bound_pvc_after_deletion() -> None:
    cluster = Cluster()

    def replace_pvc() -> None:
        cluster.pvc = _pvc(uid="replacement")

    cluster.delete_hook = replace_pvc
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).cleanup(METADATA, **_arguments())


async def test_unrelated_namespace_pod_does_not_block_proof_or_cleanup() -> None:
    cluster = Cluster()
    unrelated = _pod(name="unrelated")
    unrelated.metadata.labels = {}
    unrelated.metadata.owner_references = []
    unrelated.spec.volumes = []
    cluster.pods.items.append(unrelated)
    assert await _adapter(cluster).completed(METADATA, **_arguments())
    await _adapter(cluster).cleanup(METADATA, **_arguments())
