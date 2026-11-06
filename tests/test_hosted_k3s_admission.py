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
    # The private helper exists on agent hosts. Disposable CI uses its run identity.
    helper = shutil_which("agent-docker-clean")
    label = (
        _run([helper, "--print-label"]).stdout.strip()
        if helper
        else f"exomem.test-run={os.environ.get('GITHUB_RUN_ID', name)}"
    )
    disposable = (
        _run([helper, "--print-disposable-label"]).stdout.strip()
        if helper
        else f"exomem.disposable={os.environ.get('GITHUB_RUN_ID', name)}"
    )
    image = _run(["docker", "image", "inspect", K3S_IMAGE], check=False)
    if image.returncode != 0:
        _run(["docker", "pull", K3S_IMAGE])
        image = _run(["docker", "image", "inspect", K3S_IMAGE])
    # Inspect Docker's declared volumes so no image-created anonymous data escapes ownership.
    paths = set(json.loads(image.stdout)[0]["Config"].get("Volumes") or {}) | {
        "/var/lib/rancher/k3s"
    }
    volumes = []
    mounts = []
    try:
        for index, path in enumerate(sorted(paths)):
            volume = f"{name}-{index}"
            _run(["docker", "volume", "create", "--label", label, "--label", disposable, volume])
            volumes.append(volume)
            mounts.extend(["--mount", f"type=volume,src={volume},dst={path}"])
        _run(
            [
                "docker",
                "run",
                "--label",
                label,
                "--label",
                disposable,
                "--memory",
                os.environ.get("K3S_TEST_MEMORY", "2g"),
                "--cpus",
                "2",
                *mounts,
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
        for volume in volumes:
            _run(["docker", "volume", "rm", volume], check=False)


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


def _wait_for_admission_policy(
    k3s: str,
    name: str,
    arguments: list[str],
    *,
    documents: list[dict[str, Any]] | None = None,
) -> None:
    deadline = time.monotonic() + 60
    response = "not attempted; policy type checking is incomplete"
    while True:
        state = _kubectl(
            k3s,
            [
                "get",
                f"validatingadmissionpolicy/{name}",
                f"validatingadmissionpolicybinding/{name}",
                "--output=json",
                "--request-timeout=5s",
            ],
            check=False,
        )
        if state.returncode == 0:
            policy = next(
                item
                for item in json.loads(state.stdout)["items"]
                if item["kind"] == "ValidatingAdmissionPolicy"
            )
            status = policy.get("status", {})
            type_checking = status.get("typeChecking")
            if type_checking is not None:
                assert not type_checking.get("expressionWarnings"), (
                    f"{name} has expression warnings:\n{state.stdout}"
                )
            if (
                type_checking is not None
                and status.get("observedGeneration") == policy["metadata"]["generation"]
            ):
                result = _kubectl(
                    k3s,
                    [*arguments, "--dry-run=server", "--request-timeout=5s"],
                    documents=documents,
                    check=False,
                )
                response = f"returncode={result.returncode}\n{result.stdout}{result.stderr}"
                # Match Kubernetes' policy and binding identifiers and the rendered message.
                denial = (
                    f"ValidatingAdmissionPolicy '{name}' with binding '{name}' denied request: "
                    + policy["spec"]["validations"][0]["message"]
                )
                if result.returncode != 0 and denial in result.stderr:
                    return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1, remaining))
    raise AssertionError(
        f"{name} did not become ready within 60s.\n"
        f"Final policy and binding state (returncode={state.returncode}):\n"
        f"{state.stdout}{state.stderr}\n"
        f"Last server dry-run response:\n{response}"
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


def test_deploy_cannot_change_commands_or_metadata_and_jobs_use_admitted_image(k3s: str) -> None:
    """A deploy cannot inject a command; offline Job admission trusts only its cell StatefulSet."""
    import copy

    rendered = _run(
        [
            str(HELM),
            "template",
            "exomem-platform",
            "infra/helm/platform",
            "-f",
            "infra/helm/platform/values.validation.yaml",
            "--set",
            "operatorDeploy.enabled=true",
            "--set",
            "operatorDeploy.sourceRepository=example/runtime",
            "--set",
            "operatorDeploy.registryReadSecretName=registry-read",
        ]
    )
    documents = [doc for doc in yaml.safe_load_all(rendered.stdout) if isinstance(doc, dict)]
    names = {"exomem-image-only-deploy", "exomem-cell-job-image"}
    policies = [
        doc
        for doc in documents
        if doc.get("kind") in ("ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding")
        and doc["metadata"]["name"] in names
    ]
    _kubectl(k3s, ["apply", "-f", "-"], documents=policies)
    namespace = "deploy-proof"
    _kubectl(k3s, ["create", "namespace", namespace])
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "controller", "namespace": namespace},
        "spec": {
            "replicas": 0,
            "selector": {"matchLabels": {"app": "controller"}},
            "template": {
                "metadata": {"labels": {"app": "controller"}},
                "spec": {
                    "containers": [
                        {
                            "name": "controller",
                            "image": "unreachable.example/controller@sha256:" + "a" * 64,
                        }
                    ]
                },
            },
        },
    }
    _kubectl(k3s, ["apply", "-f", "-"], documents=[deployment])
    _kubectl(
        k3s,
        [
            "create",
            "clusterrolebinding",
            "deploy-proof-admin",
            "--clusterrole=cluster-admin",
            "--group=exomem:deploy",
        ],
    )
    actor = ["--as=deploy-proof", "--as-group=exomem:deploy", "--as-group=system:authenticated"]
    metadata_patch = [{"op": "add", "path": "/metadata/annotations", "value": {"escape": "yes"}}]
    _wait_for_admission_policy(
        k3s,
        "exomem-image-only-deploy",
        [
            *actor,
            "patch",
            "deployment/controller",
            "-n",
            namespace,
            "--type=json",
            "-p",
            json.dumps(metadata_patch),
        ],
    )
    image_only = _kubectl(
        k3s,
        [
            *actor,
            "patch",
            "deployment/controller",
            "-n",
            namespace,
            "--type=json",
            "-p",
            json.dumps(
                [
                    {
                        "op": "replace",
                        "path": "/spec/template/spec/containers/0/image",
                        "value": "unreachable.example/controller@sha256:" + "b" * 64,
                    }
                ]
            ),
        ],
        check=False,
    )
    assert image_only.returncode == 0, image_only.stderr
    for patch in (
        [
            {
                "op": "add",
                "path": "/spec/template/spec/containers/0/command",
                "value": ["sh", "-c", "read-content"],
            }
        ],
        [{"op": "add", "path": "/spec/template/spec/serviceAccountName", "value": "privileged"}],
        metadata_patch,
        [
            {
                "op": "add",
                "path": "/spec/template/spec/containers/-",
                "value": {"name": "sidecar", "image": "unreachable.example/sidecar"},
            }
        ],
    ):
        refused = _kubectl(
            k3s,
            [
                *actor,
                "patch",
                "deployment/controller",
                "-n",
                namespace,
                "--type=json",
                "-p",
                json.dumps(patch),
            ],
            check=False,
        )
        assert refused.returncode != 0 and "container images only" in refused.stderr, (
            f"patch={patch!r}\n{refused.stdout}{refused.stderr}"
        )
    cell_namespace = "exo-cell-aaaaaaaaaaaaaaaa"
    _kubectl(
        k3s,
        ["apply", "-f", "-"],
        documents=[
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": cell_namespace,
                    "labels": {"exomem.io/cloud-cell": "aaaaaaaaaaaaaaaa"},
                },
            }
        ],
    )
    image = "unreachable.example/cell@sha256:" + "c" * 64
    statefulset = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "cell", "namespace": cell_namespace},
        "spec": {
            "serviceName": "cell",
            "replicas": 0,
            "selector": {"matchLabels": {"app": "cell"}},
            "template": {
                "metadata": {"labels": {"app": "cell"}},
                "spec": {"containers": [{"name": "exomem", "image": image}]},
            },
        },
    }
    _kubectl(k3s, ["apply", "-f", "-"], documents=[statefulset])
    # No registry verifier exists on Jobs or Pods. The native parameter is enough offline.
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "backup", "namespace": cell_namespace},
        "spec": {
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [{"name": "backup", "image": image}],
                    "initContainers": [{"name": "init", "image": image}],
                }
            }
        },
    }
    wrong_image = "unreachable.example/wrong@sha256:" + "d" * 64
    wrong = copy.deepcopy(job)
    wrong["spec"]["template"]["spec"]["containers"][0]["image"] = wrong_image
    _wait_for_admission_policy(
        k3s,
        "exomem-cell-job-image",
        [*actor, "create", "-f", "-"],
        documents=[wrong],
    )
    allowed = _kubectl(
        k3s, [*actor, "create", "--dry-run=server", "-f", "-"], documents=[job], check=False
    )
    assert allowed.returncode == 0, allowed.stderr
    for key in ("containers", "initContainers"):
        wrong = copy.deepcopy(job)
        wrong["spec"]["template"]["spec"][key][0]["image"] = wrong_image
        denied = _kubectl(
            k3s, [*actor, "create", "--dry-run=server", "-f", "-"], documents=[wrong], check=False
        )
        assert denied.returncode != 0 and "admitted cell StatefulSet" in denied.stderr, (
            f"image field={key}\n{denied.stdout}{denied.stderr}"
        )
    missing = copy.deepcopy(job)
    missing["metadata"]["namespace"] = namespace
    assert (
        _kubectl(
            k3s, [*actor, "create", "--dry-run=server", "-f", "-"], documents=[missing], check=False
        ).returncode
        == 0
    )


