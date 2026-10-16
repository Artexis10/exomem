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


def _no_dedicated_hosts(tmp_path: Path) -> str:
    """The explicit empty host list the generator requires when there are none."""
    path = tmp_path / "no-dedicated-hosts.json"
    path.write_text("{}", encoding="utf-8")
    return str(path)


# --- Terraform --------------------------------------------------------------


def test_foundation_adds_agents_from_one_map_variable_defaulting_to_empty() -> None:
    variables = _read(FOUNDATION / "variables.tf")
    compute = _read(FOUNDATION / "compute.tf")
    outputs = _read(FOUNDATION / "outputs.tf")

    block = variables.split('variable "k3s_agent_nodes"', 1)[1].split("\nvariable ", 1)[0]
    assert "map(object({" in block
    assert re.search(r"private_ip\s*= string", block)
    assert re.search(r"server_type\s*= string", block)
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
    # The fleet server's address is reserved against agents; the control host
    # is not on this subnet.
    assert "reserved_private_ips = [var.private_node_ip]" in module
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
    assert hosts[:6] == [
        "hosted_nodes",
        "hosted_nodes",
        "hosted_nodes:!k3s_agents",
        "hosted_nodes:!k3s_agents",
        "dedicated_hosts",
        "k3s_agents",
    ]

    link, harden, server, tang, storage, agents = plays[:6]
    # Every WireGuard key exists before any node lists its peers only when the
    # private-link play runs each task on every node: no serial.
    assert "serial" not in link
    assert link["tasks"][0]["ansible.builtin.include_role"] == {
        "name": "k3s",
        "tasks_from": "private_link.yml",
    }
    # Tang serves before a dedicated agent binds to it, and that agent's
    # storage is unlocked before its K3s agent starts.
    assert storage["roles"] == ["dedicated_host"]
    # Several storage tasks pass the recovery passphrase on stdin; without
    # pipelining it lands in module files on the host's unencrypted root.
    assert storage["vars"]["ansible_pipelining"] is True
    # Same hardening for every K3s node, and every node's inter-node firewall
    # converged before any agent joins (a join requires its peers to admit it).
    assert harden["roles"] == ["substrate.infrastructure.base"]
    assert harden["tasks"][0]["ansible.builtin.include_role"] == {
        "name": "k3s",
        "tasks_from": "inter_node.yml",
    }
    assert server["roles"] == ["k3s"]
    assert agents["roles"] == ["k3s"]
    assert agents["vars"] == {"k3s_node_role": "agent"}
    for play in (harden, server, tang, storage, agents):
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
                            "shared_profile": "qualified-test",
                        },
                        "02": {
                            "name": "exomem-agent-02",
                            "ipv4": "192.0.2.32",
                            "private_ip": "10.50.1.32",
                            "dedicated_cell_id": "aaaaaaaaaaaaaaaa",
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
        ["python3", str(generator), str(terraform_output), str(inventory), "--user", "ops",
         "--dedicated-hosts", _no_dedicated_hosts(tmp_path)],
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
            "k3s_agent_shared_profile": "qualified-test",
        },
        "exomem-agent-02": {
            "ansible_host": "192.0.2.32",
            "ansible_user": "ops",
            "private_node_ip": "10.50.1.32",
            "k3s_agent_dedicated_cell": "aaaaaaaaaaaaaaaa",
        },
    }


