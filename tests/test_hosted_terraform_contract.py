from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FOUNDATION = ROOT / "infra/terraform/foundation"
DURABILITY = ROOT / "infra/terraform/durability"
BOOTSTRAP = ROOT / "infra/terraform/bootstrap"
HCP_BOOTSTRAP = ROOT / "infra/terraform/hcp-bootstrap"
HOSTED_CHANGE = ROOT / "openspec/changes/add-hosted-private-alpha-infrastructure"
TERRAFORM = Path(os.environ["TERRAFORM_BIN"]) if "TERRAFORM_BIN" in os.environ else None


def _all_tf(root: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("*.tf")))


def test_foundation_and_durability_are_disjoint_lifecycle_domains() -> None:
    foundation = _all_tf(FOUNDATION).lower()
    durability = _all_tf(DURABILITY).lower()
    assert "backblaze/b2" not in foundation
    assert "b2_bucket" not in foundation
    assert "hetznercloud/hcloud" not in durability
    assert "cloudflare/cloudflare" not in durability
    assert "hcloud_" not in durability
    assert "cloudflare_" not in durability

    assert re.search(
        r'cloud\s*{.*workspaces\s*{\s*name\s*=\s*"exomem-hosted-foundation"',
        foundation,
        re.S,
    )
    assert re.search(
        r'cloud\s*{.*workspaces\s*{\s*name\s*=\s*"exomem-hosted-durability"',
        durability,
        re.S,
    )
    assert 'backend "s3"' not in foundation
    assert 'backend "s3"' not in durability
    assert "use_lockfile" not in foundation
    assert "use_lockfile" not in durability
    assert 'backend "s3"' not in _all_tf(BOOTSTRAP)


def test_foundation_defaults_are_cost_safe_and_admin_cidrs_are_explicit() -> None:
    variables = (FOUNDATION / "variables.tf").read_text(encoding="utf-8")
    compute = (FOUNDATION / "compute.tf").read_text(encoding="utf-8")
    firewall = (FOUNDATION / "firewall.tf").read_text(encoding="utf-8")

    assert re.search(r'variable "admin_ssh_cidrs"\s*{(?:(?!default).)*}', variables, re.S)
    assert 'default     = "cx33"' in variables
    assert 'default     = "fsn1"' in variables
    assert 'default     = "ubuntu-24.04"' in variables
    assert 'default     = "10.50.1.10"' in variables
    assert "condition     = length(var.admin_ssh_cidrs) > 0" in variables
    assert 'cidr != "0.0.0.0/0" && cidr != "::/0"' in variables

    assert 'port        = "22"' in firewall
    for public_port in ('"80"', '"443"', '"6443"'):
        assert public_port not in firewall
    assert "source_ips  = var.admin_ssh_cidrs" in firewall

    assert len(re.findall(r"delete_protection\s*=\s*true", compute)) >= 2
    assert re.search(r"rebuild_protection\s*=\s*true", compute)
    assert re.search(r"auto_delete\s*=\s*false", compute)
    assert re.search(r"ipv6_enabled\s*=\s*false", compute)
    assert compute.count("prevent_destroy = true") >= 3

    outputs = (FOUNDATION / "outputs.tf").read_text(encoding="utf-8")
    assert 'output "estimated_fixed_monthly_eur_ex_vat"' in outputs
    assert 'output "control_hostname"' in outputs
    assert 'output "transfer_hostname"' in outputs
    assert re.search(r"value\s*=\s*8\.99", outputs)


def test_cloudflare_tunnel_has_exact_control_and_transfer_ingress() -> None:
    cloudflare = (FOUNDATION / "cloudflare.tf").read_text(encoding="utf-8")
    assert "cloudflare_zero_trust_tunnel_cloudflared" in cloudflare
    assert "cloudflare_zero_trust_access_service_token" in cloudflare
    assert re.search(r'decision\s*=\s*"non_identity"', cloudflare)
    assert "service_token" in cloudflare
    assert 'service = "http_status:404"' in cloudflare
    assert "var.control_hostname" in cloudflare
    assert "var.transfer_hostname" in cloudflare
    expected_traefik = "http://exomem-platform-traefik.exomem-platform.svc.cluster.local:80"
    assert cloudflare.count(expected_traefik) == 2
    assert "traefik.kube-system.svc.cluster.local" not in cloudflare
    assert cloudflare.count("cloudflare_dns_record") == 2
    assert 'type    = "CNAME"' in cloudflare
    assert "proxied = true" in cloudflare


