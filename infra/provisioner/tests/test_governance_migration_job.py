from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from kubernetes.client import ApiClient

from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata


def _module():
    from exomem_provisioner import governance_migration_job

    return governance_migration_job


def _request(**changes):
    return _module().MigrationJobRequest(
        **{
            "metadata": OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7),
            "vault_id": "tenant-alpha",
            "pvc_uid": "pvc-alpha",
            "runtime_image": "ghcr.io/example/runtime@sha256:" + "a" * 64,
            "custody_revision": "b" * 64,
            "phase": "inspect",
            "source_store_digest": None,
            "plan_digest": None,
            **changes,
        }
    )


def _terminal(request):
    value = {
        "artifact": "exomem-hosted-governance-migration",
        "schemaVersion": 1,
        "phase": request.phase,
        "requestSha256": request.sha256,
        "custodyRevision": request.custody_revision,
        "actualSchema": 4 if request.phase == "commit" else 3,
        "membershipSchema": 3,
        "governanceEnrolled": request.phase == "commit",
        "sourceStoreDigest": request.source_store_digest or "c" * 64,
    }
    if request.phase != "inspect":
        value.update(
            planDigest=request.plan_digest or "d" * 64,
            backupReference="exomem-governance-v3-backup://sha256/" + "f" * 64,
            activationStoreId="activation-alpha",
            activationEpoch=1,
            activationStateDigest="e" * 64,
        )
        if request.phase == "prepare":
            value["backupDigest"] = "f" * 64
        else:
            value["replayed"] = False
    return value


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class ApiMissing(Exception):
    status = 404


class Cluster:
    def __init__(self, request):
        self.request = request
        self.job = None
        self.created = []
        self.deleted = []
        self.runtime_pods = []
        self.job_pods = []
        self.desired = 0
        self.pvc_uid = request.pvc_uid
        self.create_hook = lambda: None
        self.read_hook = lambda: None
        self.model_job = False

    def read_namespaced_persistent_volume_claim(self, name, namespace):
        assert name == self.request.metadata.resource_name + "-data"
        assert namespace == self.request.metadata.resource_name
        return {
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": self.pvc_uid,
                "resourceVersion": "1",
                "annotations": self.request.metadata.kubernetes_annotations,
            },
            "spec": {"volumeName": "pv-alpha"},
            "status": {"phase": "Bound"},
        }

    def read_namespaced_stateful_set(self, name, namespace):
        assert name == namespace == self.request.metadata.resource_name
        return {
            "metadata": {
                "name": name,
                "namespace": namespace,
                "annotations": self.request.metadata.kubernetes_annotations,
            },
            "spec": {"replicas": self.desired},
        }

    def list_namespaced_pod(self, namespace, *, label_selector):
        assert namespace == self.request.metadata.resource_name
        if label_selector == f"job-name={namespace}-init":
            return SimpleNamespace(items=copy.deepcopy(self.job_pods))
        assert label_selector == (
            f"app.kubernetes.io/name=exomem-cell,exomem.io/cell={namespace},"
            "!exomem.io/storage-init,!exomem.io/vault-fingerprint"
        )
        return SimpleNamespace(items=copy.deepcopy(self.runtime_pods))

    def read_namespaced_job(self, name, namespace):
        assert name == namespace + "-init"
        self.read_hook()
        if self.job is None:
            raise ApiMissing
        if self.model_job:
            return ApiClient().deserialize(SimpleNamespace(data=json.dumps(self.job)), "V1Job")
        return copy.deepcopy(self.job)

    def create_namespaced_job(self, namespace, body):
        assert namespace == self.request.metadata.resource_name
        self.created.append(copy.deepcopy(body))
        self.job = copy.deepcopy(body)
        self.job["metadata"].update(uid="job-alpha", resourceVersion="2")
        self.job["status"] = {"succeeded": 1, "active": 0, "failed": 0}
        template = copy.deepcopy(body["spec"]["template"])
        template["metadata"].update(
            name=namespace + "-init-pod",
            namespace=namespace,
            uid="pod-alpha",
            ownerReferences=[
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": namespace + "-init",
                    "uid": "job-alpha",
                    "controller": True,
                }
            ],
        )
        template["metadata"]["labels"]["job-name"] = namespace + "-init"
        template["status"] = {
            "phase": "Succeeded",
            "containerStatuses": [
                {
                    "name": "exomem",
                    "state": {
                        "terminated": {
                            "exitCode": 0,
                            "message": _raw(_terminal(self.request)).decode(),
                        }
                    },
                }
            ],
        }
        self.job_pods = [template]
        self.create_hook()
        return copy.deepcopy(self.job)

    def delete_namespaced_job(self, name, namespace, body):
        assert name == namespace + "-init"
        assert body == {
            "propagationPolicy": "Foreground",
            "preconditions": {"uid": "job-alpha", "resourceVersion": "2"},
        }
        self.deleted.append(copy.deepcopy(body))
        self.job = None
        self.job_pods = []