@pytest.mark.parametrize(
    "agents",
    [
        {"01": {"name": "exomem-agent-01", "ipv4": "not-an-ip", "private_ip": "10.50.1.31"}},
        {"01": {"name": "exomem-alpha", "ipv4": "192.0.2.31", "private_ip": "10.50.1.31"}},
        {"01": {"name": "Bad Name", "ipv4": "192.0.2.31", "private_ip": "10.50.1.31"}},
        {"01": {"name": "exomem-agent-01", "ipv4": "192.0.2.31", "private_ip": "10.50.1.31", "dedicated_cell_id": "aaaaaaaaaaaaaaaa\n"}},
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
        ["python3", str(generator), str(terraform_output), str(tmp_path / "inventory.yml"),
         "--dedicated-hosts", _no_dedicated_hosts(tmp_path)],
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
        ["python3", str(generator), str(terraform_output), str(tmp_path / "inventory.yml"),
         "--dedicated-hosts", _no_dedicated_hosts(tmp_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


# --- Inventory: hosts Terraform does not create --------------------------------

_VSWITCH_OUTPUT = {
    "sensitive": False,
    "value": {
        "vlan_id": 4000,
        "subnet_cidr": "10.50.2.0/24",
        "gateway": "10.50.2.1",
        "network_cidr": "10.50.0.0/16",
    },
}


def _dedicated(name: str, private_ip: str, link: str, **extra: object) -> dict:
    host = {
        "ipv4": "203.0.113.40",
        "admin_address": "100.64.0.40",
        "private_ip": private_ip,
        "link": link,
        "data_disks": ["/dev/disk/by-id/nvme-a-part4", "/dev/disk/by-id/nvme-b-part4"],
    }
    return {name: {**host, **extra}}


def _generate_with_dedicated(tmp_path: Path, dedicated: dict) -> subprocess.CompletedProcess:
    terraform_output = tmp_path / "foundation.json"
    terraform_output.write_text(
        json.dumps(
            {
                "server_ipv4": {"sensitive": False, "value": "192.0.2.10"},
                "private_node_ip": {"sensitive": False, "value": "10.50.1.10"},
                "k3s_agent_nodes": {
                    "sensitive": False,
                    "value": {
                        "01": {"name": "exomem-agent-01", "ipv4": "192.0.2.31", "private_ip": "10.50.1.31"},
                    },
                },
                "vswitch": _VSWITCH_OUTPUT,
            }
        ),
        encoding="utf-8",
    )
    terraform_output.chmod(0o600)
    hosts = tmp_path / "dedicated-hosts.json"
    hosts.write_text(json.dumps(dedicated), encoding="utf-8")
    return subprocess.run(
        [
            "python3",
            str(ROOT / "infra/scripts/generate_ansible_inventory.py"),
            str(terraform_output),
            str(tmp_path / "inventory.json"),
            "--user",
            "ops",
            "--dedicated-hosts",
            str(hosts),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_inventory_generator_joins_hosts_terraform_does_not_create(tmp_path: Path) -> None:
    # Catches a dedicated host that lands outside k3s_agents (it would never
    # join) or outside dedicated_hosts (its disks would never be encrypted),
    # and a Terraform agent whose coordinates change because one was added.
    wipe = ["/dev/disk/by-id/nvme-a-part4"]
    dedicated = {
        **_dedicated("exomem-agent-dx1", "10.51.0.40", "wireguard", wipe=wipe),
        **_dedicated("exomem-agent-dx2", "10.50.2.41", "vswitch"),
    }
    result = _generate_with_dedicated(tmp_path, dedicated)
    assert result.returncode == 0, result.stderr

    hosted = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))["all"][
        "children"
    ]["hosted_nodes"]
    agents = hosted["children"]["k3s_agents"]
    assert agents["hosts"] == {
        "exomem-agent-01": {
            "ansible_host": "192.0.2.31",
            "ansible_user": "ops",
            "private_node_ip": "10.50.1.31",
        }
    }
    disks = ["/dev/disk/by-id/nvme-a-part4", "/dev/disk/by-id/nvme-b-part4"]
    common = {"ansible_host": "100.64.0.40", "ansible_user": "ops", "public_ipv4": "203.0.113.40",
              "dedicated_host_data_disks": disks}
    assert agents["children"]["dedicated_hosts"]["hosts"] == {
        "exomem-agent-dx1": {**common, "private_node_ip": "10.51.0.40",
                             "k3s_private_link": "wireguard", "dedicated_host_wipe_disks": wipe},
        "exomem-agent-dx2": {**common, "private_node_ip": "10.50.2.41", "k3s_private_link": "vswitch",
                             "k3s_vswitch": {"vlan_id": 4000, "address": "10.50.2.41/24",
                                             "gateway": "10.50.2.1", "network": "10.50.0.0/16"}},
    }


def test_inventory_generator_requires_the_dedicated_host_list(tmp_path: Path) -> None:
    # An inventory that omits dedicated hosts makes site.yml retire their
    # WireGuard link, firewall rules and Tang access on every other node.
    terraform_output = tmp_path / "foundation.json"
    terraform_output.write_text(json.dumps({
        "server_ipv4": {"sensitive": False, "value": "192.0.2.10"},
        "private_node_ip": {"sensitive": False, "value": "10.50.1.10"},
    }), encoding="utf-8")
    terraform_output.chmod(0o600)
    result = subprocess.run(
        ["python3", str(ROOT / "infra/scripts/generate_ansible_inventory.py"),
         str(terraform_output), str(tmp_path / "inventory.json")],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    assert result.returncode == 2 and "--dedicated-hosts" in result.stderr
    assert not (tmp_path / "inventory.json").exists()


@pytest.mark.parametrize(
    "dedicated,reason",
    [
        # Two inventory entries for one name would converge one machine as both.
        (_dedicated("exomem-agent-01", "10.51.0.40", "wireguard"), "reuses another node's name"),
        # A second node on an existing node's address breaks the overlay.
        (_dedicated("exomem-agent-dx1", "10.50.1.31", "wireguard"), "own private address"),
        (_dedicated("exomem-agent-dx1", "fd00::40", "wireguard"), "dx1: private_ip must be an IPv4"),
        ({"exomem-agent-dx1": {**_dedicated("x", "10.51.0.40", "wireguard")["x"], "private_ip": None}},
         "dx1: private_ip must be an IPv4"),
        # Kernel names move between boots; the wrong disk would be encrypted.
        (_dedicated("exomem-agent-dx1", "10.51.0.40", "wireguard",
                    data_disks=["/dev/nvme0n1p4", "/dev/nvme1n1p4"]), "not kernel names"),
        # Two partitions of one disk mirror nothing.
        (_dedicated("exomem-agent-dx1", "10.51.0.40", "wireguard",
                    data_disks=["/dev/disk/by-id/nvme-a-part4", "/dev/disk/by-id/nvme-a-part5"]),
         "two different disks"),
        (_dedicated("exomem-agent-dx1", "10.51.0.40", "wireguard",
                    data_disks=[{"path": "/dev/disk/by-id/nvme-a"}, "/dev/disk/by-id/nvme-b"]),
         "dx1: data_disks must be a list"),
        (_dedicated("exomem-agent-dx1", "10.51.0.40", "wireguard", wipe=[{"path": "/dev/sda"}]),
         "dx1: wipe must be a list"),
        (_dedicated("exomem-agent-dx1", "10.51.0.40", "wireguard", wipe=["/dev/disk/by-id/nvme-c"]),
         "wipe may only name its own data_disks"),
        # A vSwitch address must be a host in the Terraform vSwitch subnet.
        (_dedicated("exomem-agent-dx1", "10.50.3.40", "vswitch"), "other than its gateway"),
        (_dedicated("exomem-agent-dx1", "10.50.2.1", "vswitch"), "other than its gateway"),
        (_dedicated("exomem-agent-dx1", "10.51.0.40", "ipsec"), "link must be wireguard or vswitch"),
    ],
)
def test_inventory_generator_refuses_malformed_dedicated_hosts(
    tmp_path: Path, dedicated: dict, reason: str
) -> None:
    result = _generate_with_dedicated(tmp_path, dedicated)
    # 1 is a refused input; argparse's usage error is 2. The reason names the
    # host and field, so a traceback never stands in for a refusal.
    assert result.returncode == 1, result.stderr
    assert reason in result.stderr
    assert not (tmp_path / "inventory.json").exists()


def test_escrowed_tang_keys_reach_the_variable_the_role_reads() -> None:
    # A renamed variable on either side leaves Tang and every Clevis binding
    # without keys; per-host passphrases follow one naming rule.
    matrix = json.loads(_read(ROOT / "infra/contracts/secret-destinations-v1.json"))
    destinations = matrix["secrets"]["dedicated_host_tang_keys"]["destinations"]
    assert destinations["ansible.hosted-node.tang-keys.active"]["variable"] == "dedicated_host_tang_keys"
    assert destinations["escrow.tang-keys.active"]["kind"] == "sops_escrow"
    defaults = _yaml(ANSIBLE / "roles/dedicated_host/defaults/main.yml")
    assert defaults["dedicated_host_tang_keys"] == ""


_TERRAFORM_ONLY = {
    "hosted_nodes": {
        "hosts": {"exomem-alpha": {"private_node_ip": "10.50.1.10"}},
        "children": {"k3s_agents": {"hosts": {"exomem-agent-01": {"private_node_ip": "10.50.1.31"}}}},
    }
}
_WITH_DEDICATED = json.loads(json.dumps(_TERRAFORM_ONLY))
_WITH_DEDICATED["hosted_nodes"]["children"]["k3s_agents"]["children"] = {
    "dedicated_hosts": {
        "hosts": {
            # A WireGuard host's private address sits on its WireGuard interface.
            "exomem-agent-dx1": {"private_node_ip": "10.51.0.40", "k3s_private_link": "wireguard",
                                 "public_ipv4": "203.0.113.40",
                                 "k3s_resolved_private_interface": "wgexomem"},
            "exomem-agent-dx2": {"private_node_ip": "10.50.2.41", "k3s_private_link": "vswitch",
                                 "k3s_resolved_private_interface": "vlan4000"},
        }
    }
}


@pytest.mark.parametrize(
    "groups,expected",
    [
        # An inventory of Terraform hosts turns every new branch off, so
        # site.yml converges them exactly as before.
        (_TERRAFORM_ONLY, {
            "exomem-alpha": [[], {"10.50.1.31": "priv"}, False],
            "exomem-agent-01": [[], {"10.50.1.10": "priv"}, False],
        }),
        # A WireGuard agent peers with every node and every node with it;
        # firewall rules for it name the WireGuard interface. Dedicated hosts
        # wait for their TopoLVM pool.
        (_WITH_DEDICATED, {
            "exomem-alpha": [["exomem-agent-dx1"],
                             {"10.50.1.31": "priv", "10.51.0.40": "wgexomem", "10.50.2.41": "priv"}, False],
            "exomem-agent-01": [["exomem-agent-dx1"],
                                {"10.50.1.10": "priv", "10.51.0.40": "wgexomem", "10.50.2.41": "priv"}, False],
            "exomem-agent-dx1": [["exomem-agent-01", "exomem-agent-dx2", "exomem-alpha"],
                                 {"10.50.1.10": "wgexomem", "10.50.1.31": "wgexomem",
                                  "10.50.2.41": "wgexomem"}, True],
            "exomem-agent-dx2": [["exomem-agent-dx1"],
                                 {"10.50.1.10": "vlan4000", "10.50.1.31": "vlan4000",
                                  "10.51.0.40": "wgexomem"}, True],
        }),
    ],
)
def test_private_link_and_storage_defaults_follow_the_inventory(
    tmp_path: Path, groups: dict, expected: dict
) -> None:
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN for local role execution")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"all": {"vars": {"ansible_connection": "local",
                                                      "k3s_resolved_private_interface": "priv"},
                                             "children": groups}}))
    play = tmp_path / "defaults.yml"
    play.write_text(yaml.safe_dump([{"hosts": "hosted_nodes", "gather_facts": False,
        "vars": {"expected": expected}, "tasks": [
            {"ansible.builtin.include_vars": {"file": str(K3S_ROLE / "defaults/main.yml")}},
            {"ansible.builtin.assert": {"that": [
                "[k3s_wireguard_peers | sort, k3s_peer_interfaces, k3s_agent_local_storage | bool]"
                " == expected[inventory_hostname]",
            ]}},
        ]}]))
    # --limit leaves a node out of the run, never out of its peers' firewall,
    # WireGuard or Tang configuration: they read the inventory's groups.
    result = subprocess.run([str(ANSIBLE_PLAYBOOK), "-i", str(inventory), "--limit", "!exomem-agent-01",
                             str(play)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stdout + result.stderr


_WIREGUARD_KEY = "HIgo9xNzJMWLKASShiTqIybxZ0U3wGLiUeJ1PKf8ykw="


@pytest.mark.parametrize(
    "published,private_ip,refusal",
    [
        (_WIREGUARD_KEY, "10.51.0.40", None),
        # Root on one node must not write wg-quick configuration, which runs
        # PreUp and PostUp commands as root, on every other node.
        (_WIREGUARD_KEY + "\n[Interface]\nPreUp = touch /tmp/owned", "10.51.0.40",
         "other than one public key"),
        (_WIREGUARD_KEY, "10.51.0.40/32 dev wgexomem; touch /tmp/owned; true",
         "not exactly one IPv4 address"),
    ],
)
def test_a_peer_cannot_inject_wireguard_configuration(
    tmp_path: Path, published: str, private_ip: str, refusal: str | None
) -> None:
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN for local role execution")
    groups = json.loads(json.dumps(_WITH_DEDICATED))
    dedicated = groups["hosted_nodes"]["children"]["k3s_agents"]["children"]["dedicated_hosts"]
    dedicated["hosts"]["exomem-agent-dx1"]["k3s_wireguard_public_key"] = published
    dedicated["hosts"]["exomem-agent-dx1"]["private_node_ip"] = private_ip
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"all": {"vars": {"ansible_connection": "local"}, "children": groups}}))
    link = next(task for task in _yaml(K3S_ROLE / "tasks/private_link.yml")
                if task["name"] == "Converge the WireGuard private link")
    checks = [task for task in link["block"] if task["name"] in (
        "Refuse a WireGuard key that is not exactly one public key",
        "Refuse a peer address that is not exactly one IPv4 address",
    )]
    assert len(checks) == 2
    play = tmp_path / "keys.yml"
    play.write_text(yaml.safe_dump([{"hosts": "exomem-alpha", "gather_facts": False, "tasks": [
        {"ansible.builtin.include_vars": {"file": str(K3S_ROLE / "defaults/main.yml")}},
        *checks,
    ]}]))
    result = subprocess.run([str(ANSIBLE_PLAYBOOK), "-i", str(inventory), str(play)],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if refusal is None:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0 and refusal in result.stdout, result.stdout


@pytest.mark.parametrize(
    "free_extents,overrides,expected",
    [
        # 2 x 3.84 TB in RAID1: 64 B per 64 KiB chunk is 895 extents (3.5 GiB),
        # and twice that stays free. A fixed 2 GiB would starve this pool.
        (915527, {}, {"pool_extents": 915527 - 4 * 895, "metadata_kib": 895 * 4096,
                      "reserve_kib": 2 * 895 * 4096}),
        # Past about 15.8 TiB the metadata stops at LVM's 15.81 GiB limit.
        (5242880, {}, {"pool_extents": 5242880 - 4 * 4048, "metadata_kib": 4048 * 4096,
                       "reserve_kib": 2 * 4048 * 4096}),
        # An operator's sizes replace the computed ones.
        (1000, {"dedicated_host_thin_metadata_mib": 64, "dedicated_host_vg_reserve_mib": 128},
         {"pool_extents": 1000 - 32 - 32, "metadata_kib": 65536, "reserve_kib": 131072}),
    ],
)
def test_thin_pool_metadata_grows_with_the_pool(
    tmp_path: Path, free_extents: int, overrides: dict, expected: dict
) -> None:
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN for local role execution")
    role = ANSIBLE / "roles/dedicated_host"
    size = next(task for task in _yaml(role / "tasks/volume_group.yml")
                if task["name"] == "Size the thin pool and its metadata")
    play = tmp_path / "size.yml"
    play.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "vars": {
        # What `vgs` reports for the group: free extents of 4 MiB each.
        "dedicated_host_vg_free": {"stdout": f"  {free_extents} 4194304"},
        "dedicated_host_pool": {"rc": 5}, "expected": expected,
    }, "tasks": [
        {"ansible.builtin.include_vars": {"file": str(role / "defaults/main.yml")}},
        *([{"ansible.builtin.set_fact": overrides}] if overrides else []),
        size,
        {"ansible.builtin.assert": {"that": ["dedicated_host_pool_geometry == expected"]}},
    ]}]))
    result = subprocess.run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", "-c", "local", str(play)],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stdout + result.stderr


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


