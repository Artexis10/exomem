#!/usr/bin/env python3
"""Reject unapproved destructive Terraform plans without printing plan values."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
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
    parser.add_argument("plan_json", type=Path, nargs="?")
    parser.add_argument("--state-only", action="store_true", help="reject every provider mutation and import")
    parser.add_argument("--saved-plan", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--state-transfer", type=Path, help="verify a private manifest of native state-move snapshots")
    parser.add_argument(
        "--review-output", type=Path,
        help="write resource addresses/actions only; never variables, state or resource values",
    )
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


def inspect(plan: dict[str, Any], approvals: set[str], *, state_only: bool = False) -> list[str]:
    errors: list[str] = []
    for item in plan.get("resource_changes", []):
        if not isinstance(item, dict):
            errors.append("resource change has an invalid shape")
            continue
        address = item.get("address")
        change = item.get("change")
        if not isinstance(change, dict):
            errors.append("resource change has an invalid address/action shape")
            continue
        actions = change.get("actions")
        if not isinstance(address, str) or not isinstance(actions, list) or not all(
            isinstance(action, str) for action in actions
        ):
            errors.append("resource change has an invalid address/action shape")
            continue
        unknown = set(actions) - _KNOWN_ACTIONS
        if unknown:
            errors.append(f"{address}: unknown plan action")
            continue
        if state_only and (actions not in (["no-op"], ["read"]) or change.get("importing") is not None):
            errors.append(f"{address}: provider operation is outside the state-only contract")
        if state_only and actions == ["read"] and item.get("mode") != "data":
            errors.append(f"{address}: managed-resource read is outside the state-only contract")
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
            removed = change.get("actions") == ["delete"] and change.get("after") is None
            if _SECRET_OUTPUT.search(name) and not removed and change.get("after_sensitive") is not True:
                errors.append(f"{name}: secret-like output is not marked sensitive")
    if state_only:
        if plan.get("action_invocations"):
            errors.append("state-only plan invokes external actions")
        configuration = plan.get("configuration", {})
        modules = [configuration.get("root_module", {})]
        while modules:
            module = modules.pop()
            if any(resource.get("provisioners") for resource in module.get("resources", [])):
                errors.append("state-only configuration contains a provisioner")
            modules.extend(call["module"] for call in module.get("module_calls", {}).values() if "module" in call)
    return errors


def verify_saved_plan(plan: dict[str, Any], saved: Path, digest: str) -> None:
    try:
        actual = hashlib.sha256(saved.read_bytes()).hexdigest()
        result = subprocess.run(["terraform", "show", "-json", str(saved.resolve())], capture_output=True, check=False)
        produced = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("saved plan could not be verified") from exc
    if actual != digest or produced != plan:
        raise ValueError("saved plan or JSON differs from the reviewed state-only plan")


def verify_transfer(manifest: dict[str, Any]) -> int:
    """Prove native Terraform moves changed only the manifest-selected ownership."""
    if manifest.get("schema_version") != 1:
        raise ValueError("state-transfer manifest has an unsupported schema")
    selected = manifest.get("resources")
    if not isinstance(selected, list) or not selected:
        raise ValueError("state transfer must select resources")
    expected: dict[str, str] = {}
    for resource in selected:
        if not isinstance(resource, dict) or not all(isinstance(resource.get(k), str) and resource[k] for k in ("address", "provider_id")):
            raise ValueError("state-transfer resource has an invalid shape")
        if resource["address"] in expected:
            raise ValueError("state-transfer addresses must be unique")
        expected[resource["address"]] = resource["provider_id"]
    snapshots = manifest.get("snapshots")
    if not isinstance(snapshots, dict):
        raise ValueError("state-transfer snapshots are missing")
    states: dict[str, dict[str, Any]] = {}
    for name in ("source_before", "source_after", "target_before", "target_after"):
        entry = snapshots.get(name)
        if name == "target_before" and entry is None:
            states[name] = {"resources": [], "outputs": {}}
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("state-transfer snapshot has an invalid shape")
        path = Path(entry["path"])
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError("state-transfer snapshot is unreadable") from exc
        if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            raise ValueError("state-transfer snapshot differs from its reviewed hash")
        states[name] = _load(path)
    records: dict[str, dict[str, Any]] = {}
    for name, state in states.items():
        resources = state.get("resources")
        if not isinstance(resources, list):
            raise ValueError("state-transfer resources have an invalid shape")
        records[name] = {}
        for resource in resources:
            if not isinstance(resource, dict) or not all(isinstance(resource.get(k), str) for k in ("mode", "type", "name")):
                raise ValueError("state-transfer resource has an invalid shape")
            parts = [resource.get("module"), "data" if resource["mode"] == "data" else None, resource["type"], resource["name"]]
            address = ".".join(part for part in parts if part)
            if address in records[name]:
                raise ValueError("state-transfer resource addresses are duplicated")
            records[name][address] = resource
    source = records["source_before"]
    destination = records["target_before"]
    for address, provider_id in expected.items():
        resource = source.get(address)
        instances = resource.get("instances", []) if resource else []
        if address in destination or len(instances) != 1 or str(instances[0].get("attributes", {}).get("id")) != provider_id:
            raise ValueError("state transfer does not match the single-owner resource manifest")
    if records["source_after"] != {k: v for k, v in source.items() if k not in expected}:
        raise ValueError("state transfer changes unselected source resources")
    for address, provider_id in expected.items():
        resource_type = source[address]["type"]
        for checkpoint, placement in (("before", (1, 0)), ("after", (0, 1))):
            owners = []
            for side in ("source", "target"):
                # Terraform aliases and indexed instances can own the same physical resource.
                count = sum(
                    str(instance.get("attributes", {}).get("id")) == provider_id
                    for resource in records[side + "_" + checkpoint].values()
                    if resource["mode"] == "managed" and resource["type"] == resource_type
                    for instance in resource.get("instances", [])
                )
                owners.append(count)
            if tuple(owners) != placement:
                raise ValueError("state transfer has duplicate or misplaced physical ownership")
    transferred = {}
    for address in expected:
        # Native state mv drops unavailable dependency metadata; the target plan rebuilds it.
        after = records["target_after"].get(address, {})
        instances = after.get("instances", [])
        if len(instances) != 1 or not set(instances[0].get("dependencies", [])) <= set(source[address]["instances"][0].get("dependencies", [])):
            raise ValueError("state transfer introduces an invalid dependency")
        transferred[address] = {
            **source[address],
            "instances": [{k: v for k, v in instance.items() if k != "dependencies"} for instance in source[address]["instances"]],
        }
    normalized_target = {
        address: {**resource, "instances": [{k: v for k, v in instance.items() if k != "dependencies"} for instance in resource["instances"]]}
        if address in expected else resource
        for address, resource in records["target_after"].items()
    }
    if normalized_target != {**destination, **transferred}:
        raise ValueError("state transfer changes resource contents or target ownership")
    # Terraform updates serial/version and drops derived check results during state mv.
    metadata = {"resources", "serial", "terraform_version", "check_results"}
    for side in ("source", "target"):
        before, after = states[side + "_before"], states[side + "_after"]
        if side == "target" and snapshots.get("target_before") is None:
            if after.get("outputs") != {} or not isinstance(after.get("lineage"), str):
                raise ValueError("new target state contains unexpected metadata")
        elif {k: v for k, v in before.items() if k not in metadata} != {k: v for k, v in after.items() if k not in metadata}:
            raise ValueError("state transfer changes outputs or lineage")
    return len(expected)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.state_transfer is not None:
            if args.plan_json is not None or args.state_only or args.saved_plan is not None or args.plan_sha256 is not None or args.allow_destructive or args.review_output is not None:
                raise ValueError("state transfer cannot be combined with plan options")
            count = verify_transfer(_load(args.state_transfer))
            print(f"state transfer accepted: {count} preserved resources")
            return 0
        if args.plan_json is None:
            raise ValueError("plan JSON is required")
        plan = _load(args.plan_json)
        if args.saved_plan is not None or args.plan_sha256 is not None:
            if not args.state_only or args.saved_plan is None or args.plan_sha256 is None:
                raise ValueError("saved-plan binding requires state-only mode and a digest")
            verify_saved_plan(plan, args.saved_plan, args.plan_sha256)
        approvals = set(args.allow_destructive)
        if len(approvals) != len(args.allow_destructive):
            raise ValueError("destructive approvals must be unique exact addresses")
        if args.state_only and approvals:
            raise ValueError("state-only mode cannot approve destructive actions")
        errors = inspect(plan, approvals, state_only=args.state_only)
    except ValueError as exc:
        print(f"plan policy rejected: {exc}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(f"plan policy rejected: {error}", file=sys.stderr)
        return 2
    if args.review_output is not None:
        if args.review_output.resolve() == args.plan_json.resolve():
            print("plan policy rejected: review output must differ from raw input", file=sys.stderr)
            return 2
        review = {
            "values_included": False,
            "resource_changes": [
                {"address": item["address"], "actions": item["change"]["actions"]}
                for item in plan.get("resource_changes", [])
            ],
        }
        try:
            args.review_output.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")
        except OSError:
            print("plan policy rejected: review artifact could not be written", file=sys.stderr)
            return 2
    change_count = len(plan.get("resource_changes", []))
    print(f"plan policy accepted: {change_count} resource changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
