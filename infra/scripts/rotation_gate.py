#!/usr/bin/env python3
"""Fail closed unless a staged secret rotation has content-free retirement proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_VERSION = re.compile(r"v([1-9][0-9]*)\Z")
_DRILL_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_REFERENCE = re.compile(r"(?:receipt|probe|deployment|audit|metric):[A-Za-z0-9._-]{8,128}\Z")
_PROOF_TYPES = {"receipt", "probe", "deployment", "audit", "metric"}


class RotationGateError(RuntimeError):
    """Rotation evidence cannot authorize retirement."""


@dataclass(frozen=True)
class RotationResult:
    rotation: str
    old_version: str
    new_version: str


def contract_digest(contract: dict[str, Any]) -> str:
    canonical = json.dumps(contract, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RotationGateError("rotation evidence timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RotationGateError("rotation evidence timestamp is invalid") from exc
    if parsed.tzinfo != UTC:
        raise RotationGateError("rotation evidence timestamp is invalid")
    return parsed


def verify_evidence(contract: dict[str, Any], evidence: dict[str, Any]) -> RotationResult:
    if contract.get("schema_version") != 1 or evidence.get("schema_version") != 1:
        raise RotationGateError("rotation evidence has an unsupported schema")
    if evidence.get("contract_sha256") != contract_digest(contract):
        raise RotationGateError("rotation evidence is not bound to this contract")
    drill_id = evidence.get("drill_id")
    if not isinstance(drill_id, str) or not _DRILL_ID.fullmatch(drill_id):
        raise RotationGateError("rotation evidence drill identity is invalid")
    recorded_at = _timestamp(evidence.get("recorded_at"))
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
    ):
        raise RotationGateError("retirement evidence is incomplete")
    references: set[str] = set()
    for observation in observations.values():
        if not isinstance(observation, dict) or set(observation) != {
            "passed",
            "observed_at",
            "proof_type",
            "reference",
        }:
            raise RotationGateError("retirement evidence is incomplete")
        proof_type = observation.get("proof_type")
        reference = observation.get("reference")
        observed_at = _timestamp(observation.get("observed_at"))
        if (
            observation.get("passed") is not True
            or proof_type not in _PROOF_TYPES
            or not isinstance(reference, str)
            or not _REFERENCE.fullmatch(reference)
            or not reference.startswith(f"{proof_type}:")
            or reference in references
            or observed_at > recorded_at
            or (recorded_at - observed_at).total_seconds() > 86400
        ):
            raise RotationGateError("retirement evidence is incomplete")
        references.add(reference)
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
