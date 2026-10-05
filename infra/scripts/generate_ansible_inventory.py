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