@pytest.mark.skipif(
    os.environ.get("RUN_IMAGE_ADMISSION_RUNTIME_TEST") != "1",
    reason="dedicated disposable admission runtime job",
)
@pytest.mark.timeout(900)
def test_image_admission_runtime_has_no_tenant_keys_and_rotates_its_webhook_ca(k3s: str) -> None:
    """The selected vendor profile starts with scoped credentials and updates its own trust after CA rotation."""
    profile = json.loads((ROOT / "infra/helm/platform/files/image-admission-v1.json").read_text())
    rendered = _run(
        [
            str(HELM),
            "template",
            "exomem-platform",
            "infra/helm/platform",
            "-f",
            "infra/helm/platform/values.validation.yaml",
            "--include-crds",
            "--set",
            "operatorDeploy.enabled=true",
            "--set",
            "operatorDeploy.sourceRepository=example/runtime",
            "--set",
            "operatorDeploy.registryReadSecretName=registry-read",
            "--set",
            "cellctl.cellImageRepository=127.0.0.1:1/runtime",
        ]
    )
    all_docs = [doc for doc in yaml.safe_load_all(rendered.stdout) if isinstance(doc, dict)]
    namespace = "exomem-admission"
    # These groups are the selected vendor's versioned CRD API, not tenant vocabulary.
    crds = [
        doc
        for doc in all_docs
        if doc.get("kind") == "CustomResourceDefinition"
        and doc["spec"]["group"]
        in ("kyverno.io", "policies.kyverno.io", "reports.kyverno.io", "wgpolicyk8s.io")
    ]
    _kubectl(k3s, ["apply", "--server-side", "-f", "-"], documents=crds)
    cert_chart = next((ROOT / "infra/helm/platform/charts").glob("cert-manager-*.tgz"))
    certificates = _run(
        [
            str(HELM),
            "template",
            "proof-certs",
            str(cert_chart),
            "--namespace",
            "proof-certs",
            "--include-crds",
            "--set",
            "crds.enabled=true",
            "--set",
            "startupapicheck.enabled=false",
            "--set",
            "global.leaderElection.namespace=proof-certs",
        ]
    )
    cert_docs = [doc for doc in yaml.safe_load_all(certificates.stdout) if isinstance(doc, dict)]
    _kubectl(k3s, ["create", "namespace", "proof-certs"])
    _kubectl(k3s, ["apply", "--server-side", "-f", "-"], documents=cert_docs)
    deadline = time.monotonic() + 180
    for deployment in (doc for doc in cert_docs if doc["kind"] == "Deployment"):
        _kubectl(
            k3s,
            [
                "rollout",
                "status",
                f"deployment/{deployment['metadata']['name']}",
                "-n",
                "proof-certs",
                f"--timeout={max(1, int(deadline - time.monotonic()))}s",
            ],
        )
    own_names = {hook["name"] for hook in profile["webhooks"]}
    selected = [
        doc
        for doc in all_docs
        if (
            doc.get("metadata", {}).get("namespace") == namespace
            or doc.get("kind") == "Namespace"
            and doc["metadata"]["name"] == namespace
            or doc.get("metadata", {}).get("name") == "exomem-image-admission"
            or doc.get("metadata", {}).get("name") in own_names
            or doc.get("kind") == "ImageValidatingPolicy"
            or doc.get("kind") in ("ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding")
            and doc["metadata"]["name"] == "exomem-cell-job-image"
            or doc.get("kind") == "ClusterIssuer"
            and doc["metadata"]["name"] == "kyverno-selfsigned-issuer"
        )
    ]
    namespaces = [doc for doc in selected if doc["kind"] == "Namespace"]
    _kubectl(k3s, ["apply", "-f", "-"], documents=namespaces)
    _kubectl(
        k3s,
        ["apply", "-f", "-"],
        documents=[
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "registry-read", "namespace": namespace},
                "type": "kubernetes.io/dockerconfigjson",
                "stringData": {".dockerconfigjson": '{"auths":{}}'},
            }
        ],
    )
    _kubectl(
        k3s,
        ["apply", "--server-side", "-f", "-"],
        documents=[doc for doc in selected if doc["kind"] != "Namespace"],
    )
    deployments = [doc for doc in selected if doc["kind"] == "Deployment"]
    assert len(deployments) == 1
    for container in deployments[0]["spec"]["template"]["spec"]["containers"]:
        assert (
            container["resources"]["requests"]["memory"]
            and container["resources"]["limits"]["memory"]
        )
        assert container["resources"]["requests"]["cpu"] and container["resources"]["limits"]["cpu"]
    rollout = _kubectl(
        k3s,
        [
            "rollout",
            "status",
            "deployment/kyverno-admission-controller",
            "-n",
            namespace,
            "--timeout=180s",
        ],
        check=False,
    )
    if rollout.returncode != 0:
        for arguments in (
            ["get", "pods", "-n", namespace, "-o", "wide"],
            ["describe", "pods", "-n", namespace],
            ["get", "events", "-n", namespace, "--sort-by=.metadata.creationTimestamp"],
            [
                "logs",
                "deployment/kyverno-admission-controller",
                "-n",
                namespace,
                "--all-containers=true",
                "--tail=100",
                "--prefix=true",
            ],
            [
                "logs",
                "deployment/kyverno-admission-controller",
                "-n",
                namespace,
                "--all-containers=true",
                "--previous=true",
                "--tail=100",
                "--prefix=true",
            ],
        ):
            diagnostic = _kubectl(k3s, [*arguments, "--request-timeout=10s"], check=False)
            print("kubectl " + " ".join(arguments) + "\n" + diagnostic.stdout + diagnostic.stderr)
    assert rollout.returncode == 0, rollout.stdout + rollout.stderr
    actor = f"system:serviceaccount:{namespace}:exomem-image-admission"
    for verb, resource, target_namespace in (
        ("get", "secrets", "exo-cell-aaaaaaaaaaaaaaaa"),
        ("list", "secrets", "exo-cell-aaaaaaaaaaaaaaaa"),
        ("create", "secrets", namespace),
        ("update", "serviceaccounts", namespace),
        ("create", "validatingadmissionpolicies", None),
        ("update", "imagevalidatingpolicies.policies.kyverno.io", None),
        ("create", "validatingwebhookconfigurations", None),
    ):
        arguments = ["auth", "can-i", verb, resource, f"--as={actor}"]
        if target_namespace:
            arguments.extend(["-n", target_namespace])
        result = _kubectl(k3s, arguments, check=False)
        assert result.stdout.strip() == "no", (verb, resource, result.stdout, result.stderr)
    for hook in profile["webhooks"]:
        resource = (
            "validatingwebhookconfigurations"
            if hook["kind"] == "ValidatingWebhookConfiguration"
            else "mutatingwebhookconfigurations"
        )
        allowed = _kubectl(
            k3s,
            ["auth", "can-i", "update", f"{resource}/{hook['name']}", f"--as={actor}"],
        )
        assert allowed.stdout.strip() == "yes"
    hook = "kyverno-resource-validating-webhook-cfg"

    # Wait for actual policy registration, then rotate only this test installation's CA and leaf.
    def bundle():
        item = json.loads(
            _kubectl(k3s, ["get", "validatingwebhookconfiguration", hook, "-o", "json"]).stdout
        )
        return next(
            (
                entry.get("clientConfig", {}).get("caBundle")
                for entry in item.get("webhooks", [])
                if entry.get("clientConfig", {}).get("caBundle")
            ),
            None,
        )

    deadline = time.monotonic() + 120
    previous = None
    while time.monotonic() < deadline and not previous:
        previous = bundle()
        if not previous:
            time.sleep(1)
    assert previous, "the runtime did not register its validating webhook"

    controller = next(
        doc
        for doc in all_docs
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "cellctl"
    )
    producer_namespace = controller["metadata"]["namespace"]
    producer_account = controller["spec"]["template"]["spec"]["serviceAccountName"]
    producer = [
        f"--as=system:serviceaccount:{producer_namespace}:{producer_account}",
        "--as-group=system:authenticated",
    ]
    cell_namespace = "exo-cell-eeeeeeeeeeeeeeee"
    image = "127.0.0.1:1/runtime@sha256:" + "a" * 64
    # Admin seeds the previously admitted image; this does not prove a live signed family.
    _kubectl(
        k3s,
        ["apply", "-f", "-"],
        documents=[
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": cell_namespace,
                    "labels": {"exomem.io/cloud-cell": "eeeeeeeeeeeeeeee"},
                },
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "runtime-proof-producer", "namespace": cell_namespace},
                "rules": [
                    {
                        "apiGroups": ["apps"],
                        "resources": ["statefulsets"],
                        "verbs": ["get", "patch"],
                    },
                    {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create"]},
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": "runtime-proof-producer", "namespace": cell_namespace},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": "runtime-proof-producer",
                },
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": producer_account,
                        "namespace": producer_namespace,
                    }
                ],
            },
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {"name": "cell", "namespace": cell_namespace},
                "spec": {
                    "serviceName": "cell",
                    "replicas": 0,
                    "selector": {"matchLabels": {"app": "cell"}},
                    "template": {
                        "metadata": {"labels": {"app": "cell"}},
                        "spec": {"containers": [{"name": "exomem", "image": image}]},
                    },
                },
            },
        ],
    )
    changed_patch = [
        *producer,
        "patch",
        "statefulset/cell",
        "-n",
        cell_namespace,
        "--dry-run=server",
        "--type=json",
        "-p",
        json.dumps(
            [
                {
                    "op": "replace",
                    "path": "/spec/template/spec/containers/0/image",
                    "value": "127.0.0.1:1/runtime@sha256:" + "b" * 64,
                }
            ]
        ),
    ]
    changed_image = _kubectl(k3s, changed_patch, check=False)
    assert (
        changed_image.returncode != 0 and "exomem-release-image-cloud-cell" in changed_image.stderr
    ), changed_image.stderr
    unchanged_patch = [
        *producer,
        "patch",
        "statefulset/cell",
        "-n",
        cell_namespace,
        "--dry-run=server",
        "--type=merge",
        "-p",
        json.dumps(
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": "2026-10-26T12:00:00Z"
                            }
                        }
                    }
                }
            }
        ),
    ]
    _kubectl(k3s, unchanged_patch)
    _kubectl(
        k3s,
        [*producer, "create", "--dry-run=server", "-f", "-"],
        documents=[
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "backup", "namespace": cell_namespace},
                "spec": {
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "containers": [{"name": "backup", "image": image}],
                        }
                    }
                },
            }
        ],
    )
    secrets = [f"kyverno-svc.{namespace}.svc.{suffix}" for suffix in profile["tlsSecretSuffixes"]]
    _kubectl(k3s, ["delete", "secret", *secrets, "-n", namespace])
    deadline = time.monotonic() + 180
    changed = False
    while time.monotonic() < deadline:
        current = bundle()
        if current and current != previous:
            changed = True
            break
        time.sleep(1)
    assert changed, "the runtime did not update its webhook CA with UPDATE-only credentials"
    # The API server must also trust the replacement leaf, not only store a new CA string.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        trusted = _kubectl(k3s, unchanged_patch, check=False)
        if trusted.returncode == 0:
            break
        time.sleep(1)
    assert trusted.returncode == 0, trusted.stderr
    changed_image = _kubectl(k3s, changed_patch, check=False)
    assert (
        changed_image.returncode != 0 and "exomem-release-image-cloud-cell" in changed_image.stderr
    ), changed_image.stderr
