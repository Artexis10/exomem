from __future__ import annotations

import copy
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "infra/helm/platform"
CELL = ROOT / "infra/helm/cell"
ADMISSION_CONFIG = ROOT / "infra/ansible/roles/k3s/files/admission-config.yaml"
HELM = Path(os.environ["HELM_BIN"]) if "HELM_BIN" in os.environ else None
RUN_LIVE = os.environ.get("RUN_K3S_ADMISSION_TEST") == "1"
K3S_IMAGE = "rancher/k3s:v1.35.6-k3s1"


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def _render(chart: Path, values: Path, namespace: str) -> list[dict[str, Any]]:
    assert HELM is not None
    result = _run(
        [
            str(HELM),
            "template",
            "admission-test",
            str(chart),
            "--namespace",
            namespace,
            "--values",
            str(values),
            "--include-crds",
        ]
    )
    return [item for item in yaml.safe_load_all(result.stdout) if isinstance(item, dict)]


def _yaml(documents: list[dict[str, Any]]) -> str:
    return "---\n".join(yaml.safe_dump(item, sort_keys=False) for item in documents)


def _retain_k3s_logs(name: str, path: Path) -> None:
    logs = _run(["docker", "logs", name], check=False)
    path.write_text(logs.stdout + logs.stderr, encoding="utf-8")
    print(f"Retained exact-K3s failure logs at {path}")


@pytest.fixture(scope="module")
def k3s(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not RUN_LIVE:
        pytest.skip("set RUN_K3S_ADMISSION_TEST=1 to run exact K3s API admission tests")
    if HELM is None:
        pytest.skip("set HELM_BIN to run exact K3s API admission tests")
    if not shutil_which("docker"):
        pytest.skip("Docker is required for exact K3s API admission tests")

    name = f"exomem-admission-{uuid.uuid4().hex[:12]}"
    log_path = tmp_path_factory.mktemp("k3s-admission") / f"{name}.log"
    failures_before = request.session.testsfailed
    logs_retained = False
    _run(
        [
            "docker",
            "run",
            "--privileged",
            "--detach",
            "--name",
            name,
            "--volume",
            f"{ADMISSION_CONFIG}:/etc/rancher/k3s/admission-config.yaml:ro",
            K3S_IMAGE,
            "server",
            "--disable=traefik",
            "--disable=servicelb",
            "--disable=local-storage",
            "--write-kubeconfig-mode=600",
            "--kube-apiserver-arg=admission-control-config-file=/etc/rancher/k3s/admission-config.yaml",
        ]
    )
    try:
        consecutive_ready = 0
        for _ in range(90):
            ready = _run(
                ["docker", "exec", name, "kubectl", "get", "--raw=/readyz"],
                check=False,
            )
            if ready.returncode == 0 and ready.stdout.strip() == "ok":
                consecutive_ready += 1
                if consecutive_ready == 3:
                    break
            else:
                consecutive_ready = 0
            time.sleep(1)
        else:
            _retain_k3s_logs(name, log_path)
            logs_retained = True
            raise AssertionError(f"K3s did not become ready; logs retained at {log_path}")
        yield name
    except BaseException:
        if not logs_retained:
            _retain_k3s_logs(name, log_path)
            logs_retained = True
        raise
    finally:
        if not logs_retained and request.session.testsfailed > failures_before:
            _retain_k3s_logs(name, log_path)
        _run(["docker", "rm", "--force", name], check=False)


def shutil_which(command: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _kubectl(
    k3s: str,
    arguments: list[str],
    *,
    documents: list[dict[str, Any]] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["docker", "exec", "--interactive", k3s, "kubectl", *arguments],
        input_text=None if documents is None else _yaml(documents),
        check=check,
    )


def _wait_for_policy_typecheck(k3s: str, policy_name: str) -> None:
    for _ in range(30):
        policy = _kubectl(
            k3s,
            ["get", "validatingadmissionpolicy", policy_name, "--output=json"],
        )
        policy_document = json.loads(policy.stdout)
        status = policy_document.get("status", {})
        if status.get("observedGeneration") == policy_document["metadata"]["generation"]:
            assert status.get("typeChecking", {}).get("expressionWarnings", []) == []
            return
        time.sleep(1)
    raise AssertionError(f"K3s did not type-check {policy_name}")


def _pod(workload: dict[str, Any], *, name: str, namespace: str) -> dict[str, Any]:
    template = workload["spec"]["template"]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": copy.deepcopy(template["metadata"]["labels"]),
        },
        "spec": copy.deepcopy(template["spec"]),
    }


