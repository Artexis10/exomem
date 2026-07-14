from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
PLAN_INSPECTOR = INFRA / "scripts" / "inspect_terraform_plan.py"


def test_hosted_infrastructure_surfaces_and_ownership_are_explicit() -> None:
    required = (
        "terraform/foundation",
        "terraform/durability",
        "terraform/bootstrap",
        "ansible",
        "helm/platform",
        "helm/cell",
        "provisioner",
        "scripts",
    )
    for relative in required:
        path = INFRA / relative
        assert path.is_dir(), relative
        assert (path / "OWNERSHIP.md").is_file(), relative
    assert (ROOT / "docs/runbooks/hosted/README.md").is_file()


def test_tool_versions_are_exact_and_required_provider_versions_are_exact() -> None:
    versions = dict(
        line.split("=", 1)
        for line in (INFRA / "tool-versions.env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert versions["TERRAFORM_VERSION"] == "1.15.8"
    assert versions["K3S_VERSION"] == "v1.35.6+k3s1"
    assert versions["HCLOUD_CSI_CHART_VERSION"] == "2.21.1"
    assert all(value and not any(token in value for token in ("latest", "*", ">", "~")) for value in versions.values())

    for root in ("foundation", "durability"):
        required = (INFRA / "terraform" / root / "versions.tf").read_text(encoding="utf-8")
        assert 'required_version = "= 1.15.8"' in required
        assert "~>" not in required


def test_state_plan_and_plaintext_artifacts_are_gitignored() -> None:
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            "infra/terraform/foundation/terraform.tfstate",
            "infra/terraform/foundation/review.tfplan",
            "infra/terraform/foundation/review.plan.json",
            "infra/terraform/foundation/secrets.auto.tfvars",
            "infra/ansible/inventory.yml",
            "infra/secrets/platform.dec.yaml",
            "infra/secrets/age.key",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0, ignored.stderr
    assert len(ignored.stdout.splitlines()) == 7


def _plan(*changes: tuple[str, list[str]], outputs: dict | None = None) -> dict:
    return {
        "format_version": "1.2",
        "resource_changes": [
            {"address": address, "change": {"actions": actions}}
            for address, actions in changes
        ],
        "output_changes": outputs or {},
    }


def _inspect(tmp_path: Path, plan: dict, *extra: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(PLAN_INSPECTOR), str(path), *extra],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_plan_inspector_accepts_nondestructive_changes_without_echoing_values(
    tmp_path: Path,
) -> None:
    result = _inspect(
        tmp_path,
        _plan(
            ("hcloud_server.alpha", ["no-op"]),
            ("cloudflare_dns_record.control", ["update"]),
            outputs={"server_id": {"after": "provider-secret-looking-value", "after_sensitive": False}},
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "provider-secret-looking-value" not in result.stdout + result.stderr


def test_plan_inspector_rejects_destroy_replace_and_unredacted_secret_output(
    tmp_path: Path,
) -> None:
    for actions in (["delete"], ["delete", "create"], ["create", "delete"]):
        result = _inspect(tmp_path, _plan(("hcloud_server.alpha", actions)))
        assert result.returncode == 2
        assert "hcloud_server.alpha" in result.stderr

    secret = _inspect(
        tmp_path,
        _plan(outputs={"provisioner_token": {"after": "sentinel", "after_sensitive": False}}),
    )
    assert secret.returncode == 2
    assert "sentinel" not in secret.stdout + secret.stderr


def test_plan_inspector_requires_exact_per_address_destructive_approval(tmp_path: Path) -> None:
    plan = _plan(
        ("hcloud_server.alpha", ["delete", "create"]),
        ("hcloud_primary_ip.alpha", ["delete"]),
    )
    partial = _inspect(
        tmp_path,
        plan,
        "--allow-destructive",
        "hcloud_server.alpha",
    )
    assert partial.returncode == 2
    assert "hcloud_primary_ip.alpha" in partial.stderr

    approved = _inspect(
        tmp_path,
        plan,
        "--allow-destructive",
        "hcloud_server.alpha",
        "--allow-destructive",
        "hcloud_primary_ip.alpha",
    )
    assert approved.returncode == 0, approved.stderr
