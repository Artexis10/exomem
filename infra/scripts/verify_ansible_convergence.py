#!/usr/bin/env python3
"""Run the governed Ansible bootstrap twice and fail on second-run drift."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


class ConvergenceError(RuntimeError):
    """A content-free convergence failure."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", type=Path, default=Path(__file__).with_name("ansible_with_sops.sh"))
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--vars", type=Path, action="append", required=True)
    parser.add_argument("ansible_args", nargs=argparse.REMAINDER)
    return parser


def _safe_file(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise ConvergenceError(f"{label} must be a regular file")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ConvergenceError(f"{label} must be a regular file")
    return resolved


def _parse_stats(output: str) -> dict[str, dict[str, int]]:
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end < start:
        raise ConvergenceError("Ansible did not emit machine-readable convergence results")
    try:
        document: Any = json.loads(output[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ConvergenceError("Ansible emitted invalid convergence results") from exc
    stats = document.get("stats") if isinstance(document, dict) else None
    if not isinstance(stats, dict) or not stats:
        raise ConvergenceError("Ansible convergence results contain no hosts")
    normalized: dict[str, dict[str, int]] = {}
    for host, raw in stats.items():
        if not isinstance(host, str) or not isinstance(raw, dict):
            raise ConvergenceError("Ansible convergence results have invalid host statistics")
        values: dict[str, int] = {}
        for key in ("changed", "failures", "unreachable"):
            value = raw.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConvergenceError("Ansible convergence results have invalid counters")
            values[key] = value
        normalized[host] = values
    return normalized


def _run(command: list[str], environment: dict[str, str]) -> dict[str, dict[str, int]]:
    try:
        result = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConvergenceError("Ansible convergence run failed") from exc
    if result.returncode != 0:
        raise ConvergenceError("Ansible convergence run failed")
    return _parse_stats(result.stdout)


def main() -> int:
    args = _parser().parse_args()
    try:
        runner = _safe_file(args.runner, "runner")
        inventory = _safe_file(args.inventory, "inventory")
        if stat.S_IMODE(inventory.stat().st_mode) != 0o600:
            raise ConvergenceError("inventory must have mode 0600")
        variables = [_safe_file(path, "SOPS variables artifact") for path in args.vars]
        command = [str(runner), "--inventory", str(inventory)]
        for path in variables:
            command.extend(("--vars", str(path)))
        passthrough = args.ansible_args
        if passthrough and passthrough[0] == "--":
            passthrough = passthrough[1:]
        if passthrough:
            command.extend(("--", *passthrough))

        environment = {
            **os.environ,
            "ANSIBLE_STDOUT_CALLBACK": "content_free_json",
            "ANSIBLE_CALLBACK_PLUGINS": str(
                Path(__file__).resolve().parents[1] / "ansible/callback_plugins"
            ),
        }
        first = _run(command, environment)
        second = _run(command, environment)
        for stats in (first, second):
            if any(values["failures"] or values["unreachable"] for values in stats.values()):
                raise ConvergenceError("Ansible convergence run did not complete cleanly")
        changed = sum(values["changed"] for values in second.values())
        if changed:
            noun = "task" if changed == 1 else "tasks"
            raise ConvergenceError(f"second Ansible run changed {changed} {noun}")
    except (ConvergenceError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("Ansible convergence verified: second run changed=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
