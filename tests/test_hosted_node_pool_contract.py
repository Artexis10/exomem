"""Contract for Exomem Cloud K3s agent nodes (openspec add-cloud-node-provisioning).

Static checks over the Terraform root, the Ansible roles and playbooks, the
inventory generator, the platform chart and the validation script. The
Terraform module's own behaviour is proven by its mocked-provider
`terraform test` suite; the K3s join and drain by the gated containerised
test in test_hosted_k3s_agent_join.py.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FOUNDATION = ROOT / "infra/terraform/foundation"
MODULE = FOUNDATION / "modules/k3s-agents"
ANSIBLE = ROOT / "infra/ansible"
K3S_ROLE = ANSIBLE / "roles/k3s"
TERRAFORM = Path(os.environ["TERRAFORM_BIN"]) if "TERRAFORM_BIN" in os.environ else None
ANSIBLE_PLAYBOOK = (
    Path(os.environ["ANSIBLE_PLAYBOOK_BIN"]) if "ANSIBLE_PLAYBOOK_BIN" in os.environ else None
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _role_tasks() -> str:
    return "\n".join(_read(path) for path in sorted((K3S_ROLE / "tasks").glob("*.yml")))


def _yaml(path: Path):
    return yaml.safe_load(_read(path))


def _block(text: str, header: str) -> str:
    return text.split(header, 1)[1].split("\nresource ", 1)[0]


# --- Terraform --------------------------------------------------------------


def test_foundation_adds_agents_from_one_map_variable_defaulting_to_empty() -> None:
    variables = _read(FOUNDATION / "variables.tf")
    compute = _read(FOUNDATION / "compute.tf")
    outputs = _read(FOUNDATION / "outputs.tf")

    block = variables.split('variable "k3s_agent_nodes"', 1)[1].split("\nvariable ", 1)[0]
    assert "map(object({" in block
    assert "private_ip  = string" in block
    assert "server_type = string" in block
    assert "default = {}" in block

    module = compute.split('module "k3s_agents"', 1)[1]
    assert 'source = "./modules/k3s-agents"' in module
    assert "nodes                = var.k3s_agent_nodes" in module
    # The existing subnet, location, image, key and admin CIDRs: never a
    # second network or a per-agent location (volumes attach only in-location).
    assert "subnet_id            = hcloud_network_subnet.alpha.id" in module
    assert "subnet_cidr          = var.private_subnet_cidr" in module
    assert "location             = var.server_location" in module
    assert "image                = var.server_image" in module
    assert "ssh_key_ids          = [hcloud_ssh_key.admin.id]" in module
    assert "admin_ssh_cidrs      = var.admin_ssh_cidrs" in module
    # Both addresses already on the subnet are reserved against agents.
    assert (
        "reserved_private_ips = [var.private_node_ip, var.control_db_private_ip]" in module
    )
    assert compute.count('resource "hcloud_network"') == 1
    assert compute.count('resource "hcloud_network_subnet"') == 1

    output = outputs.split('output "k3s_agent_nodes"', 1)[1].split("\noutput ", 1)[0]
    assert "module.k3s_agents.nodes" in output
    assert not re.search(r"^\s*sensitive\s*=", output, re.M)


def test_agent_module_is_keyed_disposable_and_exposes_only_443_and_admin_ssh() -> None:
    main = _read(MODULE / "main.tf")
    variables = _read(MODULE / "variables.tf")

    server = _block(main, 'resource "hcloud_server" "agent"')
    assert "for_each = var.nodes" in server
    assert "count" not in server
    assert "firewall_ids             = [hcloud_firewall.agents.id]" in server
    assert "location                 = var.location" in server
    assert "subnet_id = var.subnet_id" in server
    assert "ip        = each.value.private_ip" in server
    assert "delete_protection        = false" in server
    assert "rebuild_protection       = false" in server
    assert "shutdown_before_deletion = true" in server
    assert "ipv6_enabled = false" in server
    assert "prevent_destroy" not in main
    assert "user_data" not in main

    firewall = _block(main, 'resource "hcloud_firewall" "agents"')
    ports = re.findall(r'port\s*=\s*"([^"]+)"', firewall)
    assert ports == ["22", "443"]
    assert firewall.count("rule {") == 2
    ssh = firewall.split('port        = "22"', 1)[1].split("}", 1)[0]
    assert "source_ips  = var.admin_ssh_cidrs" in ssh
    for forbidden in ('"6443"', '"10250"', '"8472"', '"80"', '"5432"'):
        assert forbidden not in main

    assert "cidrhost(var.subnet_cidr, 0)" in variables
    assert "!contains(var.reserved_private_ips, node.private_ip)" in variables
    assert '["cpx42", "ccx23", "ccx33", "ccx43"]' in variables


def test_agent_module_carries_an_exact_hcloud_lock_and_its_own_offline_tests() -> None:
    module_lock = _read(MODULE / ".terraform.lock.hcl")
    root_lock = _read(FOUNDATION / ".terraform.lock.hcl")
    block = re.search(
        r'provider "registry.terraform.io/hetznercloud/hcloud" \{.*?\n\}\n', root_lock, re.S
    )
    assert block is not None
    assert block.group(0) in module_lock
    assert "cloudflare" not in module_lock

    suite = _read(MODULE / "tests/agents.tftest.hcl")
    assert 'mock_provider "hcloud" {}' in suite
    assert suite.count("expect_failures = [var.nodes]") >= 6
    root_suite = _read(FOUNDATION / "tests/k3s_agents.tftest.hcl")
    for provider in ("hcloud", "cloudflare", "random", "local"):
        assert f'mock_provider "{provider}" {{}}' in root_suite


def test_validate_script_formats_validates_and_tests_the_agent_module() -> None:
    script = _read(ROOT / "infra/scripts/validate.sh")
    assert "foundation/modules/k3s-agents" in script
    assert re.search(r'"\$\{terraform_bin\}" -chdir=[^\n]*foundation[^\n]* test', script)
    assert '"${terraform_bin}" -chdir="${agent_module}" test' in script
    assert 'agent_module="${infra_dir}/terraform/foundation/modules/k3s-agents"' in script
    assert "remove-agent.yml" in script


# --- Ansible: site and inventory --------------------------------------------


def test_site_hardens_every_node_first_then_runs_server_then_agents() -> None:
    plays = _yaml(ANSIBLE / "site.yml")
    hosts = [play["hosts"] for play in plays]
    assert hosts[:3] == ["hosted_nodes", "hosted_nodes:!k3s_agents", "k3s_agents"]

    harden, server, agents = plays[:3]
    # Same hardening for every K3s node, and every node's inter-node firewall
    # converged before any agent joins (a join requires its peers to admit it).
    assert harden["roles"] == ["base"]
    assert harden["tasks"][0]["ansible.builtin.include_role"] == {
        "name": "k3s",
        "tasks_from": "inter_node.yml",
    }
    assert server["roles"] == ["k3s"]
    assert agents["roles"] == ["k3s"]
    assert agents["vars"] == {"k3s_node_role": "agent"}
    for play in (harden, server, agents):
        assert play["serial"] == 1
        assert play["become"] is True
        assert play["any_errors_fatal"] is True


def test_server_play_never_matches_an_agent_and_agent_files_hold_no_server_secret() -> None:
    defaults = _read(K3S_ROLE / "defaults/main.yml")
    assert (
        "k3s_node_role: \"{{ 'agent' if inventory_hostname in (groups['k3s_agents'] | default([]))"
        " else 'server' }}\"" in defaults
    )
    for relative in (
        "tasks/agent.yml",
        "templates/agent-config.yaml.j2",
        "templates/k3s-agent.service.j2",
        "tasks/remove_stop.yml",
    ):
        text = _read(K3S_ROLE / relative)
        assert "k3s_server_token" not in text, relative
        assert "k3s_etcd_" not in text, relative


def test_inventory_example_nests_agents_under_hosted_nodes() -> None:
    inventory = _yaml(ANSIBLE / "inventory.example.yml")
    hosted = inventory["all"]["children"]["hosted_nodes"]
    assert "exomem-alpha" in hosted["hosts"]
    agents = hosted["children"]["k3s_agents"]["hosts"]
    assert agents
    for coordinates in agents.values():
        assert set(coordinates) == {"ansible_host", "ansible_user", "private_node_ip"}


def test_inventory_generator_emits_agents_without_sensitive_values(tmp_path: Path) -> None:
    generator = ROOT / "infra/scripts/generate_ansible_inventory.py"
    terraform_output = tmp_path / "foundation.json"
    inventory = tmp_path / "inventory.yml"
    terraform_output.write_text(
        json.dumps(
            {
                "server_ipv4": {"sensitive": False, "value": "192.0.2.10"},
                "private_node_ip": {"sensitive": False, "value": "10.50.1.10"},
                "k3s_agent_nodes": {
                    "sensitive": False,
                    "value": {
                        "01": {
                            "name": "exomem-agent-01",
                            "ipv4": "192.0.2.31",
                            "private_ip": "10.50.1.31",
                        },
                        "02": {
                            "name": "exomem-agent-02",
                            "ipv4": "192.0.2.32",
                            "private_ip": "10.50.1.32",
                        },
                    },
                },
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
        ["python3", str(generator), str(terraform_output), str(inventory), "--user", "ops"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    rendered = inventory.read_text(encoding="utf-8")
    assert "must-never-appear" not in rendered
    parsed = json.loads(rendered)
    hosted = parsed["all"]["children"]["hosted_nodes"]
    assert hosted["hosts"]["exomem-alpha"]["private_node_ip"] == "10.50.1.10"
    assert hosted["children"]["k3s_agents"]["hosts"] == {
        "exomem-agent-01": {
            "ansible_host": "192.0.2.31",
            "ansible_user": "ops",
            "private_node_ip": "10.50.1.31",
        },
        "exomem-agent-02": {
            "ansible_host": "192.0.2.32",
            "ansible_user": "ops",
            "private_node_ip": "10.50.1.32",
        },
    }


@pytest.mark.parametrize(
    "agents",
    [
        {"01": {"name": "exomem-agent-01", "ipv4": "not-an-ip", "private_ip": "10.50.1.31"}},
        {"01": {"name": "exomem-alpha", "ipv4": "192.0.2.31", "private_ip": "10.50.1.31"}},
        {"01": {"name": "Bad Name", "ipv4": "192.0.2.31", "private_ip": "10.50.1.31"}},
    ],
)
def test_inventory_generator_refuses_malformed_agents(tmp_path: Path, agents: dict) -> None:
    generator = ROOT / "infra/scripts/generate_ansible_inventory.py"
    terraform_output = tmp_path / "foundation.json"
    terraform_output.write_text(
        json.dumps(
            {
                "server_ipv4": {"sensitive": False, "value": "192.0.2.10"},
                "private_node_ip": {"sensitive": False, "value": "10.50.1.10"},
                "k3s_agent_nodes": {"sensitive": False, "value": agents},
            }
        ),
        encoding="utf-8",
    )
    terraform_output.chmod(0o600)
    result = subprocess.run(
        ["python3", str(generator), str(terraform_output), str(tmp_path / "inventory.yml")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not (tmp_path / "inventory.yml").exists()


def test_inventory_generator_refuses_sensitive_agent_output(tmp_path: Path) -> None:
    generator = ROOT / "infra/scripts/generate_ansible_inventory.py"
    terraform_output = tmp_path / "foundation.json"
    terraform_output.write_text(
        json.dumps(
            {
                "server_ipv4": {"sensitive": False, "value": "192.0.2.10"},
                "private_node_ip": {"sensitive": False, "value": "10.50.1.10"},
                "k3s_agent_nodes": {"sensitive": True, "value": {}},
            }
        ),
        encoding="utf-8",
    )
    terraform_output.chmod(0o600)
    result = subprocess.run(
        ["python3", str(generator), str(terraform_output), str(tmp_path / "inventory.yml")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


# --- Ansible: the k3s role --------------------------------------------------


def test_agent_configuration_carries_no_server_grade_secret() -> None:
    agent = _read(K3S_ROLE / "templates/agent-config.yaml.j2")
    assert "token: {{ k3s_agent_join_token | to_json }}" in agent
    assert 'server: {{ ("https://" ~ k3s_server_private_ip ~ ":6443") | to_json }}' in agent
    for forbidden in (
        "k3s_server_token",
        "cluster-init",
        "etcd",
        "secrets-encryption",
        "kube-apiserver-arg",
        "write-kubeconfig",
        "agent-token",
    ):
        assert forbidden not in agent, forbidden
    assert "node-name: {{ inventory_hostname | to_json }}" in agent
    assert "node-ip: {{ private_node_ip | to_json }}" in agent
    assert "flannel-iface: {{ k3s_resolved_private_interface | to_json }}" in agent
    assert "protect-kernel-defaults: true" in agent
    assert "{{ k3s_agent_node_label | to_json }}" in agent


def test_agent_presents_the_ca_pinned_secure_token_format() -> None:
    agent = _read(K3S_ROLE / "tasks/agent.yml")
    assert "path: /var/lib/rancher/k3s/server/tls/server-ca.crt" in agent
    assert "checksum_algorithm: sha256" in agent
    assert "'K10' ~ k3s_server_ca.stat.checksum ~ '::node:' ~ k3s_agent_token" in agent
    # Only the checksum leaves the server; never the certificate or a token file.
    assert "slurp" not in agent and "fetch" not in agent
    assert "/var/lib/rancher/k3s/server/agent-token" not in agent


def test_agent_play_refuses_a_host_that_removal_marked() -> None:
    validate = _read(K3S_ROLE / "tasks/validate.yml")
    stop = _read(K3S_ROLE / "tasks/remove_stop.yml")
    assert "path: /etc/rancher/k3s/removed" in validate
    assert "not k3s_removed_marker.stat.exists" in validate
    assert "dest: /etc/rancher/k3s/removed" in stop


def _kubelet_args(text: str) -> list[str]:
    block = text.split("kubelet-arg:\n", 1)[1]
    arguments = []
    for line in block.splitlines():
        if not line.startswith("  - "):
            break
        arguments.append(line[4:])
    return arguments


def test_agent_kubelet_limits_match_the_server_exactly() -> None:
    server = _kubelet_args(_read(K3S_ROLE / "templates/config.yaml.j2"))
    agent = _kubelet_args(_read(K3S_ROLE / "templates/agent-config.yaml.j2"))
    assert "image-gc-high-threshold=75" in server
    assert server == agent


def test_server_gains_a_distinct_agent_token_only_when_set() -> None:
    server = _read(K3S_ROLE / "templates/config.yaml.j2")
    tasks = _role_tasks()
    assert "{% if k3s_agent_token | length > 0 %}" in server
    assert "agent-token: {{ k3s_agent_token | to_json }}" in server
    assert "k3s_agent_token != k3s_server_token" in tasks
    assert "k3s_agent_token | length >= 32" in tasks
    # Agents in the inventory make the agent token mandatory on the server.
    assert "groups['k3s_agents'] | default([]) | length == 0" in tasks


def test_role_dispatches_on_node_role_and_restarts_the_right_unit() -> None:
    defaults = _read(K3S_ROLE / "defaults/main.yml")
    main = _read(K3S_ROLE / "tasks/main.yml")
    handlers = _read(K3S_ROLE / "handlers/main.yml")
    unit = _read(K3S_ROLE / "templates/k3s-agent.service.j2")

    assert 'k3s_agent_token: ""' in defaults
    assert "k3s_agent_require_csi_capacity: true" in defaults
    assert "k3s_csi_driver: csi.hetzner.cloud" in defaults
    assert "k3s_remove_headroom: 5" in defaults
    assert "k3s_cell_namespace_prefix: exo-cell-" in defaults
    assert "k3s_node_role in ['server', 'agent']" in main
    assert "server.yml" in main and "agent.yml" in main
    assert "{{ k3s_service_name }}" in handlers
    assert "ExecStart=/usr/local/bin/k3s agent --config /etc/rancher/k3s/config.yaml" in unit


def test_agent_join_waits_for_ready_and_published_attach_capacity() -> None:
    agent = _read(K3S_ROLE / "tasks/agent.yml")
    assert "delegate_to: \"{{ k3s_server_host }}\"" in agent
    assert "Ready" in agent
    assert "csinode" in agent
    assert "allocatable" in agent
    assert "csidriver" in agent
    assert "k3s_csi_driver_present.stdout | length > 0" in agent
    # A --limit or partial run fails loudly instead of breaking the overlay.
    assert "Require every other K3s node to admit this agent" in agent
    assert "- show\n      - added" in agent
    assert "--overwrite" in agent
    # A converged node relabels to "not labeled": no change reported.
    assert "'not labeled' not in" in agent
    assert "no_log: true" in agent


def test_inter_node_firewall_names_peer_addresses_never_the_subnet() -> None:
    firewall = _read(K3S_ROLE / "tasks/firewall.yml")
    defaults = _read(K3S_ROLE / "defaults/main.yml")
    assert "community.general.ufw" in firewall
    assert "map('extract', hostvars, 'private_node_ip')" in defaults
    assert "k3s_firewall_peer_ips" in firewall
    assert "interface: \"{{ k3s_resolved_private_interface }}\"" in firewall
    assert "direction: in" in firewall
    for port, proto in (("8472", "udp"), ("10250", "tcp"), ("6443", "tcp")):
        assert f'port: "{port}"' in firewall
        assert f"proto: {proto}" in firewall
    assert "10.50.1.0/24" not in firewall
    assert "private_subnet" not in firewall
    # Stale commented rules are pruned, so the set converges to the inventory.
    assert "k3s_firewall_stale" in firewall
    assert "delete: true" in firewall
    assert "comment: \"{{ k3s_firewall_comment }}\"" in firewall
    # 6443 is admitted only on the server.
    api_task = next(task for task in firewall.split("- name:") if 'port: "6443"' in task)
    assert "k3s_node_role == 'server'" in api_task


# --- Ansible: removal --------------------------------------------------------


def _tasks_in_order(plays: list[dict]) -> list[dict]:
    ordered: list[dict] = []

    def walk(tasks: list[dict]) -> None:
        for task in tasks:
            ordered.append(task)
            walk(task.get("block", []))

    for play in plays:
        walk(play.get("tasks", []))
    return ordered


def test_remove_agent_playbook_orders_its_steps_and_never_forces() -> None:
    plays = _yaml(ANSIBLE / "remove-agent.yml")
    names = [task["name"] for task in _tasks_in_order(plays)]
    order = [
        "Refuse anything but one inventoried agent",
        "Refuse a node that is not an agent",
        "Refuse unless the cells fit the remaining nodes",
        "Cordon the agent",
        "Drain the agent without force",
        "Stop the agent completely",
        "Require a confirmed stop before deleting a present node",
        "Taint the stopped node out of service",
        "Wait until no volume is attached to the node",
        "Delete the node",
        "Confirm the node stays absent",
        "Converge the inter-node firewall without the removed agent",
    ]
    assert [name for name in names if name in order] == order

    tasks = {task["name"]: task for task in _tasks_in_order(plays)}
    argv = tasks["Drain the agent without force"]["ansible.builtin.command"]["argv"]
    assert "--ignore-daemonsets" in argv
    assert "--delete-emptydir-data" in argv
    assert any(argument.startswith("--timeout=") for argument in argv)
    assert "--force" not in argv
    assert "--disable-eviction" not in argv
    assert "Ready" in tasks["Drain the agent without force"]["when"]

    stop = tasks["Stop the agent completely"]
    assert stop["ansible.builtin.include_role"]["apply"] == {"ignore_unreachable": True}
    confirm = tasks["Require a confirmed stop before deleting a present node"]
    assert "k3s_remove_host_gone" in confirm["ansible.builtin.assert"]["that"][0]
    assert "k3s_remove_stop_confirmed" in confirm["ansible.builtin.assert"]["that"][0]
    taint = tasks["Taint the stopped node out of service"]["ansible.builtin.command"]["argv"]
    assert "node.kubernetes.io/out-of-service=nodeshutdown:NoExecute" in taint
    delete = tasks["Delete the node"]["ansible.builtin.command"]["argv"]
    assert "--ignore-not-found" in delete
    revoke = tasks["Converge the inter-node firewall without the removed agent"]
    assert "k3s_remove_node" in revoke["vars"]["k3s_firewall_peer_ips"]


def test_remove_stop_kills_containers_unmounts_and_closes_volume_mappings() -> None:
    stop = _read(K3S_ROLE / "tasks/remove_stop.yml")
    defaults = _read(K3S_ROLE / "defaults/main.yml")
    assert "state: stopped" in stop and "enabled: false" in stop
    assert "- containerd-shim" in stop and "/usr/bin/pkill" in stop
    assert "/usr/bin/umount" in stop
    assert "k3s_remove_pod_mount_pattern" in stop
    assert "k3s_remove_csi_mount_pattern" in stop
    assert "k3s_remove_pod_mount_pattern: ^/var/lib/kubelet/pods/" in defaults
    assert "k3s_remove_csi_mount_pattern: ^/var/lib/kubelet/plugins/kubernetes.io/csi/" in defaults
    assert "/usr/sbin/cryptsetup" in stop and "- close" in stop
    assert "k3s_remove_stop_confirmed" in stop
    assert "- -umount" not in stop


def test_remove_preflight_refuses_when_other_nodes_lack_attachment_slots() -> None:
    preflight = _read(K3S_ROLE / "tasks/remove_preflight.yml")
    assert "unschedulable" in preflight
    assert "allocatable" in preflight
    assert "k3s_remove_headroom" in preflight
    assert "k3s_cell_namespace_prefix" in preflight
    assert "exomem.io/hold" in preflight
    assert "k3s_remove_cell_volumes | int <= k3s_remove_remaining_slots | int - k3s_remove_target_non_cell | int" in preflight


# --- Helm and secrets ---------------------------------------------------------


def test_traefik_is_pinned_to_the_control_plane_node() -> None:
    values = _yaml(ROOT / "infra/helm/platform/values.yaml")
    assert values["traefik"]["nodeSelector"] == {"node-role.kubernetes.io/control-plane": "true"}
    # One pinned hostPort replica can only roll by replacement.
    assert values["traefik"]["updateStrategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0},
    }


def test_agent_token_is_a_registered_sops_destination_beside_the_server_token() -> None:
    matrix = json.loads(_read(ROOT / "infra/contracts/secret-destinations-v1.json"))
    destinations = matrix["secrets"]["k3s_agent_token"]["destinations"]
    ansible = destinations["ansible.hosted-node.k3s-agent-token.active"]
    assert ansible == {
        "kind": "sops_ansible_vars",
        "slot": "active",
        "target": "infra/secrets/ansible/k3s-agent-token.{version}.sops.json",
        "variable": "k3s_agent_token",
    }
    assert destinations["escrow.k3s-agent-token.active"]["kind"] == "sops_escrow"


def test_group_vars_example_declares_the_agent_token_placeholder() -> None:
    example = _read(ANSIBLE / "group_vars/hosted_nodes.example.yml")
    assert "k3s_agent_token:" in example


# --- Pinned-binary checks -------------------------------------------------------


def test_node_pool_playbooks_pass_syntax_check_with_pinned_binary() -> None:
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN to run pinned Ansible syntax validation")
    for playbook in ("site.yml", "remove-agent.yml"):
        result = subprocess.run(
            [
                str(ANSIBLE_PLAYBOOK),
                "--syntax-check",
                str(ANSIBLE / playbook),
                "-e",
                "k3s_remove_node=exomem-agent-01",
            ],
            cwd=ANSIBLE,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_agent_module_terraform_tests_pass_offline() -> None:
    if TERRAFORM is None:
        pytest.skip("set TERRAFORM_BIN to run the mocked-provider module tests")
    result = subprocess.run(
        [str(TERRAFORM), "test", "-no-color"],
        cwd=MODULE,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
