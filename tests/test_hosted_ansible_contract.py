from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "infra/ansible"
ANSIBLE_PLAYBOOK = (
    Path(os.environ["ANSIBLE_PLAYBOOK_BIN"]) if "ANSIBLE_PLAYBOOK_BIN" in os.environ else None
)


def _read(relative: str) -> str:
    return (ANSIBLE / relative).read_text(encoding="utf-8")


def test_site_playbook_is_idempotent_by_construction_and_never_fetches_admin_state() -> None:
    site = _read("site.yml")
    base = _read("roles/base/tasks/main.yml")
    k3s = _read("roles/k3s/tasks/main.yml")
    combined = "\n".join((site, base, k3s)).lower()

    assert "roles:" in site
    assert "- base" in site
    assert "- k3s" in site
    assert "become: true" in site
    assert "ansible.builtin.command" not in base
    assert "ansible.builtin.shell" not in combined
    assert "ansible.builtin.fetch" not in combined
    assert "ansible.builtin.slurp" not in combined
    assert "notify:" in base
    assert "notify:" in k3s
    assert "no_log: true" in k3s


def test_base_role_hardens_ssh_firewall_time_logging_and_disk_support() -> None:
    defaults = _read("roles/base/defaults/main.yml")
    tasks = _read("roles/base/tasks/main.yml")
    ssh = _read("roles/base/templates/99-exomem-hardening.conf.j2")
    fail2ban = _read("roles/base/templates/exomem-sshd.local.j2")
    journald = _read("roles/base/templates/99-exomem-storage.conf.j2")

    for package in ("cryptsetup", "fail2ban", "ufw", "unattended-upgrades"):
        assert package in defaults
    assert "base_admin_ssh_cidrs" in tasks
    assert "ansible.builtin.apt" in tasks
    assert "ansible.builtin.systemd_service" in tasks
    assert "PermitRootLogin prohibit-password" in ssh
    assert "PasswordAuthentication no" in ssh
    assert "KbdInteractiveAuthentication no" in ssh
    assert "bantime = 1h" in fail2ban
    assert "SystemMaxUse=512M" in journald


def test_k3s_role_pins_binary_and_hardens_single_server_configuration() -> None:
    defaults = _read("roles/k3s/defaults/main.yml")
    tasks = _read("roles/k3s/tasks/main.yml")
    config = _read("roles/k3s/templates/config.yaml.j2")
    service = _read("roles/k3s/templates/k3s.service.j2")
    audit = _read("roles/k3s/files/audit-policy.yaml")
    admission = _read("roles/k3s/files/admission-config.yaml")

    assert 'k3s_version: "v1.35.6+k3s1"' in defaults
    assert "2b52a2c1ca6eb502e2a0ffa1a4cf79eef94875926577c1e43347ed292cc92432" in defaults
    assert "get_url:" in tasks
    assert 'checksum: "sha256:{{ k3s_sha256_amd64 }}"' in tasks
    assert "cluster-init: true" in config
    assert "secrets-encryption: true" in config
    assert "write-kubeconfig-mode: \"0640\"" in config
    assert 'disable:\n  - traefik\n  - servicelb\n  - local-storage' in config
    assert "service-account-max-token-expiration=24h" in config
    assert "image-gc-high-threshold=75" in config
    assert "container-log-max-size=10Mi" in config
    assert "audit-log-path=/var/lib/rancher/k3s/server/logs/audit.log" in config
    assert "admission-control-config-file=/etc/rancher/k3s/admission-config.yaml" in config
    assert "etcd-snapshot-schedule-cron: \"*/30 * * * *\"" in config
    assert "etcd-s3: true" in config
    assert "etcd-s3-secret-key:" in config
    assert "ExecStart=/usr/local/bin/k3s server" in service
    assert "omitStages:" in audit
    assert "kind: PodSecurityConfiguration" in admission
    assert "enforce: baseline" in admission
    assert "audit: restricted" in admission
    assert "warn: restricted" in admission
    assert "- exomem-storage-init" in admission
    assert "- exomem-platform" in admission


def test_inventory_generator_emits_only_non_sensitive_host_coordinates(tmp_path: Path) -> None:
    generator = ROOT / "infra/scripts/generate_ansible_inventory.py"
    terraform_output = tmp_path / "foundation.json"
    inventory = tmp_path / "inventory.yml"
    terraform_output.write_text(
        json.dumps(
            {
                "server_ipv4": {"sensitive": False, "value": "192.0.2.10"},
                "private_node_ip": {"sensitive": False, "value": "10.50.1.10"},
                "access_service_token_client_secret": {
                    "sensitive": True,
                    "value": "must-never-appear",
                },
            }
        ),
        encoding="utf-8",
    )
    terraform_output.chmod(0o600)

    result = subprocess.run(
        [
            "python3",
            str(generator),
            str(terraform_output),
            str(inventory),
            "--user",
            "alpha-admin",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert inventory.stat().st_mode & 0o777 == 0o600
    rendered = inventory.read_text(encoding="utf-8")
    assert "192.0.2.10" in rendered
    assert "10.50.1.10" in rendered
    assert "alpha-admin" in rendered
    assert "must-never-appear" not in rendered
    assert "secret" not in rendered.lower()


def test_ansible_syntax_with_pinned_binary() -> None:
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN to run pinned Ansible syntax validation")
    result = subprocess.run(
        [str(ANSIBLE_PLAYBOOK), "--syntax-check", str(ANSIBLE / "site.yml")],
        cwd=ANSIBLE,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
