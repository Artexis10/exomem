from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
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


def test_storage_init_env_contract_allows_only_the_two_exact_operator_forms() -> None:
    """Keep the offline migration exception narrower than the serving env contract."""
    text = (PLATFORM / "templates" / "tenant-admission.yaml").read_text(encoding="utf-8")

    # One normal init environment entry, or the ordered two-entry offline migration
    # form.  The value/valueFrom checks make a secret/config-map based lookalike
    # and an extra/misordered environment entry fail the CEL expression.
    assert "size(object.spec.containers[0].env) == 1" in text
    assert "size(object.spec.containers[0].env) == 2" in text
    assert "object.spec.containers[0].env[0].name == 'EXOMEM_LOG_DIR'" in text
    assert "object.spec.containers[0].env[0].value == '/dev'" in text
    assert "!has(object.spec.containers[0].env[0].valueFrom)" in text
    assert "object.spec.containers[0].env[1].name == 'EXOMEM_HOSTED_OFFLINE_STATE_MIGRATION'" in text
    assert "object.spec.containers[0].env[1].value == '1'" in text
    assert "!has(object.spec.containers[0].env[1].valueFrom)" in text


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
    lock = json.loads(validation_values["provisioner"]["deploymentLockJson"])
    assert lock["components"]["runtime"]["image"] == "ghcr.io/artexis10/exomem@sha256:" + "a" * 64
    assert lock["components"]["provisioner"]["image"] == (
        "ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64
    )
    platform_schema = json.loads((PLATFORM / "values.schema.json").read_text(encoding="utf-8"))
    assert platform_schema["properties"]["runtime"] is False
    required = platform_schema["properties"]["provisioner"]["required"]
    assert "deploymentLockJson" in required
    assert "deploymentLockSha256" in required
    assert "image" not in platform_schema["properties"]["provisioner"]["properties"]
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


def test_platform_rejects_mutable_or_partial_deployment_lock_overrides(
    tmp_path: Path,
) -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    validation_values = yaml.safe_load(
        (PLATFORM / "values.validation.yaml").read_text(encoding="utf-8")
    )
    lock = json.loads(validation_values["provisioner"]["deploymentLockJson"])
    mutable = json.loads(json.dumps(lock))
    mutable["components"]["runtime"]["image"] = "ghcr.io/artexis10/exomem:latest"
    partial = dict(lock)
    partial.pop("runtimeTarget")
    for index, invalid in enumerate((mutable, partial), start=1):
        override = tmp_path / f"invalid-lock-{index}.yaml"
        override.write_text(
            yaml.safe_dump(
                {
                    "provisioner": {
                        "deploymentLockJson": json.dumps(invalid, separators=(",", ":")) + "\n",
                        "deploymentLockSha256": hashlib.sha256(
                            (json.dumps(invalid, separators=(",", ":")) + "\n").encode()
                        ).hexdigest(),
                    }
                }
            ),
            encoding="utf-8",
        )
        result = _render_process(
            PLATFORM,
            PLATFORM / "values.validation.yaml",
            namespace="exomem-platform",
            release_name="exomem-platform",
            extra_args=("--values", str(override)),
        )
        assert result.returncode != 0
        assert "deployment lock" in result.stderr


def test_platform_requires_cross_repository_trust_in_runtime_upgrade_metadata(
    tmp_path: Path,
) -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    values = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))
    lock = json.loads(values["provisioner"]["deploymentLockJson"])
    lock["runtimeUpgrade"] = {
        "compatibilityDigest": "c" * 64,
        "migrationMode": "none",
        "substrateConsumerCommit": "d" * 40,
        "substrateTrustSha256": "e" * 64,
    }
    accepted = _lock_override(tmp_path / "trusted", lock)
    _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=("--values", str(accepted)),
    )

    del lock["runtimeUpgrade"]["substrateTrustSha256"]
    rejected = _lock_override(tmp_path / "untrusted", lock)
    result = _render_process(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        release_name="exomem-platform",
        extra_args=("--values", str(rejected)),
    )
    assert result.returncode != 0
    assert "deployment lock" in result.stderr
    assert "runtime upgrade is invalid" in result.stderr


def test_platform_accepts_an_authoritatively_empty_legacy_catalog(tmp_path: Path) -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    values = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))
    lock = json.loads(values["provisioner"]["deploymentLockJson"])
    lock["composition"]["legacyCatalog"] = []
    lock["composition"]["legacyReleaseSetSha256"] = hashlib.sha256(b"[]\n").hexdigest()
    override = _lock_override(tmp_path, lock)

    _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=("--values", str(override)),
    )


def test_platform_rejects_deployment_lock_hash_drift(
    tmp_path: Path,
) -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    override = tmp_path / "lock-hash-drift.yaml"
    override.write_text(
        yaml.safe_dump({"provisioner": {"deploymentLockSha256": "0" * 64}}),
        encoding="utf-8",
    )
    result = _render_process(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        release_name="exomem-platform",
        extra_args=("--values", str(override)),
    )
    assert result.returncode != 0
    assert "deployment lock SHA-256 mismatch" in result.stderr


def _v3_lock(lock: dict[str, object]) -> dict[str, object]:
    v3 = json.loads(json.dumps(lock))
    v3["schemaVersion"] = 3
    v3["runtimeTarget"]["agentProfile"] = "hosted-alpha-agent-v2"
    v3["recordsCompatibility"] = {
        "minimum_records_reader_version": 2,
        "activeProfile": "hosted-alpha-agent-v2",
        "activeLifecycleActionsEnabled": True,
        "rollbackProfile": "hosted-alpha-agent-v1",
        "rollbackLifecycleActionsEnabled": False,
        "rollbackRuntime": {
            "image": "ghcr.io/artexis10/exomem@sha256:" + "c" * 64,
            "sourceCommit": "d" * 40,
            "candidateSha256": "e" * 64,
            "recordsReaderVersion": 2,
            "readerStatusProof": {
                "profile": "hosted-alpha-agent-v1",
                "recordsReaderVersion": 2,
                "lifecycleActionsEnabled": False,
                "issuedAt": "2026-08-12T10:00:00Z",
                "expiresAt": "2026-08-12T11:00:00Z",
                "signerWorkflow": "Artexis10/exomem/.github/workflows/release-please.yml",
                "signerWorkflowDigest": "d" * 40,
            },
            "runtimeTarget": {
                "releaseVersion": "0.35.0",
                "protocolVersion": "1",
                "agentProfile": "hosted-alpha-agent-v1",
                "gatewayContractDigest": "6" * 64,
                "commandFingerprint": "7" * 64,
                "schemaDigest": "8" * 64,
            },
        },
    }
    return v3


def _lock_override(tmp_path: Path, lock: dict[str, object]) -> Path:
    raw = json.dumps(lock, separators=(",", ":")) + "\n"
    tmp_path.mkdir(parents=True, exist_ok=True)
    override = tmp_path / "lock.yaml"
    override.write_text(
        yaml.safe_dump(
            {
                "provisioner": {
                    "deploymentLockJson": raw,
                    "deploymentLockSha256": hashlib.sha256(raw.encode()).hexdigest(),
                    **({"runtimeSelection": "active"} if lock["schemaVersion"] == 3 else {}),
                }
            }
        ),
        encoding="utf-8",
    )
    return override


@pytest.mark.parametrize(
    "mutation",
    (
        "provisioner-surplus",
        "runtime-target-missing",
        "closure-missing-paths",
        "closure-surplus",
        "legacy-contract-surplus",
        "rollback-missing",
        "rollback-surplus",
        "rollback-target-missing",
        "rollback-target-surplus",
        "records-proof-surplus",
    ),
)
def test_platform_fully_validates_v2_inherited_and_v3_records_lock_shapes(
    tmp_path: Path, mutation: str
) -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    values = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))
    lock = _v3_lock(json.loads(values["provisioner"]["deploymentLockJson"]))
    valid = _render_process(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=("--values", str(_lock_override(tmp_path, lock))),
    )
    assert valid.returncode == 0, valid.stdout + valid.stderr

    invalid = json.loads(json.dumps(lock))
    if mutation == "provisioner-surplus":
        invalid["components"]["provisioner"]["surplus"] = True
    elif mutation == "runtime-target-missing":
        invalid["runtimeTarget"].pop("schemaDigest")
    elif mutation == "closure-missing-paths":
        invalid["composition"]["sourceClosure"]["runtime"].pop("paths")
    elif mutation == "closure-surplus":
        invalid["composition"]["sourceClosure"]["provisioner"]["surplus"] = True
    elif mutation == "legacy-contract-surplus":
        invalid["composition"]["legacyCatalog"][0]["contract"]["surplus"] = True
    elif mutation == "rollback-missing":
        invalid["rollback"].pop("v1CorpusSha256")
    elif mutation == "rollback-surplus":
        invalid["rollback"]["surplus"] = True
    elif mutation == "rollback-target-missing":
        invalid["recordsCompatibility"]["rollbackRuntime"]["runtimeTarget"].pop("schemaDigest")
    elif mutation == "rollback-target-surplus":
        invalid["recordsCompatibility"]["rollbackRuntime"]["runtimeTarget"]["surplus"] = True
    else:
        invalid["recordsCompatibility"]["rollbackRuntime"]["readerStatusProof"]["surplus"] = True

    result = _render_process(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=("--values", str(_lock_override(tmp_path, invalid))),
    )
    assert result.returncode != 0


