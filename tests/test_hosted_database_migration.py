from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "infra/helm/platform"
VALUES = CHART / "values.validation.yaml"
HELM = Path(os.environ.get("HELM_BIN", "/tmp/exomem-hosted-tools/linux-amd64/helm"))


def _render(*, upgrade: bool) -> list[dict]:
    if not HELM.is_file():
        pytest.skip(f"Helm is unavailable at {HELM}")
    command = [
        str(HELM),
        "template",
        "migration-contract",
        str(CHART),
        "--namespace",
        "exomem-platform",
        "--values",
        str(VALUES),
    ]
    if upgrade:
        command.append("--is-upgrade")
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return [item for item in yaml.safe_load_all(completed.stdout) if isinstance(item, dict)]


def _migration_job(*, upgrade: bool) -> dict:
    return next(
        document
        for document in _render(upgrade=upgrade)
        if document.get("kind") == "Job"
        and document.get("metadata", {}).get("name") == "exomem-provisioner-database-migration"
    )


def test_install_database_gate_migrates_as_runtime_with_hardened_restart_safe_job() -> None:
    job = _migration_job(upgrade=False)
    annotations = job["metadata"]["annotations"]
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    environment = {item["name"]: item for item in container["env"]}

    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert "before-hook-creation" in annotations["helm.sh/hook-delete-policy"]
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 300
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["command"] == ["exomem-provisioner-database-migrate"]
    assert "@sha256:" in container["image"]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "capabilities": {"drop": ["ALL"]},
    }
    assert environment["EXOMEM_PROVISIONER_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "exomem-provisioner-database",
        "key": "url",
    }
    assert environment["EXOMEM_PROVISIONER_DATABASE_SCHEMA"]["value"] == "exomem_provisioner"
    assert environment["EXOMEM_PROVISIONER_DATABASE_ROLE"]["value"] == (
        "exomem_provisioner_runtime"
    )


def test_upgrade_database_gate_is_exact_head_validation_not_migration() -> None:
    container = _migration_job(upgrade=True)["spec"]["template"]["spec"]["containers"][0]

    assert container["command"] == ["exomem-provisioner-database-validate"]


@pytest.mark.parametrize("upgrade", [False, True])
def test_stable_chart_never_renders_admin_database_authority(upgrade: bool) -> None:
    documents = _render(upgrade=upgrade)
    rendered = yaml.safe_dump_all(documents)

    assert "EXOMEM_PROVISIONER_DATABASE_ADMIN_URL" not in rendered
    assert "exomem-provisioner-database-admin" not in rendered
    assert "admin-url" not in rendered


def test_admin_database_authority_is_ephemeral_and_rotation_gated() -> None:
    matrix = json.loads(
        (ROOT / "infra/contracts/secret-destinations-v1.json").read_text(encoding="utf-8")
    )
    matrix_text = json.dumps(matrix, sort_keys=True)
    deploy = (ROOT / "docs/runbooks/hosted/deploy.md").read_text(encoding="utf-8")
    secrets = (ROOT / "docs/runbooks/hosted/secrets.md").read_text(encoding="utf-8")

    assert "DATABASE_ADMIN_URL" not in matrix_text.upper()
    assert "database-bootstrap-admin" not in matrix_text
    assert "trap bootstrap_cleanup EXIT INT TERM" in deploy
    assert "EXOMEM_DATABASE_ADMIN_ROTATION_RECEIPT" in deploy
    assert "exomem-provisioner-database-bootstrap-admin" in deploy
    assert "no admin URL destination" in secrets
    assert "rotate/revoke" in secrets
