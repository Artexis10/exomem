#!/usr/bin/env python3
"""Print the --vars arguments for one host group's active SOPS Ansible variables.

infra/contracts/active-ansible-selection-v1.json names, for each activated
Ansible variable destination, the version in use. Escrowing a new version
activates nothing: a reviewed commit to that file does. A destination it does
not name has no active version and is not passed. The convergence gate and
every runbook that runs site.yml read this output.

Refuses, naming the destination, when the selection names a destination the
secret matrix does not declare, or a version whose file is missing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "infra/contracts/secret-destinations-v1.json"
SELECTION = ROOT / "infra/contracts/active-ansible-selection-v1.json"
_VERSION = re.compile(r"v[1-9][0-9]*")


def active_files(
    group: str, matrix_path: Path = MATRIX, selection_path: Path = SELECTION, root: Path = ROOT
) -> list[Path]:
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    targets = {
        destination_id: destination["target"]
        for secret in matrix["secrets"].values()
        for destination_id, destination in secret["destinations"].items()
        if destination.get("kind") == "sops_ansible_vars"
    }
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    destinations = selection.get("destinations")
    if selection.get("schema_version") != 1 or not isinstance(destinations, dict):
        raise ValueError("the active Ansible selection is invalid")
    files: list[Path] = []
    for destination_id, version in destinations.items():
        if destination_id not in targets:
            raise ValueError(f"{destination_id}: not an Ansible destination in the secret matrix")
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise ValueError(f"{destination_id}: the selected version must look like v1")
        path = root / targets[destination_id].replace("{version}", version)
        if not path.is_file():
            raise ValueError(f"{destination_id}: the selected {version} file is missing")
        if destination_id.startswith(f"ansible.{group}."):
            files.append(path)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", help="the destination group, such as hosted-node")
    args = parser.parse_args()
    try:
        files = active_files(args.group)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    if not files:
        print(f"no active Ansible variables for ansible.{args.group}", file=sys.stderr)
        return 1
    for path in files:
        print("--vars")
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
