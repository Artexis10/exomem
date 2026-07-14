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


@pytest.fixture(scope="module")
def k3s() -> Iterator[str]:
    if not RUN_LIVE:
        pytest.skip("set RUN_K3S_ADMISSION_TEST=1 to run exact K3s API admission tests")
    if HELM is None:
        pytest.skip("set HELM_BIN to run exact K3s API admission tests")
    if not shutil_which("docker"):
        pytest.skip("Docker is required for exact K3s API admission tests")

    name = f"exomem-admission-{uuid.uuid4().hex[:12]}"
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
        for _ in range(90):
            ready = _run(
                ["docker", "exec", name, "kubectl", "get", "--raw=/readyz"],
                check=False,
            )
            if ready.returncode == 0 and ready.stdout.strip() == "ok":
                break
            time.sleep(1)
        else:
            logs = _run(["docker", "logs", name], check=False)
            raise AssertionError(f"K3s did not become ready:\n{logs.stdout}{logs.stderr}")
        yield name
    finally:
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

    initialize = _render(CELL, CELL / "values.initialize.yaml", namespace)
    namespace_document = next(item for item in initialize if item.get("kind") == "Namespace")
    service_account = next(item for item in initialize if item.get("kind") == "ServiceAccount")
    _kubectl(k3s, ["apply", "--filename=-"], documents=[namespace_document])
    _kubectl(
        k3s,
        ["apply", "--namespace", namespace, "--filename=-"],
        documents=[service_account],
    )

    for _ in range(30):
        policy = _kubectl(
            k3s,
            ["get", "validatingadmissionpolicy", "exomem-tenant-boundary", "--output=json"],
        )
        status = json.loads(policy.stdout).get("status", {})
        if status.get("observedGeneration") == 1:
            assert status.get("typeChecking", {}).get("expressionWarnings", []) == []
            break
        time.sleep(1)
    else:
        raise AssertionError("K3s did not type-check the tenant admission policy")

    init_job = next(item for item in initialize if item.get("kind") == "Job")
    init_pod = _pod(init_job, name="cell-alpha-init-positive", namespace=namespace)
    assert _server_dry_run(k3s, init_pod).returncode == 0

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
    wrong_image["spec"]["containers"][0]["image"] = (
        "ghcr.io/artexis10/exomem@sha256:" + "c" * 64
    )
    _assert_denied(k3s, wrong_image, message="exact approved immutable image")

    extra_capability = copy.deepcopy(init_pod)
    extra_capability["metadata"]["name"] = "cell-alpha-init-extra-capability"
    extra_capability["spec"]["containers"][0]["securityContext"]["capabilities"][
        "add"
    ].append("SYS_ADMIN")
    _assert_denied(k3s, extra_capability, message="exact approved operator command")

    excessive_resources = copy.deepcopy(init_pod)
    excessive_resources["metadata"]["name"] = "cell-alpha-init-excessive-resources"
    excessive_resources["spec"]["containers"][0]["resources"]["limits"]["memory"] = "2Gi"
    _assert_denied(k3s, excessive_resources, message="exact approved compute resources")

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

    unbounded_tmp = copy.deepcopy(serving_pod)
    unbounded_tmp["metadata"]["name"] = "cell-alpha-serve-unbounded-tmp"
    temporary = next(
        item for item in unbounded_tmp["spec"]["volumes"] if item["name"] == "tmp"
    )
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
