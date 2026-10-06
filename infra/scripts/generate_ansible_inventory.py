#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a mode-0600 Ansible inventory from Terraform's JSON outputs."
    )
    parser.add_argument("terraform_output", type=Path)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--user", default="root")
    parser.add_argument(
        "--admin-addresses",
        type=Path,
        help=(
            "Private JSON object mapping inventory host names to the address the "
            "operator administers them on (their NetBird IP). Hosts it does not "
            "name keep their public IPv4."
        ),
    )
    parser.add_argument(
        "--dedicated-hosts",
        type=Path,
        help=(
            "Private JSON object of K3s agents Terraform does not create (dedicated or "
            "auction servers, other providers), keyed by inventory name: ipv4, "
            "admin_address, private_ip, link (wireguard or vswitch), data_disks and "
            "optional wipe."
        ),
    )
    return parser


def _admin_addresses(path: Path) -> dict[str, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not all(
        isinstance(name, str) and isinstance(address, str) for name, address in document.items()
    ):
        raise ValueError("administration addresses must map host names to addresses")
    return {name: str(ipaddress.ip_address(address)) for name, address in document.items()}


def _public_output(document: dict[str, Any], name: str) -> str:
    item = document.get(name)
    if not isinstance(item, dict) or item.get("sensitive") is not False:
        raise ValueError(f"{name} must be an explicit non-sensitive Terraform output")
    value = item.get("value")
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _optional_public_output(document: dict[str, Any], name: str) -> str | None:
    if name not in document:
        return None
    return _public_output(document, name)


# Terraform names every agent server exomem-agent-<key>; that name is also the
# inventory name and, through the agent's explicit node-name, the Kubernetes
# node name that remove-agent.yml acts on.
_AGENT_NAME = re.compile(r"^exomem-agent-[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")


def _agent_hosts(document: dict[str, Any], user: str) -> dict[str, dict[str, str]]:
    """Validated coordinates for every K3s agent, keyed by server name."""
    if "k3s_agent_nodes" not in document:
        return {}
    item = document["k3s_agent_nodes"]
    if not isinstance(item, dict) or item.get("sensitive") is not False:
        raise ValueError("k3s_agent_nodes must be an explicit non-sensitive Terraform output")
    nodes = item.get("value")
    if not isinstance(nodes, dict):
        raise ValueError("k3s_agent_nodes must be a map")
    hosts: dict[str, dict[str, str]] = {}
    for node in nodes.values():
        if not isinstance(node, dict):
            raise ValueError("each k3s_agent_nodes entry must be an object")
        name = node.get("name")
        if not isinstance(name, str) or not _AGENT_NAME.match(name):
            raise ValueError("each K3s agent must be named exomem-agent-<key>")
        if name in hosts:
            raise ValueError("K3s agent names must be unique")
        reservation = node.get("dedicated_cell_id", "")
        if not isinstance(reservation, str) or (
            reservation and re.fullmatch(r"[a-z2-7]{16}", reservation) is None
        ):
            raise ValueError("dedicated_cell_id must be empty or an exact cell identifier")
        profile = node.get("shared_profile", "")
        if not isinstance(profile, str) or (profile and (
            reservation or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", profile) is None
        )):
            raise ValueError("shared_profile must be a DNS label on a non-dedicated agent")
        hosts[name] = {
            "ansible_host": str(ipaddress.ip_address(str(node.get("ipv4")))),
            "ansible_user": user,
            "private_node_ip": str(ipaddress.ip_address(str(node.get("private_ip")))),
        }
        if profile:
            hosts[name]["k3s_agent_shared_profile"] = profile
        if reservation:
            hosts[name]["k3s_agent_dedicated_cell"] = reservation
    return hosts


def _vswitch(document: dict[str, Any]) -> dict[str, Any] | None:
    """The foundation's optional vSwitch subnet, or None when it is off."""
    if "vswitch" not in document:
        return None
    item = document["vswitch"]
    if not isinstance(item, dict) or item.get("sensitive") is not False:
        raise ValueError("vswitch must be an explicit non-sensitive Terraform output")
    value = item.get("value")
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("vlan_id"), int):
        raise ValueError("vswitch must carry a VLAN ID and its subnet")
    return {
        "vlan_id": value["vlan_id"],
        "subnet": ipaddress.ip_network(str(value.get("subnet_cidr"))),
        "gateway": str(ipaddress.ip_address(str(value.get("gateway")))),
        "network": str(ipaddress.ip_network(str(value.get("network_cidr")))),
    }