def _server_dry_run(k3s: str, pod: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return _kubectl(
        k3s,
        ["apply", "--dry-run=server", "--filename=-", "--output=name"],
        documents=[pod],
        check=False,
    )


def _assert_denied(
    k3s: str, pod: dict[str, Any], *, message: str
) -> subprocess.CompletedProcess[str]:
    result = _server_dry_run(k3s, pod)
    assert result.returncode != 0
    assert message in result.stderr
    return result


def test_exact_k3s_accepts_only_the_rendered_service_account_token_audience(
    k3s: str,
) -> None:
    namespace = "audience-contract"
    _kubectl(
        k3s,
        ["apply", "--filename=-"],
        documents=[
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "scheduler", "namespace": namespace},
            },
        ],
    )
    audience = "https://kubernetes.default.svc.cluster.local"
    issued = _kubectl(
        k3s,
        [
            "create",
            "token",
            "scheduler",
            "--namespace",
            namespace,
            "--audience",
            audience,
            "--duration",
            "10m",
        ],
    )
    review = _kubectl(
        k3s,
        ["create", "--filename=-", "--output=json"],
        documents=[
            {
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": issued.stdout.strip(), "audiences": [audience]},
            }
        ],
    )
    reviewed = json.loads(review.stdout)
    assert reviewed["status"]["authenticated"] is True
    assert reviewed["status"]["audiences"] == [audience]

    short_audience_token = _kubectl(
        k3s,
        [
            "create",
            "token",
            "scheduler",
            "--namespace",
            namespace,
            "--audience",
            "https://kubernetes.default.svc",
            "--duration",
            "10m",
        ],
    )
    rejected = _kubectl(
        k3s,
        ["create", "--filename=-", "--output=json"],
        documents=[
            {
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {
                    "token": short_audience_token.stdout.strip(),
                    "audiences": [audience],
                },
            }
        ],
    )
    rejected_status = json.loads(rejected.stdout)["status"]
    assert rejected_status.get("authenticated") is not True
    assert "audience" in rejected_status.get("error", "").lower()


