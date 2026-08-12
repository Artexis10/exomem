#!/usr/bin/env python3
"""Sign and publish one immutable active-secret registry/public-key pair."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, NoReturn

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class ActiveSecretRegistrySigningError(RuntimeError):
    """The active-secret registry cannot be safely signed and published."""


class _ContentFreeParser(argparse.ArgumentParser):
    def __init__(self) -> None:
        super().__init__(description=__doc__, allow_abbrev=False)

    def error(self, message: str) -> NoReturn:
        raise ActiveSecretRegistrySigningError("command arguments are invalid")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ActiveSecretRegistrySigningError("input JSON is invalid")
        result[key] = value
    return result


def _safe_input(path: Path, description: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ActiveSecretRegistrySigningError(f"{description} is unavailable") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o022:
            raise ActiveSecretRegistrySigningError(f"{description} is unsafe")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise ActiveSecretRegistrySigningError(f"{description} is unavailable") from exc
    finally:
        os.close(descriptor)


def _strict_json(path: Path, description: str) -> tuple[bytes, Any]:
    raw = _safe_input(path, description)
    try:
        return raw, json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActiveSecretRegistrySigningError(f"{description} is invalid") from exc


def _is_exact_integer(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _active_destinations(matrix: dict[str, Any]) -> dict[str, tuple[str, str]]:
    if not _is_exact_integer(matrix.get("schema_version"), 1) or not isinstance(
        matrix.get("secrets"), dict
    ):
        raise ActiveSecretRegistrySigningError("secret destination matrix is invalid")
    result: dict[str, tuple[str, str]] = {}
    destination_ids: set[str] = set()
    target_paths: set[str] = set()
    for secret_name, secret in matrix["secrets"].items():
        destinations = secret.get("destinations") if isinstance(secret, dict) else None
        if not isinstance(secret_name, str) or not isinstance(destinations, dict):
            raise ActiveSecretRegistrySigningError("secret destination matrix is invalid")
        for destination_id, destination in destinations.items():
            if not isinstance(destination_id, str) or not isinstance(destination, dict):
                raise ActiveSecretRegistrySigningError("secret destination matrix is invalid")
            if destination_id in destination_ids:
                raise ActiveSecretRegistrySigningError("secret destination matrix is invalid")
            destination_ids.add(destination_id)
            kind = destination.get("kind")
            if kind in {"sops_k8s_secret", "sops_escrow", "sops_ansible_vars"}:
                target = destination.get("target")
                if (
                    not isinstance(target, str)
                    or not target.endswith(".sops.json")
                    or target.count("{version}") != 1
                    or Path(target).is_absolute()
                    or ".." in Path(target).parts
                    or target in target_paths
                ):
                    raise ActiveSecretRegistrySigningError("secret destination matrix is invalid")
                target_paths.add(target)
            if destination.get("kind") == "sops_k8s_secret" and destination.get("slot") == "active":
                target = destination.get("target")
                if not isinstance(target, str) or target.count("{version}") != 1:
                    raise ActiveSecretRegistrySigningError("secret destination matrix is invalid")
                result[destination_id] = (secret_name, target)
    return result


def load_active_destinations(matrix_path: Path) -> tuple[bytes, dict[str, tuple[str, str]]]:
    raw, matrix = _strict_json(matrix_path, "secret destination matrix")
    if not isinstance(matrix, dict):
        raise ActiveSecretRegistrySigningError("secret destination matrix is invalid")
    return raw, _active_destinations(matrix)


def load_selection(selection_path: Path, expected: set[str]) -> dict[str, str]:
    _raw, selection = _strict_json(selection_path, "active-secret selection")
    if (
        not isinstance(selection, dict)
        or set(selection) != {"schema_version", "destinations"}
        or not _is_exact_integer(selection.get("schema_version"), 1)
        or not isinstance(selection.get("destinations"), dict)
    ):
        raise ActiveSecretRegistrySigningError("active-secret selection is invalid")
    destinations = selection["destinations"]
    if set(destinations) != expected or any(
        not isinstance(destination, str)
        or not isinstance(version, str)
        or not version.startswith("v")
        or not version[1:].isdigit()
        or not re.fullmatch(r"v[1-9][0-9]*", version)
        for destination, version in destinations.items()
    ):
        raise ActiveSecretRegistrySigningError("active-secret selection is invalid")
    return destinations


def load_private_key(key_buffer: bytearray, trust_path: Path) -> Ed25519PrivateKey:
    if not re.fullmatch(
        rb"-----BEGIN PRIVATE KEY-----\r?\n(?:[A-Za-z0-9+/]{1,64}\r?\n)+"
        rb"-----END PRIVATE KEY-----\r?\n?",
        key_buffer,
    ):
        raise ActiveSecretRegistrySigningError("registry signing key is invalid")
    try:
        private_key = serialization.load_pem_private_key(bytes(key_buffer), password=None)
    except (TypeError, ValueError) as exc:
        raise ActiveSecretRegistrySigningError("registry signing key is invalid") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ActiveSecretRegistrySigningError("registry signing key is invalid")
    _raw, trust = _strict_json(trust_path, "registry trust contract")
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if (
        not isinstance(trust, dict)
        or set(trust) != {"schema_version", "algorithm", "public_key_id", "private_key_custody"}
        or not _is_exact_integer(trust.get("schema_version"), 1)
        or trust.get("algorithm") != "ed25519"
        or trust.get("private_key_custody") != "secret-release-custodian-only"
        or trust.get("public_key_id") != hashlib.sha256(public).hexdigest()
    ):
        raise ActiveSecretRegistrySigningError("registry signing key is not trusted")
    return private_key


def build_registry(
    matrix_raw: bytes,
    destinations: dict[str, tuple[str, str]],
    selection: dict[str, str],
    repository_root: Path,
    private_key: Ed25519PrivateKey,
) -> dict[str, Any]:
    entries: dict[str, dict[str, str]] = {}
    for destination_id in sorted(destinations):
        secret, target = destinations[destination_id]
        artifact = Path(target.format(version=selection[destination_id]))
        if not artifact.is_absolute():
            artifact = repository_root / artifact
        entries[destination_id] = {
            "secret": secret,
            "version": selection[destination_id],
            "artifact_sha256": hashlib.sha256(
                _strict_json(artifact, "active ciphertext artifact")[0]
            ).hexdigest(),
        }
    unsigned = {
        "schema_version": 1,
        "matrix_sha256": hashlib.sha256(matrix_raw).hexdigest(),
        "destinations": entries,
    }
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    canonical = json.dumps(unsigned, separators=(",", ":"), sort_keys=True).encode()
    return {
        **unsigned,
        "authentication": {
            "algorithm": "ed25519",
            "key_id": hashlib.sha256(public).hexdigest(),
            "signature": private_key.sign(canonical).hex(),
        },
    }


def _open_output_directory(
    registry_output: Path, public_key_output: Path
) -> tuple[int, tuple[str, str, str]]:
    registry_output = registry_output.absolute()
    public_key_output = public_key_output.absolute()
    directory = registry_output.parent
    if (
        directory != public_key_output.parent
        or not registry_output.name
        or not public_key_output.name
    ):
        raise ActiveSecretRegistrySigningError("registry output directory is invalid")
    absolute = directory.absolute()
    try:
        directory_fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ActiveSecretRegistrySigningError("registry output directory is invalid") from exc
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
    except OSError as exc:
        try:
            os.close(directory_fd)
        finally:
            pass
        raise ActiveSecretRegistrySigningError("registry output directory is invalid") from exc
    details = os.fstat(directory_fd)
    if not (
        stat.S_ISDIR(details.st_mode)
        and details.st_uid == os.getuid()
        and stat.S_IMODE(details.st_mode) == 0o700
    ):
        os.close(directory_fd)
        raise ActiveSecretRegistrySigningError("registry output directory is invalid")
    names = (registry_output.name, public_key_output.name, f"{registry_output.name}.complete")
    for name in names:
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            os.close(directory_fd)
            raise ActiveSecretRegistrySigningError("registry output is invalid") from exc
        os.close(directory_fd)
        raise ActiveSecretRegistrySigningError("registry output already exists")
    return directory_fd, names


def _write_staged(directory_fd: int, content: bytes) -> str:
    descriptor = -1
    name = f".active-secret-registry-{secrets.token_hex(16)}"
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        return name
    except OSError as exc:
        raise ActiveSecretRegistrySigningError("registry staging failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unlink_all(directory_fd: int, names: list[str]) -> None:
    for name in names:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ActiveSecretRegistrySigningError("registry cleanup failed") from exc


def _sync_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        raise ActiveSecretRegistrySigningError("registry publication failed") from exc


def _load_registry_verifier() -> Any:
    path = Path(__file__).with_name("apply_active_sops_secrets.py")
    spec = importlib.util.spec_from_file_location("active_secret_registry_verifier", path)
    if spec is None or spec.loader is None:
        raise ActiveSecretRegistrySigningError("registry staging failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def publish_verified_pair(
    *,
    matrix_path: Path,
    trust_path: Path,
    registry_output: Path,
    public_key_output: Path,
    registry: dict[str, Any],
    private_key: Ed25519PrivateKey,
) -> None:
    directory_fd, (registry_name, public_key_name, completion_name) = _open_output_directory(
        registry_output, public_key_output
    )
    registry_stage: str | None = None
    public_stage: str | None = None
    completion_stage: str | None = None
    published: list[str] = []
    try:
        registry_bytes = (
            json.dumps(registry, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        )
        public_bytes = private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        registry_stage = _write_staged(directory_fd, registry_bytes)
        public_stage = _write_staged(directory_fd, public_bytes)
        completion_stage = _write_staged(
            directory_fd,
            json.dumps(
                {
                    "schema_version": 1,
                    "registry": registry_name,
                    "public_key": public_key_name,
                    "registry_sha256": hashlib.sha256(registry_bytes).hexdigest(),
                    "public_key_sha256": hashlib.sha256(public_bytes).hexdigest(),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            + b"\n",
        )
        verifier = _load_registry_verifier()
        verifier.load_registry(
            matrix_path=matrix_path,
            registry_path=Path(f"/proc/self/fd/{directory_fd}/{registry_stage}"),
            public_key_path=Path(f"/proc/self/fd/{directory_fd}/{public_stage}"),
            trust_contract_path=trust_path,
            require_completion=False,
        )
        _sync_directory(directory_fd)
        os.link(registry_stage, registry_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        published.append(registry_name)
        os.link(public_stage, public_key_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        published.append(public_key_name)
        _sync_directory(directory_fd)
        os.link(completion_stage, completion_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        published.append(completion_name)
        _sync_directory(directory_fd)
    except Exception as exc:
        _unlink_all(directory_fd, published)
        _sync_directory(directory_fd)
        if isinstance(exc, ActiveSecretRegistrySigningError):
            raise
        raise ActiveSecretRegistrySigningError("registry publication failed") from exc
    finally:
        _unlink_all(
            directory_fd,
            [name for name in (registry_stage, public_stage, completion_stage) if name is not None],
        )
        _sync_directory(directory_fd)
        os.close(directory_fd)


def parser() -> argparse.ArgumentParser:
    result = _ContentFreeParser()
    result.add_argument("--matrix", type=Path, required=True)
    result.add_argument("--selection", type=Path, required=True)
    result.add_argument("--trust-contract", type=Path, required=True)
    result.add_argument("--private-key-stdin", action="store_true", required=True)
    result.add_argument("--registry-output", type=Path, required=True)
    result.add_argument("--public-key-output", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
    except ActiveSecretRegistrySigningError:
        print("active-secret registry signing failed", file=sys.stderr)
        return 2
    raw_key = sys.stdin.buffer.read()
    key_buffer = raw_key if isinstance(raw_key, bytearray) else bytearray(raw_key)
    try:
        matrix_raw, destinations = load_active_destinations(args.matrix)
        selection = load_selection(args.selection, set(destinations))
        private_key = load_private_key(key_buffer, args.trust_contract)
        registry = build_registry(
            matrix_raw,
            destinations,
            selection,
            Path(__file__).resolve().parents[2],
            private_key,
        )
        publish_verified_pair(
            matrix_path=args.matrix,
            trust_path=args.trust_contract,
            registry_output=args.registry_output,
            public_key_output=args.public_key_output,
            registry=registry,
            private_key=private_key,
        )
    except (OSError, ActiveSecretRegistrySigningError):
        print("active-secret registry signing failed", file=sys.stderr)
        return 2
    finally:
        key_buffer[:] = b"\0" * len(key_buffer)
    print("active-secret registry pair published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
