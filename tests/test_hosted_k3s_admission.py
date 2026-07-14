from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import subprocess
import time
import tomllib
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
RUNTIME_GATE = ROOT / "infra/contracts/exomem-hosted-runtime-k3s-gate-v1.json"
HELM = Path(os.environ["HELM_BIN"]) if "HELM_BIN" in os.environ else None
RUN_LIVE = os.environ.get("RUN_K3S_ADMISSION_TEST") == "1"
RUN_RUNTIME = os.environ.get("RUN_K3S_RUNTIME_TEST") == "1"
RUNTIME_REPOSITORY = Path(os.environ.get("EXOMEM_RUNTIME_REPO", ROOT))
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


def _render(
    chart: Path,
    values: Path,
    namespace: str,
    *,
    extra_args: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
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
            *extra_args,
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
    if not (RUN_LIVE or RUN_RUNTIME):
        pytest.skip("set RUN_K3S_ADMISSION_TEST=1 or RUN_K3S_RUNTIME_TEST=1 to run exact K3s tests")
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


def _build_reviewed_runtime_image(gate: dict[str, Any], source_checkout: Path) -> str:
    commit = gate["sourceCommit"]
    source = source_checkout / "source"
    _run(
        [
            "git",
            "clone",
            "--shared",
            "--no-checkout",
            str(RUNTIME_REPOSITORY),
            str(source),
        ]
    )
    _run(["git", "-C", str(source), "checkout", "--detach", commit])
    actual_commit = _run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
    assert actual_commit == commit
    assert _run(["git", "-C", str(source), "status", "--porcelain"]).stdout == ""

    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == gate["release"]
    operator_contract = (
        source
        / "openspec/changes/complete-hosted-runtime-deployment-contract/contracts/hosted-operator-v1.json"
    )
    assert (
        hashlib.sha256(operator_contract.read_bytes()).hexdigest() == gate["operatorContractSha256"]
    )

    image = f"exomem-hosted-k3s-gate:{commit[:12]}-{uuid.uuid4().hex[:8]}"
    _run(
        [
            "docker",
            "build",
            "--target",
            gate["dockerTarget"],
            "--build-arg",
            f"EXOMEM_RELEASE_BUILD_TIME={gate['releaseBuildTime']}",
            "--tag",
            image,
            str(source),
        ]
    )
    try:
        inspected = json.loads(_run(["docker", "image", "inspect", image]).stdout)[0]
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", inspected["Id"])
        config = inspected["Config"]
        assert config["User"] == "10001:10001"
        assert config["Entrypoint"] == ["exomem"]
        assert config.get("Volumes") in (None, {})
        environment = dict(value.split("=", 1) for value in config["Env"] if "=" in value)
        assert environment["EXOMEM_CONTAINER_VARIANT"] == "hosted"
        assert environment["EXOMEM_RELEASE_BUILD_TIME"] == gate["releaseBuildTime"]
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    except BaseException:
        _run(["docker", "image", "rm", "--force", image], check=False)
        raise
    return image


def _import_runtime_image(k3s: str, image: str) -> str:
    save = subprocess.Popen(
        ["docker", "save", image],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert save.stdout is not None
    imported = subprocess.run(
        ["docker", "exec", "--interactive", k3s, "ctr", "images", "import", "-"],
        cwd=ROOT,
        stdin=save.stdout,
        capture_output=True,
        check=False,
    )
    save.stdout.close()
    save_stderr = b"" if save.stderr is None else save.stderr.read()
    save_returncode = save.wait()
    assert imported.returncode == 0, (imported.stdout + imported.stderr).decode(
        errors="replace"
    ) + save_stderr.decode(errors="replace")
    assert save_returncode == 0, save_stderr.decode(errors="replace")

    images = _run(["docker", "exec", k3s, "ctr", "images", "ls"]).stdout
    imported_reference: str | None = None
    manifest_digest: str | None = None
    for line in images.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[0].endswith(image):
            imported_reference = fields[0]
            manifest_digest = fields[2]
            break
    assert imported_reference is not None, images
    assert manifest_digest is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_digest)
    repository = imported_reference.rsplit(":", 1)[0]
    digest_reference = f"{repository}@{manifest_digest}"
    _run(
        [
            "docker",
            "exec",
            k3s,
            "ctr",
            "images",
            "tag",
            imported_reference,
            digest_reference,
        ]
    )
    return digest_reference


def _wait_for_admission_policy(k3s: str, policy_name: str) -> None:
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


def _pod_logs(k3s: str, namespace: str, selector: str) -> str:
    result = _kubectl(
        k3s,
        ["logs", "--namespace", namespace, selector, "--all-containers=true"],
        check=False,
    )
    return result.stdout + result.stderr


def _wait_for_pod_ready(k3s: str, namespace: str, pod: str) -> None:
    for _ in range(30):
        exists = _kubectl(
            k3s,
            ["get", "pod", "--namespace", namespace, pod],
            check=False,
        )
        if exists.returncode == 0:
            break
        time.sleep(1)
    else:
        raise AssertionError(f"K3s did not create pod {namespace}/{pod}")
    ready = _kubectl(
        k3s,
        [
            "wait",
            "--namespace",
            namespace,
            "--for=condition=Ready",
            f"pod/{pod}",
            "--timeout=120s",
        ],
        check=False,
    )
    if ready.returncode != 0:
        describe = _kubectl(
            k3s,
            ["describe", "pod", "--namespace", namespace, pod],
            check=False,
        )
        raise AssertionError(
            ready.stdout
            + ready.stderr
            + describe.stdout
            + describe.stderr
            + _pod_logs(k3s, namespace, pod)
        )


def _exec_python_json(k3s: str, namespace: str, pod: str, source: str) -> dict[str, Any]:
    result = _kubectl(
        k3s,
        ["exec", "--namespace", namespace, pod, "--", "python", "-c", source],
    )
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _run_authenticated_probe(
    k3s: str,
    *,
    namespace: str,
    pod: str,
    gate: dict[str, Any],
    credential_revision: int,
    worker_policy_digest: str,
) -> dict[str, Any]:
    last_result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(20):
        request_document = {
            "request_id": str(uuid.uuid4()),
            "operation_id": f"k3s-runtime-probe-{attempt}-{uuid.uuid4()}",
            "cell_id": "alpha-test-original",
            "vault_id": "vault-alpha-original",
            "state_root": "/var/lib/exomem/state",
            "selected_credential_version": "active-v1",
            "expected_release": gate["release"],
            "expected_protocol": gate["hostedProtocol"],
            "expected_worker_policy_digest": worker_policy_digest,
            "expected_revision": credential_revision,
            "port": 8765,
        }
        last_result = _run(
            [
                "docker",
                "exec",
                "--interactive",
                k3s,
                "kubectl",
                "exec",
                "-i",
                "--namespace",
                namespace,
                pod,
                "--",
                "exomem",
                "hosted",
                "probe",
                "--contract-version",
                "1",
                "--request-file",
                "-",
            ],
            input_text=json.dumps(request_document, separators=(",", ":")) + "\n",
            check=False,
        )
        if last_result.returncode == 0:
            envelope = json.loads(last_result.stdout)
            assert envelope["ok"] is True
            assert envelope["code"] == "HOSTED_PROBE_READY"
            return envelope
        time.sleep(1)
    assert last_result is not None
    raise AssertionError(last_result.stdout + last_result.stderr + _pod_logs(k3s, namespace, pod))


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
        for _ in range(30):
            policy = _kubectl(
                k3s,
                ["get", "validatingadmissionpolicy", policy_name, "--output=json"],
            )
            policy_document = json.loads(policy.stdout)
            status = policy_document.get("status", {})
            if status.get("observedGeneration") == policy_document["metadata"]["generation"]:
                assert status.get("typeChecking", {}).get("expressionWarnings", []) == []
                break
            time.sleep(1)
        else:
            raise AssertionError(f"K3s did not type-check {policy_name}")

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

    writable_tmp = copy.deepcopy(serving_pod)
    writable_tmp["metadata"]["name"] = "cell-alpha-serve-writable-image-tmp"
    writable_tmp["spec"]["volumes"].append({"name": "tmp", "emptyDir": {}})
    writable_tmp["spec"]["containers"][0]["volumeMounts"].append(
        {"name": "tmp", "mountPath": "/tmp"}
    )
    _assert_denied(k3s, writable_tmp, message="own PVC and credential Secret")

    side_init = copy.deepcopy(serving_pod)
    side_init["metadata"]["name"] = "cell-alpha-serve-side-init"
    helper = copy.deepcopy(side_init["spec"]["containers"][0])
    helper["name"] = "helper"
    helper["args"] = ["--version"]
    for field in ("ports", "env", "startupProbe", "livenessProbe", "readinessProbe"):
        helper.pop(field, None)
    side_init["spec"]["initContainers"] = [helper]
    _assert_denied(k3s, side_init, message="sidecars or init containers")


@pytest.mark.skipif(
    not RUN_RUNTIME,
    reason="set RUN_K3S_RUNTIME_TEST=1 to run the reviewed hosted runtime in exact K3s",
)
@pytest.mark.timeout(900)
def test_exact_k3s_runs_the_reviewed_hosted_runtime_release(k3s: str, tmp_path: Path) -> None:
    gate = json.loads(RUNTIME_GATE.read_text(encoding="utf-8"))
    namespace = "cell-runtime-gate"
    worker_policy_digest = "b" * 64
    credential = base64.urlsafe_b64encode(hashlib.sha256(b"k3s-runtime-gate").digest())
    credential = credential.rstrip(b"=").decode("ascii")
    credential_bundle = {
        "schema_version": 1,
        "credentials": {"active-v1": credential},
    }

    image = _build_reviewed_runtime_image(gate, tmp_path)
    try:
        runtime_image = _import_runtime_image(k3s, image)

        platform = _render(
            PLATFORM,
            PLATFORM / "values.validation.yaml",
            "exomem-platform",
            extra_args=("--set-string", f"runtime.image={runtime_image}"),
        )
        platform_admission = [
            item
            for item in platform
            if item.get("kind")
            in {
                "RuntimeClass",
                "ValidatingAdmissionPolicy",
                "ValidatingAdmissionPolicyBinding",
            }
        ]
        _kubectl(k3s, ["apply", "--filename=-"], documents=platform_admission)
        _wait_for_admission_policy(k3s, "exomem-tenant-boundary")
        _wait_for_admission_policy(k3s, "exomem-tenant-namespace-contract")

        helm_overrides = (
            "--set-string",
            f"image={runtime_image}",
            "--set-string",
            f"expectedRelease={gate['release']}",
        )
        initialize = _render(
            CELL,
            CELL / "values.initialize.yaml",
            namespace,
            extra_args=helm_overrides,
        )
        namespace_document = next(item for item in initialize if item.get("kind") == "Namespace")
        _kubectl(k3s, ["apply", "--filename=-"], documents=[namespace_document])

        persistent_volume = {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {"name": "exomem-runtime-gate-pv"},
            "spec": {
                "capacity": {"storage": "10Gi"},
                "accessModes": ["ReadWriteOnce"],
                "volumeMode": "Filesystem",
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": "exomem-hcloud-encrypted-retain",
                "claimRef": {
                    "namespace": namespace,
                    "name": "cell-alpha-data",
                },
                "hostPath": {
                    "path": "/var/lib/exomem-runtime-gate-pv",
                    "type": "DirectoryOrCreate",
                },
            },
        }
        _kubectl(k3s, ["apply", "--filename=-"], documents=[persistent_volume])

        secret = next(item for item in initialize if item.get("kind") == "Secret")
        secret["stringData"] = {
            "credentials.json": json.dumps(credential_bundle, sort_keys=True, separators=(",", ":"))
            + "\n"
        }
        claim = next(item for item in initialize if item.get("kind") == "PersistentVolumeClaim")
        claim["spec"]["volumeName"] = "exomem-runtime-gate-pv"
        init_job = next(item for item in initialize if item.get("kind") == "Job")
        namespaced_prerequisites = [
            item
            for item in initialize
            if item.get("kind")
            not in {
                "Namespace",
                "PersistentVolumeClaim",
                "Job",
            }
        ]
        namespaced_prerequisites = [
            secret if item.get("kind") == "Secret" else item for item in namespaced_prerequisites
        ]
        _kubectl(
            k3s,
            ["apply", "--namespace", namespace, "--filename=-"],
            documents=namespaced_prerequisites,
        )
        _kubectl(
            k3s,
            ["apply", "--namespace", namespace, "--filename=-"],
            documents=[claim],
        )
        bound = _kubectl(
            k3s,
            [
                "wait",
                "--namespace",
                namespace,
                "--for=jsonpath={.status.phase}=Bound",
                "persistentvolumeclaim/cell-alpha-data",
                "--timeout=60s",
            ],
            check=False,
        )
        assert bound.returncode == 0, bound.stdout + bound.stderr

        _kubectl(
            k3s,
            ["apply", "--namespace", namespace, "--filename=-"],
            documents=[init_job],
        )
        initialized = _kubectl(
            k3s,
            [
                "wait",
                "--namespace",
                namespace,
                "--for=condition=complete",
                "job/cell-alpha-init",
                "--timeout=180s",
            ],
            check=False,
        )
        if initialized.returncode != 0:
            describe = _kubectl(
                k3s,
                ["describe", "job", "--namespace", namespace, "cell-alpha-init"],
                check=False,
            )
            raise AssertionError(
                initialized.stdout
                + initialized.stderr
                + describe.stdout
                + describe.stderr
                + _pod_logs(k3s, namespace, "job/cell-alpha-init")
            )
        init_envelope = json.loads(_pod_logs(k3s, namespace, "job/cell-alpha-init"))
        assert init_envelope["ok"] is True
        assert init_envelope["code"] == "HOSTED_CELL_INITIALIZED"
        assert init_envelope["data"]["status"] == "provisioned"
        assert init_envelope["data"]["binding_version"] == 2
        assert init_envelope["data"]["exomem_release"] == gate["release"]
        assert init_envelope["data"]["hosted_protocol"] == gate["hostedProtocol"]
        assert init_envelope["data"]["runtime_uid"] == 10001
        assert init_envelope["data"]["runtime_gid"] == 10001
        credential_revision = init_envelope["data"]["credential_revision"]

        serving = _render(
            CELL,
            CELL / "values.validation.yaml",
            namespace,
            extra_args=helm_overrides,
        )
        serving_resources = [
            item
            for item in serving
            if item.get("kind") in {"Service", "StatefulSet", "NetworkPolicy"}
        ]
        _kubectl(
            k3s,
            ["apply", "--namespace", namespace, "--filename=-"],
            documents=serving_resources,
        )
        pod_name = "cell-alpha-0"
        _wait_for_pod_ready(k3s, namespace, pod_name)

        live_pod = json.loads(
            _kubectl(
                k3s,
                ["get", "pod", "--namespace", namespace, pod_name, "--output=json"],
            ).stdout
        )
        pod_spec = live_pod["spec"]
        container = pod_spec["containers"][0]
        assert pod_spec.get("securityContext", {}).get("fsGroup") is None
        assert container["image"] == runtime_image
        assert container["securityContext"]["runAsNonRoot"] is True
        assert container["securityContext"]["runAsUser"] == 10001
        assert container["securityContext"]["runAsGroup"] == 10001
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert {volume["name"] for volume in pod_spec["volumes"]} == {
            "data",
            "credentials",
        }
        assert {mount["mountPath"] for mount in container["volumeMounts"]} == {
            "/var/lib/exomem/vault",
            "/var/lib/exomem/state",
            "/var/lib/exomem/logs",
            "/run/exomem/credentials",
        }
        assert (
            next(volume for volume in pod_spec["volumes"] if volume["name"] == "data")[
                "persistentVolumeClaim"
            ]["claimName"]
            == "cell-alpha-data"
        )
        credential_volume = next(
            volume for volume in pod_spec["volumes"] if volume["name"] == "credentials"
        )
        assert credential_volume["secret"] == {
            "defaultMode": 0o444,
            "secretName": "cell-alpha-credentials",
        }
        image_status = live_pod["status"]["containerStatuses"][0]
        assert image_status["ready"] is True
        assert image_status["containerID"].startswith("containerd://")

        inspection_source = """
import json, os, pathlib, stat
from exomem.hosted_transfer import TRANSFER_RUNTIME_TEMP_QUOTA_BYTES, TRANSFER_TEMP_QUOTA_BYTES

def details(path):
    value = pathlib.Path(path).stat()
    return {"uid": value.st_uid, "gid": value.st_gid, "mode": stat.S_IMODE(value.st_mode)}

roots = ["/var/lib/exomem/vault", "/var/lib/exomem/state", "/var/lib/exomem/logs"]
markers = [str(pathlib.Path(root) / ".exomem-hosted-cell.json") for root in roots]
credential = pathlib.Path("/run/exomem/credentials/credentials.json")
bundle = json.loads(credential.read_text())
tmp_write_errno = None
tmp_probe = pathlib.Path("/tmp/exomem-runtime-gate-write")
try:
    tmp_probe.write_text("must-not-write")
except OSError as exc:
    tmp_write_errno = exc.errno
else:
    tmp_probe.unlink()
credential_write_errno = None
credential_probe = pathlib.Path("/run/exomem/credentials/must-not-write")
try:
    credential_probe.write_text("must-not-write")
except OSError as exc:
    credential_write_errno = exc.errno
else:
    credential_probe.unlink()
print(json.dumps({
    "uid": os.getuid(),
    "gid": os.getgid(),
    "tmpdir": os.environ.get("TMPDIR"),
    "roots": {root: details(root) for root in roots},
    "markers": {marker: details(marker) for marker in markers},
    "credential_mount": details("/run/exomem/credentials"),
    "credential_leaf_target": os.readlink(credential),
    "credential_data_target": os.readlink("/run/exomem/credentials/..data"),
    "credential_file": details(credential),
    "credential_write_errno": credential_write_errno,
    "credential_schema": bundle.get("schema_version"),
    "credential_versions": sorted(bundle.get("credentials", {})),
    "runtime_tmp": details("/var/lib/exomem/state/tmp/runtime"),
    "transfer_tmp": details("/var/lib/exomem/state/tmp/transfers-v2"),
    "runtime_tmp_limit": TRANSFER_RUNTIME_TEMP_QUOTA_BYTES,
    "transfer_tmp_limit": TRANSFER_TEMP_QUOTA_BYTES,
    "tmp_write_errno": tmp_write_errno,
}))
"""
        inspection = _exec_python_json(k3s, namespace, pod_name, inspection_source)
        assert inspection["uid"] == 10001
        assert inspection["gid"] == 10001
        assert inspection["tmpdir"] == "/var/lib/exomem/state/tmp/runtime"
        assert all(
            value == {"uid": 10001, "gid": 10001, "mode": 0o700}
            for value in inspection["roots"].values()
        )
        assert all(
            value == {"uid": 10001, "gid": 10001, "mode": 0o600}
            for value in inspection["markers"].values()
        )
        assert inspection["credential_mount"]["uid"] == 0
        assert inspection["credential_mount"]["gid"] == 0
        assert inspection["credential_leaf_target"] == "..data/credentials.json"
        assert inspection["credential_data_target"].startswith("..20")
        assert inspection["credential_file"] == {"uid": 0, "gid": 0, "mode": 0o444}
        assert inspection["credential_write_errno"] == 30
        assert inspection["credential_schema"] == 1
        assert inspection["credential_versions"] == ["active-v1"]
        assert inspection["runtime_tmp"] == {"uid": 10001, "gid": 10001, "mode": 0o700}
        assert inspection["transfer_tmp"] == {"uid": 10001, "gid": 10001, "mode": 0o700}
        assert inspection["runtime_tmp_limit"] == 16 * 1024 * 1024
        assert inspection["transfer_tmp_limit"] == 96 * 1024 * 1024
        assert inspection["tmp_write_errno"] == 30

        probe = _run_authenticated_probe(
            k3s,
            namespace=namespace,
            pod=pod_name,
            gate=gate,
            credential_revision=credential_revision,
            worker_policy_digest=worker_policy_digest,
        )
        probe_data = probe["data"]
        assert probe_data["cell_id"] == "alpha-test-original"
        assert probe_data["vault_id"] == "vault-alpha-original"
        assert probe_data["exomem_release"] == gate["release"]
        assert probe_data["hosted_protocol"] == gate["hostedProtocol"]
        assert probe_data["authenticated_credential_version"] == "active-v1"
        assert probe_data["security_revision"] == credential_revision
        assert probe_data["service_authenticated"] is True
        assert probe_data["mutation_authority"] is True
        assert probe_data["admission_phase"] == "active"
        assert probe_data["read_admission"] is True
        assert probe_data["write_admission"] is True
        assert probe_data["worker_policy_digest"] == worker_policy_digest

        contract_source = """
import base64, hashlib, http.client, json, pathlib, uuid
bundle = json.loads(pathlib.Path("/run/exomem/credentials/credentials.json").read_text())
credential = bundle["credentials"]["active-v1"]
principal = base64.urlsafe_b64encode(hashlib.sha256(b"k3s-contract-principal").digest()).rstrip(b"=").decode()
connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=3)
connection.request("GET", "/private/exomem/v1/contract", headers={
    "Authorization": "Bearer " + credential,
    "X-Exomem-Cell-Id": "alpha-test-original",
    "X-Exomem-Protocol-Version": "1",
    "X-Exomem-Request-Id": str(uuid.uuid4()),
    "X-Exomem-Principal-Scope": principal,
})
response = connection.getresponse()
body = response.read()
print(json.dumps({"status": response.status, "media": response.getheader("content-type"), "body": json.loads(body)}))
"""
        contract_result = _exec_python_json(k3s, namespace, pod_name, contract_source)
        assert contract_result["status"] == 200
        assert contract_result["media"] == "application/json"
        contract = contract_result["body"]
        assert contract["schema_version"] == 1
        assert contract["protocol_version"] == gate["hostedProtocol"]
        assert contract["exomem_release"] == gate["release"]
        digest = contract.pop("digest")
        canonical_contract = json.dumps(
            contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        assert digest == {
            "algorithm": "sha256",
            "value": hashlib.sha256(canonical_contract).hexdigest(),
        }
        assert contract["commands"]

        sentinel_source = """
import json, pathlib
runtime = pathlib.Path("/var/lib/exomem/state/tmp/runtime/k3s-stale-runtime.tmp")
transfer = pathlib.Path(
    "/var/lib/exomem/state/tmp/transfers-v2/"
    "upload-00000000-0000-4000-8000-000000000000.tmp"
)
runtime.write_text("stale")
transfer.write_text("stale")
print(json.dumps({"runtime": runtime.exists(), "transfer": transfer.exists()}))
"""
        assert _exec_python_json(k3s, namespace, pod_name, sentinel_source) == {
            "runtime": True,
            "transfer": True,
        }
        _kubectl(
            k3s,
            ["delete", "pod", "--namespace", namespace, pod_name, "--wait=true"],
        )
        _wait_for_pod_ready(k3s, namespace, pod_name)
        cleanup_source = """
import json, pathlib
print(json.dumps({
    "runtime": pathlib.Path("/var/lib/exomem/state/tmp/runtime/k3s-stale-runtime.tmp").exists(),
    "transfer": pathlib.Path(
        "/var/lib/exomem/state/tmp/transfers-v2/"
        "upload-00000000-0000-4000-8000-000000000000.tmp"
    ).exists(),
}))
"""
        assert _exec_python_json(k3s, namespace, pod_name, cleanup_source) == {
            "runtime": False,
            "transfer": False,
        }
        _run_authenticated_probe(
            k3s,
            namespace=namespace,
            pod=pod_name,
            gate=gate,
            credential_revision=credential_revision,
            worker_policy_digest=worker_policy_digest,
        )
    finally:
        _run(["docker", "image", "rm", "--force", image], check=False)
