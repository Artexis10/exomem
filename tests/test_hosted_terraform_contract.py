from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FOUNDATION = ROOT / "infra/terraform/foundation"
DURABILITY = ROOT / "infra/terraform/durability"
BOOTSTRAP = ROOT / "infra/terraform/bootstrap"
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

    assert re.search(r'key\s*=\s*"foundation/terraform\.tfstate"', foundation)
    assert re.search(r'key\s*=\s*"durability/terraform\.tfstate"', durability)
    assert re.search(r"use_lockfile\s*=\s*true", foundation)
    assert re.search(r"use_lockfile\s*=\s*true", durability)
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

    assert 'key_name     = "exomem-recovery-upload"' in storage
    assert 'capabilities = ["listBuckets", "listFiles", "writeFiles"]' in storage
    assert 'key_name     = "exomem-recovery-restore"' in storage
    assert 'capabilities = ["listBuckets", "listFiles", "readFiles"]' in storage
    assert 'key_name     = "exomem-recovery-delete"' in storage
    assert 'capabilities = ["deleteFiles", "listBuckets", "listFiles"]' in storage
    assert 'key_name     = "exomem-database-backup"' in storage
    assert "deleteFiles" not in storage.split('key_name     = "exomem-database-backup"', 1)[1]

    for secret_output in (
        "recovery_upload_application_key",
        "recovery_restore_application_key",
        "recovery_delete_application_key",
        "database_backup_application_key",
    ):
        block = outputs.split(f'output "{secret_output}"', 1)[1].split("}", 1)[0]
        assert re.search(r"sensitive\s*=\s*true", block)


def test_bootstrap_versions_remote_state_and_splits_backend_identities() -> None:
    bootstrap = _all_tf(BOOTSTRAP)

    assert 'resource "b2_bucket" "terraform_state"' in bootstrap
    assert 'bucket_type = "allPrivate"' in bootstrap
    assert 'mode      = "SSE-B2"' in bootstrap
    assert 'algorithm = "AES256"' in bootstrap
    assert "days_from_hiding_to_deleting = 30" in bootstrap
    assert "days_from_uploading_to_hiding" not in bootstrap
    assert "prevent_destroy = true" in bootstrap

    assert bootstrap.count('resource "b2_application_key"') == 2
    assert 'key_name     = "exomem-terraform-foundation"' in bootstrap
    assert 'name_prefix  = "foundation/"' in bootstrap
    assert 'key_name     = "exomem-terraform-durability"' in bootstrap
    assert 'name_prefix  = "durability/"' in bootstrap
    assert (
        bootstrap.count(
            'capabilities = ["deleteFiles", "listBuckets", "listFiles", "readFiles", "writeFiles"]'
        )
        == 1
    )
    assert bootstrap.count("capabilities = local.state_capabilities") == 2

    outputs = (BOOTSTRAP / "outputs.tf").read_text(encoding="utf-8")
    for secret_output in (
        "foundation_backend_application_key",
        "durability_backend_application_key",
    ):
        block = outputs.split(f'output "{secret_output}"', 1)[1].split("}", 1)[0]
        assert re.search(r"sensitive\s*=\s*true", block)


def test_plan_wrapper_requires_private_backend_config() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    for script_name in ("plan.sh", "apply_saved_plan.sh"):
        script = (ROOT / f"infra/scripts/{script_name}").read_text(encoding="utf-8")
        assert 'backend_config="${TF_BACKEND_CONFIG_FILE:-}"' in script
        assert 'stat -c "%a"' in script
        assert "backend config must have mode 0600" in script
        assert '-backend-config="${backend_config}"' in script
        assert "AWS_ACCESS_KEY_ID" in script
        assert "AWS_SECRET_ACCESS_KEY" in script
    assert "*.tfbackend" in gitignore


def test_backend_bootstrap_seals_local_state_and_uses_saved_plans() -> None:
    script = (ROOT / "infra/scripts/bootstrap_backend.sh").read_text(encoding="utf-8")

    assert 'case "${action}" in' in script
    assert "plan|apply" in script
    assert '-state="${state_path}"' in script
    assert "inspect_terraform_plan.py" in script
    assert "plaintext_safe_to_remove=false" in script
    assert "SOPS_AGE_RECIPIENTS" in script
    assert '"${sops_bin}" encrypt' in script
    assert '"${sops_bin}" decrypt "${encrypted_tmp}" >/dev/null' in script
    assert 'mv -f -- "${encrypted_tmp}" "${escrow_path}"' in script


def test_terraform_roots_format_and_validate_offline() -> None:
    if TERRAFORM is None:
        pytest.skip("set TERRAFORM_BIN to run the pinned-provider validation")
    assert TERRAFORM.is_file(), "TERRAFORM_BIN must name the pinned Terraform binary"
    for root in (FOUNDATION, DURABILITY, BOOTSTRAP):
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