def _adapter(cluster):
    return _module().KubernetesGovernanceMigrationAdapter(
        core_v1=cluster,
        apps_v1=cluster,
        batch_v1=cluster,
        sleep=lambda _: None,
        poll_attempts=4,
    )


def test_request_uses_original_replica_and_canonical_digest():
    request = _request()
    value = request.as_dict()
    assert set(value) == {
        "schemaVersion",
        "phase",
        "cellId",
        "vaultId",
        "replicaId",
        "operationId",
        "fenceGeneration",
        "pvcUid",
        "runtimeImage",
        "custodyRevision",
        "sourceStoreDigest",
        "planDigest",
    }
    assert value["replicaId"] == request.metadata.resource_name + "-0"
    assert value["operationId"] == request.metadata.operation_id
    assert value["fenceGeneration"] == request.metadata.fence_generation
    assert request.sha256 == hashlib.sha256(_raw(value)).hexdigest()


@pytest.mark.parametrize(
    "changes",
    [
        {"runtime_image": "runtime:latest"},
        {"custody_revision": "wrong"},
        {"pvc_uid": ""},
        {"vault_id": "../foreign"},
        {"vault_id": "foreign-vault"},
        {"phase": "enroll"},
        {"source_store_digest": "c" * 64},
        {"plan_digest": "d" * 64},
        {"phase": "prepare"},
        {"phase": "commit", "source_store_digest": "c" * 64},
    ],
)
def test_request_refuses_invalid_or_phase_inconsistent_inputs(changes):
    with pytest.raises(MetadataConflict):
        _request(**changes)


@pytest.mark.parametrize("phase", ["inspect", "prepare", "commit"])
def test_manifest_is_exact_nonroot_custody_read_only_job(phase):
    request = _request(
        phase=phase,
        source_store_digest=None if phase == "inspect" else "c" * 64,
        plan_digest="d" * 64 if phase == "commit" else None,
    )
    body = _module().build_governance_migration_job(request, recovery_envelope="signed-envelope")
    assert body["spec"]["podReplacementPolicy"] == "Failed"
    assert body["metadata"]["name"] == request.metadata.resource_name + "-init"
    spec = body["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["restartPolicy"] == "Never"
    assert len(spec["initContainers"]) == len(spec["containers"]) == 1
    container = spec["containers"][0]
    assert container["command"] == ["python"]
    assert container["args"] == ["-m", "exomem.hosted_governance_job"]
    assert container["image"] == spec["initContainers"][0]["image"] == request.runtime_image
    assert spec["initContainers"][0]["args"] == [
        "-m",
        "exomem.governance.authorization_hosted_mount",
    ]
    assert container["securityContext"]["runAsUser"] == 10001
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    environment = {entry["name"]: entry["value"] for entry in container["env"]}
    assert environment["EXOMEM_AUTH_SESSION_REPLICA_ID"] == request.metadata.resource_name + "-0"
    assert json.loads(environment["EXOMEM_GOVERNANCE_MIGRATION_REQUEST"]) == request.as_dict()
    mounts = {item["mountPath"]: item for item in container["volumeMounts"]}
    assert mounts["/run/exomem/authorization-session"]["readOnly"] is True
    assert mounts["/var/lib/exomem/logs"]["readOnly"] is True
    assert mounts["/var/lib/exomem/vault"]["readOnly"] is (phase == "inspect")
    assert mounts["/var/lib/exomem/state"]["readOnly"] is (phase == "inspect")
    assert "exomem-cell-credentials" not in json.dumps(body)


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": "private-sentinel"},
        {"schemaVersion": True},
        {"actualSchema": True},
        {"actualSchema": 4},
        {"governanceEnrolled": 0},
        {"governanceEnrolled": True},
        {"requestSha256": "f" * 64},
        {"custodyRevision": "f" * 64},
        {"phase": "commit"},
        {"membershipSchema": 5},
    ],
)
def test_terminal_refuses_wrong_closed_shape_or_identity(mutation):
    request = _request()
    with pytest.raises(MetadataConflict):
        _module().parse_migration_terminal(request, _raw({**_terminal(request), **mutation}))