# A data disk is named by its stable /dev/disk/by-id/ link; a partition adds
# -part<N> to its disk's link.
_DISK_ID = re.compile(r"^/dev/disk/by-id/(?P<disk>[^/]+?)(?:-part[0-9]+)?$")


def _address(name: str, host: dict[str, Any], field: str, ipv4_only: bool = False) -> str:
    value = host.get(field)
    try:
        address = ipaddress.ip_address(value) if isinstance(value, str) else None
    except ValueError:
        address = None
    if address is None or (ipv4_only and address.version != 4):
        kind = "an IPv4 address" if ipv4_only else "an IP address"
        raise ValueError(f"{name}: {field} must be {kind}")
    return str(address)


def _data_disks(name: str, host: dict[str, Any]) -> tuple[list[str], list[str]]:
    disks = host.get("data_disks")
    if not isinstance(disks, list) or not all(isinstance(disk, str) for disk in disks):
        raise ValueError(f"{name}: data_disks must be a list of /dev/disk/by-id/ paths")
    matches = [_DISK_ID.match(disk) for disk in disks]
    if not all(matches):
        raise ValueError(f"{name}: data_disks must name /dev/disk/by-id/ paths, not kernel names")
    # Two partitions of one disk mirror nothing; the role also compares the
    # kernel's parent disks, which catches one disk under two by-id names.
    if len(disks) != 2 or len({match["disk"] for match in matches if match}) != 2:
        raise ValueError(f"{name}: data_disks must be two devices on two different disks")
    wipe = host.get("wipe", [])
    if not isinstance(wipe, list) or not all(isinstance(disk, str) for disk in wipe):
        raise ValueError(f"{name}: wipe must be a list of its data_disks")
    if not set(wipe) <= set(disks):
        raise ValueError(f"{name}: wipe may only name its own data_disks")
    return disks, wipe


