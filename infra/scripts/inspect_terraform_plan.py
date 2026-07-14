#!/usr/bin/env python3
"""Reject unapproved destructive Terraform plans without printing plan values."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_DESTRUCTIVE = frozenset({"delete"})
_KNOWN_ACTIONS = frozenset({"no-op", "read", "create", "update", "delete"})
_SECRET_OUTPUT = re.compile(
    r"(?:^|_)(?:secret|token|password|credential|private_key|application_key|access_key)(?:_|$)",
    re.IGNORECASE,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect `terraform show -json` output without echoing values."
    )
    parser.add_argument("plan_json", type=Path)
    parser.add_argument(
        "--allow-destructive",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="approve replacement/deletion for one exact resource address",
    )
    return parser


def _load(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("plan JSON is unreadable or invalid") from exc
    if not isinstance(value, dict) or not isinstance(value.get("resource_changes", []), list):
        raise ValueError("plan JSON has an invalid resource-change envelope")
    return value


def inspect(plan: dict[str, Any], approvals: set[str]) -> list[str]:
    errors: list[str] = []
    for item in plan.get("resource_changes", []):
        if not isinstance(item, dict):
            errors.append("resource change has an invalid shape")
            continue
        address = item.get("address")
        change = item.get("change")
        actions = change.get("actions") if isinstance(change, dict) else None
        if not isinstance(address, str) or not isinstance(actions, list) or not all(
            isinstance(action, str) for action in actions
        ):
            errors.append("resource change has an invalid address/action shape")
            continue
        unknown = set(actions) - _KNOWN_ACTIONS
        if unknown:
            errors.append(f"{address}: unknown plan action")
            continue
        if _DESTRUCTIVE.intersection(actions) and address not in approvals:
            errors.append(f"{address}: replacement/deletion lacks exact approval")

    outputs = plan.get("output_changes", {})
    if not isinstance(outputs, dict):
        errors.append("output changes have an invalid shape")
    else:
        for name, change in outputs.items():
            if not isinstance(name, str) or not isinstance(change, dict):
                errors.append("output change has an invalid shape")
                continue
            if _SECRET_OUTPUT.search(name) and change.get("after_sensitive") is not True:
                errors.append(f"{name}: secret-like output is not marked sensitive")
    return errors


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = _load(args.plan_json)
        approvals = set(args.allow_destructive)
        if len(approvals) != len(args.allow_destructive):
            raise ValueError("destructive approvals must be unique exact addresses")
        errors = inspect(plan, approvals)
    except ValueError as exc:
        print(f"plan policy rejected: {exc}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(f"plan policy rejected: {error}", file=sys.stderr)
        return 2
    change_count = len(plan.get("resource_changes", []))
    print(f"plan policy accepted: {change_count} resource changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