@pytest.mark.parametrize("phase", ["inspect", "prepare", "commit"])
def test_terminal_accepts_exact_phase_contract(phase):
    request = _request(
        phase=phase,
        source_store_digest=None if phase == "inspect" else "c" * 64,
        plan_digest="d" * 64 if phase == "commit" else None,
    )
    expected = _terminal(request)
    assert _module().parse_migration_terminal(request, _raw(expected)) == expected


@pytest.mark.parametrize(
    "mutation",
    [
        {"planDigest": "e" * 64},
        {"sourceStoreDigest": "e" * 64},
        {"replayed": 0},
        {"activationEpoch": True},
        {"activationStoreId": ""},
        {"backupReference": "file:///private/backup"},
        {"activationStateDigest": "bad"},
        {"governanceEnrolled": False},
        {"actualSchema": 3},
    ],
)
def test_commit_terminal_refuses_plan_source_or_target_shape_drift(mutation):
    request = _request(phase="commit", source_store_digest="c" * 64, plan_digest="d" * 64)
    with pytest.raises(MetadataConflict):
        _module().parse_migration_terminal(request, _raw({**_terminal(request), **mutation}))


@pytest.mark.asyncio
@pytest.mark.parametrize("sdk_model", [False, True])
async def test_adapter_proves_job_and_pod_then_deletes_exact_observed_uid(sdk_model):
    request = _request()
    cluster = Cluster(request)
    cluster.model_job = sdk_model
    evidence = await _adapter(cluster).run(request, recovery_envelope="signed-envelope")
    assert evidence.job_uid == "job-alpha"
    assert evidence.pod_uid == "pod-alpha"
    assert evidence.terminal == _terminal(request)
    assert len(cluster.created) == len(cluster.deleted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["desired", "runtime-pod", "pvc"])
async def test_adapter_refuses_unproven_stop_or_pvc_before_submission(kind):
    request = _request()
    cluster = Cluster(request)
    if kind == "desired":
        cluster.desired = 1
    elif kind == "runtime-pod":
        cluster.runtime_pods = [{"metadata": {"uid": "runtime-alpha"}}]
    else:
        cluster.pvc_uid = "foreign-pvc"
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).run(request, recovery_envelope="signed-envelope")
    assert cluster.created == cluster.deleted == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["owner", "extra-pod", "image", "message", "pvc", "resumed-runtime"]
)
async def test_adapter_refuses_drifted_result_without_cleanup(kind):
    request = _request()
    cluster = Cluster(request)

    def corrupt():
        if kind == "owner":
            cluster.job_pods[0]["metadata"]["ownerReferences"][0]["uid"] = "foreign-job"
        elif kind == "extra-pod":
            cluster.job_pods *= 2
        elif kind == "image":
            cluster.job_pods[0]["spec"]["containers"][0]["image"] = "foreign:latest"
        elif kind == "message":
            cluster.job_pods[0]["status"]["containerStatuses"][0]["state"]["terminated"][
                "message"
            ] = "x" * 4097
        elif kind == "pvc":
            cluster.pvc_uid = "foreign-pvc"
        else:
            cluster.desired = 1

    cluster.create_hook = corrupt
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).run(request, recovery_envelope="signed-envelope")
    assert cluster.deleted == []


@pytest.mark.asyncio
async def test_adapter_preserves_foreign_terminal_job_in_fixed_slot():
    request = _request()
    cluster = Cluster(request)
    body = _module().build_governance_migration_job(request, recovery_envelope="foreign-envelope")
    cluster.create_namespaced_job(request.metadata.resource_name, body)
    cluster.created.clear()
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).run(request, recovery_envelope="signed-envelope")
    assert cluster.created == cluster.deleted == []