def test_exact_k3s_api_admits_only_the_rendered_tenant_shapes(k3s: str) -> None:
    namespace = "cell-alpha-test"
    platform = _render(PLATFORM, PLATFORM / "values.validation.yaml", "exomem-platform")
    platform_admission = [
        item
        for item in platform
        if item.get("kind")
        in {"RuntimeClass", "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}
    ]
    _kubectl(k3s, ["apply", "--filename=-"], documents=platform_admission)

    for policy_name in (
        "exomem-tenant-boundary",
        "exomem-tenant-namespace-contract",
    ):
        _wait_for_policy_typecheck(k3s, policy_name)

    insecure_namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": "cell-insecure-contract",
            "labels": {
                "exomem.io/tenant-cell": "true",
                "exomem.io/cell-resource": "cell-insecure",
                "pod-security.kubernetes.io/enforce": "privileged",
                "pod-security.kubernetes.io/enforce-version": "latest",
            },
            "annotations": {
                "helm.sh/resource-policy": "keep",
                "exomem.io/resource-name": "cell-insecure",
                "exomem.io/pvc-name": "cell-insecure-data",
                "exomem.io/credentials-secret-name": "cell-insecure-credentials",
                "exomem.io/init-request-configmap-name": "cell-insecure-init-request",
            },
        },
    }
    insecure_create = _kubectl(
        k3s,
        ["apply", "--filename=-"],
        documents=[insecure_namespace],
        check=False,
    )
    assert insecure_create.returncode != 0
    assert "restricted-v1.35 tenant namespace contract" in insecure_create.stderr

    initialize = _render(CELL, CELL / "values.initialize.yaml", namespace)
    namespace_document = next(item for item in initialize if item.get("kind") == "Namespace")
    service_account = next(item for item in initialize if item.get("kind") == "ServiceAccount")
    _kubectl(k3s, ["apply", "--filename=-"], documents=[namespace_document])
    _kubectl(
        k3s,
        ["apply", "--namespace", namespace, "--filename=-"],
        documents=[service_account],
    )

    routine_username = "system:serviceaccount:exomem-platform:routine-provisioner"
    _kubectl(
        k3s,
        ["apply", "--filename=-"],
        documents=[
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "exomem-platform"},
            },
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": "routine-provisioner",
                    "namespace": "exomem-platform",
                },
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": "admission-test-namespace-editor"},
                "rules": [
                    {
                        "apiGroups": [""],
                        "resources": ["namespaces"],
                        "verbs": ["create", "get", "patch", "update"],
                    }
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": "admission-test-namespace-editor"},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": "admission-test-namespace-editor",
                },
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": "routine-provisioner",
                        "namespace": "exomem-platform",
                    }
                ],
            },
        ],
    )

    routine_namespace = copy.deepcopy(namespace_document)
    routine_namespace["metadata"]["name"] = "cell-routine-create"
    routine_namespace["metadata"]["labels"]["exomem.io/cell-resource"] = "cell-routine"
    routine_namespace["metadata"]["annotations"].update(
        {
            "exomem.io/resource-name": "cell-routine",
            "exomem.io/pvc-name": "cell-routine-data",
            "exomem.io/credentials-secret-name": "cell-routine-credentials",
            "exomem.io/init-request-configmap-name": "cell-routine-init-request",
        }
    )
    routine_create = _kubectl(
        k3s,
        ["apply", "--filename=-", f"--as={routine_username}"],
        documents=[routine_namespace],
        check=False,
    )
    assert routine_create.returncode == 0, routine_create.stderr

    init_job = next(item for item in initialize if item.get("kind") == "Job")
    init_pod = _pod(init_job, name="cell-alpha-init-positive", namespace=namespace)
    assert _server_dry_run(k3s, init_pod).returncode == 0

    protected_update = _kubectl(
        k3s,
        [
            "annotate",
            "namespace",
            namespace,
            "exomem.io/credentials-secret-name=cell-rotated-credentials",
            "--overwrite",
            f"--as={routine_username}",
        ],
        check=False,
    )
    assert protected_update.returncode != 0
    assert "operator authority" in protected_update.stderr

    legacy_allowlist_update = _kubectl(
        k3s,
        [
            "annotate",
            "namespace",
            namespace,
            "exomem.io/approved-image=ghcr.io/example/wrong@sha256:" + "c" * 64,
            "--overwrite",
            f"--as={routine_username}",
        ],
        check=False,
    )
    assert legacy_allowlist_update.returncode != 0
    assert "operator authority" in legacy_allowlist_update.stderr

    _kubectl(
        k3s,
        [
            "annotate",
            "namespace",
            namespace,
            "exomem.io/approved-image=ghcr.io/example/wrong@sha256:" + "c" * 64,
            "--overwrite",
        ],
    )

    _kubectl(
        k3s,
        ["create", "namespace", "untrusted"],
    )
    untrusted_account = copy.deepcopy(service_account)
    untrusted_account["metadata"]["namespace"] = "untrusted"
    _kubectl(k3s, ["apply", "--filename=-"], documents=[untrusted_account])
    untrusted_init = copy.deepcopy(init_pod)
    untrusted_init["metadata"]["name"] = "untrusted-storage-init"
    untrusted_init["metadata"]["namespace"] = "untrusted"
    _assert_denied(k3s, untrusted_init, message="platform-owned tenant namespace contract")

    ordinary_untrusted = copy.deepcopy(untrusted_init)
    ordinary_untrusted["metadata"]["name"] = "ordinary-untrusted-pod"
    ordinary_untrusted["spec"].pop("runtimeClassName")
    ordinary_untrusted["spec"]["restartPolicy"] = "Always"
    assert _server_dry_run(k3s, ordinary_untrusted).returncode == 0

    wrong_image = copy.deepcopy(init_pod)
    wrong_image["metadata"]["name"] = "cell-alpha-init-wrong-image"
    wrong_image["spec"]["containers"][0]["image"] = "ghcr.io/artexis10/exomem@sha256:" + "c" * 64
    _assert_denied(k3s, wrong_image, message="exact approved immutable image")

    extra_capability = copy.deepcopy(init_pod)
    extra_capability["metadata"]["name"] = "cell-alpha-init-extra-capability"
    extra_capability["spec"]["containers"][0]["securityContext"]["capabilities"]["add"].append(
        "SYS_ADMIN"
    )
    _assert_denied(k3s, extra_capability, message="exact approved operator command")

    excessive_resources = copy.deepcopy(init_pod)
    excessive_resources["metadata"]["name"] = "cell-alpha-init-excessive-resources"
    excessive_resources["spec"]["containers"][0]["resources"]["limits"]["memory"] = "2Gi"
    _assert_denied(k3s, excessive_resources, message="exact approved compute resources")

    surface_mutations = (
        ("command", ["/bin/sh"], "exact approved operator command"),
        ("args", ["hosted", "serve"], "exact approved operator command"),
        (
            "env",
            [{"name": "UNTRUSTED", "value": "1"}],
            "exact approved operator command",
        ),
        (
            "lifecycle",
            {"postStart": {"exec": {"command": ["/bin/sh"]}}},
            "executable or interactive surfaces",
        ),
        (
            "livenessProbe",
            {"exec": {"command": ["/bin/true"]}},
            "executable or interactive surfaces",
        ),
        (
            "readinessProbe",
            {"httpGet": {"path": "/", "port": 8765}},
            "executable or interactive surfaces",
        ),
        (
            "startupProbe",
            {"tcpSocket": {"port": 8765}},
            "executable or interactive surfaces",
        ),
        (
            "startupProbe",
            {"grpc": {"port": 8765}},
            "executable or interactive surfaces",
        ),
        (
            "ports",
            [{"containerPort": 8765, "hostPort": 8765}],
            "executable or interactive surfaces",
        ),
        ("stdin", True, "executable or interactive surfaces"),
        ("stdinOnce", True, "executable or interactive surfaces"),
        ("tty", True, "executable or interactive surfaces"),
        (
            "envFrom",
            [{"configMapRef": {"name": "foreign-env"}}],
            "executable or interactive surfaces",
        ),
        ("workingDir", "/tmp", "executable or interactive surfaces"),
        (
            "terminationMessagePath",
            "/run/exomem/credentials/service-credential",
            "safe termination message",
        ),
        (
            "terminationMessagePolicy",
            "FallbackToLogsOnError",
            "safe termination message",
        ),
    )
    for index, (field, value, message) in enumerate(surface_mutations):
        shape_drift = copy.deepcopy(init_pod)
        shape_drift["metadata"]["name"] = f"cell-alpha-init-surface-{index}"
        shape_drift["spec"]["containers"][0][field] = value
        _assert_denied(k3s, shape_drift, message=message)

    unmasked_proc = copy.deepcopy(init_pod)
    unmasked_proc["metadata"]["name"] = "cell-alpha-init-unmasked-proc"
    unmasked_proc["spec"]["hostUsers"] = False
    unmasked_proc["spec"]["containers"][0]["securityContext"]["procMount"] = "Unmasked"
    _assert_denied(k3s, unmasked_proc, message="exact approved operator command")

    init_unconfined = copy.deepcopy(init_pod)
    init_unconfined["metadata"]["name"] = "cell-alpha-init-unconfined"
    init_unconfined["spec"]["containers"][0]["securityContext"]["seccompProfile"] = {
        "type": "Unconfined"
    }
    _assert_denied(k3s, init_unconfined, message="base security context")

    for volume_name, source, key, message in (
        ("data", "persistentVolumeClaim", "claimName", "exact tenant PVC"),
        ("credentials", "secret", "secretName", "exact tenant PVC"),
        ("init-request", "configMap", "name", "exact tenant PVC"),
    ):
        foreign_mount = copy.deepcopy(init_pod)
        foreign_mount["metadata"]["name"] = f"cell-alpha-init-foreign-{volume_name}"
        volume = next(
            item for item in foreign_mount["spec"]["volumes"] if item["name"] == volume_name
        )
        volume[source][key] = "cell-foreign-resource"
        _assert_denied(k3s, foreign_mount, message=message)

    serving = _render(CELL, CELL / "values.validation.yaml", namespace)
    stateful_set = next(item for item in serving if item.get("kind") == "StatefulSet")
    serving_pod = _pod(stateful_set, name="cell-alpha-serve-positive", namespace=namespace)
    assert "initContainers" not in serving_pod["spec"]
    assert _server_dry_run(k3s, serving_pod).returncode == 0

    serving_unconfined = copy.deepcopy(serving_pod)
    serving_unconfined["metadata"]["name"] = "cell-alpha-serve-unconfined"
    serving_unconfined["spec"]["containers"][0]["securityContext"]["seccompProfile"] = {
        "type": "Unconfined"
    }
    _assert_denied(k3s, serving_unconfined, message="seccompProfile")

    unbounded_tmp = copy.deepcopy(serving_pod)
    unbounded_tmp["metadata"]["name"] = "cell-alpha-serve-unbounded-tmp"
    temporary = next(item for item in unbounded_tmp["spec"]["volumes"] if item["name"] == "tmp")
    temporary["emptyDir"].pop("sizeLimit")
    _assert_denied(k3s, unbounded_tmp, message="bounded temporary volume")

    side_init = copy.deepcopy(serving_pod)
    side_init["metadata"]["name"] = "cell-alpha-serve-side-init"
    helper = copy.deepcopy(side_init["spec"]["containers"][0])
    helper["name"] = "helper"
    helper["args"] = ["--version"]
    for field in ("ports", "env", "startupProbe", "livenessProbe", "readinessProbe"):
        helper.pop(field, None)
    side_init["spec"]["initContainers"] = [helper]
    _assert_denied(k3s, side_init, message="sidecars or init containers")


