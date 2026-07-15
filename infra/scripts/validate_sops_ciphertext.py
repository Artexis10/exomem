#!/usr/bin/env python3
"""Validate every tracked SOPS artifact against its exact destination contract."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "infra/contracts/secret-destinations-v1.json"
_VERSION = r"v[1-9][0-9]*"


@dataclass(frozen=True)
class Destination:
    secret_name: str
    destination_id: str
    kind: str
    target: str
    fields: dict[str, str]
    pattern: re.Pattern[str]


def _tracked_secret_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "infra/secrets"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("could not enumerate tracked secret artifacts")
    return [root / line for line in result.stdout.splitlines() if line]


def _destinations(matrix: dict[str, Any]) -> list[Destination]:
    secrets = matrix.get("secrets")
    if matrix.get("schema_version") != 1 or not isinstance(secrets, dict):
        raise RuntimeError("secret destination matrix is invalid")
    destinations: list[Destination] = []
    targets: set[str] = set()
    for secret_name, secret in secrets.items():
        raw_destinations = secret.get("destinations") if isinstance(secret, dict) else None
        if not isinstance(secret_name, str) or not isinstance(raw_destinations, dict):
            raise RuntimeError("secret destination matrix is invalid")
        for destination_id, raw in raw_destinations.items():
            if not isinstance(destination_id, str) or not isinstance(raw, dict):
                raise RuntimeError("secret destination matrix is invalid")
            kind = raw.get("kind")
            if kind not in {"sops_k8s_secret", "sops_ansible_vars", "sops_escrow"}:
                continue
            target = raw.get("target")
            if (
                not isinstance(target, str)
                or target.count("{version}") != 1
                or target in targets
                or Path(target).is_absolute()
                or ".." in Path(target).parts
            ):
                raise RuntimeError("secret destination matrix has an invalid SOPS target")
            fields = {key: value for key, value in raw.items() if isinstance(value, str)}
            targets.add(target)
            expression = re.escape(target).replace(re.escape("{version}"), _VERSION)
            destinations.append(
                Destination(
                    secret_name,
                    destination_id,
                    kind,
                    target,
                    fields,
                    re.compile(expression + r"\Z"),
                )
            )
    if not destinations:
        raise RuntimeError("secret destination matrix has no SOPS destinations")
    return destinations


def _encrypted_leaf(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("ENC[") and value.endswith("]")


def _require_all_payload_leaves_encrypted(value: Any) -> None:
    if isinstance(value, dict):
        if not value:
            raise RuntimeError("tracked SOPS artifact has an empty payload container")
        for item in value.values():
            _require_all_payload_leaves_encrypted(item)
        return
    if isinstance(value, list):
        if not value:
            raise RuntimeError("tracked SOPS artifact has an empty payload container")
        for item in value:
            _require_all_payload_leaves_encrypted(item)
        return
    if not _encrypted_leaf(value):
        raise RuntimeError("tracked SOPS artifact contains a plaintext payload leaf")


def _require_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise RuntimeError(f"tracked SOPS artifact has an invalid {label} shape")
    return value


def _validate_shape(document: dict[str, Any], destination: Destination) -> None:
    payload = {key: value for key, value in document.items() if key != "sops"}
    if destination.kind == "sops_k8s_secret":
        _require_exact_keys(
            payload,
            {"apiVersion", "kind", "metadata", "type", "stringData"},
            "Kubernetes Secret",
        )
        metadata = _require_exact_keys(
            payload["metadata"], {"name", "namespace", "labels"}, "Secret metadata"
        )
        _require_exact_keys(
            metadata["labels"],
            {"app.kubernetes.io/managed-by", "exomem.io/secret-version"},
            "Secret labels",
        )
        _require_exact_keys(
            payload["stringData"], {destination.fields["key"]}, "Secret stringData"
        )
    elif destination.kind == "sops_ansible_vars":
        _require_exact_keys(payload, {destination.fields["variable"]}, "Ansible variables")
    else:
        _require_exact_keys(
            payload,
            {
                "schema_version",
                "secret_name",
                "secret_version",
                destination.fields["secret_key"],
            },
            "escrow",
        )
    _require_all_payload_leaves_encrypted(payload)


def validate_artifact(path: Path, *, root: Path, destinations: list[Destination]) -> None:
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError("tracked SOPS artifact escapes the repository root") from exc
    lowered = path.name.lower()
    if any(token in lowered for token in (".dec.", ".plain.", ".decrypted.", "age.key", ".agekey")):
        raise RuntimeError("tracked plaintext secret artifact is forbidden")
    matches = [destination for destination in destinations if destination.pattern.fullmatch(relative)]
    if len(matches) != 1:
        raise RuntimeError("tracked SOPS artifact does not match exactly one destination")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("tracked SOPS artifact is invalid") from exc
    if not isinstance(document, dict):
        raise RuntimeError("tracked SOPS artifact has no encrypted payload")
    sops = document.get("sops")
    if (
        not isinstance(sops, dict)
        or not _encrypted_leaf(sops.get("mac"))
        or not isinstance(sops.get("version"), str)
        or not any(isinstance(sops.get(key), list) and bool(sops[key]) for key in ("age", "pgp"))
    ):
        raise RuntimeError("tracked SOPS artifact has invalid encryption metadata")
    _validate_shape(document, matches[0])


def validate(
    *, matrix_path: Path = DEFAULT_MATRIX, artifacts: list[Path] | None = None, root: Path = ROOT
) -> int:
    try:
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("secret destination matrix is invalid") from exc
    destinations = _destinations(matrix)
    candidates = artifacts if artifacts is not None else _tracked_secret_files(root)
    checked = 0
    for path in candidates:
        try:
            relative = path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise RuntimeError("tracked SOPS artifact escapes the repository root") from exc
        if path.name == "README.md" or "receipts" in relative.parts:
            continue
        validate_artifact(path, root=root, destinations=destinations)
        checked += 1
    return checked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--artifact", type=Path, action="append")
    parser.add_argument("--require-artifact", action="store_true")
    args = parser.parse_args()
    try:
        checked = validate(matrix_path=args.matrix, artifacts=args.artifact)
        if args.require_artifact and checked == 0:
            raise RuntimeError("SOPS ciphertext validation exercised no artifacts")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"SOPS ciphertext validation passed: {checked} artifact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