def test_durability_has_object_lock_retention_and_split_credentials() -> None:
    storage = (DURABILITY / "storage.tf").read_text(encoding="utf-8")
    outputs = (DURABILITY / "outputs.tf").read_text(encoding="utf-8")

    assert storage.count("file_lock_configuration") == 2
    assert storage.count("is_file_lock_enabled = true") == 2
    assert storage.count('mode = "governance"') == 2
    assert storage.count("duration = 7") == 2
    assert storage.count('unit     = "days"') == 2
    assert storage.count("days_from_uploading_to_hiding = 30") == 2
    assert 'resource "b2_bucket" "user_export"' in storage
    user_export = storage.split('resource "b2_bucket" "user_export"', 1)[1].split(
        'resource "b2_bucket" "database_backup"', 1
    )[0]
    assert "file_lock_configuration" not in user_export
    assert "days_from_uploading_to_hiding = 31" in user_export

    assert 'key_name     = "exomem-recovery-upload"' in storage
    assert (
        'capabilities = ["listBuckets", "listFiles", "readFiles", "readFileRetentions", '
        '"writeFiles", "writeFileRetentions"]' in storage
    )
    assert 'key_name     = "exomem-recovery-restore"' in storage
    assert 'capabilities = ["listBuckets", "listFiles", "readFiles"]' in storage
    assert 'key_name     = "exomem-recovery-delete"' in storage
    assert (
        'capabilities = ["deleteFiles", "listBuckets", "listFiles", "readFiles", '
        '"readFileRetentions"]'
        in storage
    )
    assert 'key_name     = "exomem-database-backup-upload"' in storage
    assert 'key_name     = "exomem-database-backup-restore-jit"' in storage
    assert 'key_name     = "exomem-database-backup-delete-jit"' not in storage
    assert '"bypassGovernance"' not in storage
    assert 'key_name     = "exomem-etcd-snapshot-upload"' in storage
    assert 'key_name     = "exomem-etcd-snapshot-restore-jit"' in storage
    assert storage.count('name_prefix  = "database-backup/"') == 2
    assert storage.count('"writeFileRetentions"') == 2
    assert storage.count('"readFileRetentions"') == 5
    assert storage.count('name_prefix  = "etcd-snapshot/"') == 2
    assert 'key_name     = "exomem-user-export-upload"' in storage
    assert 'key_name     = "exomem-user-export-restore"' in storage
    assert 'key_name     = "exomem-user-export-delete"' in storage

    for secret_output in (
        "recovery_upload_application_key",
        "recovery_restore_application_key",
        "recovery_delete_application_key",
        "database_backup_upload_application_key",
        "database_backup_restore_application_key",
        "etcd_snapshot_upload_application_key",
        "etcd_snapshot_restore_application_key",
        "user_export_upload_application_key",
        "user_export_restore_application_key",
        "user_export_delete_application_key",
        "user_export_delivery_application_key",
    ):
        block = outputs.split(f'output "{secret_output}"', 1)[1].split("}", 1)[0]
        assert re.search(r"sensitive\s*=\s*true", block)

    assert "database_backup_delete_application_key" not in outputs


def test_durability_bucket_outputs_have_one_exact_platform_configmap_contract() -> None:
    contract = json.loads(
        (ROOT / "infra/contracts/durability-storage-v1.json").read_text(encoding="utf-8")
    )
    outputs = (DURABILITY / "outputs.tf").read_text(encoding="utf-8")

    assert contract["schemaVersion"] == 1
    assert contract["terraformRoot"] == "durability"
    assert contract["kubernetes"] == {
        "namespace": "exomem-platform",
        "configMap": "exomem-durability-storage",
    }
    assert contract["bindings"] == {
        "recovery_bucket_name": {
            "configMapKey": "recovery-bucket",
                "workerEnvironmentVariable": "EXOMEM_DURABILITY_RECOVERY_BUCKET",
        },
        "user_export_bucket_name": {
            "configMapKey": "user-export-bucket",
                "workerEnvironmentVariable": "EXOMEM_DURABILITY_USER_EXPORT_BUCKET",
        },
        "database_backup_bucket_name": {
            "configMapKey": "database-backup-bucket",
                "workerEnvironmentVariable": "EXOMEM_DURABILITY_DATABASE_BACKUP_BUCKET",
        },
    }
    for output_name in contract["bindings"]:
        block = outputs.split(f'output "{output_name}"', 1)[1].split("}", 1)[0]
        assert "sensitive" not in block