def test_join_playbook_skips_a_host_that_removal_marked() -> None:
    validate = _read(K3S_ROLE / "tasks/validate.yml")
    stop = _read(K3S_ROLE / "tasks/remove_stop.yml")
    site = _yaml(ANSIBLE / "site.yml")
    assert "path: /etc/rancher/k3s/removed" in validate
    assert "ansible.builtin.meta: end_host" in validate
    assert "dest: /etc/rancher/k3s/removed" in stop
    harden = next(play for play in site if play["name"].startswith("Harden"))["pre_tasks"]
    assert harden[0]["ansible.builtin.stat"]["path"] == "/etc/rancher/k3s/removed"
    assert harden[1]["ansible.builtin.meta"] == "end_host"
    assert harden[1]["when"] == "k3s_removed_marker.stat.exists"


def test_every_node_keeps_the_image_and_log_limits() -> None:
    defaults = _yaml(K3S_ROLE / "defaults/main.yml")
    assert {"image-gc-high-threshold=75", "container-log-max-size=10Mi"} <= set(defaults["k3s_kubelet_args"])


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
        "Move each cell off the agent's local storage",
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

    reach = tasks["Check whether the agent can be reached"]
    assert reach["ignore_unreachable"] is True
    stop = tasks["Stop the agent completely"]
    assert "k3s_remove_reach is not unreachable" in stop["when"]
    # Playbook knobs are play-level: role defaults are invisible outside the role.
    for play in plays:
        for knob in ("k3s_drain_timeout", "k3s_remove_host_gone"):
            if knob in json.dumps(play.get("tasks", [])):
                assert knob in play.get("vars", {}), (play["name"], knob)
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
    # A container outlives its shim: every pod-cgroup process is killed and
    # its absence is part of the confirmation.
    assert "k3s_remove_pod_procs_argv" in stop and "k3s_remove_pod_procs_argv" in defaults
    assert '"*kubepods*"' in defaults
    assert "k3s_remove_pod_procs_after.stdout" in stop
    # The agent token does not outlive the removal on the host.
    assert "path: /etc/rancher/k3s/config.yaml\n    state: absent" in stop


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
    # The no-surge rollout belongs with D11's hostPort: a surge pod could
    # never bind 443 beside the old one on the single server node.
    assert values["traefik"]["ports"]["websecure"]["hostPort"] == 443
    assert values["traefik"]["updateStrategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0},
    }