def _dedicated_hosts(
    path: Path, user: str, vswitch: dict[str, Any] | None
) -> dict[str, dict[str, Any]]:
    """Validated coordinates for K3s agents an operator installed by hand.

    They join k3s_agents like a Terraform agent, and the dedicated_hosts group
    that encrypts their data disks. Their administration address replaces the
    public one, as for every other host administered over NetBird.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("dedicated hosts must be a JSON object keyed by host name")
    hosts: dict[str, dict[str, Any]] = {}
    for name, host in document.items():
        if not isinstance(name, str) or not _AGENT_NAME.match(name):
            raise ValueError(f"{name}: a dedicated host must be named exomem-agent-<key>")
        if not isinstance(host, dict):
            raise ValueError(f"{name}: must be an object")
        disks, wipe = _data_disks(name, host)
        private_ip = _address(name, host, "private_ip", ipv4_only=True)
        coordinates: dict[str, Any] = {
            "ansible_host": _address(name, host, "admin_address"),
            "ansible_user": user,
            "private_node_ip": private_ip,
            "public_ipv4": _address(name, host, "ipv4", ipv4_only=True),
            "dedicated_host_data_disks": disks,
        }
        if wipe:
            coordinates["dedicated_host_wipe_disks"] = wipe
        link = host.get("link")
        if link == "vswitch":
            if vswitch is None:
                raise ValueError(f"{name}: link vswitch needs the foundation's vswitch output")
            subnet = vswitch["subnet"]
            if ipaddress.ip_address(private_ip) not in subnet or private_ip in (
                vswitch["gateway"],
                str(subnet.network_address),
                str(subnet.broadcast_address),
            ):
                raise ValueError(
                    f"{name}: private_ip must be a host address in the foundation's "
                    "vSwitch subnet other than its gateway"
                )
            coordinates["k3s_vswitch"] = {
                "vlan_id": vswitch["vlan_id"],
                "address": f"{private_ip}/{subnet.prefixlen}",
                "gateway": vswitch["gateway"],
                "network": vswitch["network"],
            }
        elif link != "wireguard":
            raise ValueError(f"{name}: link must be wireguard or vswitch")
        coordinates["k3s_private_link"] = link
        hosts[name] = coordinates
    return hosts


def main() -> int:
    args = _parser().parse_args()
    if args.terraform_output.stat().st_mode & 0o777 != 0o600:
        raise SystemExit("Terraform output JSON must have mode 0600")

    document = json.loads(args.terraform_output.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("Terraform output JSON must be an object")
    try:
        public_ip = str(ipaddress.ip_address(_public_output(document, "server_ipv4")))
        private_ip = str(ipaddress.ip_address(_public_output(document, "private_node_ip")))
        control_public_ip = _optional_public_output(document, "control_db_server_ipv4")
        control_private_ip = _optional_public_output(document, "control_db_private_ip")
        if control_public_ip is not None:
            control_public_ip = str(ipaddress.ip_address(control_public_ip))
        if control_private_ip is not None:
            control_private_ip = str(ipaddress.ip_address(control_private_ip))
        agents = _agent_hosts(document, args.user)
        admin_addresses = (
            _admin_addresses(args.admin_addresses) if args.admin_addresses else {}
        )
        dedicated = (
            _dedicated_hosts(args.dedicated_hosts, args.user, _vswitch(document))
            if args.dedicated_hosts
            else {}
        )
        reused = sorted(set(dedicated) & ({"exomem-alpha", "substrate-control-01"} | set(agents)))
        if reused:
            raise ValueError(f"{', '.join(reused)}: a dedicated host reuses another node's name")
        node_ips = [private_ip, control_private_ip] + [
            host["private_node_ip"] for host in (*agents.values(), *dedicated.values())
        ]
        taken = [address for address in node_ips if address is not None]
        shared = sorted({address for address in taken if taken.count(address) > 1})
        if shared:
            raise ValueError(f"{', '.join(shared)}: every node needs its own private address")
    except ValueError as error:
        raise SystemExit(str(error)) from error

    children: dict[str, Any] = {
        "hosted_nodes": {
            "hosts": {
                "exomem-alpha": {
                    "ansible_host": public_ip,
                    "ansible_user": args.user,
                    "private_node_ip": private_ip,
                }
            }
        }
    }

    # Agents are a child group of hosted_nodes, so they inherit its group
    # variables; site.yml's server play targets hosted_nodes:!k3s_agents.
    if agents:
        children["hosted_nodes"]["children"] = {"k3s_agents": {"hosts": agents}}

    # Hosts Terraform does not create are agents too, in a child group the
    # dedicated_host role targets. Their administration address comes with
    # them, so the --admin-addresses map below never touches them.
    if dedicated:
        k3s_agents = children["hosted_nodes"].setdefault("children", {}).setdefault(
            "k3s_agents", {}
        )
        k3s_agents["children"] = {"dedicated_hosts": {"hosts": dedicated}}

    # The control database server is optional here: not every Terraform
    # output set carries it yet (e.g. an apply that predates D12), so it is
    # added only when both of its coordinates are present, rather than
    # required unconditionally.
    if control_public_ip is not None and control_private_ip is not None:
        children["control_nodes"] = {
            "hosts": {
                "substrate-control-01": {
                    "ansible_host": control_public_ip,
                    "ansible_user": args.user,
                    "postgres_private_ip": control_private_ip,
                }
            }
        }

    # Administration runs over the company NetBird; the operator's own SSH
    # arguments supply any proxy, so the inventory carries only the address.
    # One map serves every runbook flow, so names absent from this inventory
    # are ignored.
    hosts: dict[str, dict[str, str]] = {
        **children["hosted_nodes"]["hosts"],
        **agents,
        **children.get("control_nodes", {}).get("hosts", {}),
    }
    for name, host in hosts.items():
        if name in admin_addresses:
            host["ansible_host"] = admin_addresses[name]

    inventory = {"all": {"children": children}}

    args.inventory.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{args.inventory.name}.", dir=args.inventory.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(inventory, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, args.inventory)
        os.chmod(args.inventory, 0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
