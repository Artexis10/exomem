#!/usr/bin/env python3
"""Apply the exact active K3s ciphertext set from a signed version registry."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_VERSION = re.compile(r"v[1-9][0-9]*\Z")


class ActiveSecretRegistryError(RuntimeError):
    """The active-secret registry cannot authorize a cluster mutation."""


@dataclass(frozen=True)
class ActiveSecret:
    destination: str
    secret: str
    version: str
    artifact: Path


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode()


def _safe_public_file(path: Path, description: str) -> bytes:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o022:
        raise ActiveSecretRegistryError(f"{description} must be a non-writable regular file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ActiveSecretRegistryError(f"{description} cannot be read") from exc


def _public_key(path: Path, trust_contract_path: Path) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(_safe_public_file(path, "registry public key"))
    except ValueError as exc:
        raise ActiveSecretRegistryError("registry public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ActiveSecretRegistryError("registry public key is invalid")
    try:
        trust = json.loads(_safe_public_file(trust_contract_path, "registry trust contract"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActiveSecretRegistryError("registry trust contract is invalid") from exc
    raw_public_key = key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    if (
        not isinstance(trust, dict)
        or set(trust)
        != {
            "schema_version",
            "algorithm",
            "public_key_id",
            "private_key_custody",
        }
        or trust.get("schema_version") != 1
        or trust.get("algorithm") != "ed25519"
        or trust.get("private_key_custody") != "secret-release-custodian-only"
        or trust.get("public_key_id") != hashlib.sha256(raw_public_key).hexdigest()
    ):
        raise ActiveSecretRegistryError("registry public key is not trusted")
    return key


def _verified_registry(path: Path, public_key: Ed25519PublicKey) -> dict[str, Any]:
    try:
        document = json.loads(_safe_public_file(path, "active-secret registry"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActiveSecretRegistryError("active-secret registry is invalid") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "matrix_sha256",
        "destinations",
        "authentication",
    }:
        raise ActiveSecretRegistryError("active-secret registry is invalid")
    authentication = document["authentication"]
    unsigned = {key: value for key, value in document.items() if key != "authentication"}
    if not isinstance(authentication, dict) or set(authentication) != {
        "algorithm",
        "key_id",
        "signature",
    }:
        raise ActiveSecretRegistryError("active-secret registry signature is invalid")
    raw_public_key = public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    signature = authentication.get("signature")
    if (
        authentication.get("algorithm") != "ed25519"
        or authentication.get("key_id") != hashlib.sha256(raw_public_key).hexdigest()
        or not isinstance(signature, str)
    ):
        raise ActiveSecretRegistryError("active-secret registry signature is invalid")
    try:
        public_key.verify(bytes.fromhex(signature), _canonical(unsigned))
    except (ValueError, InvalidSignature) as exc:
        raise ActiveSecretRegistryError("active-secret registry signature is invalid") from exc
    return unsigned


def load_registry(
    *,
    matrix_path: Path,
    registry_path: Path,
    public_key_path: Path,
    trust_contract_path: Path,
) -> tuple[ActiveSecret, ...]:
    matrix_raw = _safe_public_file(matrix_path, "secret destination matrix")
    try:
        matrix = json.loads(matrix_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActiveSecretRegistryError("secret destination matrix is invalid") from exc
    if not isinstance(matrix, dict) or matrix.get("schema_version") != 1:
        raise ActiveSecretRegistryError("secret destination matrix is invalid")
    expected: dict[str, tuple[str, str]] = {}
    secrets = matrix.get("secrets")
    if not isinstance(secrets, dict):
        raise ActiveSecretRegistryError("secret destination matrix is invalid")
    for secret_name, secret in secrets.items():
        destinations = secret.get("destinations") if isinstance(secret, dict) else None
        if not isinstance(destinations, dict):
            raise ActiveSecretRegistryError("secret destination matrix is invalid")
        for destination_id, destination in destinations.items():
            if (
                isinstance(destination, dict)
                and destination.get("kind") == "sops_k8s_secret"
                and destination.get("slot") == "active"
            ):
                target = destination.get("target")
                if not isinstance(target, str) or target.count("{version}") != 1:
                    raise ActiveSecretRegistryError("secret destination matrix is invalid")
                expected[destination_id] = (secret_name, target)
    registry = _verified_registry(
        registry_path,
        _public_key(public_key_path, trust_contract_path),
    )
    if (
        registry.get("schema_version") != 1
        or registry.get("matrix_sha256") != hashlib.sha256(matrix_raw).hexdigest()
        or not isinstance(registry.get("destinations"), dict)
    ):
        raise ActiveSecretRegistryError("active-secret registry is not bound to this matrix")
    destinations = registry["destinations"]
    if set(destinations) != set(expected):
        raise ActiveSecretRegistryError(
            "active-secret registry must contain the exact active destination set"
        )

    repository_root = Path(__file__).resolve().parents[2]
    active: list[ActiveSecret] = []
    for destination_id in sorted(expected):
        entry = destinations[destination_id]
        secret_name, target = expected[destination_id]
        if not isinstance(entry, dict) or set(entry) != {
            "secret",
            "version",
            "artifact_sha256",
        }:
            raise ActiveSecretRegistryError("active-secret registry entry is invalid")
        version = entry.get("version")
        digest = entry.get("artifact_sha256")
        if (
            entry.get("secret") != secret_name
            or not isinstance(version, str)
            or not _VERSION.fullmatch(version)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            raise ActiveSecretRegistryError("active-secret registry entry is invalid")
        artifact = Path(target.format(version=version))
        if not artifact.is_absolute():
            artifact = repository_root / artifact
        if artifact.is_symlink() or not artifact.is_file():
            raise ActiveSecretRegistryError("active ciphertext artifact is unavailable")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
            raise ActiveSecretRegistryError("active ciphertext artifact digest does not match")
        active.append(
            ActiveSecret(
                destination=destination_id,
                secret=secret_name,
                version=version,
                artifact=artifact,
            )
        )
    return tuple(active)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--registry-public-key", type=Path, required=True)
    parser.add_argument("--trust-contract", type=Path, required=True)
    parser.add_argument(
        "--apply-script",
        type=Path,
        default=Path(__file__).with_name("apply_sops_secret.py"),
    )
    parser.add_argument("--sops", default="sops")
    parser.add_argument("--kubectl", default="kubectl")
    args = parser.parse_args()
    try:
        active = load_registry(
            matrix_path=args.matrix,
            registry_path=args.registry,
            public_key_path=args.registry_public_key,
            trust_contract_path=args.trust_contract,
        )
        for item in active:
            result = subprocess.run(
                [
                    sys.executable,
                    str(args.apply_script),
                    "--matrix",
                    str(args.matrix),
                    "--destination",
                    item.destination,
                    "--artifact",
                    str(item.artifact),
                    "--sops",
                    args.sops,
                    "--kubectl",
                    args.kubectl,
                ],
                check=False,
                capture_output=True,
                timeout=60,
            )
            if result.returncode != 0:
                raise ActiveSecretRegistryError(
                    f"active secret application failed for {item.destination}"
                )
            print(f"Applied active destination {item.destination} at {item.version}")
    except (OSError, subprocess.TimeoutExpired, ActiveSecretRegistryError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