def test_rendered_traefik_deployment_is_pinned_to_the_control_plane() -> None:
    helm = os.environ.get("HELM_BIN")
    if not helm:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    platform = ROOT / "infra/helm/platform"
    rendered = subprocess.run(
        [
            helm, "template", "contract-test", str(platform),
            "--namespace", "exomem-platform",
            "--values", str(platform / "values.validation.yaml"),
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    deployments = [
        document
        for document in yaml.safe_load_all(rendered)
        if document and document.get("kind") == "Deployment"
        and document["metadata"]["name"].endswith("-traefik")
    ]
    assert len(deployments) == 1
    spec = deployments[0]["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"node-role.kubernetes.io/control-plane": "true"}


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


@pytest.mark.parametrize("key", ["exomem.io/dedicated-cell", "exomem.io/shared-profile"])
@pytest.mark.parametrize("reserved_by", ["label", "taint", "selected-template"])
def test_removal_excludes_reserved_capacity_and_requires_relocation(tmp_path: Path, reserved_by: str, key: str) -> None:
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN for local role execution")
    nodes = [{"metadata": {"name": name, "labels": {}}, "spec": {},
              "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
             for name in ("target", "shared", "reserved")]
    target = nodes[0] if reserved_by == "selected-template" else nodes[2]
    if reserved_by == "taint":
        target["spec"]["taints"] = [{"key": key, "value": "aaaaaaaaaaaaaaaa", "effect": "NoSchedule"}]
    else:
        target["metadata"]["labels"][key] = "aaaaaaaaaaaaaaaa"
    statefulsets = [] if reserved_by != "selected-template" else [{
        "metadata": {"namespace": "exo-cell-aaaaaaaaaaaaaaaa"},
        "spec": {"replicas": 0, "template": {"spec": {"nodeSelector": {key: "aaaaaaaaaaaaaaaa"}}}},
    }]
    variables = {
        "k3s_remove_node": "target", "k3s_cell_namespace_prefix": "exo-cell-",
        "k3s_csi_driver": "csi.hetzner.cloud", "k3s_remove_headroom": 5, "k3s_local_storage_driver": "topolvm.io",
        "k3s_remove_nodes_doc": {"items": nodes},
        "k3s_remove_csinodes_doc": {"items": [{"metadata": {"name": n["metadata"]["name"]},
            "spec": {"drivers": [{"name": "csi.hetzner.cloud", "allocatable": {"count": 16}}]}}
            for n in nodes]},
        "k3s_remove_statefulsets_doc": {"items": statefulsets},
        **{f"k3s_remove_{name}_doc": {"items": []} for name in ("attachments", "pvs", "pvcs", "storageclasses")},
    }
    play = tmp_path / "preflight.yml"
    play.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False,
        "vars": variables, "tasks": [
            {"ansible.builtin.include_tasks": str(K3S_ROLE / "tasks/remove_preflight.yml")},
            {"ansible.builtin.assert": {"that": ["k3s_remove_remaining_slots | int == 11"]}},
        ]}]))
    result = subprocess.run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", "-c", "local", str(play)],
                            capture_output=True, text=True)
    if reserved_by == "selected-template":
        assert result.returncode != 0 and "still selects" in result.stdout, result.stdout + result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr


