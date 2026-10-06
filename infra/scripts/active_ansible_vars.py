#!/usr/bin/env python3
"""Print the --vars arguments for one host group's active SOPS Ansible variables.

The secret destination matrix declares every Ansible variable destination, as
`ansible.<group>.<name>.active` with a versioned target. secret_handoff.py only
ever writes a version higher than every existing one, so the highest version
on disk is the active one. The convergence gate and every runbook that runs
site.yml read this output, so a rotation to a new version is picked up by all
of them at once. Destinations with no file yet are skipped; a play that needs
one refuses without it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "infra/contracts/secret-destinations-v1.json"


def active_files(group: str, matrix_path: Path = MATRIX, root: Path = ROOT) -> list[Path]:
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    prefix = f"ansible.{group}."
    files: list[Path] = []
    for secret in matrix["secrets"].values():
        for destination_id, destination in secret["destinations"].items():
            if destination.get("kind") != "sops_ansible_vars" or not destination_id.startswith(prefix):
                continue
            before, after = destination["target"].split("{version}")
            directory = root / Path(before).parent
            pattern = re.compile(
                re.escape(Path(before).name) + r"v([1-9][0-9]*)" + re.escape(after)
            )
            versions = [
                (int(match.group(1)), path)
                for path in (directory.iterdir() if directory.is_dir() else [])
                if (match := pattern.fullmatch(path.name)) and path.is_file()
            ]
            if versions:
                files.append(max(versions)[1])
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", help="the destination group, such as hosted-node")
    args = parser.parse_args()
    files = active_files(args.group)
    if not files:
        print(f"no active Ansible variables for ansible.{args.group}", file=sys.stderr)
        return 1
    for path in files:
        print("--vars")
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