def test_existing_b2_bootstrap_stays_quarantined_until_separate_cleanup() -> None:
    bootstrap = _all_tf(BOOTSTRAP)

    assert 'resource "b2_bucket" "terraform_state"' in bootstrap
    assert bootstrap.count('resource "b2_application_key"') == 2
    assert "prevent_destroy = true" in bootstrap
    assert 'resource "tfe_workspace"' not in bootstrap


def test_hcp_bootstrap_manages_exact_state_only_workspaces() -> None:
    bootstrap = _all_tf(HCP_BOOTSTRAP)

    assert 'source  = "hashicorp/tfe"' in bootstrap
    assert 'version = "= 0.78.0"' in bootstrap
    assert 'resource "tfe_project" "hosted"' in bootstrap
    assert bootstrap.count('resource "tfe_workspace"') == 3
    assert '"foundation"' in bootstrap
    assert '"durability"' in bootstrap
    assert '"backend_proof"' in bootstrap
    assert bootstrap.count('resource "tfe_workspace_settings"') == 3
    assert len(re.findall(r'execution_mode\s*=\s*"local"', bootstrap)) == 3
    assert len(re.findall(r"global_remote_state\s*=\s*false", bootstrap)) == 3
    assert len(re.findall(r"project_remote_state\s*=\s*false", bootstrap)) == 3
    assert len(re.findall(r"auto_apply\s*=\s*false", bootstrap)) == 3
    assert len(re.findall(r"assessments_enabled\s*=\s*false", bootstrap)) == 3
    assert "var.terraform_version" in bootstrap
    assert 'resource "b2_bucket" "terraform_state"' not in bootstrap
    assert 'resource "b2_application_key"' not in bootstrap

    outputs = (HCP_BOOTSTRAP / "outputs.tf").read_text(encoding="utf-8")
    for output_name in (
        "hcp_project_id",
        "foundation_workspace_id",
        "durability_workspace_id",
        "backend_proof_workspace_id",
    ):
        assert f'output "{output_name}"' in outputs
    assert "application_key" not in outputs


def test_plan_wrapper_binds_exact_hcp_workspace_and_environment_only_token() -> None:
    for script_name in ("plan.sh", "apply_saved_plan.sh"):
        script = (ROOT / f"infra/scripts/{script_name}").read_text(encoding="utf-8")
        assert "TF_CLOUD_ORGANIZATION" in script
        assert "TF_TOKEN_app_terraform_io" in script
        assert 'workspace="exomem-hosted-${root_name}"' in script
        assert "unset TF_WORKSPACE" in script
        assert "verify_hcp_backend.py" in script
        assert script.index("verify_hcp_backend.py") < script.index(' init -input=false')
        assert "TF_BACKEND_CONFIG_FILE" not in script
        assert "AWS_ACCESS_KEY_ID" not in script
        assert "AWS_SECRET_ACCESS_KEY" not in script
        assert "-backend-config" not in script


def test_backend_bootstrap_seals_local_state_and_uses_saved_plans() -> None:
    script = (ROOT / "infra/scripts/bootstrap_hcp_backend.sh").read_text(encoding="utf-8")

    assert 'case "${action}" in' in script
    assert "plan|apply" in script
    assert '-state="${state_path}"' in script
    assert "inspect_terraform_plan.py" in script
    assert "plaintext_safe_to_remove=false" in script
    assert "SOPS_AGE_RECIPIENTS" in script
    assert '"${sops_bin}" encrypt' in script
    assert '"${sops_bin}" decrypt "${encrypted_tmp}" >/dev/null' in script
    assert 'mv -f -- "${encrypted_tmp}" "${escrow_path}"' in script
    assert "TFE_TOKEN" in script
    assert "TF_CLOUD_ORGANIZATION" in script


