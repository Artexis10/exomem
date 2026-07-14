#!/usr/bin/env python3
"""Fail closed unless a staged secret rotation has content-free retirement proof."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_VERSION = re.compile(r"v([1-9][0-9]*)\Z")


class RotationGateError(RuntimeError):
    """Rotation evidence cannot authorize retirement."""


@dataclass(frozen=True)
class RotationResult:
    rotation: str
    old_version: str
    new_version: str


def verify_evidence(contract: dict[str, Any], evidence: dict[str, Any]) -> RotationResult:
    if contract.get("schema_version") != 1 or evidence.get("schema_version") != 1:
        raise RotationGateError("rotation evidence has an unsupported schema")
    rotation = evidence.get("rotation")
    rotations = contract.get("rotations")
    if not isinstance(rotation, str) or not isinstance(rotations, dict) or rotation not in rotations:
        raise RotationGateError("rotation evidence names an unsupported rotation")
    old_version = evidence.get("old_version")
    new_version = evidence.get("new_version")
    old_match = _VERSION.fullmatch(old_version) if isinstance(old_version, str) else None
    new_match = _VERSION.fullmatch(new_version) if isinstance(new_version, str) else None
    if not old_match or not new_match or int(new_match.group(1)) <= int(old_match.group(1)):
        raise RotationGateError("rotation evidence versions are not strictly increasing")
    definition = rotations[rotation]
    required = definition.get("retirement_requires") if isinstance(definition, dict) else None
    observations = evidence.get("observations")
    if (
        not isinstance(required, list)
        or not required
        or any(not isinstance(item, str) or not item for item in required)
        or not isinstance(observations, dict)
        or set(observations) != set(required)
        or any(value is not True for value in observations.values())
    ):
        raise RotationGateError("retirement evidence is incomplete")
    assert isinstance(old_version, str)
    assert isinstance(new_version, str)
    return RotationResult(rotation=rotation, old_version=old_version, new_version=new_version)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        result = verify_evidence(contract, evidence)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RotationGateError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Retirement authorized for {result.rotation} at {result.new_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