def test_platform_accepts_runtime_source_closure_at_the_signed_candidate_anchor(
    tmp_path: Path,
) -> None:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    values = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))
    lock = json.loads(values["provisioner"]["deploymentLockJson"])
    runtime_commit = "f" * 40
    lock["components"]["runtime"]["sourceCommit"] = runtime_commit
    lock["composition"]["sourceClosure"]["runtime"].update(
        candidateCommit=runtime_commit,
        compositionCommit=runtime_commit,
    )

    result = _render_process(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=("--values", str(_lock_override(tmp_path, lock))),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_platform_admission_policies_admit_each_governed_legacy_runtime_image(
    tmp_path: Path,
) -> None:
    validation_values = yaml.safe_load(
        (PLATFORM / "values.validation.yaml").read_text(encoding="utf-8")
    )
    lock = json.loads(validation_values["provisioner"]["deploymentLockJson"])
    forward_image = lock["components"]["runtime"]["image"]
    legacy_images = [
        "ghcr.io/artexis10/exomem@sha256:" + "f" * 64,
        "ghcr.io/artexis10/exomem@sha256:" + "e" * 64,
    ]
    unrelated_image = "ghcr.io/artexis10/exomem@sha256:" + "9" * 64
    legacy_catalog = lock["composition"]["legacyCatalog"]
    legacy = legacy_catalog[0]
    for image in legacy_images:
        unit = json.loads(json.dumps(legacy))
        unit["runtimeImage"] = image
        unit["contract"]["runtimeImage"] = image
        unit["contractSha256"] = hashlib.sha256(
            (json.dumps(unit["contract"], sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
        legacy_catalog.append(unit)
    legacy["runtimeImage"] = forward_image
    legacy["contract"]["runtimeImage"] = forward_image
    legacy["contractSha256"] = hashlib.sha256(
        (json.dumps(legacy["contract"], sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    lock_json = json.dumps(lock, separators=(",", ":")) + "\n"
    override = tmp_path / "legacy-runtime-lock.yaml"
    override.write_text(
        yaml.safe_dump(
            {
                "provisioner": {
                    "deploymentLockJson": lock_json,
                    "deploymentLockSha256": hashlib.sha256(lock_json.encode()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )

    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=("--values", str(override)),
    )
    policy_expressions = {
        name: [
            validation["expression"]
            for validation in _find(documents, "ValidatingAdmissionPolicy", name)["spec"][
                "validations"
            ]
        ]
        for name in (
            "exomem-tenant-boundary",
            "exomem-tenant-restore-candidate",
            "exomem-provisioner-scope",
            "exomem-durability-actions-scope",
        )
    }
    policies = {name: "\n".join(expressions) for name, expressions in policy_expressions.items()}
    runtime_images = json.dumps([forward_image, *legacy_images], separators=(",", ":"))
    assert (
        f"object.spec.containers[0].image in {runtime_images}" in policies["exomem-tenant-boundary"]
    )
    assert (
        f"variables.restore.image in {runtime_images}"
        in policies["exomem-tenant-restore-candidate"]
    )
    provisioner_image_guard = (
        f"variables.target.spec.template.spec.containers[0].image in {runtime_images}"
    )
    assert provisioner_image_guard in policies["exomem-provisioner-scope"]
    durability_guards = (
        "request.resource.resource != 'jobs'",
        "request.resource.resource != 'statefulsets'",
    )
    assert all(
        any(
            guard in expression and provisioner_image_guard in expression
            for expression in policy_expressions["exomem-durability-actions-scope"]
        )
        for guard in durability_guards
    )
    assert unrelated_image not in "\n".join(policies.values())


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
    result = _render_process(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        release_name="exomem-platform",
        extra_args=("--values", str(override)),
    )
    assert result.returncode != 0


def test_platform_renders_real_provisioner_composition() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    validation_values = yaml.safe_load(
        (PLATFORM / "values.validation.yaml").read_text(encoding="utf-8")
    )
    expected_lock = json.loads(validation_values["provisioner"]["deploymentLockJson"])
    expected_image = expected_lock["components"]["provisioner"]["image"]

    lock_name = (
        "exomem-hosted-deployment-lock-v2-"
        + validation_values["provisioner"]["deploymentLockSha256"][:16]
    )
    lock_config = _find(documents, "ConfigMap", lock_name)
    assert lock_config["metadata"]["namespace"] == "exomem-platform"
    assert lock_config["immutable"] is True
    assert json.loads(lock_config["data"]["exomem-hosted-deployment-lock-v2.json"]) == expected_lock
    rendered_lock = lock_config["data"]["exomem-hosted-deployment-lock-v2.json"]
    assert (
        hashlib.sha256(rendered_lock.encode()).hexdigest()
        == validation_values["provisioner"]["deploymentLockSha256"]
    )

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
    assert "EXOMEM_PROVISIONER_HCLOUD_SERVER_ID" in environment
    assert not any(
        privileged_fragment in name
        for name in environment
        for privileged_fragment in ("HCLOUD_TOKEN", "B2_", "DELETE_CREDENTIAL")
    )
    assert environment["EXOMEM_PROVISIONER_DEPLOYMENT_LOCK_PATH"]["value"] == (
        "/etc/exomem/deployment-lock/exomem-hosted-deployment-lock-v2.json"
    )
    assert environment["EXOMEM_PROVISIONER_ADMISSION_MODE"]["value"] == "expand"
    assert (
        json.loads(environment["EXOMEM_PROVISIONER_RUNTIME_TARGET_JSON"]["value"])
        == expected_lock["runtimeTarget"]
    )
    assert {item["name"] for item in worker_container["volumeMounts"]} >= {
        "deployment-lock",
        "temporary",
    }
    assert (
        next(volume for volume in worker_spec["volumes"] if volume["name"] == "deployment-lock")[
            "configMap"
        ]["name"]
        == lock_name
    )
    provisioner_role = _find(documents, "ClusterRole", "exomem-cell-provisioner")
    provisioner_configmaps = next(
        rule
        for rule in provisioner_role["rules"]
        if rule.get("apiGroups") == [""] and "configmaps" in rule.get("resources", [])
    )
    assert set(provisioner_configmaps["verbs"]) == {
        "create",
        "delete",
        "get",
        "list",
        "patch",
        "update",
        "watch",
    }
    volume_attachment_rule = next(
        rule
        for rule in provisioner_role["rules"]
        if rule.get("apiGroups") == ["storage.k8s.io"]
        and rule.get("resources") == ["volumeattachments"]
    )
    assert volume_attachment_rule["verbs"] == ["get", "list", "watch"]
    persistent_volume_rule = next(
        rule
        for rule in provisioner_role["rules"]
        if rule.get("apiGroups") == [""] and "persistentvolumes" in rule.get("resources", [])
    )
    assert persistent_volume_rule["verbs"] == ["get", "list", "watch"]
    fingerprint_result_rule = next(
        rule
        for rule in provisioner_role["rules"]
        if rule.get("apiGroups") == [""] and rule.get("resources") == ["pods"]
    )
    assert fingerprint_result_rule["verbs"] == ["get", "list"]
    assert not any(
        resource in {"pods/exec", "pods/log"}
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


def test_platform_limits_provisioner_authorization_secret_access_to_required_lifecycle() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")

    provisioner_role = _find(documents, "ClusterRole", "exomem-cell-provisioner")
    secret_rules = {
        tuple(rule.get("resourceNames", [])): set(rule["verbs"])
        for rule in provisioner_role["rules"]
        if rule.get("apiGroups") == [""] and rule.get("resources") == ["secrets"]
    }
    assert secret_rules[()] == {"create"}
    assert secret_rules[("exomem-cell-credentials",)] == {
        "delete",
        "get",
        "patch",
        "update",
    }
    assert secret_rules.get(("exomem-authorization-session",)) == {
        "get",
        "patch",
        "update",
    }

    provisioner_scope = _find(
        documents,
        "ValidatingAdmissionPolicy",
        "exomem-provisioner-scope",
    )
    validations = {
        validation["message"]: " ".join(validation["expression"].split())
        for validation in provisioner_scope["spec"]["validations"]
    }
    namespace_scope = validations[
        "The hosted provisioner may mutate only fixed resources in opaque exo-* tenant namespaces."
    ]
    assert "oldObject.metadata.name == 'exomem-cell-credentials'" in namespace_scope
    assert (
        "object.metadata.namein['exomem-cell-credentials','exomem-authorization-session']"
    ) in namespace_scope.replace(" ", "")

    fixed_names = validations[
        "The hosted provisioner may mutate only exact fixed names derived from the tenant namespace."
    ]
    assert (
        "request.operation=='DELETE'?variables.name=='exomem-cell-credentials':"
        "variables.namein['exomem-cell-credentials','exomem-authorization-session']"
    ) in fixed_names.replace(" ", "")


def test_platform_uses_the_selected_v3_rollback_runtime_everywhere(tmp_path: Path) -> None:
    values = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))
    lock = _v3_lock(json.loads(values["provisioner"]["deploymentLockJson"]))
    raw = json.dumps(lock, separators=(",", ":")) + "\n"
    override = tmp_path / "rollback.yaml"
    override.write_text(
        yaml.safe_dump(
            {
                "provisioner": {
                    "deploymentLockJson": raw,
                    "deploymentLockSha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "runtimeSelection": "rollback",
                }
            }
        ),
        encoding="utf-8",
    )
    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=("--values", str(override)),
    )
    rollback = lock["recordsCompatibility"]["rollbackRuntime"]
    rollback_target = rollback["runtimeTarget"]
    rollback_image = rollback["image"]

    api = _find(documents, "Deployment", "exomem-provisioner-api")
    worker = _find(documents, "Deployment", "exomem-provisioner-worker")
    for deployment in (api, worker):
        pod = deployment["spec"]["template"]
        env = {item["name"]: item for item in pod["spec"]["containers"][0]["env"]}
        assert pod["metadata"]["annotations"]["exomem.io/runtime-selection"] == "rollback"
        assert env["EXOMEM_PROVISIONER_RUNTIME_SELECTION"]["value"] == "rollback"
    worker_env = {
        item["name"]: item for item in worker["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert (
        json.loads(worker_env["EXOMEM_PROVISIONER_RUNTIME_TARGET_JSON"]["value"]) == rollback_target
    )

    deletion_job = json.loads(
        _find(documents, "ConfigMap", "exomem-deletion-job-template")["data"]["job-template.json"]
    )
    workloads = {
        "actions": _find(documents, "CronJob", "exomem-durability-actions")["spec"]["jobTemplate"][
            "spec"
        ]["template"]["spec"],
        "backup": _find(documents, "CronJob", "exomem-durability-backup")["spec"]["jobTemplate"][
            "spec"
        ]["template"]["spec"],
        "deletion": deletion_job["spec"]["template"]["spec"],
    }
    for name, pod in workloads.items():
        env = {item["name"]: item for item in pod["containers"][0]["env"]}
        selector = (
            "EXOMEM_PROVISIONER_RUNTIME_SELECTION"
            if name == "deletion"
            else "EXOMEM_DURABILITY_RUNTIME_SELECTION"
        )
        assert env[selector]["value"] == "rollback"

    policies = "\n".join(
        validation["expression"]
        for name in (
            "exomem-tenant-boundary",
            "exomem-tenant-restore-candidate",
            "exomem-provisioner-scope",
            "exomem-durability-actions-scope",
        )
        for validation in _find(documents, "ValidatingAdmissionPolicy", name)["spec"]["validations"]
    )
    assert rollback_image in policies


@pytest.mark.parametrize(
    ("lock_kind", "selection", "message"),
    (
        ("v3", "", "v3 requires runtimeSelection"),
        ("v3", "unknown", "value must be one of"),
        ("v2", "rollback", "v2 does not support rollback"),
    ),
)
def test_platform_refuses_an_invalid_runtime_selection(
    tmp_path: Path, lock_kind: str, selection: str, message: str
) -> None:
    values = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))
    source_lock = json.loads(values["provisioner"]["deploymentLockJson"])
    lock = _v3_lock(source_lock) if lock_kind == "v3" else source_lock
    raw = json.dumps(lock, separators=(",", ":")) + "\n"
    override = tmp_path / f"{lock_kind}-{selection or 'unset'}.yaml"
    override.write_text(
        yaml.safe_dump(
            {
                "provisioner": {
                    "deploymentLockJson": raw,
                    "deploymentLockSha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "runtimeSelection": selection,
                }
            }
        ),
        encoding="utf-8",
    )
    result = _render_process(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        extra_args=("--values", str(override)),
    )
    assert result.returncode != 0
    assert message in result.stderr


def test_platform_renders_a_read_only_recovery_operator_identity() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")

    service_account = _find(documents, "ServiceAccount", "exomem-init-retry-recovery")
    assert service_account["automountServiceAccountToken"] is True
    role = _find(documents, "ClusterRole", "exomem-init-retry-recovery")
    for rule in role["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}
        assert "secrets" not in rule["resources"]
    expected_resources = {
        ("", "namespaces"),
        ("", "persistentvolumeclaims"),
        ("", "persistentvolumes"),
        ("", "configmaps"),
        ("apps", "statefulsets"),
        ("batch", "jobs"),
        ("traefik.io", "ingressroutes"),
    }
    observed_resources = {
        (rule["apiGroups"][0], resource) for rule in role["rules"] for resource in rule["resources"]
    }
    assert observed_resources == expected_resources
    _find(documents, "ClusterRoleBinding", "exomem-init-retry-recovery")
    assert not any(
        document.get("kind") == "Service"
        and document.get("metadata", {}).get("name") == "exomem-init-retry-recovery"
        for document in documents
    )
    runbook = (ROOT / "docs/runbooks/hosted/cell.md").read_text(encoding="utf-8")
    assert "exomem-init-retry-recovery" in runbook
    assert (
        "spec:\n  enableServiceLinks: false\n  serviceAccountName: exomem-init-retry-recovery"
        in runbook
    )
    assert 'exomem-provisioner-recover-init-retry "$mode" --stdin < "$recovery_identity"' in runbook
    assert 'kubectl -n exomem-platform exec -i "$operator_pod" --' in runbook
    assert "--identity-file" not in runbook
    assert "another `reopen`" in runbook
    assert "set -euo pipefail" in runbook
    assert 'test "$mode" != reopen || :' not in runbook
    assert "run_recovery preflight\nrun_recovery reopen\nrun_recovery verify-recovery" in runbook
    assert ".items[0]" not in runbook
    assert 'test "${#lock_names[@]}" -eq 1' in runbook
    assert 'select(test("^exomem-hosted-deployment-lock-v[23]\\\\.json$"))' in runbook
    assert "EXOMEM_RECOVERY_RUNTIME_SELECTION, value: $runtime_selection" in runbook
    assert 'helm -n "$helm_release" get manifest "$helm_release"' in runbook
    assert "sleep 1200" in runbook
    assert "verify-recovery" in runbook
    assert "0006_operation_wire_protocol" in runbook
    assert ".final_proof == true" in runbook
    assert 'metadata.annotations."exomem.io/deployment-lock-sha256" == $digest' in runbook
    lock = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))[
        "provisioner"
    ]["deploymentLockSha256"]
    rendered_lock = _find(
        documents,
        "ConfigMap",
        "exomem-hosted-deployment-lock-v2-" + lock[:16],
    )
    assert rendered_lock["metadata"]["annotations"]["exomem.io/deployment-lock-sha256"] == lock
    # `_render` above already built the pinned dependencies in an isolated chart
    # copy; assert against those exact rendered documents rather than rerendering
    # the source chart without its dependency archives.
    assert rendered_lock["metadata"]["annotations"]["exomem.io/deployment-lock-sha256"] == lock


def test_platform_mounts_the_selected_lock_for_every_lock_consuming_workload() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    values = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))
    lock_name = (
        "exomem-hosted-deployment-lock-v2-" + values["provisioner"]["deploymentLockSha256"][:16]
    )
    deletion_job = json.loads(
        _find(documents, "ConfigMap", "exomem-deletion-job-template")["data"]["job-template.json"]
    )
    workloads = {
        "exomem-provisioner-worker": _find(documents, "Deployment", "exomem-provisioner-worker")[
            "spec"
        ]["template"]["spec"],
        "exomem-durability-actions": _find(documents, "CronJob", "exomem-durability-actions")[
            "spec"
        ]["jobTemplate"]["spec"]["template"]["spec"],
        "exomem-durability-backup": _find(documents, "CronJob", "exomem-durability-backup")["spec"][
            "jobTemplate"
        ]["spec"]["template"]["spec"],
        "exomem-deletion-worker": deletion_job["spec"]["template"]["spec"],
    }
    for name, pod in workloads.items():
        container = pod["containers"][0]
        environment = {item["name"]: item for item in container["env"]}
        assert environment["EXOMEM_PROVISIONER_DEPLOYMENT_LOCK_PATH"]["value"] == (
            "/etc/exomem/deployment-lock/exomem-hosted-deployment-lock-v2.json"
        ), name
        assert any(item["name"] == "deployment-lock" for item in container["volumeMounts"]), name
        volume = next(item for item in pod["volumes"] if item["name"] == "deployment-lock")
        assert volume["configMap"]["name"] == lock_name, name
        assert volume["configMap"]["items"] == [
            {
                "key": "exomem-hosted-deployment-lock-v2.json",
                "path": "exomem-hosted-deployment-lock-v2.json",
            }
        ], name
        if name == "exomem-deletion-worker":
            assert volume["configMap"]["defaultMode"] == 0o444


def test_platform_rotation_quiescence_surfaces_every_database_consumer() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    expected_deployments = {
        "exomem-provisioner-api",
        "exomem-provisioner-worker",
        "exomem-volume-worker",
    }
    expected_cronjobs = {
        "exomem-durability-actions",
        "exomem-export-gc",
        "exomem-durability-backup",
        "exomem-database-backup",
        "exomem-deletion-dispatcher",
    }

    def references_database(container: dict) -> bool:
        return any(
            item.get("valueFrom", {}).get("secretKeyRef", {}).get("name")
            == "exomem-provisioner-database"
            for item in container.get("env", [])
        )

    standing: set[tuple[str, str]] = set()
    for document in documents:
        kind = document.get("kind")
        pod_paths = {
            "Deployment": ("spec", "template", "spec"),
            "StatefulSet": ("spec", "template", "spec"),
            "DaemonSet": ("spec", "template", "spec"),
            "ReplicaSet": ("spec", "template", "spec"),
            "Job": ("spec", "template", "spec"),
            "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
            "Pod": ("spec",),
        }
        if kind not in pod_paths:
            continue
        pod: dict = document
        for key in pod_paths[kind]:
            pod = pod[key]
        if any(
            references_database(container)
            for container in [*(pod.get("initContainers") or []), *(pod.get("containers") or [])]
        ):
            standing.add((kind, document["metadata"]["name"]))
    assert standing == {
        *(("Deployment", name) for name in expected_deployments),
        *(("CronJob", name) for name in expected_cronjobs),
        ("Job", "exomem-provisioner-database-migration"),
    }

    deletion_template = json.loads(
        _find(documents, "ConfigMap", "exomem-deletion-job-template")["data"]["job-template.json"]
    )
    migration = _find(documents, "Job", "exomem-provisioner-database-migration")
    for transient in (deletion_template, migration):
        pod = transient["spec"]["template"]["spec"]
        assert any(
            references_database(container)
            for container in [*(pod.get("initContainers") or []), *(pod.get("containers") or [])]
        )


def test_platform_renders_live_capacity_receipt_collector_with_isolated_keys() -> None:
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    runtime = _find(documents, "ConfigMap", "exomem-operational-receipt-collector")
    assert runtime["immutable"] is True
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
    assert container["args"] == [
        "--contract",
        # Its own subPath mount: a regular file, not a ..data/ symlink.
        "/etc/exomem/capacity/private-alpha-capacity-v1.json",
        "--namespace",
        "exomem-platform",
        "--state-configmap",
        "exomem-capacity-receipt",
        "--hcloud-server-id",
        "156895713",
        "--hcloud-location",
        "fsn1",
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
    volume_worker = _find(documents, "Deployment", "exomem-volume-worker")
    assert "EXOMEM_CAPACITY_RECEIPT_PRIVATE_KEY" not in json.dumps([api, worker, volume_worker])
    assert "EXOMEM_HCLOUD_CAPACITY_TOKEN" not in json.dumps([api, worker, volume_worker])
    assert rendered.count("exomem-capacity-receipt-signer") == 1

    contract = _find(documents, "ConfigMap", "exomem-capacity-contract")
    assert contract["immutable"] is True
    assert contract["data"]["private-alpha-capacity-v1.json"] == (
        ROOT / "infra/operations/private-alpha-capacity-v1.json"
    ).read_text(encoding="utf-8")
    for deployment in (worker, volume_worker):
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        environment = {item["name"]: item for item in container["env"]}
        assert environment["EXOMEM_PROVISIONER_CAPACITY_RECEIPT_PUBLIC_KEY"]["valueFrom"][
            "secretKeyRef"
        ] == {
            "name": "exomem-capacity-receipt-verifier",
            "key": "public-key",
        }
        assert environment["EXOMEM_PROVISIONER_CAPACITY_CONTRACT_PATH"]["value"] == (
            "/etc/exomem/capacity/private-alpha-capacity-v1.json"
        )
        assert environment["EXOMEM_PROVISIONER_CAPACITY_RECEIPT_NAMESPACE"]["value"] == (
            "exomem-platform"
        )
        assert environment["EXOMEM_PROVISIONER_CAPACITY_RECEIPT_CONFIG_MAP"]["value"] == (
            "exomem-capacity-receipt"
        )
        assert environment["EXOMEM_PROVISIONER_HCLOUD_SERVER_ID"]["value"] == "156895713"
        # subPath, so the loader sees a regular file rather than a ..data/ symlink.
        assert any(
            mount["name"] == "capacity-contract"
            and mount["mountPath"] == "/etc/exomem/capacity/private-alpha-capacity-v1.json"
            and mount["subPath"] == "private-alpha-capacity-v1.json"
            and mount["readOnly"] is True
            for mount in container["volumeMounts"]
        )
    provisioner_role = _find(documents, "ClusterRole", "exomem-cell-provisioner")
    for resource, api_group in (
        ("nodes", ""),
        ("persistentvolumes", ""),
        ("volumeattachments", "storage.k8s.io"),
    ):
        rule = next(
            item
            for item in provisioner_role["rules"]
            if resource in item.get("resources", []) and api_group in item.get("apiGroups", [])
        )
        assert rule["verbs"] == ["get", "list", "watch"]


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
        # Daily: each vault run is a full independent encrypted archive and
        # quiesces the cell, so frequency multiplies storage and downtime.
        # The database dump below stays sub-daily — it is small and its loss
        # window governs control-plane recovery, not tenant vault content.
        "exomem-durability-backup": (
            "CronJob",
            ["exomem-durability-backup-worker"],
            "17 2 * * *",
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
            "exomem-capacity-receipt-verifier/public-key",
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
    assert database_env["EXOMEM_DURABILITY_SCRATCH_ROOT"] == (
        "/var/lib/exomem-scratch/database-backup"
    )
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
    assert volume_env["EXOMEM_PROVISIONER_CAPACITY_CONTRACT_PATH"] == (
        "/etc/exomem/capacity/private-alpha-capacity-v1.json"
    )
    assert volume_env["EXOMEM_PROVISIONER_CAPACITY_RECEIPT_NAMESPACE"] == "exomem-platform"
    assert volume_env["EXOMEM_PROVISIONER_CAPACITY_RECEIPT_CONFIG_MAP"] == (
        "exomem-capacity-receipt"
    )
    assert volume_env["EXOMEM_PROVISIONER_HCLOUD_SERVER_ID"] == "156895713"

    deletion_job = json.loads(
        _find(documents, "ConfigMap", "exomem-deletion-job-template")["data"]["job-template.json"]
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
    for resource, api_group in (
        ("nodes", ""),
        ("volumeattachments", "storage.k8s.io"),
    ):
        rule = next(
            item
            for item in volume_role["rules"]
            if resource in item.get("resources", []) and api_group in item.get("apiGroups", [])
        )
        assert rule["verbs"] == ["get", "list", "watch"]

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
    provisioner_scope = _find(documents, "ValidatingAdmissionPolicy", "exomem-provisioner-scope")
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
    assert dispatcher["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 30
    assert dispatcher_container["resources"] == {
        "requests": {"cpu": "10m", "memory": "128Mi"},
        "limits": {"cpu": "250m", "memory": "256Mi"},
    }
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


def test_deletion_dispatcher_admission_closes_probe_and_container_override_surfaces(
    tmp_path: Path,
) -> None:
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
    assert f"{container}.resources.requests.cpu == quantity('25m')" not in expressions
    assert f"{container}.resources.limits.memory == quantity('384Mi')" not in expressions
    assert (
        "quantity(dyn(object.spec.template.spec.containers[0].resources).requests['cpu']).compareTo(quantity('25m')) == 0"
        in expressions
    )
    assert (
        "quantity(dyn(object.spec.template.spec.containers[0].resources).limits['memory']).compareTo(quantity('384Mi')) == 0"
        in expressions
    )
    assert (
        "size(dyn(object.spec.template.spec.containers[0].resources).requests) == 2" in expressions
    )
    assert "size(dyn(object.spec.template.spec.containers[0].resources).limits) == 2" in expressions
    assert "!has(dyn(object.spec.template.spec).resources)" in expressions
    assert "EXOMEM_PROVISIONER_DEPLOYMENT_LOCK_PATH" in expressions
    values = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))
    v2_lock = json.loads(values["provisioner"]["deploymentLockJson"])
    for version, lock in ((2, v2_lock), (3, _v3_lock(v2_lock))):
        override = _lock_override(tmp_path / f"v{version}", lock)
        variant_documents = _render(
            PLATFORM,
            PLATFORM / "values.validation.yaml",
            namespace="exomem-platform",
            extra_args=("--values", str(override)),
        )
        variant_admission = _find(
            variant_documents,
            "ValidatingAdmissionPolicy",
            "exomem-deletion-dispatcher-job-scope",
        )
        variant_expressions = "\n".join(
            validation["expression"] for validation in variant_admission["spec"]["validations"]
        )
        lock_file = f"exomem-hosted-deployment-lock-v{version}.json"
        lock_name = (
            f"exomem-hosted-deployment-lock-v{version}-"
            f"{hashlib.sha256((json.dumps(lock, separators=(',', ':')) + chr(10)).encode()).hexdigest()[:16]}"
        )
        assert (
            f'{container}.env[14].value == "/etc/exomem/deployment-lock/{lock_file}"'
            in variant_expressions
        )
        assert f'volumes[1].configMap.name == "{lock_name}"' in variant_expressions
        assert f'volumes[1].configMap.items[0].key == "{lock_file}"' in variant_expressions
        assert f'volumes[1].configMap.items[0].path == "{lock_file}"' in variant_expressions
    assert "volumes[1].configMap.defaultMode == 292" in expressions
    assert "!has(dyn(object.spec.template.spec.volumes[1].configMap.items[0]).mode)" in expressions
    assert "!has(object.spec.template.spec.volumes[1].configMap.defaultMode)" not in expressions
    assert (
        "quantity(dyn(object.spec.template.spec.volumes[0].emptyDir).sizeLimit).compareTo(quantity('64Mi')) == 0"
        in expressions
    )
    assert "!has(dyn(object.spec.template.spec).overhead)" in expressions
    assert "!has(dyn(object.spec.template.spec).activeDeadlineSeconds)" in expressions
    assert "object.spec.parallelism == 1" in expressions
    assert "object.spec.completions == 1" in expressions
    assert "object.spec.completionMode == 'NonIndexed'" in expressions
    assert "dyn(object.spec).podReplacementPolicy == 'TerminatingOrFailed'" in expressions
    assert "!has(dyn(object.spec).managedBy)" in expressions
    assert (
        "object.spec.selector.matchLabels['batch.kubernetes.io/controller-uid'] == object.metadata.uid"
        in expressions
    )
    assert (
        "object.spec.template.metadata.labels['batch.kubernetes.io/controller-uid'] == object.metadata.uid"
        in expressions
    )
    assert "object.spec.template.spec.serviceAccount == 'exomem-deletion-worker'" in expressions
    assert f"{container}.env[17].valueFrom.fieldRef.apiVersion == 'v1'" in expressions
    assert f"{container}.volumeMounts[1].readOnly == true" in expressions


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
    assert container["image"] == ("ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64)
    assert container["command"] == ["exomem-durability-actions"]
    env = {item["name"]: item for item in container["env"]}
    assert env["EXOMEM_DURABILITY_MAX_OPERATIONS"]["value"] == "1"
    assert env["EXOMEM_DURABILITY_SCRATCH_ROOT"]["value"] == "/var/lib/exomem-scratch"
    assert env["EXOMEM_PROVISIONER_DEPLOYMENT_LOCK_PATH"]["value"] == (
        "/etc/exomem/deployment-lock/exomem-hosted-deployment-lock-v2.json"
    )
    assert (
        next(item for item in pod["volumes"] if item["name"] == "deployment-lock")["configMap"][
            "name"
        ]
        == "exomem-hosted-deployment-lock-v2-97c1fc1bf93e0492"
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
        "EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY": ("exomem-provider-recovery-signer/private-key"),
        "EXOMEM_DURABILITY_RECOVERY_RESTORE_KEY_ID": (
            "exomem-recovery-restore-key-id/application-key-id"
        ),
        "EXOMEM_DURABILITY_RECOVERY_RESTORE_KEY": ("exomem-recovery-restore-key/application-key"),
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
    assert permissions[(("",), ("namespaces",))] == {"get", "list", "patch", "update", "watch"}
    assert permissions[(("",), ("configmaps",))] == {
        "create",
        "delete",
        "get",
        "list",
        "patch",
        "update",
    }
    assert permissions[(("",), ("secrets",))] == {"create", "delete", "get", "list"}
    assert permissions[(("",), ("persistentvolumeclaims",))] == {"get", "list", "patch", "update"}
    assert permissions[(("",), ("limitranges", "resourcequotas", "serviceaccounts"))] == {
        "get",
        "list",
        "patch",
        "update",
    }
    assert permissions[(("",), ("services",))] == {
        "create",
        "delete",
        "get",
        "list",
        "patch",
        "update",
    }
    assert permissions[(("",), ("pods",))] == {"delete", "get", "list"}
    assert permissions[(("",), ("pods/log",))] == {"get"}
    assert permissions[(("apps",), ("statefulsets", "statefulsets/scale"))] == {
        "get",
        "list",
        "patch",
        "update",
        "create",
        "delete",
    }
    assert permissions[(("batch",), ("jobs",))] == {"create", "delete", "get", "list"}
    assert permissions[(("coordination.k8s.io",), ("leases",))] == {
        "create",
        "delete",
        "get",
        "patch",
        "update",
    }
    assert permissions[(("networking.k8s.io",), ("networkpolicies",))] == {
        "create",
        "delete",
        "get",
        "list",
        "patch",
        "update",
    }
    assert permissions[(("traefik.io",), ("ingressroutes", "middlewares"))] == {
        "create",
        "delete",
        "get",
        "list",
        "patch",
        "update",
    }

    action_scope = _find(documents, "ValidatingAdmissionPolicy", "exomem-durability-actions-scope")
    action_scope_text = json.dumps(action_scope)
    exact_tenant_pvc_quantity = (
        "quantity(dyn(variables.target.spec).resources.requests['storage'])"
        ".compareTo(quantity('10Gi')) == 0"
    )
    assert exact_tenant_pvc_quantity in action_scope_text
    assert (
        "variables.target.spec.resources.requests.storage == quantity('10Gi')"
        not in action_scope_text
    )
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
        "vaultFingerprint",
        "lifecycleJob",
        "tenantNamespace",
        "restoreCandidate",
        "inScope",
        "controllerUpdate",
        "controllerJobFinalizerRemoval",
        "controllerJobFinalizerTransition",
    ]
    assert "exomem-storage-init" in variables[0]["expression"]
    assert "exomem.io/vault-fingerprint" in variables[1]["expression"]
    assert "storageInit" in variables[2]["expression"]
    assert "exomem.io/tenant-cell" in variables[3]["expression"]
    assert all(
        "!variables.inScope" in validation["expression"]
        for validation in tenant_admission["spec"]["validations"]
    )
    admission_text = json.dumps(tenant_admission)
    lock = json.loads(
        yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))[
            "provisioner"
        ]["deploymentLockJson"]
    )
    provisioner_image = lock["components"]["provisioner"]["image"]
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
    assert "size(object.spec.volumes) == 4" in admission_text
    assert "size(object.spec.containers[0].volumeMounts) == 5" in admission_text
    assert "seccompProfile.type == 'RuntimeDefault'" in admission_text
    assert "securityContext.seccompProfile" in admission_text
    assert "terminationMessagePath == '/dev/termination-log'" in admission_text
    assert "terminationMessagePolicy == 'File'" in admission_text
    assert (
        "request.userInfo.username == 'system:serviceaccount:kube-system:job-controller'"
        in admission_text
    )
    assert "batch.kubernetes.io/job-tracking" in admission_text
    assert "size(object.spec.containers[0].env) == 28" in admission_text
    normalized_admission = " ".join(
        "\n".join(
            validation["expression"] for validation in tenant_admission["spec"]["validations"]
        ).split()
    )
    assert (
        f"variables.vaultFingerprint ? object.spec.containers[0].image == "
        f"'{provisioner_image}' : object.spec.containers[0].image in"
    ) in normalized_admission
    assert (
        "object.spec.containers[0].args == ['exomem-provisioner-vault-fingerprint']"
    ) in admission_text
    assert "object.spec.containers[0].volumeMounts[0].readOnly == true" in admission_text
    assert "variables.lifecycleJob" in admission_text
    assert "EXOMEM_HOSTED_RECORDS_READER_VERSION" in admission_text
    assert "exomem.io/records-reader-version" in admission_text
    assert "EXOMEM_HOSTED_LIFECYCLE_ACTIONS_ENABLED" in admission_text
    assert "exomem.io/lifecycle-actions-enabled" in admission_text
    assert "EXOMEM_AUTH_SESSION_REPLICA_ID" in admission_text
    assert "exomem.io/authorization-session-secret-name" in admission_text
    assert "/run/exomem/authorization-session/private/keyring.json" in admission_text
    assert "/run/exomem/authorization-session/private/control.json" in admission_text
    assert "/run/exomem/authorization-session/private/serving-membership.json" in admission_text
    assert "!has(object.spec.securityContext.fsGroup)" in admission_text
    assert "!has(object.spec.securityContext.fsGroupChangePolicy)" in admission_text
    assert "volume.secret.defaultMode == 288" not in admission_text
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
    assert "^([2-9]|[1-9][0-9]+)$" in namespace_policy_text
    namespace_operations = namespace_policy["spec"]["matchConstraints"]["resourceRules"][0][
        "operations"
    ]
    assert namespace_operations == ["CREATE", "UPDATE"]
    assert "request.userInfo.username" in namespace_policy_text
    assert "system:admin" in namespace_policy_text
    assert "system:serviceaccount:exomem-platform:exomem-cell-provisioner" in namespace_policy_text
    assert (
        "system:serviceaccount:exomem-platform:exomem-durability-actions" in namespace_policy_text
    )
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
        "exomem.io/authorization-session-secret-name",
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

    provisioner_scope = _find(documents, "ValidatingAdmissionPolicy", "exomem-provisioner-scope")
    provisioner_scope_text = json.dumps(provisioner_scope)
    normalized_provisioner_scope = " ".join(
        "\n".join(
            [
                *(variable["expression"] for variable in provisioner_scope["spec"]["variables"]),
                *(
                    validation["expression"]
                    for validation in provisioner_scope["spec"]["validations"]
                ),
            ]
        ).split()
    )
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
    assert (
        "quantity(dyn(variables.target.spec).resources.requests['storage'])"
        ".compareTo(quantity('10Gi')) == 0"
    ) in provisioner_scope_text
    assert (
        "variables.target.spec.resources.requests.storage == quantity('10Gi')"
        not in provisioner_scope_text
    )
    assert "NetworkPolicy deletion is reserved for namespace destruction" in provisioner_scope_text
    for fingerprint_job_guard in (
        "variables.labels['exomem.io/vault-fingerprint'] == 'true'",
        "variables.annotations['exomem.io/vault-fingerprint-phase'] in ['before', 'after']",
        "variables.annotations['exomem.io/vault-fingerprint-operation'].matches('^[a-f0-9]{64}$')",
        f"variables.target.spec.template.spec.containers[0].image == '{provisioner_image}'",
        "variables.target.spec.template.spec.containers[0].args == ['exomem-provisioner-vault-fingerprint']",
        "variables.target.spec.template.spec.containers[0].volumeMounts[0].readOnly == true",
        "variables.target.spec.template.spec.volumes[0].persistentVolumeClaim.claimName == request.namespace + '-data'",
        "variables.target.spec.backoffLimit == 0",
        "variables.target.spec.activeDeadlineSeconds == 600",
        "variables.target.spec.ttlSecondsAfterFinished == 300",
    ):
        assert fingerprint_job_guard in normalized_provisioner_scope
    action_scope = _find(documents, "ValidatingAdmissionPolicy", "exomem-durability-actions-scope")
    for scope in (provisioner_scope, action_scope):
        scope_text = json.dumps(scope)
        assert "size(variables.target.spec.ingress) == 3" in scope_text
        for index in range(3):
            assert f"size(variables.target.spec.ingress[{index}].from) == 1" in scope_text
        assert (
            "variables.target.spec.ingress[2].from[0].namespaceSelector.matchLabels" in scope_text
        )
        assert "variables.target.spec.ingress[2].from[0].podSelector.matchLabels" in scope_text
        assert "'app.kubernetes.io/name': 'exomem-provisioner-worker'" in scope_text
        assert "size(variables.target.spec.ingress[2].ports) == 1" in scope_text
        assert "variables.target.spec.ingress[2].ports[0].protocol == 'TCP'" in scope_text
        assert "variables.target.spec.ingress[2].ports[0].port == 8765" in scope_text

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    cronjobs = {
        document["metadata"]["name"]: document
        for document in documents
        if document.get("kind") == "CronJob"
        and document.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/part-of")
        == "exomem-hosted-scheduler"
    }
    # Scheduler CronJobs are prefixed; the bare contract name collided with the
    # durability `exomem-export-gc` CronJob in the same namespace.
    assert set(cronjobs) == {f"exomem-hosted-scheduler-{job['name']}" for job in contract["jobs"]}
    for job in contract["jobs"]:
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
    assert contract["alerts"]["scheduler_missed_run_seconds"] == 180
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
        "exomem.io/authorization-session-secret-name": "exomem-authorization-session",
        "exomem.io/init-request-configmap-name": "cell-alpha-init-request",
        "exomem.io/provision-mode": "serve",
        "exomem.io/vault-id": "vault-alpha-original",
        "exomem.io/expected-release": "0.1.0-alpha",
        "exomem.io/worker-policy-digest": "b" * 64,
        "exomem.io/records-reader-version": "2",
        "exomem.io/lifecycle-actions-enabled": "false",
        "exomem.io/browser-origin": "https://substratesystems.io",
        "exomem.io/transfer-hostname": "transfer.example.test",
    }
    if expected_kind == "Job":
        assert workload["spec"]["ttlSecondsAfterFinished"] == 300
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
        assert container["env"] == [
            {"name": "EXOMEM_LOG_DIR", "value": "/dev"},
            {"name": "EXOMEM_HOSTED_OFFLINE_STATE_MIGRATION", "value": "1"},
        ]
    else:
        assert workload["spec"]["template"]["metadata"]["annotations"] == {
            "exomem.io/authorization-session-revision": "a" * 64
        }
        pod = workload["spec"]["template"]["spec"]
        assert pod["restartPolicy"] == "Always"
        assert "runtimeClassName" not in pod
        assert "fsGroup" not in pod["securityContext"]
        assert "fsGroupChangePolicy" not in pod["securityContext"]
        assert len(pod.get("initContainers", [])) == 1
        custody_init = pod["initContainers"][0]
        assert custody_init["name"] == "authorization-session-custody"
        assert custody_init["args"] == [
            "-m",
            "exomem.governance.authorization_hosted_mount",
        ]
        assert custody_init["securityContext"]["runAsUser"] == 10001
        assert custody_init["resources"]["requests"]["ephemeral-storage"] == "16Mi"
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
        assert env["EXOMEM_HOSTED_RECORDS_READER_VERSION"] == "2"
        assert env["EXOMEM_HOSTED_LIFECYCLE_ACTIONS_ENABLED"] == "false"
        assert env["EXOMEM_AUTH_SESSION_KEYRING_FILE"] == (
            "/run/exomem/authorization-session/private/keyring.json"
        )
        assert env["EXOMEM_AUTH_SESSION_CONTROL_FILE"] == (
            "/run/exomem/authorization-session/private/control.json"
        )
        assert env["EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE"] == (
            "/run/exomem/authorization-session/private/serving-membership.json"
        )
        replica = next(
            item
            for item in container["env"]
            if item["name"] == "EXOMEM_AUTH_SESSION_REPLICA_ID"
        )
        assert replica["valueFrom"] == {"fieldRef": {"fieldPath": "metadata.name"}}
        assert "EXOMEM_HOSTED_BROWSER_ORIGIN" not in env
        assert env["EXOMEM_HOSTED_STORAGE_LIMIT_BYTES"] == "5368709120"
        assert env["EXOMEM_HOSTED_UPLOAD_LIMIT_BYTES"] == "94371840"
        # A hosted cell must not be a lesser product than the free local
        # runtime. hosted_runtime.py SETS EXOMEM_DISABLE_EMBEDDINGS whenever the
        # worker limit is zero or the grant is missing, and nothing downstream
        # surfaces that: the cell serves keyword-only recall in silence.
        assert env["EXOMEM_HOSTED_WORKER_LIMIT"] == "2"
        assert env["EXOMEM_HOSTED_FEATURE_GRANTS"] == "embeddings,file-watcher"
        assert "EXOMEM_DISABLE_EMBEDDINGS" not in env
        assert "EXOMEM_DISABLE_FILE_WATCHER" not in env
        # torch would otherwise size its pool from the node's core count and
        # oversubscribe every cell's cgroup.
        assert env["OMP_NUM_THREADS"] == "1"
        assert env["MKL_NUM_THREADS"] == "1"
        assert int(env["OMP_NUM_THREADS"]) <= int(container["resources"]["limits"]["cpu"])
        # Measured at the CPU encode batch the runtime uses: 918 MiB peak
        # mid-encode, 791 MiB warm. The pre-embedding 1Gi limit would
        # OOM-kill the cell inside its first batch.
        assert container["resources"]["limits"]["memory"] == "1536Mi"
        assert container["resources"]["requests"]["memory"] == "1Gi"
        assert env["TMPDIR"] == "/var/lib/exomem/state/tmp/runtime"
        assert {volume["name"] for volume in pod["volumes"]} == {
            "authorization-session-custody",
            "authorization-session-source",
            "data",
            "credentials",
        }
        assert {mount["mountPath"] for mount in container["volumeMounts"]} == {
            "/var/lib/exomem/vault",
            "/var/lib/exomem/state",
            "/var/lib/exomem/logs",
            "/run/exomem/credentials",
            "/run/exomem/authorization-session",
        }
        assert container["resources"]["limits"]["ephemeral-storage"] == "512Mi"

        ingress = _find(documents, "NetworkPolicy", "cell-alpha-traefik-ingress")
        assert ingress["spec"]["policyTypes"] == ["Ingress"]
        assert "egress" not in ingress["spec"]
        assert ingress["spec"]["ingress"] == [
            {
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "exomem-platform"}
                        },
                        "podSelector": {
                            "matchLabels": {
                                "app.kubernetes.io/name": "traefik",
                                "exomem.io/ingress": "traefik",
                            }
                        },
                    }
                ],
                "ports": [{"protocol": "TCP", "port": 8765}],
            },
            {
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "exomem-platform"}
                        },
                        "podSelector": {
                            "matchLabels": {"app.kubernetes.io/name": "exomem-durability-actions"}
                        },
                    }
                ],
                "ports": [{"protocol": "TCP", "port": 8765}],
            },
            {
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "exomem-platform"}
                        },
                        "podSelector": {
                            "matchLabels": {"app.kubernetes.io/name": "exomem-provisioner-worker"}
                        },
                    }
                ],
                "ports": [{"protocol": "TCP", "port": 8765}],
            },
        ]

        credentials = next(volume for volume in pod["volumes"] if volume["name"] == "credentials")
        assert credentials["secret"]["defaultMode"] == 0o444
        assert credentials["secret"]["secretName"] == "exomem-cell-credentials"
        authorization_source = next(
            volume
            for volume in pod["volumes"]
            if volume["name"] == "authorization-session-source"
        )
        assert authorization_source["secret"]["defaultMode"] == 0o444
        assert "EXOMEM_HOSTED_SERVICE_CREDENTIAL" not in env
        assert not any("secretKeyRef" in item.get("valueFrom", {}) for item in container["env"])

    network_policies = [item for item in documents if item.get("kind") == "NetworkPolicy"]
    assert len(network_policies) >= (2 if expected_kind == "StatefulSet" else 1)
    assert all(item["spec"].get("policyTypes") for item in network_policies)
    service = [item for item in documents if item.get("kind") == "Service"]
    assert (len(service) == 1) == (expected_kind == "StatefulSet")
    if expected_kind == "StatefulSet":
        assert not [item for item in documents if item.get("kind") == "Job"]
    if service:
        assert service[0]["spec"]["type"] == "ClusterIP"