def test_exact_k3s_scopes_privileged_volume_and_deletion_mutations(k3s: str) -> None:
    platform = _render(PLATFORM, PLATFORM / "values.validation.yaml", "exomem-platform")
    admission = [
        item
        for item in platform
        if item.get("kind") in {"ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}
    ]
    ingressroute_crds = [
        item
        for item in platform
        if item.get("kind") == "CustomResourceDefinition"
        and item.get("metadata", {}).get("name") == "ingressroutes.traefik.io"
    ]
    assert len(ingressroute_crds) == 1
    _kubectl(k3s, ["apply", "--filename=-"], documents=ingressroute_crds)
    _kubectl(k3s, ["apply", "--filename=-"], documents=admission)
    for policy_name in ("exomem-deletion-worker-scope", "exomem-volume-worker-scope"):
        _wait_for_policy_typecheck(k3s, policy_name)

    namespace = "exo-privileged-scope"
    _kubectl(
        k3s,
        ["apply", "--filename=-"],
        documents=[
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "exomem-platform"},
            },
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": namespace,
                    "annotations": {"exomem.io/credentials-secret-name": "exomem-cell-credentials"},
                },
            },
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "exomem-volume-worker", "namespace": "exomem-platform"},
            },
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": "exomem-deletion-worker",
                    "namespace": "exomem-platform",
                },
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": "admission-test-privileged-workers"},
                "rules": [
                    {
                        "apiGroups": [""],
                        "resources": ["persistentvolumes", "persistentvolumeclaims"],
                        "verbs": ["create", "delete", "get", "patch", "update"],
                    },
                    {
                        "apiGroups": [""],
                        "resources": ["namespaces"],
                        "verbs": ["get", "patch", "update"],
                    },
                    {
                        "apiGroups": ["apps"],
                        "resources": ["statefulsets"],
                        "verbs": ["create", "get", "patch", "update"],
                    },
                    {
                        "apiGroups": ["traefik.io"],
                        "resources": ["ingressroutes"],
                        "verbs": ["create", "get", "patch", "update"],
                    },
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": "admission-test-volume-worker"},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": "admission-test-privileged-workers",
                },
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": "exomem-volume-worker",
                        "namespace": "exomem-platform",
                    }
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": "admission-test-deletion-worker"},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": "admission-test-privileged-workers",
                },
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": "exomem-deletion-worker",
                        "namespace": "exomem-platform",
                    }
                ],
            },
        ],
    )

    identity = {
        "exomem.io/recovery-envelope": "A" * 64,
        "exomem.io/tenant-id": "tenant-scope",
        "exomem.io/cell-id": "cell-scope",
        "exomem.io/operation-id": "operation-scope",
        "exomem.io/tenant-digest": "a" * 64,
        "exomem.io/subject-digest": "b" * 64,
        "exomem.io/operation-digest": "c" * 64,
        "exomem.io/fence": "1",
    }
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": namespace + "-data",
            "namespace": namespace,
            "annotations": identity,
            "labels": {"exomem.io/resource-name": namespace},
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "10Gi"}},
            "storageClassName": "exomem-hcloud-encrypted-retain",
            "volumeMode": "Filesystem",
            "volumeName": "pvc-1234",
        },
    }
    volume_user = "system:serviceaccount:exomem-platform:exomem-volume-worker"
    valid_pvc = _kubectl(
        k3s,
        ["apply", "--dry-run=server", "--filename=-", f"--as={volume_user}"],
        documents=[pvc],
        check=False,
    )
    assert valid_pvc.returncode == 0, valid_pvc.stderr
    oversized_pvc = copy.deepcopy(pvc)
    oversized_pvc["spec"]["resources"]["requests"]["storage"] = "20Gi"
    denied_pvc = _kubectl(
        k3s,
        ["apply", "--dry-run=server", "--filename=-", f"--as={volume_user}"],
        documents=[oversized_pvc],
        check=False,
    )
    assert denied_pvc.returncode != 0
    assert "exactly 10 GiB of requested storage" in denied_pvc.stderr

    pv = {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {
            "name": "pvc-1234",
            "annotations": identity,
            "labels": {"exomem.io/resource-name": namespace},
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "capacity": {"storage": "10Gi"},
            "csi": {
                "driver": "csi.hetzner.cloud",
                "fsType": "ext4",
                "nodePublishSecretRef": {
                    "name": "exomem-volume-encryption",
                    "namespace": "exomem-platform",
                },
                "volumeHandle": "1234",
            },
            "claimRef": {"name": namespace + "-data", "namespace": namespace},
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "exomem-hcloud-encrypted-retain",
            "volumeMode": "Filesystem",
        },
    }
    valid_pv = _kubectl(
        k3s,
        ["apply", "--dry-run=server", "--filename=-", f"--as={volume_user}"],
        documents=[pv],
        check=False,
    )
    assert valid_pv.returncode == 0, valid_pv.stderr
    wrong_secret_pv = copy.deepcopy(pv)
    wrong_secret_pv["spec"]["csi"]["nodePublishSecretRef"]["name"] = "foreign-key"
    denied_pv = _kubectl(
        k3s,
        ["apply", "--dry-run=server", "--filename=-", f"--as={volume_user}"],
        documents=[wrong_secret_pv],
        check=False,
    )
    assert denied_pv.returncode != 0
    assert "exact encrypted 10 GiB Retain HCloud" in denied_pv.stderr

    deletion_user = "system:serviceaccount:exomem-platform:exomem-deletion-worker"
    stateful_set = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": namespace,
            "namespace": namespace,
            "annotations": identity,
            "labels": {"exomem.io/resource-name": namespace},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": namespace}},
            "serviceName": namespace,
            "template": {
                "metadata": {"labels": {"app": namespace}},
                "spec": {
                    "containers": [
                        {
                            "name": "runtime",
                            "image": "busybox:1.37.0",
                            "command": ["sleep", "3600"],
                        }
                    ]
                },
            },
        },
    }
    _kubectl(k3s, ["apply", "--filename=-"], documents=[stateful_set])
    valid_scale_down = _kubectl(
        k3s,
        [
            "patch",
            "statefulset",
            namespace,
            "--namespace",
            namespace,
            "--type=merge",
            "--patch",
            json.dumps({"spec": {"replicas": 0}}),
            "--dry-run=server",
            f"--as={deletion_user}",
        ],
        check=False,
    )
    assert valid_scale_down.returncode != 0
    assert "update only the namespace deletion receipt" in valid_scale_down.stderr
    invalid_scale_down = _kubectl(
        k3s,
        [
            "patch",
            "statefulset",
            namespace,
            "--namespace",
            namespace,
            "--type=merge",
            "--patch",
            json.dumps(
                {
                    "spec": {
                        "replicas": 0,
                        "template": {
                            "spec": {"containers": [{"name": "runtime", "image": "busybox:latest"}]}
                        },
                    }
                }
            ),
            "--dry-run=server",
            f"--as={deletion_user}",
        ],
        check=False,
    )
    assert invalid_scale_down.returncode != 0
    assert "update only the namespace deletion receipt" in invalid_scale_down.stderr

    ingress_route = {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "IngressRoute",
        "metadata": {
            "name": namespace + "-control",
            "namespace": namespace,
            "annotations": identity,
            "labels": {
                "exomem.io/resource-name": namespace,
                "exomem.io/tenant-route": "true",
            },
        },
        "spec": {
            "entryPoints": ["web"],
            "routes": [
                {
                    "kind": "Rule",
                    "match": "Host(`cell.example.test`)",
                    "services": [{"name": namespace, "port": 8765}],
                }
            ],
        },
    }
    _kubectl(k3s, ["apply", "--filename=-"], documents=[ingress_route])
    valid_route_close = _kubectl(
        k3s,
        [
            "patch",
            "ingressroute",
            namespace + "-control",
            "--namespace",
            namespace,
            "--type=merge",
            "--patch",
            json.dumps({"spec": {"routes": []}}),
            "--dry-run=server",
            f"--as={deletion_user}",
        ],
        check=False,
    )
    assert valid_route_close.returncode != 0
    assert "update only the namespace deletion receipt" in valid_route_close.stderr
    invalid_route_close = _kubectl(
        k3s,
        [
            "patch",
            "ingressroute",
            namespace + "-control",
            "--namespace",
            namespace,
            "--type=merge",
            "--patch",
            json.dumps({"spec": {"entryPoints": ["websecure"], "routes": []}}),
            "--dry-run=server",
            f"--as={deletion_user}",
        ],
        check=False,
    )
    assert invalid_route_close.returncode != 0
    assert "update only the namespace deletion receipt" in invalid_route_close.stderr

    first_receipt = _kubectl(
        k3s,
        [
            "annotate",
            "namespace",
            namespace,
            "exomem.io/credential-deletion-operation-digest=" + "d" * 64,
            "exomem.io/credential-deletion-fence=1",
            "--overwrite",
            f"--as={deletion_user}",
        ],
        check=False,
    )
    assert first_receipt.returncode == 0, first_receipt.stderr
    same_fence_drift = _kubectl(
        k3s,
        [
            "annotate",
            "namespace",
            namespace,
            "exomem.io/credential-deletion-operation-digest=" + "e" * 64,
            "--overwrite",
            f"--as={deletion_user}",
        ],
        check=False,
    )
    assert same_fence_drift.returncode != 0
    unrelated_drift = _kubectl(
        k3s,
        [
            "annotate",
            "namespace",
            namespace,
            "exomem.io/unrelated=forbidden",
            "--overwrite",
            f"--as={deletion_user}",
        ],
        check=False,
    )
    assert unrelated_drift.returncode != 0
    higher_fence = _kubectl(
        k3s,
        [
            "annotate",
            "namespace",
            namespace,
            "exomem.io/credential-deletion-operation-digest=" + "e" * 64,
            "exomem.io/credential-deletion-fence=2",
            "--overwrite",
            f"--as={deletion_user}",
        ],
        check=False,
    )
    assert higher_fence.returncode == 0, higher_fence.stderr
