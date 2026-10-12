from __future__ import annotations

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
ADMISSION_CONFIG = ROOT / "infra/ansible/roles/k3s/files/admission-config.yaml"
RUNTIME_GATE = ROOT / "infra/contracts/exomem-hosted-runtime-k3s-gate-v1.json"
HELM = Path(os.environ["HELM_BIN"]) if "HELM_BIN" in os.environ else None
RUN_LIVE = os.environ.get("RUN_K3S_ADMISSION_TEST") == "1"
K3S_IMAGE = json.loads(RUNTIME_GATE.read_text(encoding="utf-8"))["k3sImage"]


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def _yaml(documents: list[dict[str, Any]]) -> str:
    return "---\n".join(yaml.safe_dump(item, sort_keys=False) for item in documents)


def _retain_k3s_logs(name: str, path: Path) -> None:
    logs = _run(["docker", "logs", name], check=False)
    path.write_text(logs.stdout + logs.stderr, encoding="utf-8")
    print(f"Retained exact-K3s failure logs at {path}")


@pytest.fixture(scope="module")
def k3s(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not RUN_LIVE:
        pytest.skip("set RUN_K3S_ADMISSION_TEST=1 to run exact K3s tests")
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
            "--publish",
            "127.0.0.1::6443",
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