def test_cell_schema_rejects_mutable_image_and_non_fixed_limits() -> None:
    schema = json.loads((CELL / "values.schema.json").read_text(encoding="utf-8"))
    text = json.dumps(schema)
    assert "@sha256:" in text
    assert '"const": 5368709120' in text
    assert '"const": 94371840' in text
    assert '"const": "10Gi"' in text
    # Every knob that decides what the tenant actually receives is pinned, so a
    # values override cannot quietly change the product or the resource
    # envelope. The worker limit and grants are here because a zero/empty pair
    # renders a cell that serves keyword-only recall without erroring.
    properties = schema["properties"]
    assert properties["workerLimit"]["const"] == 2
    assert properties["featureGrants"]["const"] == "embeddings,file-watcher"
    assert properties["cpuRequest"]["const"] == "250m"
    assert properties["cpuLimit"]["const"] == "2"
    assert properties["memoryRequest"]["const"] == "1Gi"
    assert properties["memoryLimit"]["const"] == "1536Mi"
    assert properties["embeddingThreads"]["const"] == 1
    assert properties["embeddingThreads"]["const"] <= int(properties["cpuLimit"]["const"])
    assert properties["runtimeUpgrade"]["additionalProperties"] is False
    assert properties["runtimeUpgrade"]["properties"]["priorRevision"]["minimum"] == 1
    assert schema["properties"]["workloadMode"]["enum"] == [
        "initialize",
        "migrate",
        "restore",
        "serve",
    ]
    assert "migrationMode" in schema["required"]
    assert schema["properties"]["migrationMode"]["enum"] == [
        "none",
        "binding-v1-to-v2",
        "state-root-v1",
    ]
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


