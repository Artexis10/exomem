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
    assert "runtime" not in values
    release = json.loads(validation_values["provisioner"]["releaseManifestJson"])
    assert release["runtimeImage"] == "ghcr.io/artexis10/exomem@sha256:" + "a" * 64
    platform_schema = json.loads((PLATFORM / "values.schema.json").read_text(encoding="utf-8"))
    assert platform_schema["properties"]["runtime"] is False
    assert "releaseManifestJson" in platform_schema["properties"]["provisioner"]["required"]
    assert values["cloudflared"]["image"].endswith(
        "@sha256:5e49861633763e8933475477c20bae6039ed47f32c1d267a34babc347f28f0df"
    )
    assert "@sha256:79b979d2fc7b46fdddab19e619c65faa201d0d76080765f0ec4b1969e0abe33f" in json.dumps(
        values["hcloud-csi"]
    )
    provenance = (PLATFORM / "HCLOUD_CSI_PROVENANCE.md").read_text(encoding="utf-8")
    assert "1dd5776c2810f80f038454c9333a3814a2319b1b" in provenance
    assert "encryption-passphrase" in provenance
    assert "crypto_LUKS" in provenance


def test_platform_rejects_mutable_or_partial_runtime_release_overrides(
    tmp_path: Path,
) -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    validation_values = yaml.safe_load(
        (PLATFORM / "values.validation.yaml").read_text(encoding="utf-8")
    )
    release = json.loads(validation_values["provisioner"]["releaseManifestJson"])
    mutable = {**release, "runtimeImage": "ghcr.io/artexis10/exomem:latest"}
    partial = dict(release)
    partial.pop("gatewayContractSha256")
    for index, invalid in enumerate((mutable, partial), start=1):
        override = tmp_path / f"invalid-release-{index}.yaml"
        override.write_text(
            yaml.safe_dump({"provisioner": {"releaseManifestJson": json.dumps(invalid)}}),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                str(HELM),
                "template",
                "exomem-platform",
                str(PLATFORM),
                "--namespace",
                "exomem-platform",
                "--values",
                str(PLATFORM / "values.validation.yaml"),
                "--values",
                str(override),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "hosted release" in result.stderr


def test_platform_rejects_malformed_release_fields_and_noncanonical_registry(
    tmp_path: Path,
) -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    validation_values = yaml.safe_load(
        (PLATFORM / "values.validation.yaml").read_text(encoding="utf-8")
    )
    release = json.loads(validation_values["provisioner"]["releaseManifestJson"])
    invalid_releases = []
    for field, value in (
        ("schemaVersion", "1"),
        ("release", {"not": "a version"}),
        ("hostedProtocol", "999"),
        ("releaseBuildTime", False),
        ("releaseBuildTime", "2026-99-99T03:50:34Z"),
        ("commandRegistry", [None] * 21),
        ("commandRegistry", list(reversed(release["commandRegistry"]))),
    ):
        invalid_releases.append({**release, field: value})
    for index, invalid in enumerate(invalid_releases, start=1):
        override = tmp_path / f"malformed-release-{index}.yaml"
        override.write_text(
            yaml.safe_dump({"provisioner": {"releaseManifestJson": json.dumps(invalid)}}),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                str(HELM),
                "template",
                "exomem-platform",
                str(PLATFORM),
                "--namespace",
                "exomem-platform",
                "--values",
                str(PLATFORM / "values.validation.yaml"),
                "--values",
                str(override),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (index, result.stdout)
        assert "hosted release" in result.stderr


def test_platform_rejects_wrong_provisioner_image_repository(tmp_path: Path) -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    override = tmp_path / "wrong-provisioner-image.yaml"
    override.write_text(
        yaml.safe_dump(
            {
                "provisioner": {
                    "image": "ghcr.io/someone/else@sha256:" + "e" * 64,
                }
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            str(HELM),
            "template",
            "exomem-platform",
            str(PLATFORM),
            "--namespace",
            "exomem-platform",
            "--values",
            str(PLATFORM / "values.validation.yaml"),
            "--values",
            str(override),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_platform_renders_real_provisioner_composition() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    validation_values = yaml.safe_load(
        (PLATFORM / "values.validation.yaml").read_text(encoding="utf-8")
    )
    expected_image = validation_values["provisioner"]["image"]
    expected_release = json.loads(validation_values["provisioner"]["releaseManifestJson"])

    release = _find(documents, "ConfigMap", "exomem-hosted-release-v1")
    assert release["metadata"]["namespace"] == "exomem-platform"
    assert release.get("immutable") is not True
    assert json.loads(release["data"]["exomem-hosted-release-v1.json"]) == expected_release

    service = _find(documents, "Service", "exomem-provisioner")
    assert service["metadata"]["namespace"] == "exomem-platform"
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [
        {"name": "http", "port": 8080, "protocol": "TCP", "targetPort": "http"}
    ]

    api = _find(documents, "Deployment", "exomem-provisioner-api")
    worker = _find(documents, "Deployment", "exomem-provisioner-worker")
    api_spec = api["spec"]["template"]["spec"]
    worker_spec = worker["spec"]["template"]["spec"]
    assert api_spec["automountServiceAccountToken"] is False
    assert api_spec["containers"][0]["image"] == expected_image
    api_environment = {item["name"]: item for item in api_spec["containers"][0]["env"]}
    assert api_environment["EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "exomem-provider-recovery-signer",
        "key": "private-key",
    }
    assert worker_spec["serviceAccountName"] == "exomem-cell-provisioner"
    assert worker_spec["containers"][0]["image"] == expected_image
    worker_container = worker_spec["containers"][0]
    environment = {item["name"]: item for item in worker_container["env"]}
    assert environment["EXOMEM_PROVIDER_RECOVERY_PUBLIC_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "exomem-provider-recovery-verifier",
        "key": "public-key",
    }
    assert "EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY" not in environment
    assert not any(
        privileged_fragment in name
        for name in environment
        for privileged_fragment in ("HCLOUD", "B2_", "DELETE_CREDENTIAL")
    )
    assert environment["EXOMEM_PROVISIONER_RELEASE_MANIFEST_PATH"]["value"] == (
        "/etc/exomem/release/exomem-hosted-release-v1.json"
    )
    assert {item["name"] for item in worker_container["volumeMounts"]} >= {
        "hosted-release",
        "temporary",
    }
    assert (
        next(volume for volume in worker_spec["volumes"] if volume["name"] == "hosted-release")[
            "configMap"
        ]["name"]
        == "exomem-hosted-release-v1"
    )
    provisioner_role = _find(documents, "ClusterRole", "exomem-cell-provisioner")
    provisioner_configmaps = next(
        rule
        for rule in provisioner_role["rules"]
        if rule.get("apiGroups") == [""] and "configmaps" in rule.get("resources", [])
    )
    assert set(provisioner_configmaps["verbs"]) == {
        "create", "delete", "get", "list", "patch", "update", "watch"
    }
    volume_attachment_rule = next(
        rule
        for rule in provisioner_role["rules"]
        if rule.get("apiGroups") == ["storage.k8s.io"]
        and rule.get("resources") == ["volumeattachments"]
    )
    assert volume_attachment_rule["verbs"] == ["get", "list", "watch"]
    assert not any(
        resource in {"persistentvolumes", "pods", "pods/exec"}
        for rule in provisioner_role["rules"]
        for resource in rule.get("resources", [])
    )
    _find(documents, "ClusterRoleBinding", "exomem-cell-provisioner")

    route = _find(documents, "IngressRoute", "exomem-provisioner-control")
    assert route["metadata"]["namespace"] == "exomem-platform"
    assert route["spec"]["entryPoints"] == ["web"]
    assert len(route["spec"]["routes"]) == 1
    rule = route["spec"]["routes"][0]
    assert "Host(`control.example.test`)" in rule["match"]
    assert "transfer.example.test" not in rule["match"]
    assert "PathPrefix" not in rule["match"]
    actions = json.loads(
        (ROOT / "infra/contracts/platform-composition-v1.json").read_text(encoding="utf-8")
    )["provisioner"]["actions"]
    for action in actions:
        assert f"Path(`/cells/{action}`)" in rule["match"]
    assert rule["services"] == [{"name": "exomem-provisioner", "port": 8080}]


def test_platform_renders_live_capacity_receipt_collector_with_isolated_keys() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    runtime = _find(documents, "ConfigMap", "exomem-operational-receipt-collector")
    assert runtime["data"]["private-alpha-capacity-v1.json"] == (
        ROOT / "infra/operations/private-alpha-capacity-v1.json"
    ).read_text(encoding="utf-8")
    assert (
        "exomem.capacity-live-receipt.v1\\0" in runtime["data"]["operational_receipt_collector.py"]
    )

    state = _find(documents, "ConfigMap", "exomem-capacity-receipt")
    assert state["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert json.loads(state["data"]["state.json"]) == {
        "schema_version": 1,
        "last_sequence": 0,
    }

    collector = _find(documents, "CronJob", "exomem-capacity-receipt-collector")
    assert collector["spec"]["schedule"] == "* * * * *"
    assert collector["spec"]["concurrencyPolicy"] == "Forbid"
    pod = collector["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "exomem-capacity-receipt-collector"
    container = pod["containers"][0]
    assert container["command"] == [
        "python",
        "/opt/exomem-hosted/operational_receipt_collector.py",
        "capacity",
    ]
    environment = {item["name"]: item for item in container["env"]}
    assert environment["EXOMEM_CAPACITY_RECEIPT_PRIVATE_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "exomem-capacity-receipt-signer",
        "key": "private-key",
    }
    assert environment["EXOMEM_HCLOUD_CAPACITY_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "exomem-hcloud-capacity-reader",
        "key": "token",
    }
    assert environment["EXOMEM_ALERT_WEBHOOK_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "exomem-hosted-alert-delivery",
        "key": "url",
    }
    projected = next(volume for volume in pod["volumes"] if volume["name"] == "kube-api")
    token_projection = projected["projected"]["sources"][0]["serviceAccountToken"]
    assert token_projection["audience"] == "https://kubernetes.default.svc.cluster.local"
    assert token_projection["expirationSeconds"] == 600

    role = _find(documents, "ClusterRole", "exomem-capacity-receipt-collector")
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["namespaces"],
            "verbs": ["get", "list"],
        }
    ]
    namespaced_role = _find(documents, "Role", "exomem-capacity-receipt-collector")
    assert namespaced_role["rules"] == [
        {
            "apiGroups": [""],
            "resourceNames": ["exomem-capacity-receipt"],
            "resources": ["configmaps"],
            "verbs": ["get", "patch"],
        }
    ]

    rendered = json.dumps(documents)
    api = _find(documents, "Deployment", "exomem-provisioner-api")
    worker = _find(documents, "Deployment", "exomem-provisioner-worker")
    assert "EXOMEM_CAPACITY_RECEIPT_PRIVATE_KEY" not in json.dumps([api, worker])
    assert rendered.count("exomem-capacity-receipt-signer") == 1


def test_platform_renders_disjoint_durability_workloads() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    storage = _find(documents, "ConfigMap", "exomem-durability-storage")
    assert storage["data"] == {
        "s3-endpoint": "https://s3.eu-central-003.backblazeb2.com",
        "s3-region": "eu-central-003",
        "recovery-bucket": "recovery-example",
        "user-export-bucket": "user-export-example",
        "database-backup-bucket": "database-backup-example",
    }
    contract = json.loads(
        _find(documents, "ConfigMap", "exomem-durability-workload-contract")["data"][
            "durability-workloads-v1.json"
        ]
    )
    assert contract["schemaVersion"] == 1

    expected = {
        "exomem-export-gc": ("CronJob", ["exomem-export-gc"], "*/5 * * * *", False),
        "exomem-durability-backup": (
            "CronJob",
            ["exomem-durability-backup-worker"],
            "*/30 * * * *",
            True,
        ),
        "exomem-database-backup": (
            "CronJob",
            ["exomem-database-backup-worker"],
            "*/30 * * * *",
            False,
        ),
        "exomem-deletion-dispatcher": (
            "CronJob",
            ["exomem-deletion-dispatcher"],
            "* * * * *",
            True,
        ),
        "exomem-volume-worker": (
            "Deployment",
            ["exomem-volume-worker"],
            None,
            True,
        ),
    }

    def pod_spec(document: dict) -> dict:
        if document["kind"] == "CronJob":
            return document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        return document["spec"]["template"]["spec"]

    def secret_refs(pod: dict) -> set[str]:
        refs = {
            f"{item['valueFrom']['secretKeyRef']['name']}/{item['valueFrom']['secretKeyRef']['key']}"
            for container in pod.get("containers", [])
            for item in container.get("env", [])
            if "valueFrom" in item and "secretKeyRef" in item["valueFrom"]
        }
        refs.update(
            f"{source['secret']['name']}/{item['key']}"
            for volume in pod.get("volumes", [])
            for source in volume.get("projected", {}).get("sources", [])
            if "secret" in source
            for item in source["secret"].get("items", [])
        )
        return refs

    expected_secrets = {
        "exomem-export-gc": {
            "exomem-provisioner-database/url",
            "exomem-provisioner-wrapping-key/key-material",
            "exomem-user-export-delete-key-id/application-key-id",
            "exomem-user-export-delete-key/application-key",
        },
        "exomem-durability-backup": {
            "exomem-provisioner-database/url",
            "exomem-provisioner-wrapping-key/key-material",
            "exomem-provider-recovery-signer/private-key",
            "exomem-recovery-upload-key-id/application-key-id",
            "exomem-recovery-upload-key/application-key",
        },
        "exomem-database-backup": {
            "exomem-provisioner-database/url",
            "exomem-provisioner-wrapping-key/key-material",
            "exomem-provider-recovery-signer/private-key",
            "exomem-database-backup-upload-key-id/application-key-id",
            "exomem-database-backup-upload-key/application-key",
            "exomem-database-backup-pg-service/pg_service.conf",
            "exomem-database-backup-pgpass/pgpass",
        },
        "exomem-deletion-dispatcher": {
            "exomem-provisioner-database/url",
        },
        "exomem-volume-worker": {
            "exomem-provisioner-auth/credential",
            "exomem-provisioner-database/url",
            "exomem-provisioner-wrapping-key/key-material",
            "exomem-provider-recovery-signer/private-key",
            "exomem-provisioner-hcloud-token/token",
        },
    }
    for name, (kind, command, schedule, token) in expected.items():
        workload = _find(documents, kind, name)
        if kind == "CronJob":
            assert workload["spec"]["schedule"] == schedule
            assert workload["spec"]["concurrencyPolicy"] == "Forbid"
        pod = pod_spec(workload)
        assert pod["automountServiceAccountToken"] is token
        assert pod["containers"][0]["command"] == command
        assert secret_refs(pod) == expected_secrets[name]
        assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"

    database_pod = pod_spec(_find(documents, "CronJob", "exomem-database-backup"))
    assert database_pod["initContainers"][0]["name"] == "prepare-pg-files"
    database_env = {
        item["name"]: item.get("value")
        for item in database_pod["containers"][0]["env"]
        if "value" in item
    }
    assert database_env["EXOMEM_DATABASE_BACKUP_PG_SERVICE_FILE"] == (
        "/run/secrets/exomem/database-backup/pg_service.conf"
    )
    assert database_env["EXOMEM_DATABASE_BACKUP_PGPASS_FILE"] == (
        "/run/secrets/exomem/database-backup/.pgpass"
    )
    assert database_env["EXOMEM_DATABASE_BACKUP_PG_DUMP"] == "/usr/bin/pg_dump"
    assert database_env["EXOMEM_DATABASE_BACKUP_PROOF_TENANT_ID"] == "tenant-owner-proof"
    assert database_env["EXOMEM_DATABASE_BACKUP_PROOF_CELL_ID"] == "cell-owner-proof"

    volume_pod = pod_spec(_find(documents, "Deployment", "exomem-volume-worker"))
    volume_env = {
        item["name"]: item.get("value")
        for item in volume_pod["containers"][0]["env"]
        if "value" in item
    }
    assert volume_env["EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS"] == "10.0.0.0/8"
    assert volume_env["EXOMEM_PROVISIONER_VOLUME_ENCRYPTION_SECRET_NAME"] == (
        "exomem-volume-encryption"
    )
    assert volume_env["EXOMEM_PROVISIONER_VOLUME_ENCRYPTION_SECRET_NAMESPACE"] == (
        "exomem-platform"
    )

    deletion_job = json.loads(
        _find(documents, "ConfigMap", "exomem-deletion-job-template")["data"][
            "job-template.json"
        ]
    )
    assert not any(
        item.get("kind") == "Deployment"
        and item.get("metadata", {}).get("name") == "exomem-deletion-worker"
        for item in documents
    )
    assert deletion_job["kind"] == "Job"
    assert deletion_job["metadata"]["generateName"] == "exomem-deletion-"
    assert deletion_job["spec"]["backoffLimit"] == 0
    assert deletion_job["spec"]["ttlSecondsAfterFinished"] == 300
    deletion_pod = deletion_job["spec"]["template"]["spec"]
    deletion_env_names = {item["name"] for item in deletion_pod["containers"][0]["env"]}
    assert {
        "EXOMEM_PROVISIONER_HCLOUD_TOKEN",
        "EXOMEM_PROVISIONER_B2_ENDPOINT_URL",
        "EXOMEM_PROVISIONER_B2_REGION",
        "EXOMEM_PROVISIONER_RECOVERY_BUCKET",
        "EXOMEM_PROVISIONER_USER_EXPORT_BUCKET",
        "EXOMEM_PROVISIONER_RECOVERY_DELETE_KEY_ID",
        "EXOMEM_PROVISIONER_RECOVERY_DELETE_KEY",
        "EXOMEM_PROVISIONER_USER_EXPORT_DELETE_KEY_ID",
        "EXOMEM_PROVISIONER_USER_EXPORT_DELETE_KEY",
        "EXOMEM_PROVISIONER_WORKER_ID",
    } <= deletion_env_names
    assert (
        not {
            "EXOMEM_DURABILITY_RECOVERY_BUCKET",
            "EXOMEM_DURABILITY_USER_EXPORT_BUCKET",
            "EXOMEM_DURABILITY_DATABASE_BACKUP_BUCKET",
            "EXOMEM_PROVISIONER_DATABASE_BACKUP_BUCKET",
            "EXOMEM_DURABILITY_DATABASE_BACKUP_DELETE_KEY_ID",
            "EXOMEM_DURABILITY_DATABASE_BACKUP_DELETE_KEY",
            "EXOMEM_PROVISIONER_DATABASE_BACKUP_DELETE_KEY_ID",
            "EXOMEM_PROVISIONER_DATABASE_BACKUP_DELETE_KEY",
            "EXOMEM_DELETION_WORKER_ID",
        }
        & deletion_env_names
    )

    backup_role = _find(documents, "ClusterRole", "exomem-durability-backup")
    assert backup_role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["namespaces"],
            "verbs": ["get", "list", "watch"],
        },
        {
            "apiGroups": ["apps"],
            "resources": ["statefulsets"],
            "verbs": ["get"],
        },
        {
            "apiGroups": ["coordination.k8s.io"],
            "resources": ["leases"],
            "verbs": ["create", "delete", "get", "patch", "update"],
        },
        {
            "apiGroups": ["traefik.io"],
            "resources": ["ingressroutes"],
            "verbs": ["get", "list", "patch"],
        },
    ]
    deletion_role = _find(documents, "ClusterRole", "exomem-deletion-worker")
    assert deletion_role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["namespaces"],
            "verbs": ["delete", "get", "list", "patch", "watch"],
        },
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["delete"]},
        {
            "apiGroups": [""],
            "resources": ["persistentvolumes"],
            "verbs": ["delete", "get", "list"],
        },
        {"apiGroups": ["apps"], "resources": ["statefulsets"], "verbs": ["get"]},
        {
            "apiGroups": ["traefik.io"],
            "resources": ["ingressroutes"],
            "verbs": ["delete", "get", "list"],
        },
    ]
    volume_role = _find(documents, "ClusterRole", "exomem-volume-worker")
    assert not any("secrets" in rule.get("resources", []) for rule in volume_role["rules"])
    namespace_rule = next(
        rule for rule in volume_role["rules"] if "namespaces" in rule.get("resources", [])
    )
    assert namespace_rule["resources"] == ["namespaces"]
    assert namespace_rule["verbs"] == ["get", "list", "watch"]

    for policy_name, service_account in (
        ("exomem-provisioner-scope", "exomem-cell-provisioner"),
        ("exomem-durability-backup-scope", "exomem-durability-backup"),
        ("exomem-deletion-worker-scope", "exomem-deletion-worker"),
        ("exomem-volume-worker-scope", "exomem-volume-worker"),
    ):
        policy = _find(documents, "ValidatingAdmissionPolicy", policy_name)
        rendered_policy = json.dumps(policy)
        assert f"system:serviceaccount:exomem-platform:{service_account}" in rendered_policy
        assert "request.namespace.startsWith('exo-')" in rendered_policy
        if policy_name not in {
            "exomem-provisioner-scope",
            "exomem-volume-worker-scope",
        }:
            assert "exomem-platform" not in " ".join(
                validation["expression"] for validation in policy["spec"]["validations"]
            )
        _find(documents, "ValidatingAdmissionPolicyBinding", policy_name)
    provisioner_scope = _find(
        documents, "ValidatingAdmissionPolicy", "exomem-provisioner-scope"
    )
    provisioner_scope_text = json.dumps(provisioner_scope)
    for required_guard in (
        "helmRelease",
        "variables.labels['owner'] == 'helm'",
        "variables.labels['name'] == request.namespace",
        "variables.target.data['release'].size() <= 1048576",
        "exomem.io/approved-image",
        "exomem-cell-credentials",
    ):
        assert required_guard in provisioner_scope_text
    volume_scope = _find(documents, "ValidatingAdmissionPolicy", "exomem-volume-worker-scope")
    rendered_volume_scope = json.dumps(volume_scope)
    for required_guard in (
        "exomem.io/recovery-envelope",
        "exomem.io/tenant-id",
        "exomem.io/cell-id",
        "exomem.io/operation-id",
        "exomem.io/fence",
        "exomem-hcloud-encrypted-retain",
        "ReadWriteOnce",
        "quantity('10Gi')",
        "Filesystem",
        "nodePublishSecretRef",
        "exomem-volume-encryption",
        "exomem-platform",
        "oldObject.spec == object.spec",
    ):
        assert required_guard in rendered_volume_scope
    backup_scope = _find(documents, "ValidatingAdmissionPolicy", "exomem-durability-backup-scope")
    assert backup_scope["spec"]["matchConstraints"]["resourceRules"] == [
        {
            "apiGroups": ["coordination.k8s.io"],
            "apiVersions": ["*"],
            "operations": ["CREATE", "UPDATE", "DELETE"],
            "resources": ["leases"],
            "scope": "Namespaced",
        },
        {
            "apiGroups": ["traefik.io"],
            "apiVersions": ["*"],
            "operations": ["UPDATE"],
            "resources": ["ingressroutes"],
            "scope": "Namespaced",
        },
    ]
    deletion_scope = _find(documents, "ValidatingAdmissionPolicy", "exomem-deletion-worker-scope")
    rendered_deletion_scope = json.dumps(deletion_scope)
    for required_guard in (
        "exomem.io/recovery-envelope",
        "exomem.io/credentials-secret-name",
        "exomem-cell-credentials",
        "exomem.io/credential-deletion-operation-digest",
        "exomem.io/credential-deletion-fence",
        "9007199254740991",
        "oldObject.metadata.labels == object.metadata.labels",
        "dyn(oldObject).spec == dyn(object).spec",
        "may update only the namespace deletion receipt",
    ):
        assert required_guard in rendered_deletion_scope
    assert "middlewares" not in rendered_deletion_scope