def _local_volume(cell: str, node: str, phase: str) -> dict:
    return {"metadata": {"name": f"pv-{cell}"},
            "spec": {"csi": {"driver": "topolvm.io", "volumeHandle": f"lv-{cell}"},
                     "claimRef": {"namespace": f"exo-cell-{cell}", "name": "cell-data"},
                     "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                         {"key": "topology.topolvm.io/node", "operator": "In", "values": [node]}]}]}}},
            "status": {"phase": phase}}


@pytest.mark.parametrize(("hold_on", "volume_phase", "target_ready", "refusal", "moves"), [
    # An hourly backup on another node's cell no longer blocks the removal...
    ("shared", "Bound", "True", None, []),
    # ...one on a cell whose volume is on the target still does.
    ("target", "Released", "True", "maintenance is in flight", None),
    # A cell still living on the target moves off it before the node goes:
    # deleting the Node would relocate it from its last backup.
    (None, "Bound", "True", None, ["exo-cell-aaaaaaaaaaaaaaaa"]),
    # A target that is not Ready cannot take the move's stopped backup.
    (None, "Bound", "Unknown", "is not Ready", None),
    # A volume a relocation or a move retained holds no live cell.
    (None, "Released", "True", None, []),
], ids=["hold-elsewhere", "hold-on-target", "live-volume-on-target", "live-volume-on-a-target-not-ready",
        "retained-volume-on-target"])
def test_removal_refuses_only_for_cells_whose_volume_is_on_the_target(
    tmp_path: Path, hold_on: str | None, volume_phase: str, target_ready: str, refusal: str | None,
    moves: list[str] | None,
) -> None:
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN for local role execution")
    nodes = [{"metadata": {"name": name, "labels": {}, "annotations": {"capacity.topolvm.io/thin": str(100 * 1024**3)}},
              "spec": {},
              "status": {"conditions": [{"type": "Ready", "status": target_ready if name == "target" else "True"}]}}
             for name in ("target", "shared")]
    held = "aaaaaaaaaaaaaaaa"
    volumes = [_local_volume(held, hold_on or "target", volume_phase)]
    claims = [{"metadata": {"namespace": f"exo-cell-{held}", "name": "cell-data"},
               "spec": {"storageClassName": "exomem-cloud-local", "resources": {"requests": {"storage": "4Gi"}}}}]
    statefulsets = [] if hold_on is None else [{
        "metadata": {"namespace": f"exo-cell-{held}", "annotations": {"exomem.io/hold": "snapshot-backup"}},
        "spec": {"replicas": 1, "template": {"spec": {}}},
    }]
    variables = {
        "k3s_remove_node": "target", "k3s_cell_namespace_prefix": "exo-cell-",
        "k3s_csi_driver": "csi.hetzner.cloud", "k3s_remove_headroom": 5,
        "k3s_local_storage_driver": "topolvm.io", "k3s_local_storage_topology_key": "topology.topolvm.io/node",
        "k3s_remove_nodes_doc": {"items": nodes},
        "k3s_remove_csinodes_doc": {"items": [{"metadata": {"name": n["metadata"]["name"]},
            "spec": {"drivers": [{"name": "csi.hetzner.cloud", "allocatable": {"count": 16}}]}}
            for n in nodes]},
        "k3s_topolvm_device_class": "thin", "k3s_local_backup_concurrency_per_node": 2,
        "k3s_local_default_cell_gib": 4,
        "k3s_remove_statefulsets_doc": {"items": statefulsets},
        "k3s_remove_pvs_doc": {"items": volumes},
        "k3s_remove_pvcs_doc": {"items": claims},
        "k3s_remove_storageclasses_doc": {"items": [{"metadata": {"name": "exomem-cloud-local"},
                                                     "provisioner": "topolvm.io"}]},
        "k3s_remove_attachments_doc": {"items": []},
    }
    play = tmp_path / "preflight.yml"
    play.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "vars": variables,
        "tasks": [{"ansible.builtin.include_tasks": str(K3S_ROLE / "tasks/remove_preflight.yml")},
                  {"ansible.builtin.assert": {"that": [f"k3s_remove_moves == {moves}"]}}]}]))
    result = subprocess.run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", "-c", "local", str(play)],
                            capture_output=True, text=True)
    if refusal is None:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0 and refusal in result.stdout, result.stdout + result.stderr


