#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
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
    return parser


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

    # The control database server is optional here: not every Terraform
    # output set carries it yet (e.g. an apply that predates D12), so it is
    # added only when both of its coordinates are present, rather than
    # required unconditionally.
    if control_public_ip is not None and control_private_ip is not None:
        children["control_nodes"] = {
            "hosts": {
                "exomem-control-db": {
                    "ansible_host": control_public_ip,
                    "ansible_user": args.user,
                    "postgres_private_ip": control_private_ip,
                }
            }
        }

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
