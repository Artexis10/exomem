#!/usr/bin/env python3
"""Authorize retirement only from resolved, authenticated rotation receipts."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_VERSION = re.compile(r"v([1-9][0-9]*)\Z")
_DRILL_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class RotationGateError(RuntimeError):
    """Rotation evidence cannot authorize retirement."""


@dataclass(frozen=True)
class RotationResult:
    rotation: str
    old_version: str
    new_version: str


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode()


def contract_digest(contract: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(contract)).hexdigest()


def sign_receipt(receipt: dict[str, Any], key: bytes) -> dict[str, Any]:
    if len(key) < 32 or "authentication" in receipt:
        raise RotationGateError("rotation receipt signing input is invalid")
    signed = dict(receipt)
    signed["authentication"] = {
        "algorithm": "hmac-sha256",
        "key_id": hashlib.sha256(key).hexdigest(),
        "mac": hmac.new(key, _canonical(receipt), hashlib.sha256).hexdigest(),
    }
    return signed


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


def _load_key(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RotationGateError("rotation receipt key must be a mode-0600 regular file")
    key = path.read_bytes()
    if len(key) < 32 or len(key) > 256:
        raise RotationGateError("rotation receipt key is invalid")
    return key


def _load_receipt(*, root: Path, reference: dict[str, Any], key: bytes) -> dict[str, Any]:
    relative = reference.get("receipt_path")
    expected_sha = reference.get("sha256")
    if (
        not isinstance(relative, str)
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or not isinstance(expected_sha, str)
        or not _SHA256.fullmatch(expected_sha)
    ):
        raise RotationGateError("rotation receipt reference is invalid")
    if root.is_symlink() or not root.is_dir():
        raise RotationGateError("rotation receipt reference is unsafe")
    resolved_root = root.resolve(strict=True)
    candidate = resolved_root / relative
    current = resolved_root
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise RotationGateError("rotation receipt reference is unsafe")
    path = candidate.resolve(strict=True)
    if (
        resolved_root not in path.parents
        or not path.is_file()
        or stat.S_IMODE(path.stat().st_mode) != 0o600
    ):
        raise RotationGateError("rotation receipt reference is unsafe")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise RotationGateError("rotation receipt digest does not match")
    try:
        receipt = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RotationGateError("rotation receipt is invalid") from exc
    if not isinstance(receipt, dict):
        raise RotationGateError("rotation receipt is invalid")
    authentication = receipt.get("authentication")
    unsigned = {name: value for name, value in receipt.items() if name != "authentication"}
    if not isinstance(authentication, dict) or set(authentication) != {
        "algorithm",
        "key_id",
        "mac",
    }:
        raise RotationGateError("rotation receipt is unauthenticated")
    expected_mac = hmac.new(key, _canonical(unsigned), hashlib.sha256).hexdigest()
    if (
        authentication.get("algorithm") != "hmac-sha256"
        or authentication.get("key_id") != hashlib.sha256(key).hexdigest()
        or not isinstance(authentication.get("mac"), str)
        or not hmac.compare_digest(authentication["mac"], expected_mac)
    ):
        raise RotationGateError("rotation receipt is unauthenticated")
    return unsigned


def verify_evidence(
    contract: dict[str, Any],
    evidence: dict[str, Any],
    *,
    receipt_root: Path,
    receipt_key: bytes,
) -> RotationResult:
    if contract.get("schema_version") != 1 or evidence.get("schema_version") != 2:
        raise RotationGateError("rotation evidence has an unsupported schema")
    if evidence.get("contract_sha256") != contract_digest(contract):
        raise RotationGateError("rotation evidence is not bound to this contract")
    drill_id = evidence.get("drill_id")
    if not isinstance(drill_id, str) or not _DRILL_ID.fullmatch(drill_id):
        raise RotationGateError("rotation evidence drill identity is invalid")
    recorded_at = _timestamp(evidence.get("recorded_at"))
    rotation = evidence.get("rotation")
    rotations = contract.get("rotations")
    if (
        not isinstance(rotation, str)
        or not isinstance(rotations, dict)
        or rotation not in rotations
    ):
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
        or not isinstance(observations, dict)
        or set(observations) != set(required)
    ):
        raise RotationGateError("retirement evidence is incomplete")
    receipt_paths: set[str] = set()
    for requirement, reference in observations.items():
        if not isinstance(reference, dict) or set(reference) != {"receipt_path", "sha256"}:
            raise RotationGateError("retirement evidence is incomplete")
        receipt_path = reference.get("receipt_path")
        if not isinstance(receipt_path, str) or receipt_path in receipt_paths:
            raise RotationGateError("retirement evidence is incomplete")
        receipt_paths.add(receipt_path)
        receipt = _load_receipt(root=receipt_root, reference=reference, key=receipt_key)
        observed_at = _timestamp(receipt.get("observed_at"))
        if (
            set(receipt)
            != {
                "schema_version",
                "issuer",
                "drill_id",
                "rotation",
                "requirement",
                "old_version",
                "new_version",
                "observed_at",
                "passed",
            }
            or receipt.get("schema_version") != 1
            or receipt.get("issuer") != "exomem-rotation-drill-v1"
            or receipt.get("drill_id") != drill_id
            or receipt.get("rotation") != rotation
            or receipt.get("requirement") != requirement
            or receipt.get("old_version") != old_version
            or receipt.get("new_version") != new_version
            or receipt.get("passed") is not True
            or observed_at > recorded_at
            or (recorded_at - observed_at).total_seconds() > 86400
        ):
            raise RotationGateError("authenticated retirement receipt does not match")
    assert isinstance(old_version, str)
    assert isinstance(new_version, str)
    return RotationResult(rotation=rotation, old_version=old_version, new_version=new_version)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--receipt-key-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        result = verify_evidence(
            contract,
            evidence,
            receipt_root=args.receipt_root,
            receipt_key=_load_key(args.receipt_key_file),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RotationGateError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Retirement authorized for {result.rotation} at {result.new_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