def test_hcp_bootstrap_refuses_to_overwrite_retained_state_with_old_escrow(
    tmp_path: Path,
) -> None:
    infra = tmp_path / "infra"
    scripts = infra / "scripts"
    root = infra / "terraform/hcp-bootstrap"
    scripts.mkdir(parents=True)
    root.mkdir(parents=True)
    script = scripts / "bootstrap_hcp_backend.sh"
    shutil.copy2(ROOT / "infra/scripts/bootstrap_hcp_backend.sh", script)

    state = root / "terraform.tfstate"
    state.write_text("newer interrupted state", encoding="utf-8")
    escrow = tmp_path / "old-state.sops.json"
    escrow.write_text("old encrypted state", encoding="utf-8")
    escrow.chmod(0o600)
    plan = tmp_path / "bootstrap.tfplan"
    fake_sops_marker = tmp_path / "sops-was-called"
    fake_sops = tmp_path / "sops"
    fake_sops.write_text(
        f"#!/usr/bin/env bash\n: > {fake_sops_marker}\nexit 99\n",
        encoding="utf-8",
    )
    fake_sops.chmod(0o700)

    result = subprocess.run(
        [str(script), "plan", str(plan), str(escrow)],
        env={
            **os.environ,
            "TF_CLOUD_ORGANIZATION": "example",
            "TFE_TOKEN": "not-a-real-token",
            "SOPS_BIN": str(fake_sops),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "seal it before continuing" in result.stderr
    assert state.read_text(encoding="utf-8") == "newer interrupted state"
    assert not fake_sops_marker.exists()


def test_hcp_backend_proof_exercises_real_lock_and_state_version_rollback() -> None:
    script = (ROOT / "infra/scripts/verify_hcp_backend.py").read_text(encoding="utf-8")

    assert '"exomem-hosted-backend-proof"' in script
    assert '"execution-mode"' in script
    assert '"local"' in script
    assert "-lock-timeout=0s" in script
    assert '"rollback-state-version"' in script
    assert "/actions/lock" in script
    assert "/actions/unlock" in script
    assert '"-refresh-only"' in script
    assert '"-lock=false"' in script
    assert "start_new_session=True" in script
    assert "_stop_process_group(first)" in script
    assert script.index('"-refresh-only"') < script.index(
        "_unlock_with_retry(client, workspace.workspace_id)"
    )
    assert "terraform state pull" not in script


def test_openspec_records_hcp_state_only_and_quarantines_legacy_b2_state() -> None:
    design = (HOSTED_CHANGE / "design.md").read_text(encoding="utf-8")
    foundation_spec = (
        HOSTED_CHANGE / "specs/hosted-infrastructure-foundation/spec.md"
    ).read_text(encoding="utf-8")

    for document in (design, foundation_spec):
        assert "HCP Terraform" in document
        assert "state-only" in document
        assert "exomem-hosted-foundation" in document
        assert "exomem-hosted-durability" in document
    assert "already-applied B2 bootstrap" in design
    assert "B2 S3 lockfile compatibility is not yet proven" not in design
    assert "one process holds the disposable proof workspace state lock" in foundation_spec


def test_hcp_provider_lock_covers_amd64_and_arm64() -> None:
    lock = (HCP_BOOTSTRAP / ".terraform.lock.hcl").read_text(encoding="utf-8")

    assert "h1:G1hH2nmuFT7rXMxBhz+s32Xv0BSUNblRqr30FXQIpQc=" in lock
    assert "h1:qAb6Iv9bMxtRev5gv3EPgyybQvNQcx1pVNc6fIFDmSE=" in lock


def test_terraform_roots_format_and_validate_offline() -> None:
    if TERRAFORM is None:
        pytest.skip("set TERRAFORM_BIN to run the pinned-provider validation")
    assert TERRAFORM.is_file(), "TERRAFORM_BIN must name the pinned Terraform binary"
    for root in (FOUNDATION, DURABILITY, BOOTSTRAP, HCP_BOOTSTRAP):
        formatted = subprocess.run(
            [str(TERRAFORM), "fmt", "-check", "-recursive"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert formatted.returncode == 0, formatted.stdout + formatted.stderr
        validated = subprocess.run(
            [str(TERRAFORM), "validate", "-no-color"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert validated.returncode == 0, validated.stdout + validated.stderr
