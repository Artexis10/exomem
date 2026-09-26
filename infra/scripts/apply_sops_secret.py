#!/usr/bin/env python3
"""Decrypt one allowlisted SOPS Kubernetes Secret directly into server-side apply."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secret_keysets import KeySetError, loads_unique_json, validate_key_sets, validate_values

_VERSION = re.compile(r"(?:^|\.)v([1-9][0-9]*)(?:\.|$)")


class SecretApplyError(RuntimeError):
    """A content-free static-secret application failure."""


@dataclass(frozen=True)
class Destination:
    target: str
    namespace: str
    secret_name: str
    key: str | None
    key_sets: tuple[frozenset[str], ...] = ()


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
        document = loads_unique_json(matrix_path.read_bytes())
    except (OSError, KeySetError) as exc:
        raise SecretApplyError("secret destination matrix is invalid") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise SecretApplyError("secret destination matrix is invalid")
    matches: list[tuple[dict[str, Any], str]] = []
    secrets = document.get("secrets")
    if isinstance(secrets, dict):
        for secret in secrets.values():
            destinations = secret.get("destinations") if isinstance(secret, dict) else None
            if isinstance(destinations, dict) and destination_id in destinations:
                candidate = destinations[destination_id]
                if isinstance(candidate, dict):
                    matches.append((candidate, secret.get("value_shape", "line")))
    if len(matches) != 1:
        raise SecretApplyError("secret destination is not uniquely allowlisted")
    item, value_shape = matches[0]
    if item.get("kind") != "sops_k8s_secret" or item.get("slot") != "active":
        raise SecretApplyError("secret destination is not an active Kubernetes Secret")
    required = ("target", "namespace", "kubernetes_secret")
    if any(not isinstance(item.get(field), str) or not item[field] for field in required):
        raise SecretApplyError("secret destination is invalid")
    key_sets: tuple[frozenset[str], ...] = ()
    key = item.get("key")
    if value_shape == "json-object":
        if "key" in item:
            raise SecretApplyError("secret destination is invalid")
        try:
            key_sets = validate_key_sets(item.get("key_sets"))
        except KeySetError as exc:
            raise SecretApplyError("secret destination is invalid") from exc
    elif not isinstance(key, str) or not key or "key_sets" in item:
        raise SecretApplyError("secret destination is invalid")
    return Destination(
        target=item["target"],
        namespace=item["namespace"],
        secret_name=item["kubernetes_secret"],
        key=key,
        key_sets=key_sets,
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
        encrypted = loads_unique_json(resolved.read_bytes())
    except (OSError, KeySetError) as exc:
        raise SecretApplyError("ciphertext artifact is invalid") from exc
    if not isinstance(encrypted, dict) or not isinstance(encrypted.get("sops"), dict):
        raise SecretApplyError("ciphertext artifact has no SOPS metadata")
    return resolved


def _validate_plaintext(raw: bytes, destination: Destination, version: str) -> bytes:
    try:
        document = loads_unique_json(raw)
    except KeySetError as exc:
        raise SecretApplyError("SOPS plaintext has an invalid Kubernetes shape") from exc
    expected_labels = {
        "app.kubernetes.io/managed-by": "exomem-secret-handoff",
        "exomem.io/secret-version": version,
    }
    metadata = document.get("metadata") if isinstance(document, dict) else None
    string_data = document.get("stringData") if isinstance(document, dict) else None
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
        and isinstance(string_data, dict)
    )
    if not valid:
        raise SecretApplyError("SOPS plaintext has an invalid Kubernetes shape")
    assert isinstance(string_data, dict)
    if destination.key_sets:
        try:
            validate_values(string_data, destination.key_sets)
        except KeySetError as exc:
            raise SecretApplyError("SOPS plaintext has an invalid Kubernetes shape") from exc
    elif (
        set(string_data) != {destination.key}
        or not isinstance(string_data[destination.key], str)
        or not string_data[destination.key]
    ):
        raise SecretApplyError("SOPS plaintext has an invalid Kubernetes shape")
    try:
        document["data"] = {
            key: base64.b64encode(value.encode("utf-8")).decode("ascii")
            for key, value in string_data.items()
        }
    except UnicodeEncodeError as exc:
        raise SecretApplyError("SOPS plaintext has an invalid Kubernetes shape") from exc
    del document["stringData"]
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"


def _verify_live_keys(kubectl: str, destination: Destination, expected: set[str]) -> None:
    try:
        result = subprocess.run(
            [
                kubectl,
                "get",
                "secret",
                destination.secret_name,
                "--namespace",
                destination.namespace,
                '-o=go-template={{range $key, $value := .data}}{{$key}}{{"\\n"}}{{end}}',
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretApplyError("Kubernetes Secret key verification failed") from exc
    if result.returncode != 0:
        raise SecretApplyError("Kubernetes Secret key verification failed")
    try:
        keys = result.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SecretApplyError("Kubernetes Secret key verification failed") from exc
    if len(keys) != len(expected) or set(keys) != expected:
        raise SecretApplyError("Kubernetes Secret key verification failed")


def main() -> int:
    args = _parser().parse_args()
    plaintext = bytearray()
    try:
        destination = _load_destination(args.matrix, args.destination)
        version = _artifact_version(args.artifact)
        artifact = _validate_artifact(destination, args.artifact, version)
        try:
            decrypt = subprocess.run(
                [
                    args.sops,
                    "decrypt",
                    "--input-type",
                    "json",
                    "--output-type",
                    "json",
                    str(artifact),
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SecretApplyError("SOPS decrypt failed") from exc
        if decrypt.returncode != 0:
            raise SecretApplyError("SOPS decrypt failed")
        plaintext.extend(_validate_plaintext(decrypt.stdout, destination, version))
        expected_keys = set(json.loads(plaintext)["data"])
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
        _verify_live_keys(args.kubectl, destination, expected_keys)
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
