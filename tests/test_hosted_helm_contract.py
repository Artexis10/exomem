from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "infra/helm/platform"
CONTRACT = ROOT / "infra/contracts/exomem-hosted-schedules-v1.json"
RUNTIME_GATE = ROOT / "infra/contracts/exomem-hosted-runtime-k3s-gate-v1.json"
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
    result = _render_process(
        chart,
        values,
        namespace=namespace,
        extra_args=extra_args,
        release_name=release_name,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return _documents(result.stdout)


def _render_process(
    chart: Path,
    values: Path,
    *,
    namespace: str,
    extra_args: tuple[str, ...] = (),
    release_name: str = "contract-test",
) -> subprocess.CompletedProcess[str]:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    with tempfile.TemporaryDirectory(prefix="exomem-helm-") as directory:
        staged_chart = Path(directory) / chart.name
        shutil.copytree(chart, staged_chart)
        build = subprocess.run(
            [str(HELM), "dependency", "build", str(staged_chart)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, build.stdout + build.stderr
        staged_values = staged_chart / values.relative_to(chart)
        result = subprocess.run(
            [
                str(HELM),
                "template",
                release_name,
                str(staged_chart),
                "--namespace",
                namespace,
                "--values",
                str(staged_values),
                "--include-crds",
                *extra_args,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        return result


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
    assert "runtime" not in values
    platform_schema = json.loads((PLATFORM / "values.schema.json").read_text(encoding="utf-8"))
    assert platform_schema["properties"]["runtime"] is False
    jobs_image = platform_schema["properties"]["platformJobs"]["properties"]["image"]
    assert jobs_image["pattern"] == "^ghcr\\.io/artexis10/exomem@sha256:[0-9a-f]{64}$"
    assert "platformJobs" in platform_schema["required"]
    assert "@sha256:79b979d2fc7b46fdddab19e619c65faa201d0d76080765f0ec4b1969e0abe33f" in json.dumps(
        values["hcloud-csi"]
    )
    provenance = (PLATFORM / "HCLOUD_CSI_PROVENANCE.md").read_text(encoding="utf-8")
    assert "1dd5776c2810f80f038454c9333a3814a2319b1b" in provenance
    assert "encryption-passphrase" in provenance
    assert "crypto_LUKS" in provenance




def test_runtime_k3s_gate_pins_the_reviewed_release_unit() -> None:
    gate = json.loads(RUNTIME_GATE.read_text(encoding="utf-8"))
    assert gate == {
        "artifact": "exomem-hosted-runtime-k3s-gate",
        "schemaVersion": 1,
        "k3sImage": (
            "rancher/k3s@sha256:9d6b9c15e8031c1aea7dd7f0cdc019f5e74a23c53b9eada564b7a8dc94efc14c"
        ),
        "sourceRepository": "https://github.com/Artexis10/exomem",
        "sourceCommit": "54618b931dec8f0ad053dce48dd80cc36c95c549",
        "release": "0.22.0",
        "hostedProtocol": "1",
        "dockerTarget": "hosted",
        "releaseBuildTime": "2026-07-14T05:37:15Z",
        "operatorContractSha256": (
            "407799e723e9d996e5ab15ca76c071c3ae497041a1096f106690712ce6fe4ca6"
        ),
    }


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

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    cronjobs = {
        document["metadata"]["name"]: document
        for document in documents
        if document.get("kind") == "CronJob"
        and document.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/part-of")
        == "exomem-hosted-scheduler"
    }
    # Scheduler CronJobs are prefixed so that a contract name cannot collide
    # with another CronJob in the same namespace.
    assert set(cronjobs) == {"exomem-hosted-scheduler-exomem-access-delivery", "exomem-hosted-scheduler-exomem-reconcile"}
    policies = yaml.safe_load((PLATFORM / "values.yaml").read_text(encoding="utf-8"))["scheduler"]["jobs"]
    enabled_jobs = [job for job in contract["jobs"] if policies[job["name"]].get("enabled", True)]
    for job in enabled_jobs:
        rendered = cronjobs[f"exomem-hosted-scheduler-{job['name']}"]
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
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["TARGET_URL"] == contract["origin"] + job["path"]
        assert env["CONNECT_TIMEOUT_SECONDS"] == "5"
        assert env["TOTAL_TIMEOUT_SECONDS"] == "20"
        assert env["CADENCE_SECONDS"] == str(job["cadenceSeconds"])
        assert container["command"] == [
            "python",
            "/opt/exomem-hosted/scheduler_runtime.py",
            "request",
        ]
        assert container["image"] == "ghcr.io/artexis10/exomem@sha256:" + "a" * 64
        assert pod["automountServiceAccountToken"] is False
        assert {volume["name"] for volume in pod["volumes"]} == {"runtime", "kube-api"}
        projected = next(volume for volume in pod["volumes"] if volume["name"] == "kube-api")
        token_projection = projected["projected"]["sources"][0]["serviceAccountToken"]
        assert token_projection["audience"] == "https://kubernetes.default.svc.cluster.local"
        assert projected["projected"]["defaultMode"] == 0o440
        assert pod["securityContext"]["fsGroup"] == 10001
        assert "CRON_SECRET" not in json.dumps(rendered)

        state = _find(documents, "ConfigMap", f"exomem-hosted-scheduler-state-{job['name']}")
        persisted = json.loads(state["data"]["state.json"])
        assert persisted["job"] == job["name"]
        assert persisted["duration_seconds"]["buckets"] == {
            "1": 0,
            "5": 0,
            "20": 0,
            "+Inf": 0,
        }
        assert state["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"

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
        "missedRunAlertAfterSeconds": 900,
        "consecutiveFailureAlertThreshold": 2,
    }


@pytest.mark.parametrize(
    ("cellctl_enabled", "cloud_gateway_enabled", "expected_reconcile"),
    [
        (True, False, False),
        (False, True, False),
        (True, True, False),
        (False, False, True),
    ],
)
def test_scheduler_suspension_follows_cloud_mode(
    cellctl_enabled: bool,
    cloud_gateway_enabled: bool,
    expected_reconcile: bool,
) -> None:
    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=(
            "--set",
            f"cellctl.enabled={str(cellctl_enabled).lower()}",
            "--set",
            f"cloudGateway.enabled={str(cloud_gateway_enabled).lower()}",
        ),
    )
    suspension = {
        document["metadata"]["labels"]["app.kubernetes.io/name"]: document["spec"][
            "suspend"
        ]
        for document in documents
        if document.get("kind") == "CronJob"
        and document.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/part-of")
        == "exomem-hosted-scheduler"
    }
    assert suspension == {
        "exomem-access-delivery": expected_reconcile,
        "exomem-reconcile": expected_reconcile,
    }


def test_platform_rejects_scheduler_contract_sha_drift() -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    result = _render_process(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=("--set", f"scheduler.contractSha256={'0' * 64}"),
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
        assert namespace["metadata"]["annotations"]["meta.helm.sh/release-name"] == (
            "exomem-platform"
        )
        assert (
            namespace["metadata"]["annotations"]["meta.helm.sh/release-namespace"]
            == "exomem-platform"
        )
        assert namespace["metadata"]["labels"] == {
            "app.kubernetes.io/managed-by": "Helm",
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
    assert contract["alerts"]["scheduler_missed_run_seconds"] == 900
    assert contract["alerts"]["scheduler_consecutive_failures"] == 2
    assert contract["poll_interval_seconds"] == 300
    scheduler_check = next(
        check for check in contract["checks"] if check["name"] == "scheduler-last-success"
    )
    assert scheduler_check["maximum_age_seconds"] == 480
    # Daily full archives: the newest object always approaches the 24-hour
    # objective just before each run, so thresholds sit past it. Warn at 26h
    # catches a late run; block at 30h catches a missed one inside the same day.
    backup_check = next(
        check for check in contract["checks"] if check["name"] == "backup-freshness"
    )
    assert backup_check["maximum_age_seconds"] == 108000
    assert contract["alerts"]["backup_warn_age_seconds"] == 93600
    assert contract["alerts"]["backup_block_age_seconds"] == 108000
    assert contract["alerts"]["backup_warn_age_seconds"] > 24 * 3600
    assert contract["alerts"]["backup_block_age_seconds"] < 48 * 3600

    scheduler_jobs = [
        item
        for item in documents
        if item.get("kind") == "CronJob"
        and item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/part-of")
        == "exomem-hosted-scheduler"
    ]
    assert {
        item["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"]
        for item in scheduler_jobs
    } == {"ghcr.io/artexis10/exomem@sha256:" + "a" * 64}
    scheduler_text = json.dumps(scheduler_jobs)
    runtime = _find(documents, "ConfigMap", "exomem-hosted-scheduler-runtime")
    runtime_source = runtime["data"]["scheduler_runtime.py"]
    assert "record_attempt" in runtime_source
    assert "evaluate_alerts" in runtime_source
    assert "ThreadingHTTPServer" in runtime_source
    assert "NoRedirect" in runtime_source
    assert _find(documents, "Deployment", "exomem-hosted-scheduler-collector")
    evaluator = _find(documents, "Deployment", "exomem-hosted-scheduler-alerts")
    collector = _find(documents, "Deployment", "exomem-hosted-scheduler-collector")
    for deployment in (collector, evaluator):
        assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == (
            "ghcr.io/artexis10/exomem@sha256:" + "a" * 64
        )
    evaluator_env = {
        item["name"]: item.get("value")
        for item in evaluator["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert evaluator_env["MISSED_RUN_SECONDS"] == "900"
    assert evaluator_env["FAILURE_THRESHOLD"] == "2"
    evaluator_container = evaluator["spec"]["template"]["spec"]["containers"][0]
    webhook = next(
        item for item in evaluator_container["env"] if item["name"] == "ALERT_WEBHOOK_URL"
    )
    assert webhook["valueFrom"]["secretKeyRef"] == {
        "name": "exomem-hosted-alert-delivery",
        "key": "url",
    }
    assert evaluator_env["COLLECTOR_SNAPSHOT_URL"] == (
        "http://exomem-hosted-scheduler-metrics.exomem-platform.svc.cluster.local:9090/snapshot"
    )
    evaluator_projected = next(
        volume
        for volume in evaluator["spec"]["template"]["spec"]["volumes"]
        if volume["name"] == "kube-api"
    )
    assert (
        evaluator_projected["projected"]["sources"][0]["serviceAccountToken"]["audience"]
        == "https://kubernetes.default.svc.cluster.local"
    )
    metrics_service = _find(documents, "Service", "exomem-hosted-scheduler-metrics")
    assert metrics_service["metadata"]["annotations"]["prometheus.io/scrape"] == "true"
    alert_state = _find(documents, "ConfigMap", "exomem-hosted-scheduler-alert-state")
    assert json.loads(alert_state["data"]["state.json"])["transitions_total"] == 0
    template = (PLATFORM / "templates/observability.yaml").read_text(encoding="utf-8")
    assert 'lookup "v1" "ConfigMap"' in template
    for metric in (
        "exomem_hosted_scheduler_attempts_total",
        "exomem_hosted_scheduler_failures_total",
        "exomem_hosted_scheduler_duration_seconds",
        "exomem_hosted_scheduler_last_success_unixtime",
    ):
        assert metric in runtime_source
    for forbidden in ("response_body", "authorization_value", "environment_dump"):
        assert forbidden not in (scheduler_text + runtime_source).lower()


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
    cloudflare = (ROOT / "infra/terraform/foundation/cloudflare.tf").read_text(encoding="utf-8")
    origins = re.findall(r'service\s*=\s*"(https?://[^\"]+)"', cloudflare)
    assert origins and set(origins) == {target}


def test_no_two_rendered_objects_share_one_kubernetes_identity() -> None:
    """Two manifests with the same identity silently collapse into one object.

    The schedules contract carries a job named `exomem-export-gc`, and
    durability-workloads.yaml independently renders a CronJob of that exact name
    in the same namespace. Kubernetes accepted the second as an update of the
    first, so the chart shipped eight CronJobs and the cluster held seven, and
    `helm install --wait` failed with "no CronJob with the name exomem-export-gc
    found" only at install time. Every other assertion in this file inspects a
    document it looked up by name, which cannot notice a second document
    answering to the same name.
    """

    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    identities: dict[tuple[str, str, str, str], int] = {}
    for document in documents:
        metadata = document.get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("name"):
            continue
        identity = (
            str(document.get("apiVersion", "")),
            str(document.get("kind", "")),
            str(metadata.get("namespace", "exomem-platform")),
            str(metadata["name"]),
        )
        identities[identity] = identities.get(identity, 0) + 1

    duplicates = sorted(identity for identity, count in identities.items() if count > 1)
    assert not duplicates, f"rendered manifests collide on one identity: {duplicates}"


def test_scheduler_cronjobs_are_namespaced_away_from_workload_cronjobs() -> None:
    """The scheduler owns its object names; the contract only supplies the job name."""

    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    schedules = json.loads(
        (PLATFORM / "files/exomem-hosted-schedules-v1.json").read_text(encoding="utf-8")
    )
    policies = yaml.safe_load((PLATFORM / "values.yaml").read_text(encoding="utf-8"))["scheduler"]["jobs"]
    expected = {f"exomem-hosted-scheduler-{job['name']}" for job in schedules["jobs"] if policies[job["name"]].get("enabled", True)}
    rendered = {
        document["metadata"]["name"]
        for document in documents
        if document.get("kind") == "CronJob"
        and document.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/part-of")
        == "exomem-hosted-scheduler"
    }
    assert rendered == expected


def test_no_rendered_value_uses_scientific_notation() -> None:
    """Helm parses values as float64, and Go prints a large float64 as 1.5e+08.

    `hcloudServerId` reached the provisioner as the string '1.56895713e+08' and
    every worker died in pydantic with `unable to parse string as an integer`.
    The deploy runbook hands that value in as a JSON number (`jq --argjson`), so
    the chart has to survive a float64; the templates pipe through `int64`. The
    validation fixture used to be `101`, which is small enough that Go prints it
    plainly -- the fixture, not the template, was why this rendered clean in CI.
    """

    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    rendered = _render_process(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
    )
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    offenders = re.findall(r"\d+\.\d+e[+-]\d+", rendered.stdout)
    assert not offenders, (
        f"rendered manifests contain float-formatted numbers: {sorted(set(offenders))}"
    )




def test_no_service_requires_an_external_load_balancer() -> None:
    """The node runs k3s with servicelb disabled, so a LoadBalancer never gets an address.

    `helm --wait` blocks until every LoadBalancer Service has an ingress IP, so one
    such Service makes the install hang until it times out and rolls back. Ingress
    reaches this platform through the Cloudflare Tunnel, which dials traefik from
    inside the cluster, so nothing here should ask for an external balancer.

    This also guards a silently-ignored value: traefik 41.x reads the type from
    `service.spec.type`, and the chart used to set a bare `service.type`, which is
    not a key in that subchart. The override did nothing and the subchart default
    of LoadBalancer applied, with no error anywhere.
    """

    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    offenders = [
        document["metadata"]["name"]
        for document in documents
        if document.get("kind") == "Service"
        and document.get("spec", {}).get("type") in {"LoadBalancer", "NodePort"}
    ]
    assert not offenders, f"these Services need an external balancer the cluster lacks: {offenders}"


def test_cloud_cell_default_resources_agree_with_cellctls_own_defaults() -> None:
    """The platform's cellResources feed cellctl's CELLCTL_CELL_* settings, and
    cellctl's ResourceSettings defaults apply when they are unset; the two copies
    must name the same envelope. The 3Gi limit stays: the first index build over
    a large restored vault was OOM-killed at 1536Mi."""
    values = yaml.safe_load((PLATFORM / "values.yaml").read_text(encoding="utf-8"))
    chart = values["cellctl"]["cellResources"]
    manifests = ast.parse((ROOT / "infra/cellctl/src/cellctl/manifests.py").read_text(encoding="utf-8"))
    settings = next(
        node for node in manifests.body if isinstance(node, ast.ClassDef) and node.name == "ResourceSettings"
    )
    defaults = {
        statement.target.id: ast.literal_eval(statement.value)
        for statement in settings.body
        if isinstance(statement, ast.AnnAssign) and statement.value is not None
    }
    assert chart == {
        "cpuRequest": defaults["cpu_request"],
        "cpuLimit": defaults["cpu_limit"],
        "memoryRequest": defaults["memory_request"],
        "memoryLimit": defaults["memory_limit"],
    }
    assert chart["memoryLimit"] == "3Gi"


def test_cellctl_renders_the_media_engine_switch_under_the_name_the_runtime_reads() -> None:
    """cellctl names the per-cell engine switch itself; renaming it on one side only
    would leave every engine off in every selected cell, with no error anywhere."""
    from exomem import media_engines

    manifests = ast.parse((ROOT / "infra/cellctl/src/cellctl/manifests.py").read_text(encoding="utf-8"))
    [rendered] = [
        ast.literal_eval(node.value)
        for node in manifests.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "MEDIA_ENGINES_ENV" for target in node.targets)
    ]
    assert rendered == media_engines.ENGINES_ENV


def test_chart_schema_names_exactly_the_engines_the_runtime_knows() -> None:
    """The schema's engine list is a copy of `media_engines.ENGINES`. A name the runtime does
    not know, in a fleet default, renders into every cell's environment and restarts them all."""
    from exomem import media_engines

    schema = json.loads((PLATFORM / "values.schema.json").read_text(encoding="utf-8"))
    cellctl = schema["properties"]["cellctl"]["properties"]
    one_definition = {"$ref": "#/$defs/mediaEngineName"}
    assert cellctl["mediaEngineCellIds"]["propertyNames"] == one_definition
    assert cellctl["mediaEngineFleetDefault"]["items"] == one_definition
    assert set(schema["$defs"]["mediaEngineName"]["enum"]) == set(media_engines.ENGINES)


def test_disabled_health_jobs_have_no_workload_state_or_alert_target() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    names = {doc["metadata"]["name"] for doc in documents}
    for job in ("exomem-health-control", "exomem-health-email"):
        assert f"exomem-hosted-scheduler-{job}" not in names
        assert f"exomem-hosted-scheduler-state-{job}" not in names
    assert "exomem-memory-health-runner" not in names
    assert "exomem-memory-health-ingress" not in names
    for name in ("exomem-hosted-scheduler-collector", "exomem-hosted-scheduler-alerts"):
        container = _find(documents, "Deployment", name)["spec"]["template"]["spec"]["containers"][
            0
        ]
        env = {entry["name"]: entry.get("value") for entry in container["env"]}
        assert env["SCHEDULER_JOBS"] == "exomem-access-delivery,exomem-reconcile"
    for name in (
        "exomem-hosted-scheduler-state-writer",
        "exomem-hosted-scheduler-state-reader",
        "exomem-hosted-scheduler-alert-evaluator",
    ):
        role = _find(documents, "Role", name)
        assert "exomem-health" not in json.dumps(role["rules"])


def test_enabled_scheduler_jobs_use_contract_cadence_and_explicit_disable() -> None:
    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=(
            "--set",
            "scheduler.jobs.exomem-access-delivery.enabled=false",
            "--set",
            "scheduler.jobs.exomem-health-control.enabled=true",
            "--set",
            "scheduler.jobs.exomem-health-email.enabled=true",
        ),
    )
    names = {doc["metadata"]["name"] for doc in documents}
    assert "exomem-hosted-scheduler-exomem-access-delivery" not in names
    assert "exomem-hosted-scheduler-state-exomem-access-delivery" not in names
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    for job in contract["jobs"]:
        if job["name"] == "exomem-access-delivery":
            continue
        cronjob = _find(documents, "CronJob", f"exomem-hosted-scheduler-{job['name']}")
        container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        env = {entry["name"]: entry.get("value") for entry in container["env"]}
        assert env["TARGET_URL"] == contract["origin"] + job["path"]
        assert env["CADENCE_SECONDS"] == str(job["cadenceSeconds"])
        state = _find(documents, "ConfigMap", f"exomem-hosted-scheduler-state-{job['name']}")
        assert json.loads(state["data"]["state.json"])["cadence_seconds"] == job["cadenceSeconds"]
    for name in ("exomem-hosted-scheduler-collector", "exomem-hosted-scheduler-alerts"):
        container = _find(documents, "Deployment", name)["spec"]["template"]["spec"]["containers"][
            0
        ]
        env = {entry["name"]: entry.get("value") for entry in container["env"]}
        assert env["SCHEDULER_JOBS"] == "exomem-reconcile,exomem-health-control,exomem-health-email"
    evaluator = _find(documents, "Deployment", "exomem-hosted-scheduler-alerts")
    env = {
        entry["name"]: entry.get("value")
        for entry in evaluator["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["MISSED_RUN_SECONDS"] == "900"


def test_memory_health_runner_has_only_synthetic_cell_and_database_authority() -> None:
    cell_id = "abcdefghijklmnop"
    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=(
            "--set",
            "memoryHealth.enabled=true",
            "--set",
            f"memoryHealth.cellId={cell_id}",
            "--set-string",
            "memoryHealth.page=Knowledge Base/Notes/Insights/synthetic-health.md",
            "--set-string",
            "memoryHealth.bodyTemplate=Synthetic proof {{nonce}}",
            "--set",
            "memoryHealth.databaseSecretName=synthetic-health-database",
            "--set",
            "memoryHealth.bearerSecretName=synthetic-cell-bearer",
        ),
    )
    cronjob = _find(documents, "CronJob", "exomem-memory-health-runner")
    assert cronjob["spec"]["schedule"] == "* * * * *"
    assert cronjob["spec"]["concurrencyPolicy"] == "Forbid"
    job = cronjob["spec"]["jobTemplate"]["spec"]
    assert job["backoffLimit"] == 0
    assert job["activeDeadlineSeconds"] == 30
    pod = job["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"]["runAsUser"] == pod["securityContext"]["runAsGroup"] == 1000
    assert pod["nodeSelector"] == {"node-role.kubernetes.io/control-plane": "true"}
    container = pod["containers"][0]
    assert container["image"] == "ghcr.io/substrate-systems/substrate-gateway@sha256:" + "b" * 64
    assert container["command"] == ["node", "/app/gateway/memory-health-runner.cjs"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert set(env) == {
        "DATABASE_URL",
        "EXOMEM_HEALTH_MEMORY_BEARER",
        "EXOMEM_HEALTH_MEMORY_PAGE",
        "EXOMEM_HEALTH_MEMORY_BODY_TEMPLATE",
        "EXOMEM_HEALTH_MEMORY_TIMEOUT_MS",
        "EXOMEM_HEALTH_MEMORY_MCP_URL",
    }
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "synthetic-health-database",
        "key": "url",
    }
    assert env["EXOMEM_HEALTH_MEMORY_BEARER"]["valueFrom"]["secretKeyRef"] == {
        "name": "synthetic-cell-bearer",
        "key": "bearer",
    }
    assert (
        env["EXOMEM_HEALTH_MEMORY_MCP_URL"]["value"]
        == f"http://cell.exo-cell-{cell_id}.svc.cluster.local:8765/mcp"
    )
    runner = _find(documents, "NetworkPolicy", "exomem-memory-health-runner")
    assert runner["spec"]["ingress"] == []
    assert len(runner["spec"]["egress"]) == 3
    cell_rule = runner["spec"]["egress"][1]
    assert cell_rule["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": f"exo-cell-{cell_id}"
    }
    selector = cell_rule["to"][0]["podSelector"]
    assert selector["matchLabels"] == {
        "app.kubernetes.io/name": "exomem-cell",
        "exomem.io/cell": cell_id,
    }
    assert selector["matchExpressions"] == [
        {"key": "exomem.io/cell-job", "operator": "DoesNotExist"}
    ]
    assert cell_rule["ports"] == [{"port": 8765, "protocol": "TCP"}]
    assert runner["spec"]["egress"][2]["to"] == [{"ipBlock": {"cidr": "10.0.1.5/32"}}]
    ingress = _find(documents, "NetworkPolicy", "exomem-memory-health-ingress")
    assert ingress["metadata"]["namespace"] == f"exo-cell-{cell_id}"
    assert ingress["spec"]["podSelector"] == selector
    peer = ingress["spec"]["ingress"][0]["from"][0]
    assert peer == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "exomem-platform"}},
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "exomem-memory-health-runner"}},
    }
    assert not any(
        doc.get("kind") == "Namespace" and doc["metadata"]["name"] == f"exo-cell-{cell_id}"
        for doc in documents
    )


@pytest.mark.parametrize("contract", ["exomem-hosted-schedules-v1.json", "observability-v1.json"])
def test_chart_copy_of_a_contract_matches_the_canonical_contract(contract: str) -> None:
    # A copy, not a symlink: Helm reads only files inside the chart, and the
    # plugin directory scanner refuses to unpack a repository with symlinks.
    copy = PLATFORM / "files" / contract
    assert not copy.is_symlink()
    assert copy.read_bytes() == (ROOT / "infra/contracts" / contract).read_bytes(), (
        f"copy infra/contracts/{contract} into the chart's files/"
    )


def test_packaged_chart_materializes_the_canonical_scheduler_contract(tmp_path: Path) -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm packaging")
    package = subprocess.run(
        [str(HELM), "package", str(PLATFORM), "--destination", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert package.returncode == 0, package.stdout + package.stderr
    archive_path = next(tmp_path.glob("*.tgz"))
    with tarfile.open(archive_path) as archive:
        member = archive.getmember("exomem-platform/files/exomem-hosted-schedules-v1.json")
        assert member.isfile()
        content = archive.extractfile(member)
        assert content is not None
        assert content.read() == CONTRACT.read_bytes()
    rendered = subprocess.run(
        [
            str(HELM),
            "template",
            "packaged-contract",
            str(archive_path),
            "--namespace",
            "exomem-platform",
            "--values",
            str(PLATFORM / "values.validation.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    policy = _find(_documents(rendered.stdout), "ConfigMap", "exomem-hosted-scheduler-contract")
    assert json.loads(policy["data"]["contract.json"]) == json.loads(CONTRACT.read_text())


def test_disabling_all_jobs_cannot_create_an_unrestricted_scheduler_writer() -> None:
    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=(
            "--set",
            "scheduler.jobs.exomem-access-delivery.enabled=false",
            "--set",
            "scheduler.jobs.exomem-reconcile.enabled=false",
        ),
    )
    assert not any(
        doc.get("kind") in {"Role", "RoleBinding"}
        and doc["metadata"]["name"] == "exomem-hosted-scheduler-state-writer"
        for doc in documents
    )