def test_platform_deletion_dispatcher_is_credential_free_and_worker_is_job_only() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    dispatcher = _find(documents, "CronJob", "exomem-deletion-dispatcher")
    dispatcher_pod = dispatcher["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    dispatcher_container = dispatcher_pod["containers"][0]
    dispatcher_secret_refs = {
        f"{item['valueFrom']['secretKeyRef']['name']}/{item['valueFrom']['secretKeyRef']['key']}"
        for item in dispatcher_container["env"]
        if item.get("valueFrom", {}).get("secretKeyRef")
    }

    assert dispatcher_pod["serviceAccountName"] == "exomem-deletion-dispatcher"
    assert dispatcher_container["command"] == ["exomem-deletion-dispatcher"]
    assert dispatcher_secret_refs == {"exomem-provisioner-database/url"}
    assert not any(
        fragment in item["name"]
        for item in dispatcher_container["env"]
        for fragment in ("ENVELOPE", "WRAPPING", "HCLOUD", "B2", "DELETE", "RECOVERY")
    )
    assert not any(
        item.get("kind") == "CronJob"
        and item.get("metadata", {}).get("name") == "exomem-deletion-worker"
        for item in documents
    )

    template_config = _find(documents, "ConfigMap", "exomem-deletion-job-template")
    job = json.loads(template_config["data"]["job-template.json"])
    assert job["kind"] == "Job"
    assert job["metadata"]["generateName"] == "exomem-deletion-"
    worker_pod = job["spec"]["template"]["spec"]
    assert worker_pod["serviceAccountName"] == "exomem-deletion-worker"
    assert worker_pod["containers"][0]["command"] == ["exomem-deletion-worker"]
    worker_env = {item["name"]: item for item in worker_pod["containers"][0]["env"]}
    assert worker_env["EXOMEM_PROVISIONER_WORKER_ID"]["valueFrom"]["fieldRef"] == {
        "fieldPath": "metadata.labels['batch.kubernetes.io/job-name']"
    }
    worker_secret_refs = {
        f"{item['valueFrom']['secretKeyRef']['name']}/{item['valueFrom']['secretKeyRef']['key']}"
        for item in worker_pod["containers"][0]["env"]
        if item.get("valueFrom", {}).get("secretKeyRef")
    }
    assert worker_secret_refs == {
        "exomem-provisioner-database/url",
        "exomem-provisioner-wrapping-key/key-material",
        "exomem-provider-recovery-verifier/public-key",
        "exomem-provisioner-hcloud-token/token",
        "exomem-recovery-delete-key-id/application-key-id",
        "exomem-recovery-delete-key/application-key",
        "exomem-user-export-delete-key-id/application-key-id",
        "exomem-user-export-delete-key/application-key",
    }

    dispatcher_role = _find(documents, "Role", "exomem-deletion-dispatcher")
    assert dispatcher_role["rules"] == [
        {
            "apiGroups": ["batch"],
            "resources": ["jobs"],
            "verbs": ["create", "get", "list", "watch"],
        }
    ]
    admission = _find(
        documents,
        "ValidatingAdmissionPolicy",
        "exomem-deletion-dispatcher-job-scope",
    )
    rendered_admission = json.dumps(admission)
    assert "system:serviceaccount:exomem-platform:exomem-deletion-dispatcher" in rendered_admission
    assert "exomem.io/deletion-job" in rendered_admission
    assert "exomem-deletion-worker" in rendered_admission


def test_deletion_dispatcher_admission_closes_probe_and_container_override_surfaces() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    admission = _find(
        documents,
        "ValidatingAdmissionPolicy",
        "exomem-deletion-dispatcher-job-scope",
    )
    expressions = "\n".join(
        validation["expression"] for validation in admission["spec"]["validations"]
    )
    container = "object.spec.template.spec.containers[0]"

    assert "object.metadata.name.matches('^exomem-deletion-[0-9a-f]{16}$')" in expressions
    assert "!has(object.metadata.generateName)" in expressions
    for field in (
        "args",
        "envFrom",
        "lifecycle",
        "workingDir",
        "stdin",
        "stdinOnce",
        "tty",
        "ports",
        "volumeDevices",
        "startupProbe",
        "livenessProbe",
        "readinessProbe",
    ):
        assert f"!has({container}.{field})" in expressions

    # These are the two concrete mutation regressions from the adversarial review:
    # an exec probe and a per-container privilege/seccomp override are both outside
    # the reviewed Job shape and therefore denied at admission.
    assert f"!has({container}.startupProbe)" in expressions
    assert f"!has({container}.securityContext.privileged)" in expressions
    assert f"!has({container}.securityContext.seccompProfile)" in expressions
    assert "metadata.labels['batch.kubernetes.io/job-name']" in expressions
    assert f"{container}.resources.requests.cpu == quantity('25m')" in expressions
    assert f"{container}.resources.limits.memory == quantity('384Mi')" in expressions


def test_platform_renders_one_shot_durability_actions_and_exact_restore_scope() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    contract = json.loads(
        _find(documents, "ConfigMap", "exomem-durability-workload-contract")["data"][
            "durability-workloads-v1.json"
        ]
    )
    action_contract = contract["workloads"]["durabilityActions"]
    assert action_contract == {
        "kind": "CronJob",
        "command": ["exomem-durability-actions"],
        "serviceAccount": "exomem-durability-actions",
        "schedule": "* * * * *",
        "concurrencyPolicy": "Forbid",
        "startingDeadlineSeconds": 45,
        "activeDeadlineSeconds": 4800,
        "backoffLimit": 0,
        "automountServiceAccountToken": True,
        "maxOperations": 1,
        "scratchSize": "6Gi",
        "providerPermissions": [
            "kubernetes:authenticated-tenant-durability-actions",
            "b2:recovery:restore-read",
            "b2:user-export:upload-list",
            "b2:user-export:restore-read",
            "b2:user-export:delete-list-read-metadata",
            "b2:user-export:delivery-write-read",
        ],
        "privateKey": "exomem-provider-recovery-signer/private-key",
        "publicVerifier": False,
    }

    cronjob = _find(documents, "CronJob", "exomem-durability-actions")
    assert cronjob["spec"]["schedule"] == "* * * * *"
    assert cronjob["spec"]["concurrencyPolicy"] == "Forbid"
    assert cronjob["spec"]["startingDeadlineSeconds"] == 45
    assert cronjob["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 4800
    assert cronjob["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 0
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "exomem-durability-actions"
    assert pod["automountServiceAccountToken"] is True
    assert pod["restartPolicy"] == "Never"
    container = pod["containers"][0]
    assert container["image"] == (
        "ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64
    )
    assert container["command"] == ["exomem-durability-actions"]
    env = {item["name"]: item for item in container["env"]}
    assert env["EXOMEM_DURABILITY_MAX_OPERATIONS"]["value"] == "1"
    assert env["EXOMEM_DURABILITY_SCRATCH_ROOT"]["value"] == "/var/lib/exomem-scratch"
    assert env["EXOMEM_PROVISIONER_RELEASE_MANIFEST_PATH"]["value"] == (
        "/etc/exomem/release/exomem-hosted-release-v1.json"
    )
    assert env["EXOMEM_DURABILITY_PROVISIONER_IMAGE"]["value"] == (
        "ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64
    )
    assert {
        name: env[name]["value"]
        for name in (
            "EXOMEM_PROVISIONER_CELL_CHART_PATH",
            "EXOMEM_PROVISIONER_CELL_CHART_VERSION",
            "EXOMEM_PROVISIONER_HELM_BINARY",
            "EXOMEM_PROVISIONER_HELM_VERSION",
            "EXOMEM_PROVISIONER_CONTROL_HOSTNAME",
            "EXOMEM_PROVISIONER_TRANSFER_HOSTNAME",
            "EXOMEM_PROVISIONER_BROWSER_ORIGIN",
            "EXOMEM_PROVISIONER_LOCATION",
            "EXOMEM_PROVISIONER_INTERNAL_ORIGIN",
        )
    } == {
        "EXOMEM_PROVISIONER_CELL_CHART_PATH": "/opt/exomem/charts/cell",
        "EXOMEM_PROVISIONER_CELL_CHART_VERSION": "0.1.0",
        "EXOMEM_PROVISIONER_HELM_BINARY": "/opt/exomem/bin/helm",
        "EXOMEM_PROVISIONER_HELM_VERSION": "3.19.4",
        "EXOMEM_PROVISIONER_CONTROL_HOSTNAME": "control.example.test",
        "EXOMEM_PROVISIONER_TRANSFER_HOSTNAME": "transfer.example.test",
        "EXOMEM_PROVISIONER_BROWSER_ORIGIN": "https://substratesystems.io",
        "EXOMEM_PROVISIONER_LOCATION": "fsn1",
        "EXOMEM_PROVISIONER_INTERNAL_ORIGIN": (
            "http://{resource}.{namespace}.svc.cluster.local:8765"
        ),
    }
    expected_secret_refs = {
        "EXOMEM_DURABILITY_DATABASE_URL": "exomem-provisioner-database/url",
        "EXOMEM_DURABILITY_ENVELOPE_KEY": "exomem-provisioner-wrapping-key/key-material",
        "EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY": (
            "exomem-provider-recovery-signer/private-key"
        ),
        "EXOMEM_DURABILITY_RECOVERY_RESTORE_KEY_ID": (
            "exomem-recovery-restore-key-id/application-key-id"
        ),
        "EXOMEM_DURABILITY_RECOVERY_RESTORE_KEY": (
            "exomem-recovery-restore-key/application-key"
        ),
        "EXOMEM_DURABILITY_USER_EXPORT_UPLOAD_KEY_ID": (
            "exomem-user-export-upload-key-id/application-key-id"
        ),
        "EXOMEM_DURABILITY_USER_EXPORT_UPLOAD_KEY": (
            "exomem-user-export-upload-key/application-key"
        ),
        "EXOMEM_DURABILITY_USER_EXPORT_RESTORE_KEY_ID": (
            "exomem-user-export-restore-key-id/application-key-id"
        ),
        "EXOMEM_DURABILITY_USER_EXPORT_RESTORE_KEY": (
            "exomem-user-export-restore-key/application-key"
        ),
        "EXOMEM_DURABILITY_USER_EXPORT_DELETE_KEY_ID": (
            "exomem-user-export-delete-key-id/application-key-id"
        ),
        "EXOMEM_DURABILITY_USER_EXPORT_DELETE_KEY": (
            "exomem-user-export-delete-key/application-key"
        ),
        "EXOMEM_DURABILITY_USER_EXPORT_DELIVERY_KEY_ID": (
            "exomem-user-export-delivery-key-id/application-key-id"
        ),
        "EXOMEM_DURABILITY_USER_EXPORT_DELIVERY_KEY": (
            "exomem-user-export-delivery-key/application-key"
        ),
    }
    actual_secret_refs = {}
    for name, item in env.items():
        secret_ref = item.get("valueFrom", {}).get("secretKeyRef")
        if secret_ref:
            actual_secret_refs[name] = f"{secret_ref['name']}/{secret_ref['key']}"
    assert actual_secret_refs == expected_secret_refs
    assert {
        item["name"]
        for item in container["env"]
        if item.get("valueFrom", {}).get("configMapKeyRef")
    } == {
        "EXOMEM_DURABILITY_B2_ENDPOINT_URL",
        "EXOMEM_DURABILITY_B2_REGION",
        "EXOMEM_DURABILITY_RECOVERY_BUCKET",
        "EXOMEM_DURABILITY_USER_EXPORT_BUCKET",
    }
    scratch = next(volume for volume in pod["volumes"] if volume["name"] == "scratch")
    assert scratch["emptyDir"]["sizeLimit"] == "6Gi"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"

    role = _find(documents, "ClusterRole", "exomem-durability-actions")
    permissions = {
        (tuple(rule["apiGroups"]), tuple(rule["resources"])): set(rule["verbs"])
        for rule in role["rules"]
    }
    assert permissions[(('',), ('namespaces',))] == {
        "get", "list", "patch", "update", "watch"
    }
    assert permissions[(('',), ('configmaps',))] == {
        "create", "delete", "get", "list", "patch", "update"
    }
    assert permissions[(('',), ('secrets',))] == {"create", "delete", "get", "list"}
    assert permissions[(('',), ('persistentvolumeclaims',))] == {
        "get", "list", "patch", "update"
    }
    assert permissions[(('',), ('limitranges', 'resourcequotas', 'serviceaccounts'))] == {
        "get", "list", "patch", "update"
    }
    assert permissions[(('',), ('services',))] == {
        "create", "delete", "get", "list", "patch", "update"
    }
    assert permissions[(('',), ('pods',))] == {"delete", "get", "list"}
    assert permissions[(('',), ('pods/log',))] == {"get"}
    assert permissions[(('apps',), ('statefulsets', 'statefulsets/scale'))] == {
        "get",
        "list",
        "patch",
        "update",
        "create",
        "delete",
    }
    assert permissions[(('batch',), ('jobs',))] == {"create", "delete", "get", "list"}
    assert permissions[(('coordination.k8s.io',), ('leases',))] == {
        "create",
        "delete",
        "get",
        "patch",
        "update",
    }
    assert permissions[(('networking.k8s.io',), ('networkpolicies',))] == {
        "create",
        "delete",
        "get",
        "list",
        "patch",
        "update",
    }
    assert permissions[(('traefik.io',), ('ingressroutes', 'middlewares'))] == {
        "create",
        "delete",
        "get",
        "list",
        "patch",
        "update",
    }

    action_scope = _find(documents, "ValidatingAdmissionPolicy", "exomem-durability-actions-scope")
    action_scope_text = json.dumps(action_scope)
    for exact_guard in (
        "system:serviceaccount:exomem-platform:exomem-durability-actions",
        "^restore-[a-f0-9]{20}$",
        "^restore-[a-f0-9]{20}-request$",
        "^restore-[a-f0-9]{20}-source$",
        "^restore-[a-f0-9]{20}-egress$",
        "exomem.io/restore-request-sha256",
        "exomem.io/restore-source-sha256",
        "exomem.io/restore-egress-sha256",
        "exomem.io/restore-job-sha256",
        "restorePod",
        "job-name",
        "ttlSecondsAfterFinished == 300",
        "exomem.io/restore-candidate",
        "exomem-restore-candidate",
        "exomem-restore-fetch",
        "6Gi",
        "512Mi",
        "automountServiceAccountToken",
        "exomem-cell-credentials",
        "statefulset.kubernetes.io/pod-name",
        "statefulsets/scale",
        "ingressroutes",
        "helmRelease",
        "owner",
        "helm",
    ):
        assert exact_guard in action_scope_text
    _find(documents, "ValidatingAdmissionPolicyBinding", "exomem-durability-actions-scope")
    action_namespace_scope = _find(
        documents,
        "ValidatingAdmissionPolicy",
        "exomem-durability-actions-namespace-scope",
    )
    action_namespace_text = json.dumps(action_namespace_scope)
    for exact_guard in (
        "system:serviceaccount:exomem-platform:exomem-durability-actions",
        "exomem.io/resource-name",
        "exomem.io/pvc-name",
        "exomem-cell-credentials",
        "exomem.io/approved-image",
        "oldObject.metadata.name",
    ):
        assert exact_guard in action_namespace_text
    _find(
        documents,
        "ValidatingAdmissionPolicyBinding",
        "exomem-durability-actions-namespace-scope",
    )

    restore_pods = _find(
        documents,
        "ValidatingAdmissionPolicy",
        "exomem-tenant-restore-candidate",
    )
    restore_pod_text = json.dumps(restore_pods)
    for exact_guard in (
        "exomem.io/restore-candidate",
        "fetch-restore-source",
        "exomem-restore-fetch",
        "restore-candidate",
        "--contract-version",
        "/run/exomem/operator-requests/restore-candidate.json",
        "/run/exomem/restore-source/url",
        "/system-scratch/",
        "6Gi",
        "512Mi",
        "RuntimeDefault",
        "automountServiceAccountToken",
    ):
        assert exact_guard in restore_pod_text
    _find(documents, "ValidatingAdmissionPolicyBinding", "exomem-tenant-restore-candidate")

    routine_scope = _find(documents, "ValidatingAdmissionPolicy", "exomem-provisioner-scope")
    routine_scope_text = json.dumps(routine_scope)
    assert "restore-[a-f0-9]" not in routine_scope_text
    assert "exomem.io/restore-candidate" not in routine_scope_text


def test_platform_pins_exact_durability_contracts() -> None:
    values = yaml.safe_load((PLATFORM / "values.yaml").read_text(encoding="utf-8"))
    for name, value_key in (
        ("durability-workloads-v1.json", "contractSha256"),
        ("durability-storage-v1.json", "storageContractSha256"),
    ):
        source = (ROOT / "infra/contracts" / name).read_bytes()
        vendored = (PLATFORM / "files" / name).read_bytes()
        assert vendored == source
        assert values["durability"][value_key] == hashlib.sha256(source).hexdigest()
    workloads = json.loads(
        (ROOT / "infra/contracts/durability-workloads-v1.json").read_text(encoding="utf-8")
    )["workloads"]
    assert workloads["volumeLifecycle"]["privateKey"] == workloads["vaultBackup"]["privateKey"]
    assert workloads["deliveryGc"]["publicVerifier"] is False
    assert workloads["vaultBackup"]["publicVerifier"] is False
    assert workloads["volumeLifecycle"]["publicVerifier"] is False
    assert workloads["deletion"]["privateKey"] is None
    assert workloads["deletion"]["publicVerifier"] is True
    assert all(
        "database-backup" not in permission
        for permission in workloads["deletion"]["providerPermissions"]
    )
    bindings = json.loads(
        (ROOT / "infra/contracts/durability-workloads-v1.json").read_text(encoding="utf-8")
    )["secretBindings"]
    assert not any("databaseBackupDelete" in name for name in bindings)
    storage = json.loads(
        (ROOT / "infra/contracts/durability-storage-v1.json").read_text(encoding="utf-8")
    )
    assert {
        name: binding["workerEnvironmentVariable"] for name, binding in storage["bindings"].items()
    } == {
        "recovery_bucket_name": "EXOMEM_DURABILITY_RECOVERY_BUCKET",
        "user_export_bucket_name": "EXOMEM_DURABILITY_USER_EXPORT_BUCKET",
        "database_backup_bucket_name": "EXOMEM_DURABILITY_DATABASE_BACKUP_BUCKET",
    }

def test_runtime_k3s_gate_pins_the_reviewed_release_unit() -> None:
    gate = json.loads(RUNTIME_GATE.read_text(encoding="utf-8"))
    assert gate == {
        "artifact": "exomem-hosted-runtime-k3s-gate",
        "schemaVersion": 1,
        "k3sImage": (
            "rancher/k3s@sha256:"
            "9d6b9c15e8031c1aea7dd7f0cdc019f5e74a23c53b9eada564b7a8dc94efc14c"
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

    runtime_class = _find(documents, "RuntimeClass", "exomem-storage-init")
    assert runtime_class["handler"] == "runc"
    tenant_admission = _find(documents, "ValidatingAdmissionPolicy", "exomem-tenant-boundary")
    assert "paramKind" not in tenant_admission["spec"]
    tenant_binding = _find(documents, "ValidatingAdmissionPolicyBinding", "exomem-tenant-boundary")
    assert "matchResources" not in tenant_binding["spec"]
    assert "matchConditions" not in tenant_admission["spec"]
    variables = tenant_admission["spec"]["variables"]
    assert [variable["name"] for variable in variables] == [
        "storageInit",
        "tenantNamespace",
        "restoreCandidate",
        "inScope",
        "controllerUpdate",
        "controllerJobFinalizerRemoval",
        "controllerJobFinalizerTransition",
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
    assert "has(dyn(volume.emptyDir).sizeLimit)" not in admission_text
    assert "request.operation == 'UPDATE'" in admission_text
    assert "object.spec.nodeName == oldObject.spec.nodeName" in admission_text
    assert "object.spec == oldObject.spec" in admission_text
    assert (
        "object.spec.containers[0].args == ['hosted', 'init', '--contract-version', '1', "
        "'--request-file', '/run/exomem/operator-requests/init.json']"
    ) in admission_text
    assert "size(object.spec.volumes) == 2" in admission_text
    assert "size(object.spec.containers[0].volumeMounts) == 4" in admission_text
    assert "seccompProfile.type == 'RuntimeDefault'" in admission_text
    assert "securityContext.seccompProfile" in admission_text
    assert "terminationMessagePath == '/dev/termination-log'" in admission_text
    assert "terminationMessagePolicy == 'File'" in admission_text
    assert (
        "request.userInfo.username == 'system:serviceaccount:kube-system:job-controller'"
        in admission_text
    )
    assert "batch.kubernetes.io/job-tracking" in admission_text
    assert "exact approved serving command and environment" in admission_text
    assert "exact approved serving ports, probes, and interactive surface" in admission_text
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
    assert "system:serviceaccount:exomem-platform:exomem-cell-provisioner" in namespace_policy_text
    assert "system:serviceaccount:exomem-platform:exomem-durability-actions" in namespace_policy_text
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
        "exomem.io/cell-id",
        "exomem.io/vault-id",
        "exomem.io/expected-release",
        "exomem.io/worker-policy-digest",
        "exomem.io/browser-origin",
        "exomem.io/transfer-hostname",
    ):
        assert protected_field in namespace_policy_text

    provisioner_scope = _find(
        documents, "ValidatingAdmissionPolicy", "exomem-provisioner-scope"
    )
    provisioner_scope_text = json.dumps(provisioner_scope)
    for exact_guard in (
        "request.namespace",
        "exomem.io/cell",
        "persistentvolumeclaims",
        "statefulsets",
        "networkpolicies",
        "ingressroutes",
        "middlewares",
        "ClusterIP",
        "8765",
        "default-deny",
        "traefik-ingress",
        "strip-cell",
        "control",
        "transfer",
    ):
        assert exact_guard in provisioner_scope_text
    assert "NetworkPolicy deletion is reserved for namespace destruction" in provisioner_scope_text

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    cronjobs = {
        document["metadata"]["name"]: document
        for document in documents
        if document.get("kind") == "CronJob"
        and document.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/part-of")
        == "exomem-hosted-scheduler"
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
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["TARGET_URL"] == contract["origin"] + job["path"]
        assert env["CONNECT_TIMEOUT_SECONDS"] == "5"
        assert env["TOTAL_TIMEOUT_SECONDS"] == "20"
        assert env["CADENCE_SECONDS"] == ("60" if job["schedule"] == "* * * * *" else "3600")
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
    assert contract["alerts"]["scheduler_missed_run_seconds"] == 180
    assert contract["alerts"]["scheduler_consecutive_failures"] == 2
    assert contract["poll_interval_seconds"] == 300
    scheduler_check = next(
        check for check in contract["checks"] if check["name"] == "scheduler-last-success"
    )
    assert scheduler_check["maximum_age_seconds"] == 480
    assert contract["alerts"]["backup_warn_age_seconds"] == 2700
    assert contract["alerts"]["backup_block_age_seconds"] == 3600

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
    assert evaluator_env["MISSED_RUN_SECONDS"] == "180"
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

    workload = _find(
        documents,
        expected_kind,
        "cell-alpha" if expected_kind == "StatefulSet" else "cell-alpha-init",
    )
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
        "exomem.io/tenant-id": "tenant-alpha",
        "exomem.io/cell-id": "alpha-test-original",
        "exomem.io/operation-id": "operation-alpha",
        "exomem.io/tenant-digest": "a" * 64,
        "exomem.io/subject-digest": "b" * 64,
        "exomem.io/operation-digest": "c" * 64,
        "exomem.io/fence": "7",
        "exomem.io/recovery-envelope": "a" * 64,
        "exomem.io/resource-name": "cell-alpha",
        "exomem.io/pvc-name": "cell-alpha-data",
        "exomem.io/credentials-secret-name": "exomem-cell-credentials",
        "exomem.io/init-request-configmap-name": "cell-alpha-init-request",
        "exomem.io/provision-mode": "serve",
        "exomem.io/vault-id": "vault-alpha-original",
        "exomem.io/expected-release": "0.1.0-alpha",
        "exomem.io/worker-policy-digest": "b" * 64,
        "exomem.io/browser-origin": "https://substratesystems.io",
        "exomem.io/transfer-hostname": "transfer.example.test",
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
            "--contract-version",
            "1",
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
        assert env["TMPDIR"] == "/var/lib/exomem/state/tmp/runtime"
        assert {volume["name"] for volume in pod["volumes"]} == {"data", "credentials"}
        assert {mount["mountPath"] for mount in container["volumeMounts"]} == {
            "/var/lib/exomem/vault",
            "/var/lib/exomem/state",
            "/var/lib/exomem/logs",
            "/run/exomem/credentials",
        }
        assert container["resources"]["limits"]["ephemeral-storage"] == "512Mi"

        ingress = _find(documents, "NetworkPolicy", "cell-alpha-traefik-ingress")
        assert ingress["spec"]["policyTypes"] == ["Ingress"]
        assert "egress" not in ingress["spec"]
        assert ingress["spec"]["ingress"] == [
            {
                "from": [{
                    "namespaceSelector": {"matchLabels": {
                        "kubernetes.io/metadata.name": "exomem-platform"
                    }},
                    "podSelector": {"matchLabels": {
                        "app.kubernetes.io/name": "traefik",
                        "exomem.io/ingress": "traefik",
                    }},
                }],
                "ports": [{"protocol": "TCP", "port": 8765}],
            },
            {
                "from": [{
                    "namespaceSelector": {"matchLabels": {
                        "kubernetes.io/metadata.name": "exomem-platform"
                    }},
                    "podSelector": {"matchLabels": {
                        "app.kubernetes.io/name": "exomem-durability-actions"
                    }},
                }],
                "ports": [{"protocol": "TCP", "port": 8765}],
            },
        ]

        credentials = next(volume for volume in pod["volumes"] if volume["name"] == "credentials")
        assert credentials["secret"]["defaultMode"] == 0o444
        assert credentials["secret"]["secretName"] == "exomem-cell-credentials"
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
    assert schema["properties"]["workloadMode"]["enum"] == ["initialize", "restore", "serve"]
    assert schema["properties"]["provisionMode"]["enum"] == ["serve", "restore-candidate"]
    assert '"transferHostname"' not in json.dumps(schema["properties"]["routes"])


def test_cell_chart_restore_mode_is_an_empty_offline_storage_shell() -> None:
    documents = _render(
        CELL,
        CELL / "values.validation.yaml",
        namespace="cell-alpha-test",
        extra_args=(
            "--set",
            "workloadMode=restore",
            "--set",
            "provisionMode=restore-candidate",
        ),
    )

    assert (
        _find(documents, "Namespace", "cell-alpha-test")["metadata"]["annotations"][
            "exomem.io/provision-mode"
        ]
        == "restore-candidate"
    )
    assert _find(documents, "PersistentVolumeClaim", "cell-alpha-data")
    assert _find(documents, "ResourceQuota", "cell-alpha-quota")
    assert _find(documents, "LimitRange", "cell-alpha-limits")
    assert _find(documents, "ServiceAccount", "cell-alpha")
    policies = [document for document in documents if document.get("kind") == "NetworkPolicy"]
    assert [policy["metadata"]["name"] for policy in policies] == ["cell-alpha-default-deny"]
    forbidden = {
        "ConfigMap",
        "Job",
        "StatefulSet",
        "Service",
        "Middleware",
        "IngressRoute",
    }
    assert not [document for document in documents if document.get("kind") in forbidden]


def test_cell_chart_rejects_mismatched_runtime_and_provider_cell_ids(tmp_path: Path) -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    override = tmp_path / "mismatched-cell-id.yaml"
    override.write_text(
        yaml.safe_dump({"providerIdentity": {"cellId": "different-cell"}}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            str(HELM),
            "template",
            "contract-test",
            str(CELL),
            "--namespace",
            "cell-alpha-test",
            "--values",
            str(CELL / "values.validation.yaml"),
            "--values",
            str(override),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "providerIdentity.cellId must equal cellId" in result.stderr


def test_cell_routes_expose_only_exact_control_and_transfer_paths() -> None:
    documents = _render(
        CELL,
        CELL / "values.validation.yaml",
        namespace="cell-alpha-test",
        extra_args=("--set", "routes.enabled=true"),
    )
    middleware = _find(documents, "Middleware", "cell-alpha-strip-cell")
    assert middleware["spec"]["stripPrefix"]["prefixes"] == ["/cells/alpha-test-original"]

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
    assert route["spec"]["routes"][0]["match"].startswith("Host(`files.example.test`)")


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
    assert cloudflare.count(target) == 2