@pytest.mark.asyncio
async def test_adapter_resumes_exact_existing_request_without_duplicate_submission():
    request = _request()
    cluster = Cluster(request)
    body = _module().build_governance_migration_job(request, recovery_envelope="signed-envelope")
    cluster.create_namespaced_job(request.metadata.resource_name, body)
    cluster.created.clear()
    evidence = await _adapter(cluster).run(request, recovery_envelope="signed-envelope")
    assert evidence.job_uid == "job-alpha"
    assert cluster.created == []
    assert len(cluster.deleted) == 1


@pytest.mark.parametrize("phase", ["inspect", "prepare", "commit"])
def test_request_executes_the_runtime_source_parser_without_runtime_dependency(phase):
    import ast
    import re
    from pathlib import Path
    from typing import Any

    source = Path(__file__).resolve().parents[3] / "src/exomem/hosted_governance_job.py"
    tree = ast.parse(source.read_text())
    selected = {"HostedGovernanceJobError", "_closed_pairs", "_digest", "_request"}
    constants = {"MAX_REQUEST_BYTES", "_SHA256", "_IDENTITY", "_IMAGE", "_REQUEST_FIELDS"}
    # Execute the actual pure parser definitions; no runtime modules or SDKs
    # become dependencies of the separately installed provisioner.
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        and node.name in selected
        or isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets)
    ]
    namespace = {"json": json, "re": re, "Any": Any}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    request = _request(
        phase=phase,
        source_store_digest=None if phase == "inspect" else "c" * 64,
        plan_digest="d" * 64 if phase == "commit" else None,
    )
    assert namespace["_request"](_raw(request.as_dict())) == request.as_dict()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x" * 4097,
        b"[]",
        b"{",
        b"\xff",
        b'{"phase":"inspect","phase":"inspect"}',
        b"[" * 1500 + b"]" * 1500,
    ],
)
def test_terminal_refuses_malformed_bounded_wire(raw):
    with pytest.raises(MetadataConflict, match="^governance migration Job is unavailable$"):
        _module().parse_migration_terminal(_request(), raw)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["replacement", "failed", "running", "api", "create-conflict", "delete-conflict"]
)
async def test_adapter_refuses_ambiguous_or_failed_job_without_adopting_replacement(kind):
    request = _request()
    cluster = Cluster(request)

    class ProviderError(Exception):
        status = 409

    def error(*args, **kwargs):
        raise ProviderError("private-provider-payload")

    def corrupt():
        if kind == "replacement":
            cluster.read_hook = lambda: cluster.job["metadata"].update(uid="replacement-job")
        elif kind == "failed":
            cluster.job["status"] = {"failed": 1}
        elif kind == "running":
            cluster.job["status"] = {"active": 1}

    cluster.create_hook = corrupt
    if kind == "api":
        cluster.read_namespaced_job = error
    elif kind == "create-conflict":
        cluster.create_namespaced_job = error
    elif kind == "delete-conflict":
        cluster.delete_namespaced_job = error
    with pytest.raises(
        MetadataConflict, match="^governance migration Job is unavailable$"
    ) as caught:
        await _adapter(cluster).run(request, recovery_envelope="signed-envelope")
    assert caught.value.__suppress_context__
    assert cluster.deleted == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["env", "envFrom", "init-command", "extra-container", "mount", "token", "capability"]
)
async def test_adapter_refuses_extra_execution_or_custody_authority(kind):
    request = _request()
    cluster = Cluster(request)

    def corrupt():
        spec = cluster.job_pods[0]["spec"]
        if kind == "env":
            spec["containers"][0]["env"].append({"name": "EXTRA", "value": "value"})
        elif kind == "envFrom":
            spec["containers"][0]["envFrom"] = [{"secretRef": {"name": "private"}}]
        elif kind == "init-command":
            spec["initContainers"][0]["args"] += ["--watch"]
        elif kind == "extra-container":
            spec["containers"] *= 2
        elif kind == "mount":
            spec["containers"][0]["volumeMounts"][3]["readOnly"] = False
        elif kind == "token":
            spec["automountServiceAccountToken"] = True
        else:
            spec["containers"][0]["securityContext"]["capabilities"]["add"] = ["SYS_ADMIN"]

    cluster.create_hook = corrupt
    with pytest.raises(MetadataConflict):
        await _adapter(cluster).run(request, recovery_envelope="signed-envelope")
    assert cluster.deleted == []
