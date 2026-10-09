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


def _k3s_tasks() -> str:
    # The role splits into per-mode task files (add-cloud-node-provisioning);
    # contracts about "the k3s role's tasks" hold across all of them.
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ANSIBLE / "roles/k3s/tasks").glob("*.yml"))
    )


def _no_dedicated_hosts(tmp_path: Path) -> str:
    """The explicit empty host list the generator requires when there are none."""
    path = tmp_path / "no-dedicated-hosts.json"
    path.write_text("{}", encoding="utf-8")
    return str(path)


def test_node_memory_qos_is_opt_in_and_renders_the_measured_kubelet_configuration() -> None:
    # Catch accidental shared-node activation and a misrendered runtime bundle;
    # the live isolated gate separately proves effective kernel controls.
    import yaml
    defaults = yaml.safe_load(_read("roles/k3s/defaults/main.yml"))
    assert defaults["k3s_memory_qos_enabled"] is False
    config = yaml.safe_load(_read("roles/k3s/files/20-exomem-memoryqos.conf"))
    assert config["apiVersion"] == "kubelet.config.k8s.io/v1beta1"
    assert config["kind"] == "KubeletConfiguration"
    assert config["featureGates"] == {"MemoryQoS": True}
    assert config["memoryThrottlingFactor"] == 0.625


def test_site_playbook_is_idempotent_by_construction_and_never_fetches_admin_state() -> None:
    site = _read("site.yml")
    k3s = _k3s_tasks()
    combined = "\n".join((site, k3s)).lower()
    assert "substrate.infrastructure.base" in site
    assert "- k3s" in site
    assert "become: true" in site
    assert "ansible.builtin.shell" not in combined
    assert "ansible.builtin.fetch" not in combined
    assert "ansible.builtin.slurp" not in combined
    assert "notify:" in k3s
    assert "no_log: true" in k3s


def test_k3s_role_pins_binary_and_hardens_single_server_configuration() -> None:
    defaults = _read("roles/k3s/defaults/main.yml")
    tasks = _k3s_tasks()
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
    # harden-exomem-cloud-operator-access D4/2.6: the admin kubeconfig is
    # root-only. The operators group holds the administrator login, which
    # reaches cluster-admin only through sudo.
    assert 'write-kubeconfig-mode: "0600"' in config
    assert "write-kubeconfig-group" not in config
    # K3s rewrites k3s.yaml in place and keeps its old group, so a node that
    # once wrote it to the operators group keeps that group until the role
    # resets the file itself, after K3s has started and written it.
    import yaml

    server = yaml.safe_load(_read("roles/k3s/tasks/server.yml"))
    names = [task["name"] for task in server]
    (reset,) = [
        task for task in server
        if task.get("ansible.builtin.file", {}).get("path") == "/etc/rancher/k3s/k3s.yaml"
    ]
    assert reset["ansible.builtin.file"] == {
        "path": "/etc/rancher/k3s/k3s.yaml", "owner": "root", "group": "root", "mode": "0600",
    }
    assert names.index(reset["name"]) > names.index("Wait for the local Kubernetes API readiness endpoint")
    assert "disable:\n  - traefik\n  - servicelb\n  - local-storage" in config
    assert "service-account-max-token-expiration=24h" in config
    assert "audit-log-path=/var/lib/rancher/k3s/server/logs/audit.log" in config
    assert "admission-control-config-file=/etc/rancher/k3s/admission-config.yaml" in config
    assert 'etcd-snapshot-schedule-cron: "*/30 * * * *"' in config
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


def test_k3s_role_resolves_the_private_interface_from_the_declared_node_ip() -> None:
    defaults = _read("roles/k3s/defaults/main.yml")
    tasks = _k3s_tasks()
    config = _read("roles/k3s/templates/config.yaml.j2")

    assert 'k3s_private_interface: ""' in defaults
    assert "k3s_private_interface_candidates" in tasks
    assert 'selectattr("ipv4.address", "equalto", private_node_ip)' in tasks
    assert "k3s_private_interface_candidates | length == 1" in tasks
    assert "k3s_resolved_private_interface" in tasks
    assert "flannel-iface: {{ k3s_resolved_private_interface | to_json }}" in config


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
            "--dedicated-hosts",
            _no_dedicated_hosts(tmp_path),
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
    parsed = json.loads(rendered)
    assert "_meta" not in parsed
    assert parsed["all"]["children"]["hosted_nodes"]["hosts"]["exomem-alpha"] == {
        "ansible_host": "192.0.2.10",
        "ansible_user": "alpha-admin",
        "private_node_ip": "10.50.1.10",
    }


