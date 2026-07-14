from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "infra/helm/platform"
CELL = ROOT / "infra/helm/cell"
CONTRACT = ROOT / "infra/contracts/exomem-hosted-schedules-v1.json"
HELM = Path(os.environ["HELM_BIN"]) if "HELM_BIN" in os.environ else None


def _documents(rendered: str) -> list[dict]:
    return [document for document in yaml.safe_load_all(rendered) if isinstance(document, dict)]


def _render(
    chart: Path,
    values: Path,
    *,
    namespace: str,
    extra_args: tuple[str, ...] = (),
    release_name: str = "contract-test",
) -> list[dict]:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    result = subprocess.run(
        [
            str(HELM),
            "template",
            release_name,
            str(chart),
            "--namespace",
            namespace,
            "--values",
            str(values),
            "--include-crds",
            *extra_args,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return _documents(result.stdout)


def _find(documents: list[dict], kind: str, name: str) -> dict:
    for document in documents:
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    raise AssertionError(f"missing {kind}/{name}")


def test_platform_dependencies_and_first_party_images_are_immutable() -> None:
    chart = yaml.safe_load((PLATFORM / "Chart.yaml").read_text(encoding="utf-8"))
    dependencies = {item["name"]: item for item in chart["dependencies"]}
    assert dependencies["hcloud-csi"]["version"] == "2.21.1"
    assert dependencies["traefik"]["version"] == "41.0.2"

    values = yaml.safe_load((PLATFORM / "values.yaml").read_text(encoding="utf-8"))
    validation_values = yaml.safe_load(
        (PLATFORM / "values.validation.yaml").read_text(encoding="utf-8")
    )
    assert values["runtime"]["image"] == ""
    assert validation_values["runtime"]["image"] == (
        "ghcr.io/artexis10/exomem@sha256:" + "a" * 64
    )
    platform_schema = json.loads(
        (PLATFORM / "values.schema.json").read_text(encoding="utf-8")
    )
    assert "@sha256:" in json.dumps(platform_schema["properties"]["runtime"])
    assert values["cloudflared"]["image"].endswith(
        "@sha256:5e49861633763e8933475477c20bae6039ed47f32c1d267a34babc347f28f0df"
    )
    assert values["scheduler"]["image"].endswith(
        "@sha256:d94d07ba9e7d6de898b6d96c1a072f6f8266c687af78a74f380087a0addf5d17"
    )
    assert "@sha256:79b979d2fc7b46fdddab19e619c65faa201d0d76080765f0ec4b1969e0abe33f" in json.dumps(
        values["hcloud-csi"]
    )
    provenance = (PLATFORM / "HCLOUD_CSI_PROVENANCE.md").read_text(encoding="utf-8")
    assert "1dd5776c2810f80f038454c9333a3814a2319b1b" in provenance
    assert "encryption-passphrase" in provenance
    assert "crypto_LUKS" in provenance


def test_platform_renders_luks_retain_storage_and_exact_schedule_contract() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    storage = _find(documents, "StorageClass", "exomem-hcloud-encrypted-retain")
    assert storage["provisioner"] == "csi.hetzner.cloud"
    assert storage["reclaimPolicy"] == "Retain"
    assert storage["volumeBindingMode"] == "WaitForFirstConsumer"
    assert storage["parameters"] == {
        "csi.storage.k8s.io/fstype": "ext4",
        "csi.storage.k8s.io/node-publish-secret-name": "exomem-volume-encryption",
        "csi.storage.k8s.io/node-publish-secret-namespace": "exomem-platform",
    }

    runtime_class = _find(documents, "RuntimeClass", "exomem-storage-init")
    assert runtime_class["handler"] == "runc"
    tenant_admission = _find(documents, "ValidatingAdmissionPolicy", "exomem-tenant-boundary")
    assert "paramKind" not in tenant_admission["spec"]
    tenant_binding = _find(
        documents, "ValidatingAdmissionPolicyBinding", "exomem-tenant-boundary"
    )
    assert "matchResources" not in tenant_binding["spec"]
    assert "matchConditions" not in tenant_admission["spec"]
    variables = tenant_admission["spec"]["variables"]
    assert [variable["name"] for variable in variables] == [
        "storageInit",
        "tenantNamespace",
        "inScope",
    ]
    assert "exomem-storage-init" in variables[0]["expression"]
    assert "exomem.io/tenant-cell" in variables[1]["expression"]
    assert all(
        "!variables.inScope" in validation["expression"]
        for validation in tenant_admission["spec"]["validations"]
    )
    admission_text = json.dumps(tenant_admission)
    assert "namespaceObject.metadata.annotations['exomem.io/approved-image']" not in admission_text
    assert "ghcr.io/artexis10/exomem@sha256:" + "a" * 64 in admission_text
    assert "runAsUser == 10001" in admission_text
    assert "persistentVolumeClaim.claimName" in admission_text
    assert "secret.secretName" in admission_text
    assert "configMap.name" in admission_text
    assert "size(object.spec.initContainers)" in admission_text
    assert "has(object.spec.initContainers)" in admission_text
    assert "has(dyn(volume.emptyDir).sizeLimit)" in admission_text
    assert "seccompProfile.type == 'RuntimeDefault'" in admission_text
    assert "securityContext.seccompProfile" in admission_text
    assert "terminationMessagePath == '/dev/termination-log'" in admission_text
    assert "terminationMessagePolicy == 'File'" in admission_text
    for forbidden_surface in (
        "lifecycle",
        "livenessProbe",
        "readinessProbe",
        "startupProbe",
        "ports",
        "stdin",
        "stdinOnce",
        "tty",
        "envFrom",
        "workingDir",
    ):
        assert forbidden_surface in admission_text

    namespace_policy = _find(
        documents, "ValidatingAdmissionPolicy", "exomem-tenant-namespace-contract"
    )
    namespace_policy_text = json.dumps(namespace_policy)
    namespace_operations = namespace_policy["spec"]["matchConstraints"]["resourceRules"][0][
        "operations"
    ]
    assert namespace_operations == ["CREATE", "UPDATE"]
    assert "request.userInfo.username" in namespace_policy_text
    assert "system:admin" in namespace_policy_text
    assert "restricted-v1.35 tenant namespace contract" in namespace_policy_text
    for exact_value in (
        "pod-security.kubernetes.io/enforce",
        "pod-security.kubernetes.io/enforce-version",
        "pod-security.kubernetes.io/audit",
        "pod-security.kubernetes.io/audit-version",
        "pod-security.kubernetes.io/warn",
        "pod-security.kubernetes.io/warn-version",
        "restricted",
        "v1.35",
    ):
        assert exact_value in namespace_policy_text
    for protected_field in (
        "exomem.io/resource-name",
        "exomem.io/approved-image",
        "exomem.io/pvc-name",
        "exomem.io/credentials-secret-name",
        "exomem.io/init-request-configmap-name",
        "exomem.io/tenant-cell",
        "exomem.io/cell-resource",
    ):
        assert protected_field in namespace_policy_text

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    cronjobs = {
        document["metadata"]["name"]: document
        for document in documents
        if document.get("kind") == "CronJob"
    }
    assert set(cronjobs) == {job["name"] for job in contract["jobs"]}
    for job in contract["jobs"]:
        rendered = cronjobs[job["name"]]
        spec = rendered["spec"]
        job_spec = spec["jobTemplate"]["spec"]
        pod = job_spec["template"]["spec"]
        container = pod["containers"][0]
        assert spec["schedule"] == job["schedule"]
        assert spec["concurrencyPolicy"] == "Forbid"
        assert spec["startingDeadlineSeconds"] == 45
        assert spec["successfulJobsHistoryLimit"] == 1
        assert spec["failedJobsHistoryLimit"] == 3
        assert job_spec["activeDeadlineSeconds"] == 30
        assert job_spec["backoffLimit"] == 1
        assert job_spec["ttlSecondsAfterFinished"] == 300
        assert pod["restartPolicy"] == "Never"
        assert container["env"][0]["name"] == "EXOMEM_HOSTED_SCHEDULER_SECRET"
        command = " ".join(container["args"])
        assert contract["origin"] + job["path"] in command
        assert "--connect-timeout 5" in command
        assert "--max-time 20" in command
        assert 'test "${status}" = "200"' in command
        assert "CRON_SECRET" not in json.dumps(rendered)

    policy = _find(documents, "ConfigMap", "exomem-hosted-scheduler-contract")
    rendered_contract = json.loads(policy["data"]["contract.json"])
    assert rendered_contract == contract
    contract_sha = hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
    assert policy["metadata"]["annotations"]["exomem.io/contract-sha256"] == contract_sha
    assert all(
        item["metadata"]["annotations"]["exomem.io/contract-sha256"] == contract_sha
        for item in cronjobs.values()
    )
    assert contract["authentication"] == {
        "scheme": "bearer",
        "schedulerEnvironmentVariable": "EXOMEM_HOSTED_SCHEDULER_SECRET",
        "receiverActiveEnvironmentVariable": "EXOMEM_HOSTED_SCHEDULER_SECRET",
        "receiverPreviousEnvironmentVariable": "EXOMEM_HOSTED_SCHEDULER_SECRET_PREVIOUS",
        "maxReceiverVersions": 2,
    }
    assert rendered_contract["observability"] == {
        "contentFree": True,
        "attemptCounterMetric": "exomem_hosted_scheduler_attempts_total",
        "durationHistogramMetric": "exomem_hosted_scheduler_duration_seconds",
        "lastSuccessMetric": "exomem_hosted_scheduler_last_success_unixtime",
        "failureCounterMetric": "exomem_hosted_scheduler_failures_total",
        "missedRunAlertAfterSeconds": 180,
        "consecutiveFailureAlertThreshold": 2,
    }


def test_platform_rejects_scheduler_contract_sha_drift() -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    result = subprocess.run(
        [
            str(HELM),
            "template",
            "contract-test",
            str(PLATFORM),
            "--namespace",
            "exomem-platform",
            "--values",
            str(PLATFORM / "values.validation.yaml"),
            "--set",
            f"scheduler.contractSha256={'0' * 64}",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "scheduler contract SHA-256 mismatch" in result.stderr


def test_platform_renders_owned_namespaces_and_content_free_observability() -> None:
    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        release_name="exomem-platform",
    )
    expected_enforcement = {"exomem-platform": "privileged", "exomem-system": "restricted"}
    for name, enforcement in expected_enforcement.items():
        namespace = _find(documents, "Namespace", name)
        assert namespace["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
        assert namespace["metadata"]["labels"] == {
            "app.kubernetes.io/part-of": "exomem-hosted",
            "pod-security.kubernetes.io/enforce": enforcement,
            "pod-security.kubernetes.io/enforce-version": "v1.35",
            "pod-security.kubernetes.io/audit": "restricted",
            "pod-security.kubernetes.io/audit-version": "v1.35",
            "pod-security.kubernetes.io/warn": "restricted",
            "pod-security.kubernetes.io/warn-version": "v1.35",
        }

    observability = _find(documents, "ConfigMap", "exomem-hosted-observability-contract")
    contract = json.loads(observability["data"]["contract.json"])
    assert contract == json.loads(
        (ROOT / "infra/contracts/observability-v1.json").read_text(encoding="utf-8")
    )
    assert contract["alerts"]["scheduler_missed_run_seconds"] == 180
    assert contract["alerts"]["scheduler_consecutive_failures"] == 2
    assert contract["alerts"]["backup_warn_age_seconds"] == 2700
    assert contract["alerts"]["backup_block_age_seconds"] == 3600

    scheduler_jobs = [item for item in documents if item.get("kind") == "CronJob"]
    scheduler_text = json.dumps(scheduler_jobs)
    scheduler_commands = "\n".join(
        item["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]
        for item in scheduler_jobs
    )
    assert 'if status="$(curl' in scheduler_commands
    assert 'status="000"' in scheduler_commands
    for metric in (
        "exomem_hosted_scheduler_attempts_total",
        "exomem_hosted_scheduler_failures_total",
        "exomem_hosted_scheduler_duration_seconds",
        "exomem_hosted_scheduler_last_success_unixtime",
    ):
        assert metric in scheduler_text
    for forbidden in ("response_body", "authorization_value", "environment_dump"):
        assert forbidden not in scheduler_text.lower()


@pytest.mark.parametrize(
    ("values_name", "expected_kind"),
    (("values.initialize.yaml", "Job"), ("values.validation.yaml", "StatefulSet")),
)
def test_cell_chart_renders_separate_privileged_init_and_restricted_serving_modes(
    values_name: str, expected_kind: str
) -> None:
    documents = _render(CELL, CELL / values_name, namespace="cell-alpha-test")
    assert _find(documents, "PersistentVolumeClaim", "cell-alpha-data")
    quota = _find(documents, "ResourceQuota", "cell-alpha-quota")
    assert quota["spec"]["hard"]["persistentvolumeclaims"] == "1"
    assert quota["spec"]["hard"]["requests.storage"] == "10Gi"

    workload = _find(documents, expected_kind, "cell-alpha" if expected_kind == "StatefulSet" else "cell-alpha-init")
    namespace = _find(documents, "Namespace", "cell-alpha-test")
    assert namespace["metadata"]["labels"] == {
        "exomem.io/tenant-cell": "true",
        "exomem.io/cell-resource": "cell-alpha",
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "v1.35",
        "pod-security.kubernetes.io/audit": "restricted",
        "pod-security.kubernetes.io/audit-version": "v1.35",
        "pod-security.kubernetes.io/warn": "restricted",
        "pod-security.kubernetes.io/warn-version": "v1.35",
    }
    assert namespace["metadata"]["annotations"] == {
        "helm.sh/resource-policy": "keep",
        "exomem.io/resource-name": "cell-alpha",
        "exomem.io/pvc-name": "cell-alpha-data",
        "exomem.io/credentials-secret-name": "cell-alpha-credentials",
        "exomem.io/init-request-configmap-name": "cell-alpha-init-request",
    }
    if expected_kind == "Job":
        pod = workload["spec"]["template"]["spec"]
        assert pod["runtimeClassName"] == "exomem-storage-init"
        container = pod["containers"][0]
        assert container["name"] == "exomem"
        assert container["securityContext"]["runAsUser"] == 0
        assert "seccompProfile" not in container["securityContext"]
        assert container["terminationMessagePath"] == "/dev/termination-log"
        assert container["terminationMessagePolicy"] == "File"
        assert container["args"] == [
            "hosted",
            "init",
            "--request-file",
            "/run/exomem/operator-requests/init.json",
        ]
    else:
        pod = workload["spec"]["template"]["spec"]
        assert pod["restartPolicy"] == "Always"
        assert "runtimeClassName" not in pod
        assert "fsGroup" not in pod.get("securityContext", {})
        assert len(pod.get("initContainers", [])) == 0
        container = pod["containers"][0]
        security = container["securityContext"]
        assert "seccompProfile" not in security
        assert container["terminationMessagePath"] == "/dev/termination-log"
        assert container["terminationMessagePolicy"] == "File"
        assert security["runAsNonRoot"] is True
        assert security["runAsUser"] == 10001
        assert security["readOnlyRootFilesystem"] is True
        assert security["allowPrivilegeEscalation"] is False
        assert security["capabilities"]["drop"] == ["ALL"]
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["EXOMEM_HOSTED_CELL_ID"] == "alpha-test-original"
        assert env["EXOMEM_HOSTED_VAULT_ID"] == "vault-alpha-original"
        assert env["EXOMEM_HOSTED_RUNTIME_UID"] == "10001"
        assert env["EXOMEM_HOSTED_RUNTIME_GID"] == "10001"
        assert env["EXOMEM_HOSTED_TRANSFER_BROWSER_ORIGIN"] == "https://substratesystems.io"
        assert "EXOMEM_HOSTED_BROWSER_ORIGIN" not in env
        assert env["EXOMEM_HOSTED_STORAGE_LIMIT_BYTES"] == "5368709120"
        assert env["EXOMEM_HOSTED_UPLOAD_LIMIT_BYTES"] == "94371840"
        assert env["EXOMEM_HOSTED_WORKER_LIMIT"] == "0"
        assert env["EXOMEM_HOSTED_FEATURE_GRANTS"] == ""
        assert env["TMPDIR"] == "/tmp/runtime"

        temporary = next(volume for volume in pod["volumes"] if volume["name"] == "tmp")
        assert temporary["emptyDir"]["sizeLimit"] == "256Mi"
        assert container["resources"]["limits"]["ephemeral-storage"] == "512Mi"

        credentials = next(
            volume for volume in pod["volumes"] if volume["name"] == "credentials"
        )
        assert credentials["secret"]["defaultMode"] == 0o444
        assert credentials["secret"]["secretName"] == "cell-alpha-credentials"
        assert "EXOMEM_HOSTED_SERVICE_CREDENTIAL" not in env
        assert not any("secretKeyRef" in item.get("valueFrom", {}) for item in container["env"])

    network_policies = [item for item in documents if item.get("kind") == "NetworkPolicy"]
    assert len(network_policies) >= (2 if expected_kind == "StatefulSet" else 1)
    assert all(item["spec"].get("policyTypes") for item in network_policies)
    service = [item for item in documents if item.get("kind") == "Service"]
    assert (len(service) == 1) == (expected_kind == "StatefulSet")
    if service:
        assert service[0]["spec"]["type"] == "ClusterIP"


def test_cell_schema_rejects_mutable_image_and_non_fixed_limits() -> None:
    schema = json.loads((CELL / "values.schema.json").read_text(encoding="utf-8"))
    text = json.dumps(schema)
    assert "@sha256:" in text
    assert '"const": 5368709120' in text
    assert '"const": 94371840' in text
    assert '"const": 0' in text
    assert '"const": "10Gi"' in text
    assert '"transferHostname"' not in json.dumps(schema["properties"]["routes"])


def test_cell_routes_expose_only_exact_control_and_transfer_paths() -> None:
    documents = _render(
        CELL,
        CELL / "values.validation.yaml",
        namespace="cell-alpha-test",
        extra_args=("--set", "routes.enabled=true"),
    )
    middleware = _find(documents, "Middleware", "cell-alpha-strip-cell")
    assert middleware["spec"]["stripPrefix"]["prefixes"] == [
        "/cells/alpha-test-original"
    ]

    control = _find(documents, "IngressRoute", "cell-alpha-control")
    transfer = _find(documents, "IngressRoute", "cell-alpha-transfer")
    assert control["spec"]["routes"][0]["match"] == (
        "Host(`control.example.test`) && "
        "(Path(`/cells/alpha-test-original/private/exomem/v1`) || "
        "PathPrefix(`/cells/alpha-test-original/private/exomem/v1/`))"
    )
    control_match = control["spec"]["routes"][0]["match"]
    assert "PathPrefix(`/cells/alpha-test-original/private/exomem/v1`)" not in control_match
    assert "/private/exomem/v10" not in control_match
    assert transfer["spec"]["routes"][0]["match"] == (
        "Host(`transfer.example.test`) && "
        "(Path(`/cells/alpha-test-original/public/exomem/v2/transfers/upload`) || "
        "Path(`/cells/alpha-test-original/public/exomem/v2/transfers/download`))"
    )
    for route in (control, transfer):
        upstream = route["spec"]["routes"][0]["services"]
        assert upstream == [{"name": "cell-alpha", "port": 8765}]

    services = [document for document in documents if document.get("kind") == "Service"]
    assert services and all(service["spec"]["type"] == "ClusterIP" for service in services)
    rendered_text = json.dumps(documents).lower()
    assert "email" not in rendered_text
    assert "owner-name" not in rendered_text


def test_cell_route_and_runtime_share_one_transfer_hostname() -> None:
    documents = _render(
        CELL,
        CELL / "values.validation.yaml",
        namespace="cell-alpha-test",
        extra_args=(
            "--set",
            "routes.enabled=true",
            "--set",
            "transferHostname=files.example.test",
        ),
    )
    stateful_set = _find(documents, "StatefulSet", "cell-alpha")
    environment = {
        item["name"]: item.get("value")
        for item in stateful_set["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert environment["EXOMEM_HOSTED_TRANSFER_HOST"] == "files.example.test"
    route = _find(documents, "IngressRoute", "cell-alpha-transfer")
    assert route["spec"]["routes"][0]["match"].startswith(
        "Host(`files.example.test`)"
    )


def test_cloudflare_tunnel_targets_the_rendered_production_traefik_service() -> None:
    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        release_name="exomem-platform",
    )
    traefik = next(
        document
        for document in documents
        if document.get("kind") == "Service"
        and document.get("metadata", {}).get("name") == "exomem-platform-traefik"
    )
    assert traefik["spec"]["ports"] == [
        {"name": "web", "port": 80, "protocol": "TCP", "targetPort": "web"}
    ]
    target = "http://exomem-platform-traefik.exomem-platform.svc.cluster.local:80"
    cloudflare = (ROOT / "infra/terraform/foundation/cloudflare.tf").read_text(
        encoding="utf-8"
    )
    assert cloudflare.count(target) == 2
