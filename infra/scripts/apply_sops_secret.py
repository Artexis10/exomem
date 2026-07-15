#!/usr/bin/env python3
"""Decrypt one allowlisted SOPS Kubernetes Secret directly into server-side apply."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_VERSION = re.compile(r"(?:^|\.)v([1-9][0-9]*)(?:\.|$)")


class SecretApplyError(RuntimeError):
    """A content-free static-secret application failure."""


@dataclass(frozen=True)
class Destination:
    target: str
    namespace: str
    secret_name: str
    key: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--sops", default="sops")
    parser.add_argument("--kubectl", default="kubectl")
    return parser


def _load_destination(matrix_path: Path, destination_id: str) -> Destination:
    try:
        document = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecretApplyError("secret destination matrix is invalid") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise SecretApplyError("secret destination matrix is invalid")
    matches: list[dict[str, Any]] = []
    secrets = document.get("secrets")
    if isinstance(secrets, dict):
        for secret in secrets.values():
            destinations = secret.get("destinations") if isinstance(secret, dict) else None
            if isinstance(destinations, dict) and destination_id in destinations:
                candidate = destinations[destination_id]
                if isinstance(candidate, dict):
                    matches.append(candidate)
    if len(matches) != 1:
        raise SecretApplyError("secret destination is not uniquely allowlisted")
    item = matches[0]
    if item.get("kind") != "sops_k8s_secret" or item.get("slot") != "active":
        raise SecretApplyError("secret destination is not an active Kubernetes Secret")
    required = ("target", "namespace", "kubernetes_secret", "key")
    if any(not isinstance(item.get(field), str) or not item[field] for field in required):
        raise SecretApplyError("secret destination is invalid")
    return Destination(
        target=item["target"],
        namespace=item["namespace"],
        secret_name=item["kubernetes_secret"],
        key=item["key"],
    )


def _artifact_version(artifact: Path) -> str:
    matches = list(_VERSION.finditer(artifact.name))
    if len(matches) != 1:
        raise SecretApplyError("ciphertext artifact name must contain one version")
    return f"v{matches[0].group(1)}"


def _validate_artifact(destination: Destination, artifact: Path, version: str) -> Path:
    resolved = artifact.resolve(strict=True)
    if artifact.is_symlink() or not resolved.is_file():
        raise SecretApplyError("ciphertext artifact must be a regular file")
    expected = Path(destination.target.format(version=version))
    if expected.is_absolute():
        matches = expected.resolve(strict=False) == resolved
    else:
        repository_root = Path(__file__).resolve().parents[2]
        matches = (repository_root / expected).resolve(strict=False) == resolved
    if not matches:
        raise SecretApplyError("ciphertext artifact does not match its destination")
    try:
        encrypted = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecretApplyError("ciphertext artifact is invalid") from exc
    if not isinstance(encrypted, dict) or not isinstance(encrypted.get("sops"), dict):
        raise SecretApplyError("ciphertext artifact has no SOPS metadata")
    return resolved


def _validate_plaintext(raw: bytes, destination: Destination, version: str) -> bytes:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecretApplyError("SOPS plaintext has an invalid Kubernetes shape") from exc
    expected_labels = {
        "app.kubernetes.io/managed-by": "exomem-secret-handoff",
        "exomem.io/secret-version": version,
    }
    metadata = document.get("metadata") if isinstance(document, dict) else None
    valid = (
        isinstance(document, dict)
        and set(document) == {"apiVersion", "kind", "metadata", "type", "stringData"}
        and document.get("apiVersion") == "v1"
        and document.get("kind") == "Secret"
        and document.get("type") == "Opaque"
        and isinstance(metadata, dict)
        and set(metadata) == {"name", "namespace", "labels"}
        and metadata.get("name") == destination.secret_name
        and metadata.get("namespace") == destination.namespace
        and metadata.get("labels") == expected_labels
        and isinstance(document.get("stringData"), dict)
        and set(document["stringData"]) == {destination.key}
        and isinstance(document["stringData"][destination.key], str)
        and bool(document["stringData"][destination.key])
    )
    if not valid:
        raise SecretApplyError("SOPS plaintext has an invalid Kubernetes shape")
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"


def main() -> int:
    args = _parser().parse_args()
    plaintext = bytearray()
    try:
        destination = _load_destination(args.matrix, args.destination)
        version = _artifact_version(args.artifact)
        artifact = _validate_artifact(destination, args.artifact, version)
        try:
            decrypt = subprocess.run(
                [args.sops, "decrypt", "--input-type", "json", "--output-type", "json", str(artifact)],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SecretApplyError("SOPS decrypt failed") from exc
        if decrypt.returncode != 0:
            raise SecretApplyError("SOPS decrypt failed")
        plaintext.extend(_validate_plaintext(decrypt.stdout, destination, version))
        try:
            applied = subprocess.run(
                [
                    args.kubectl,
                    "apply",
                    "--server-side",
                    "--field-manager=exomem-secret-handoff",
                    "-f",
                    "-",
                ],
                input=plaintext,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SecretApplyError("Kubernetes secret apply failed") from exc
        if applied.returncode != 0:
            raise SecretApplyError("Kubernetes secret apply failed")
    except (SecretApplyError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        plaintext[:] = b"\0" * len(plaintext)
        plaintext.clear()
    print(f"Applied {destination.namespace}/{destination.secret_name} at {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