GIB = 1024**3


# D6 at the role's defaults, which are cellctl's: a node keeps 2 x 4 GiB x 2
# backups = 16 GiB of its pool free for hourly backups beyond the cells it
# takes. A cell larger than 4 GiB raises that to 2 x its size x 2.
@pytest.mark.parametrize(("pools", "cordoned", "moving", "elsewhere", "hetzner_slots", "refusal"), [
    # A local-only cluster publishes no attachment slots at all.
    ({"spare": 20}, [], [4], [], None, None),
    # Free bytes that hold the cell but not the reserve beyond it are no room.
    ({"spare": 18}, [], [4], [], None, "with room for the local volume"),
    # A volume cannot span two nodes, so room on two nodes does not add up.
    ({"spare": 36, "spare-2": 36}, [], [8], [], None, "with room for the local volume"),
    # The largest cell anywhere sets the reserve, as it does in cellctl.
    ({"spare": 24}, [], [4], [8], None, "with room for the local volume"),
    # The target's own pool leaves with it.
    ({"target": 100, "spare": 18}, [], [4], [], None, "with room for the local volume"),
    # A cordoned node takes no new claim.
    ({"spare": 18, "cordoned": 100}, ["cordoned"], [4], [], None, "with room for the local volume"),
    # A removal holding both kinds counts each against its own capacity.
    ({"spare": 20}, [], [4], [], 1, None),
    ({"spare": 18}, [], [4], [], 1, "with room for the local volume"),
    ({"spare": 20}, [], [4], [], 0, "cell slots"),
    # The removal moves the largest claim first, the order the placement
    # assumes, so an early small cell cannot take a later large one's room.
    ({"spare": 100}, [], [4, 8, 4], [], None, None),
], ids=["local-with-room", "local-without-room", "room-split-across-nodes", "larger-cell-elsewhere",
        "target-excluded", "cordoned-node-excluded", "mixed-with-room", "mixed-without-local-room",
        "mixed-without-attachment-slots", "largest-claim-moves-first"])