def test_exomem_entrypoint_never_targets_the_database_in_an_old_combined_inventory(tmp_path: Path) -> None:
    # The old inventory can survive retirement; product hardening must still leave the shared DB alone.
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN to run pinned Ansible")
    inventory = tmp_path / "old-inventory.json"
    inventory.write_text(json.dumps({"all": {"children": {
        "hosted_nodes": {"hosts": {"fleet": {}}},
        "control_nodes": {"hosts": {"shared-database": {}}},
    }}}))
    result = subprocess.run([str(ANSIBLE_PLAYBOOK), "-i", str(inventory), str(ANSIBLE / "site.yml"), "--list-hosts"],
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "fleet" in result.stdout
    assert "shared-database" not in result.stdout


def test_inventory_generator_does_not_emit_the_shared_database_host(
    tmp_path: Path,
) -> None:
    generator = ROOT / "infra/scripts/generate_ansible_inventory.py"
    terraform_output = tmp_path / "foundation.json"
    inventory = tmp_path / "inventory.yml"
    terraform_output.write_text(
        json.dumps(
            {
                "server_ipv4": {"sensitive": False, "value": "192.0.2.10"},
                "private_node_ip": {"sensitive": False, "value": "10.50.1.10"},
                "control_db_server_ipv4": {"sensitive": False, "value": "192.0.2.20"},
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
            "--dedicated-hosts",
            _no_dedicated_hosts(tmp_path),
            "--user",
            "alpha-admin",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(inventory.read_text(encoding="utf-8"))
    assert "control_nodes" not in parsed["all"]["children"]
    assert "substrate-control-01" not in inventory.read_text()


def test_inventory_generator_addresses_hosts_by_their_administration_address(
    tmp_path: Path,
) -> None:
    generator = ROOT / "infra/scripts/generate_ansible_inventory.py"
    terraform_output = tmp_path / "foundation.json"
    terraform_output.write_text(
        json.dumps(
            {
                "server_ipv4": {"sensitive": False, "value": "192.0.2.10"},
                "private_node_ip": {"sensitive": False, "value": "10.50.1.10"},
                "control_db_server_ipv4": {"sensitive": False, "value": "192.0.2.20"},
            }
        ),
        encoding="utf-8",
    )
    terraform_output.chmod(0o600)
    addresses = tmp_path / "admin-addresses.json"

    # One private map serves every flow, including ones whose inventory omits
    # some of the hosts it names.
    addresses.write_text(
        json.dumps({"exomem-alpha": "100.64.0.10", "exomem-agent-01": "100.64.0.31"}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "python3",
            str(generator),
            str(terraform_output),
            str(tmp_path / "inventory.json"),
            "--dedicated-hosts",
            _no_dedicated_hosts(tmp_path),
            "--admin-addresses",
            str(addresses),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    children = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))[
        "all"
    ]["children"]
    assert children["hosted_nodes"]["hosts"]["exomem-alpha"]["ansible_host"] == "100.64.0.10"
    assert "control_nodes" not in children


def test_a_module_result_cannot_shadow_an_inventory_variable(tmp_path: Path) -> None:
    # Root on any host can make a module return arbitrary ansible_facts. Peers
    # render inventory variables such as private_node_ip into wg-quick files
    # they execute as root, so a returned fact must never replace one.
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN to run pinned Ansible")
    library = tmp_path / "library"
    library.mkdir()
    (library / "hostile_facts.py").write_text(
        "#!/usr/bin/python3\nimport json\n"
        "print(json.dumps({'changed': False, 'ansible_facts': "
        "{'private_node_ip': '10.0.0.9/32 dev wgexomem; touch /tmp/owned; true'}}))\n",
        encoding="utf-8",
    )
    (tmp_path / "inventory.ini").write_text(
        "node ansible_connection=local private_node_ip=10.0.0.1\n", encoding="utf-8"
    )
    (tmp_path / "play.yml").write_text(
        "- hosts: node\n  gather_facts: false\n  become: false\n  tasks:\n"
        "    - hostile_facts: {}\n"
        "    - ansible.builtin.assert: {that: \"private_node_ip == '10.0.0.1'\"}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(ANSIBLE_PLAYBOOK), "-i", "inventory.ini", "play.yml"],
        cwd=tmp_path,
        env={**os.environ, "ANSIBLE_CONFIG": str(ANSIBLE / "ansible.cfg"),
             "ANSIBLE_LIBRARY": str(library)},
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