def test_cell_chart_migrate_mode_renders_only_the_bounded_init_job() -> None:
    documents = _render(
        CELL,
        CELL / "values.initialize.yaml",
        namespace="cell-alpha-test",
        extra_args=("--set", "workloadMode=migrate"),
    )

    job = _find(documents, "Job", "cell-alpha-init")
    assert _find(documents, "ConfigMap", "cell-alpha-init-request")
    assert not any(document.get("kind") == "StatefulSet" for document in documents)
    assert not any(document.get("kind") == "IngressRoute" for document in documents)
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == [
        "hosted",
        "init",
        "--contract-version",
        "1",
        "--request-file",
        "/run/exomem/operator-requests/init.json",
    ]
    assert container["securityContext"]["capabilities"]["add"] == [
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
    ]
    assert "EXOMEM_HOSTED_OFFLINE_STATE_MIGRATION" not in {
        item["name"] for item in container["env"]
    }
    assert not any(document.get("kind") == "Service" for document in documents)


def test_fresh_initialize_always_enables_empty_state_manifest_creation() -> None:
    documents = _render(
        CELL,
        CELL / "values.initialize.yaml",
        namespace="cell-alpha-test",
        extra_args=(
            "--set",
            "workloadMode=initialize",
            "--set",
            "migrationMode=none",
        ),
    )

    job = _find(documents, "Job", "cell-alpha-init")
    env = {
        item["name"]: item.get("value")
        for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["EXOMEM_HOSTED_OFFLINE_STATE_MIGRATION"] == "1"


def test_cell_state_root_migration_mode_enables_only_the_offline_state_migrator() -> None:
    documents = _render(
        CELL,
        CELL / "values.initialize.yaml",
        namespace="cell-alpha-test",
        extra_args=(
            "--set",
            "workloadMode=migrate",
            "--set",
            "migrationMode=state-root-v1",
        ),
    )

    job = _find(documents, "Job", "cell-alpha-init")
    env = {
        item["name"]: item.get("value")
        for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["EXOMEM_HOSTED_OFFLINE_STATE_MIGRATION"] == "1"
    assert not any(document.get("kind") == "StatefulSet" for document in documents)


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


@pytest.mark.parametrize(
    ("override", "messages"),
    [
        ({"workerLimit": 0}, ("workerLimit must be greater than zero", "/workerLimit")),
        ({"featureGrants": ""}, ("featureGrants must include embeddings", "/featureGrants")),
        (
            {"featureGrants": "file-watcher"},
            ("featureGrants must include embeddings", "/featureGrants"),
        ),
    ],
)
def test_cell_chart_refuses_to_render_a_keyword_only_cell(
    tmp_path: Path, override: dict[str, object], messages: tuple[str, ...]
) -> None:
    """Either setting silently downgrades the tenant, so rendering must fail.

    `hosted_runtime.apply_process_environment` SETS EXOMEM_DISABLE_EMBEDDINGS
    when the worker limit is zero or the grant is absent. The cell then starts
    healthy, accepts writes, answers queries, and never matches on meaning.
    Nothing downstream distinguishes that from a working cell, so render time is
    the last point where it can be caught.

    Two layers reject it and either is a pass: values.schema.json pins the exact
    shipped shape, and the chart's own guard catches a zero worker limit or a
    missing grant if that schema is ever relaxed to a per-plan range.
    """
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    override_file = tmp_path / "downgraded-cell.yaml"
    override_file.write_text(yaml.safe_dump(override), encoding="utf-8")
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
            str(override_file),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, result.stdout
    assert any(message in result.stderr for message in messages), result.stderr


def test_cell_quota_holds_the_serving_pod_and_its_init_job_together() -> None:
    """The quota counts every non-terminal pod, so it must cover both at once.

    The pre-embedding ceiling was exactly the old runtime limit plus the init
    job's. Raising the runtime limit without raising this would have made the
    init job fail admission on quota rather than on real capacity — and it would
    have failed during provisioning, not during a test.
    """
    documents = _render(CELL, CELL / "values.validation.yaml", namespace="cell-alpha-test")
    quota = _find(documents, "ResourceQuota", "cell-alpha-quota")["spec"]["hard"]
    statefulset = _find(documents, "StatefulSet", "cell-alpha")
    runtime = statefulset["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]

    def mebibytes(value: str) -> int:
        if value.endswith("Gi"):
            return int(value.removesuffix("Gi")) * 1024
        if value.endswith("Mi"):
            return int(value.removesuffix("Mi"))
        raise AssertionError(f"unhandled memory unit: {value}")

    init_job_memory = mebibytes("1Gi")  # init-job.yaml, limits.memory
    init_job_cpu = 1  # init-job.yaml, limits.cpu
    assert mebibytes(quota["limits.memory"]) >= mebibytes(runtime["memory"]) + init_job_memory
    assert int(quota["limits.cpu"]) >= int(runtime["cpu"]) + init_job_cpu


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
    expected = {f"exomem-hosted-scheduler-{job['name']}" for job in schedules["jobs"]}
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


def test_provisioner_api_can_read_the_selected_deployment_lock() -> None:
    """Without the lock the API cannot start at all.

    `create_app` raises "selected deployment lock is required for serving
    admission" when the path is unset, and the chart mounted the lock on the
    worker container only. Nothing rendered wrong -- the Deployment was valid,
    it just could never boot -- so only a test that looks for the mount catches
    it. The API must not inherit the worker's other EXOMEM_PROVISIONER_* vars:
    ProvisionerSettings forbids extras, so an unmodelled one fails validation.
    """

    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    api = next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document["metadata"]["name"] == "exomem-provisioner-api"
    )
    pod = api["spec"]["template"]["spec"]
    container = pod["containers"][0]
    environment = {item["name"]: item.get("value") for item in container["env"]}

    lock_path = "/etc/exomem/deployment-lock/exomem-hosted-deployment-lock-v2.json"
    assert environment["EXOMEM_PROVISIONER_DEPLOYMENT_LOCK_PATH"] == lock_path
    assert {
        "name": "deployment-lock",
        "mountPath": "/etc/exomem/deployment-lock",
        "readOnly": True,
    } in container["volumeMounts"]
    lock_volume = next(volume for volume in pod["volumes"] if volume["name"] == "deployment-lock")
    assert lock_volume["configMap"]["items"] == [
        {
            "key": "exomem-hosted-deployment-lock-v2.json",
            "path": "exomem-hosted-deployment-lock-v2.json",
        }
    ]
    assert "EXOMEM_PROVISIONER_DEPLOYMENT_LOCK_SHA256" not in environment
    assert "EXOMEM_PROVISIONER_ADMISSION_MODE" not in environment
    assert "EXOMEM_PROVISIONER_RUNTIME_TARGET_JSON" not in environment


def test_capacity_contract_is_mounted_as_a_regular_file() -> None:
    """`load_capacity_contract` rejects a symlink, and ConfigMap keys are symlinks.

    A ConfigMap volume publishes every key as a symlink into `..data/`, so a plain
    directory mount made `contract_path.is_symlink()` true and both provider workers
    died with "capacity contract is unavailable" on every start. `subPath`
    materializes a regular file. Nothing about the manifests looked wrong -- the
    volume, the key and the path env all matched -- so only asserting the mount
    *shape* catches it.
    """

    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    contract_file = "private-alpha-capacity-v1.json"
    seen = 0
    for document in documents:
        if document.get("kind") not in {"Deployment", "CronJob"}:
            continue
        spec = document["spec"]
        pod = (
            spec["template"]["spec"]
            if document["kind"] == "Deployment"
            else spec["jobTemplate"]["spec"]["template"]["spec"]
        )
        if not any(v.get("name") == "capacity-contract" for v in pod.get("volumes", [])):
            continue
        for container in pod["containers"]:
            for mount in container.get("volumeMounts", []):
                if mount["name"] != "capacity-contract":
                    continue
                seen += 1
                assert mount.get("subPath") == contract_file, (
                    f"{document['metadata']['name']} mounts the capacity contract as a "
                    "directory; the loader rejects the resulting ..data/ symlink"
                )
                assert mount["mountPath"] == f"/etc/exomem/capacity/{contract_file}"
    assert seen >= 2, f"expected the provider workers to mount the contract, saw {seen}"


def test_traefik_preserves_the_tunnel_forwarded_scheme() -> None:
    """The provisioner fails closed on any /cells/* request that did not arrive over TLS.

    Cloudflare terminates TLS and cloudflared dials traefik's web entrypoint over
    plain HTTP from inside the cluster, so `X-Forwarded-Proto: https` is the only
    evidence the provisioner has. Traefik rewrites that header to the scheme it
    received unless the client is a trusted proxy, and uvicorn then reports
    `request.url.scheme == "http"`, which the contract middleware answers with
    PROVISIONER_REJECTED 400. Every provision, health and deletion call fails, and
    the rejection is content-free at both ends, so nothing names the cause.
    """

    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    traefik = _find(documents, "Deployment", "contract-test-traefik")
    arguments = traefik["spec"]["template"]["spec"]["containers"][0]["args"]
    trusted = [
        argument
        for argument in arguments
        if argument.startswith("--entryPoints.web.forwardedHeaders.trustedIPs=")
    ]
    assert trusted, (
        "traefik's web entrypoint trusts no proxy, so it overwrites the tunnel's "
        f"X-Forwarded-Proto and the provisioner rejects every call; args were {arguments}"
    )
    networks = [
        ipaddress.ip_network(value) for value in trusted[0].split("=", 1)[1].split(",") if value
    ]
    cloudflared = ipaddress.ip_address("10.42.0.1")
    assert any(cloudflared in network for network in networks), (
        f"the trusted set {trusted[0]} does not cover the cluster pod network that "
        "cloudflared dials from"
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