def test_removal_counts_each_cell_against_the_storage_it_uses(
    tmp_path: Path, pools: dict[str, int], cordoned: list[str], moving: list[int], elsewhere: list[int],
    hetzner_slots: int | None, refusal: str | None,
) -> None:
    # Counting only attachment slots refuses every removal of a local-storage
    # agent, and never sees a moved cell with nowhere to land: its restore
    # would wait for room while the cell stays stopped.
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN for local role execution")
    nodes = [{"metadata": {"name": name, "labels": {},
                           "annotations": {"capacity.topolvm.io/thin": str(pools[name] * GIB)} if name in pools else {}},
              "spec": {"unschedulable": True} if name in cordoned else {},
              "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
             for name in ["target", *(pool for pool in pools if pool != "target")]
             + (["cloud"] if hetzner_slots is not None else [])]
    volumes, claims, attachments = [], [], []
    for index, gib in enumerate(moving):
        cell = "abcd"[index] * 16
        volumes.append(_local_volume(cell, "target", "Bound"))
        claims.append({"metadata": {"namespace": f"exo-cell-{cell}", "name": "cell-data"},
                       "spec": {"storageClassName": "exomem-cloud-local",
                                "resources": {"requests": {"storage": f"{gib}Gi"}}}})
    for index, gib in enumerate(elsewhere):
        cell = "mnop"[index] * 16
        volumes.append(_local_volume(cell, "spare", "Bound"))
        claims.append({"metadata": {"namespace": f"exo-cell-{cell}", "name": "cell-data"},
                       "spec": {"storageClassName": "exomem-cloud-local",
                                "resources": {"requests": {"storage": f"{gib}Gi"}}}})
    if hetzner_slots is not None:
        volumes.append({"metadata": {"name": "pv-hetzner"},
                        "spec": {"csi": {"driver": "csi.hetzner.cloud", "volumeHandle": "123"},
                                 "claimRef": {"namespace": f"exo-cell-{'h' * 16}", "name": "cell-data"}},
                        "status": {"phase": "Bound"}})
        claims.append({"metadata": {"namespace": f"exo-cell-{'h' * 16}", "name": "cell-data"},
                       "spec": {"storageClassName": "exomem-cloud-encrypted",
                                "resources": {"requests": {"storage": "10Gi"}}}})
        attachments.append({"spec": {"attacher": "csi.hetzner.cloud", "nodeName": "target",
                                     "source": {"persistentVolumeName": "pv-hetzner"}},
                            "status": {"attached": True}})
    defaults = _yaml(K3S_ROLE / "defaults/main.yml")
    variables = {
        "k3s_remove_node": "target", "k3s_cell_namespace_prefix": "exo-cell-",
        "k3s_csi_driver": "csi.hetzner.cloud", "k3s_remove_headroom": 5,
        **{key: defaults[key] for key in ("k3s_local_storage_driver", "k3s_local_storage_topology_key",
                                          "k3s_topolvm_device_class", "k3s_local_backup_concurrency_per_node",
                                          "k3s_local_default_cell_gib")},
        "k3s_remove_nodes_doc": {"items": nodes},
        "k3s_remove_csinodes_doc": {"items": [] if hetzner_slots is None else [
            {"metadata": {"name": "cloud"},
             "spec": {"drivers": [{"name": "csi.hetzner.cloud", "allocatable": {"count": 5 + hetzner_slots}}]}}]},
        "k3s_remove_storageclasses_doc": {"items": [
            {"metadata": {"name": "exomem-cloud-local"}, "provisioner": "topolvm.io"},
            {"metadata": {"name": "exomem-cloud-encrypted"}, "provisioner": "csi.hetzner.cloud"}]},
        "k3s_remove_attachments_doc": {"items": attachments},
        "k3s_remove_pvs_doc": {"items": volumes},
        "k3s_remove_pvcs_doc": {"items": claims},
        "k3s_remove_statefulsets_doc": {"items": []},
    }
    # What remove-agent.yml loops its moves over, evaluated after the preflight.
    tasks = {task["name"]: task for task in _tasks_in_order(_yaml(ANSIBLE / "remove-agent.yml"))}
    moves = tasks["Move each cell off the agent's local storage"]["loop"].strip()[2:-2]
    order = [f"exo-cell-{'abcd'[i] * 16}" for i in sorted(range(len(moving)), key=lambda i: (-moving[i], i))]
    play = tmp_path / "preflight.yml"
    play.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "vars": variables,
        "tasks": [{"ansible.builtin.include_tasks": str(K3S_ROLE / "tasks/remove_preflight.yml")},
                  {"ansible.builtin.assert": {"that": [f"({moves}) == {order}"]}}]}]))

    result = subprocess.run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", "-c", "local", str(play)],
                            capture_output=True, text=True)

    if refusal is None:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0 and refusal in result.stdout, result.stdout + result.stderr


# Plays cellctl's part for remove_move.yml: answers kubectl from a scenario,
# one namespace listing per poll, and logs every call.
_FAKE_KUBECTL = r"""#!/usr/bin/env python3
import json, sys
from pathlib import Path

here = Path(__file__).parent
args = sys.argv[2:]
with (here / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
scenario = json.loads((here / "scenario.json").read_text())
if args[:2] == ["get", "statefulsets,persistentvolumeclaims,jobs"]:
    count = here / "polls"
    polls = int(count.read_text()) if count.exists() else 0
    count.write_text(str(polls + 1))
    print(json.dumps({"items": scenario["polls"][min(polls, len(scenario["polls"]) - 1)]}))
elif args[:3] == ["get", "persistentvolumeclaim", "cell-data"]:
    print(scenario["claim_volume"], end="")
elif args[:2] == ["get", "persistentvolume"]:
    print(scenario["volumes"][args[2]], end="")
"""


def _cell_objects(*, hold: str | None = None, outcome: str | None = None, volume: str = "pv-old",
                  ready: bool = True, failed_restore: bool = False) -> list[dict]:
    annotations = {key: value for key, value in (("exomem.io/hold", hold), ("exomem.io/backup-outcome", outcome))
                   if value}
    objects = [
        {"kind": "StatefulSet", "metadata": {"name": "cell", "generation": 4, "annotations": annotations},
         "spec": {"replicas": 1}, "status": {"observedGeneration": 4, "readyReplicas": 1 if ready else 0}},
        {"kind": "PersistentVolumeClaim", "metadata": {"name": "cell-data"},
         "spec": {"volumeName": volume}, "status": {"phase": "Bound"}},
    ]
    if failed_restore:
        objects.append({"kind": "Job", "metadata": {"name": "cell-restore-0a1b2c3d4e",
                                                    "labels": {"exomem.io/cell-job": "restore"}},
                        "status": {"failed": 1}})
    return objects


@pytest.mark.parametrize(("polls", "new_volume_nodes", "refusal"), [
    ([_cell_objects(), _cell_objects(hold="move"), _cell_objects(hold="restore", volume="pv-new", ready=False),
      _cell_objects(volume="pv-new")], "shared", None),
    ([_cell_objects(hold="move"), _cell_objects(hold="move", outcome="failed")], "shared",
     "failed before its claim changed"),
    ([_cell_objects(hold="restore", volume="pv-new", ready=False, failed_restore=True)], "shared",
     "failed after its claim was retired"),
    ([_cell_objects(volume="pv-new")], "target", "not to another node"),
], ids=["moved", "backup-failed", "restore-failed", "landed-back-on-the-agent"])
def test_removal_moves_a_cell_off_the_agent_and_stops_on_a_failed_move(
    tmp_path: Path, polls: list, new_volume_nodes: str, refusal: str | None
) -> None:
    # move-cloud-cells-to-local-storage 6.4. Going on before a move finished,
    # or after it failed, would delete the node of a cell's only current
    # volume, and cellctl would relocate it from an older backup.
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN for local role execution")
    kubectl = tmp_path / "k3s"
    kubectl.write_text(_FAKE_KUBECTL)
    kubectl.chmod(0o755)
    (tmp_path / "scenario.json").write_text(json.dumps({
        "claim_volume": "pv-old", "polls": polls,
        "volumes": {"pv-old": "vol-old target", "pv-new": new_volume_nodes},
    }))
    defaults = _yaml(K3S_ROLE / "defaults/main.yml")
    play = tmp_path / "move.yml"
    play.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "vars": {
        "k3s_remove_node": "target", "k3s_remove_move_namespace": "exo-cell-aaaaaaaaaaaaaaaa",
        "k3s_remove_kubectl": [str(kubectl), "kubectl"], "k3s_remove_move_delay": 0,
        **{key: defaults[key] for key in ("k3s_remove_move_annotation", "k3s_remove_move_retries",
                                          "k3s_remove_move_pinned_jsonpath", "k3s_local_storage_topology_key")},
    }, "tasks": [{"ansible.builtin.include_tasks": str(K3S_ROLE / "tasks/remove_move.yml")}]}]))

    result = subprocess.run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", "-c", "local", str(play)],
                            capture_output=True, text=True)

    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    marks = [call[2:] for call in calls if call[0] == "annotate"]
    assert marks[0] == ["exo-cell-aaaaaaaaaaaaaaaa", "exomem.io/move-volume=vol-old", "--overwrite"]
    if refusal is None:
        assert result.returncode == 0, result.stdout + result.stderr
        # The mark goes only once the cell serves from its new volume.
        assert marks[1:] == [["exo-cell-aaaaaaaaaaaaaaaa", "exomem.io/move-volume-"]]
        assert (tmp_path / "polls").read_text() == str(len(polls))
    else:
        assert result.returncode != 0 and refusal in result.stdout, result.stdout + result.stderr
        # A failed move keeps its mark, which keeps the cell stopped on its volume.
        assert marks[1:] == []


@pytest.mark.parametrize("cell_id,profile", [("", ""), ("aaaaaaaaaaaaaaaa", ""), ("", "qualified-test")])
def test_agent_reservation_registers_and_converges_only_owned_fields(tmp_path: Path, cell_id: str, profile: str) -> None:
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN for local role execution")
    key = "exomem.io/dedicated-cell"
    unrelated = {"key": "other", "value": "keep", "effect": "NoExecute"}
    node = {"metadata": {"resourceVersion": "12", "labels": {"other": "keep", key: "bbbbbbbbbbbbbbbb"}},
            "spec": {"taints": [unrelated, {"key": key, "value": "bbbbbbbbbbbbbbbb", "effect": "NoSchedule"}]}}
    expected = [unrelated] + ([{"key": key, "value": cell_id, "effect": "NoSchedule"}] if cell_id else [])
    expected += ([{"key": "exomem.io/shared-profile", "value": profile, "effect": "NoSchedule"}] if profile else [])
    tasks = {task["name"]: task for task in _yaml(K3S_ROLE / "tasks/agent.yml")}
    rendered = tmp_path / "agent.yaml"
    play = tmp_path / "reservation.yml"
    play.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "vars": {
        "k3s_agent_dedicated_cell": cell_id, "k3s_node_role": "agent", "k3s_agent_shared_profile": profile,
        "k3s_agent_node_result": {"stdout": json.dumps(node)},
        "k3s_server_private_ip": "10.0.0.1", "k3s_agent_join_token": "test-token",
        "private_node_ip": "10.0.0.2", "k3s_resolved_private_interface": "eth0",
        "k3s_agent_node_label": "exomem.io/node-pool=agent", "expected_taints": expected,
        "k3s_kubelet_args": _yaml(K3S_ROLE / "defaults/main.yml")["k3s_kubelet_args"],
    }, "tasks": [
        _yaml(K3S_ROLE / "tasks/validate.yml")[0],
        {"ansible.builtin.template": {"src": str(K3S_ROLE / "templates/agent-config.yaml.j2"),
                                       "dest": str(rendered), "mode": "0600"}},
        tasks["Compute the agent reservation"],
        {"ansible.builtin.debug": {"msg": tasks["Converge the agent reservation"]["ansible.builtin.command"]["argv"][-1]},
         "register": "patch"},
        {"ansible.builtin.assert": {"that": [
            "(patch.msg | from_json).spec.taints == expected_taints",
            "(patch.msg | from_json).metadata.labels == {'exomem.io/dedicated-cell': k3s_agent_dedicated_cell or none, 'exomem.io/shared-profile': k3s_agent_shared_profile or none}",
            "(patch.msg | from_json).metadata.resourceVersion == '12'",
        ]}},
    ]}]))
    result = subprocess.run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", "-c", "local", str(play)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    config = _yaml(rendered)
    assert config["node-label"] == ["exomem.io/node-pool=agent"] + ([f"{key}={cell_id}"] if cell_id else []) + ([f"exomem.io/shared-profile={profile}"] if profile else [])
    assert config.get("node-taint", []) == ([f"{key}={cell_id}:NoSchedule"] if cell_id else []) + ([f"exomem.io/shared-profile={profile}:NoSchedule"] if profile else [])


@pytest.mark.parametrize("role,cell_id", [("server", "aaaaaaaaaaaaaaaa"), ("agent", "a" * 16 + "\n")])
def test_agent_reservation_rejects_wrong_role_or_inexact_id(tmp_path: Path, role: str, cell_id: str) -> None:
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN for local role execution")
    play = tmp_path / "validation.yml"
    play.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False,
        "vars": {"k3s_node_role": role, "k3s_agent_dedicated_cell": cell_id},
        "tasks": [_yaml(K3S_ROLE / "tasks/validate.yml")[0]]}]))
    result = subprocess.run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", "-c", "local", str(play)],
                            capture_output=True, text=True)
    assert result.returncode != 0 and "exact 16-character" in result.stdout
