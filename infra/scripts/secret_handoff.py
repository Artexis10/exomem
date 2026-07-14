#!/usr/bin/env python3
"""Move one named secret to one approved destination without printing its value."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_VERSION = re.compile(r"v[1-9][0-9]*\Z")
_SAFE_NAME = re.compile(r"[a-zA-Z0-9_.-]+\Z")
_MAX_SECRET_BYTES = 8192


class HandoffError(RuntimeError):
    """A content-free secret handoff failure."""


@dataclass(frozen=True)
class SourceSpec:
    kind: str
    root: str | None = None
    output: str | None = None


@dataclass(frozen=True)
class DestinationSpec:
    destination_id: str
    kind: str
    slot: str
    fields: dict[str, str]


@dataclass(frozen=True)
class SecretSpec:
    name: str
    sources: tuple[SourceSpec, ...]
    destinations: dict[str, DestinationSpec]


@dataclass(frozen=True)
class VercelReceiptPolicy:
    target: str


@dataclass(frozen=True)
class VercelProjectSpec:
    project_key: str
    org_id: str
    project_id: str
    project_name: str
    receipt_policy: VercelReceiptPolicy


@dataclass(frozen=True)
class HandoffMatrix:
    schema_version: int
    vercel_projects: dict[str, VercelProjectSpec]
    secrets: dict[str, SecretSpec]


@dataclass(frozen=True)
class VercelReceiptReservation:
    final_path: Path
    pending_path: Path
    document: dict[str, Any]


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or not _SAFE_NAME.fullmatch(value):
        raise HandoffError(f"secret matrix has invalid {label}")
    return value


def load_matrix(path: Path) -> HandoffMatrix:
    try:
        document = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffError("secret matrix is unreadable or invalid") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise HandoffError("secret matrix version is unsupported")
    raw_vercel_projects = document.get("vercel_projects")
    raw_secrets = document.get("secrets")
    if not isinstance(raw_vercel_projects, dict) or not raw_vercel_projects:
        raise HandoffError("secret matrix has no Vercel project identities")
    if not isinstance(raw_secrets, dict) or not raw_secrets:
        raise HandoffError("secret matrix has no secrets")

    vercel_projects: dict[str, VercelProjectSpec] = {}
    receipt_targets: set[str] = set()
    for raw_key, raw_project in raw_vercel_projects.items():
        project_key = _require_string(raw_key, "Vercel project key")
        if not isinstance(raw_project, dict) or set(raw_project) != {
            "org_id",
            "project_id",
            "project_name",
            "receipt_policy",
        }:
            raise HandoffError("secret matrix has invalid Vercel project identity")
        org_id = _require_string(raw_project["org_id"], "Vercel organization id")
        project_id = _require_string(raw_project["project_id"], "Vercel project id")
        project_name = _require_string(raw_project["project_name"], "Vercel project name")
        raw_policy = raw_project["receipt_policy"]
        if not isinstance(raw_policy, dict) or raw_policy.get("content") != (
            "destination-binding-only"
        ):
            raise HandoffError("secret matrix has invalid Vercel receipt policy")
        if (
            raw_policy.get("mode") != "immutable-strictly-monotonic"
            or raw_policy.get("partial_write_recovery") != "new-version"
            or raw_policy.get("scope") != "destination"
        ):
            raise HandoffError("secret matrix has invalid Vercel receipt policy")
        if set(raw_policy) != {
            "content",
            "mode",
            "partial_write_recovery",
            "scope",
            "target",
        }:
            raise HandoffError("secret matrix has invalid Vercel receipt policy")
        receipt_target = raw_policy.get("target")
        if (
            not isinstance(receipt_target, str)
            or not receipt_target.endswith(".receipt.json")
            or receipt_target.count("{secret}") != 1
            or receipt_target.count("{destination}") != 1
            or receipt_target.count("{version}") != 1
            or Path(receipt_target).is_absolute()
            or ".." in Path(receipt_target).parts
            or receipt_target in receipt_targets
        ):
            raise HandoffError("secret matrix has invalid Vercel receipt target")
        receipt_targets.add(receipt_target)
        vercel_projects[project_key] = VercelProjectSpec(
            project_key=project_key,
            org_id=org_id,
            project_id=project_id,
            project_name=project_name,
            receipt_policy=VercelReceiptPolicy(target=receipt_target),
        )

    secrets: dict[str, SecretSpec] = {}
    target_paths: set[str] = set()
    kubernetes_objects: set[tuple[str, str]] = set()
    for raw_name, raw_secret in raw_secrets.items():
        name = _require_string(raw_name, "secret name")
        if not isinstance(raw_secret, dict):
            raise HandoffError("secret matrix has invalid secret definition")
        raw_sources = raw_secret.get("sources")
        raw_destinations = raw_secret.get("destinations")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise HandoffError(f"secret matrix has no sources for {name}")
        if not isinstance(raw_destinations, dict) or not raw_destinations:
            raise HandoffError(f"secret matrix has no destinations for {name}")

        sources: list[SourceSpec] = []
        seen_source_kinds: set[str] = set()
        for raw_source in raw_sources:
            if not isinstance(raw_source, dict):
                raise HandoffError(f"secret matrix has invalid source for {name}")
            kind = raw_source.get("kind")
            if (
                kind
                not in {
                    "stdin",
                    "prompt",
                    "terraform",
                    "generated-ed25519-private",
                    "derived-ed25519-public",
                }
                or kind in seen_source_kinds
            ):
                raise HandoffError(f"secret matrix has invalid source kind for {name}")
            seen_source_kinds.add(kind)
            root = raw_source.get("root")
            output = raw_source.get("output")
            if kind == "terraform":
                root = _require_string(root, "Terraform root")
                output = _require_string(output, "Terraform output")
                if root not in {"foundation", "durability", "bootstrap"}:
                    raise HandoffError(f"secret matrix has unknown Terraform root for {name}")
            elif root is not None or output is not None:
                raise HandoffError(f"secret matrix has source fields on {kind} for {name}")
            sources.append(SourceSpec(kind=kind, root=root, output=output))

        destinations: dict[str, DestinationSpec] = {}
        for raw_id, raw_destination in raw_destinations.items():
            destination_id = _require_string(raw_id, "destination id")
            if not isinstance(raw_destination, dict):
                raise HandoffError(f"secret matrix has invalid destination for {name}")
            kind = raw_destination.get("kind")
            slot = raw_destination.get("slot")
            if kind not in {
                "vercel_env",
                "sops_k8s_secret",
                "sops_escrow",
                "sops_ansible_vars",
            } or slot not in {
                "active",
                "previous",
            }:
                raise HandoffError(f"secret matrix has invalid destination policy for {name}")
            fields = {
                key: value
                for key, value in raw_destination.items()
                if key not in {"kind", "slot"} and isinstance(value, str)
            }
            if len(fields) != len(raw_destination) - 2:
                raise HandoffError(f"secret matrix has invalid destination fields for {name}")
            if kind == "vercel_env":
                if set(fields) != {"project", "environment", "name"}:
                    raise HandoffError(f"secret matrix has invalid Vercel destination for {name}")
                if fields["project"] not in vercel_projects:
                    raise HandoffError(f"secret matrix has unknown Vercel project for {name}")
                if fields["environment"] not in {"production", "preview", "development"}:
                    raise HandoffError(f"secret matrix has invalid Vercel environment for {name}")
                _require_string(fields["name"], "Vercel variable")
            else:
                expected_fields = {
                    "sops_k8s_secret": {"target", "namespace", "kubernetes_secret", "key"},
                    "sops_escrow": {"target", "secret_key"},
                    "sops_ansible_vars": {"target", "variable"},
                }[kind]
                if set(fields) != expected_fields:
                    raise HandoffError(f"secret matrix has invalid SOPS destination for {name}")
                target = fields["target"]
                if (
                    not target.endswith(".sops.json")
                    or target.count("{version}") != 1
                    or Path(target).is_absolute()
                    or ".." in Path(target).parts
                    or target in target_paths
                ):
                    raise HandoffError(
                        f"secret matrix has invalid or duplicate SOPS target for {name}"
                    )
                target_paths.add(target)
                if kind == "sops_k8s_secret":
                    _require_string(fields["namespace"], "Kubernetes namespace")
                    _require_string(fields["kubernetes_secret"], "Kubernetes Secret")
                    _require_string(fields["key"], "Kubernetes Secret key")
                    kubernetes_object = (fields["namespace"], fields["kubernetes_secret"])
                    if kubernetes_object in kubernetes_objects:
                        raise HandoffError(f"secret matrix reuses a Kubernetes Secret for {name}")
                    kubernetes_objects.add(kubernetes_object)
                elif kind == "sops_escrow":
                    _require_string(fields["secret_key"], "escrow secret key")
                else:
                    _require_string(fields["variable"], "Ansible variable")
            destinations[destination_id] = DestinationSpec(
                destination_id=destination_id,
                kind=kind,
                slot=slot,
                fields=fields,
            )
        secrets[name] = SecretSpec(
            name=name,
            sources=tuple(sources),
            destinations=destinations,
        )
    return HandoffMatrix(
        schema_version=1,
        vercel_projects=vercel_projects,
        secrets=secrets,
    )


def _normalize_secret(value: bytes) -> bytes:
    if value.endswith(b"\n"):
        value = value[:-1]
        if value.endswith(b"\r"):
            value = value[:-1]
    if (
        not value
        or len(value) > _MAX_SECRET_BYTES
        or b"\x00" in value
        or b"\n" in value
        or b"\r" in value
    ):
        raise HandoffError("secret source has an invalid value")
    return value


def _read_secret(
    *,
    source_kind: str,
    secret_spec: SecretSpec,
    repository_root: Path,
    terraform_bin: str,
) -> bytes:
    source = next((item for item in secret_spec.sources if item.kind == source_kind), None)
    if source is None:
        raise HandoffError(f"source {source_kind} is not allowed for {secret_spec.name}")
    if source_kind in {"generated-ed25519-private", "derived-ed25519-public"}:
        raise HandoffError("derived key material requires the atomic keypair handoff")
    if source_kind == "stdin":
        return _normalize_secret(sys.stdin.buffer.read(_MAX_SECRET_BYTES + 2))
    if source_kind == "prompt":
        return _normalize_secret(getpass.getpass("Secret value: ").encode("utf-8"))

    assert source.root is not None and source.output is not None
    terraform_root = repository_root / "infra" / "terraform" / source.root
    command = [
        terraform_bin,
        f"-chdir={terraform_root}",
        "output",
        "-raw",
        source.output,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HandoffError("Terraform secret source failed") from exc
    if result.returncode != 0:
        raise HandoffError("Terraform secret source failed")
    return _normalize_secret(result.stdout)


def _assert_safe_target(repository_root: Path, relative_target: str) -> Path:
    root = repository_root.resolve()
    target = (root / relative_target).resolve(strict=False)
    if target == root or root not in target.parents:
        raise HandoffError("SOPS target escapes the repository root")
    current = root
    for part in Path(relative_target).parts[:-1]:
        current /= part
        if current.exists() and current.is_symlink():
            raise HandoffError("SOPS target traverses a symbolic link")
    return target


def _version_number(version: str) -> int:
    return int(version[1:])


def _assert_version_is_new_and_increasing(
    *,
    repository_root: Path,
    relative_template: str,
    version: str,
    replacements: dict[str, str],
    allow_pending: bool,
    label: str,
) -> Path:
    rendered_template = relative_template.format(version="{version}", **replacements)
    template_path = Path(rendered_template)
    if "{version}" not in template_path.name:
        raise HandoffError(f"{label} target has an invalid version position")
    relative_target = rendered_template.format(version=version)
    target = _assert_safe_target(repository_root, relative_target)

    before, after = template_path.name.split("{version}", 1)
    suffixes = [after]
    if allow_pending and after.endswith(".json"):
        suffixes.append(f"{after[:-5]}.pending.json")
    patterns = [
        re.compile(rf"{re.escape(before)}v([1-9][0-9]*){re.escape(suffix)}\Z")
        for suffix in suffixes
    ]
    existing_versions: list[int] = []
    if target.parent.is_dir():
        for candidate in target.parent.iterdir():
            for pattern in patterns:
                match = pattern.fullmatch(candidate.name)
                if match is not None:
                    existing_versions.append(int(match.group(1)))
                    break
    if existing_versions and _version_number(version) <= max(existing_versions):
        raise HandoffError(f"{label} version must be new and increasing")
    if target.exists() or target.is_symlink():
        raise HandoffError(f"{label} version must be new and increasing")
    return target


def _verify_vercel_project(*, expected: VercelProjectSpec, vercel_project: Path | None) -> Path:
    if vercel_project is None or not vercel_project.is_dir():
        raise HandoffError("--vercel-project is required for a Vercel destination")
    project_root = vercel_project.resolve()
    link_dir = project_root / ".vercel"
    link_path = link_dir / "project.json"
    if link_dir.is_symlink() or link_path.is_symlink() or not link_path.is_file():
        raise HandoffError("Vercel project link is missing or unsafe")
    try:
        linked = json.loads(link_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffError("Vercel project link is unreadable or invalid") from exc
    if not isinstance(linked, dict) or (
        linked.get("orgId"),
        linked.get("projectId"),
        linked.get("projectName"),
    ) != (expected.org_id, expected.project_id, expected.project_name):
        raise HandoffError("Vercel project identity does not match the destination contract")
    return project_root


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise HandoffError("local evidence durability check failed") from exc


def _write_json_exclusive(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        payload = json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    except (FileExistsError, OSError) as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise HandoffError("Vercel receipt could not be written safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _reserve_vercel_receipt(
    *,
    final_path: Path,
    secret_name: str,
    version: str,
    destination: DestinationSpec,
    project: VercelProjectSpec,
) -> VercelReceiptReservation:
    pending_path = final_path.with_suffix(".pending.json")
    document = {
        "schema_version": 1,
        "secret_name": secret_name,
        "destination_version": version,
        "destination_id": destination.destination_id,
        "destination_slot": destination.slot,
        "environment": destination.fields["environment"],
        "variable_name": destination.fields["name"],
        "project_key": project.project_key,
        "org_id": project.org_id,
        "project_id": project.project_id,
        "project_name": project.project_name,
        "status": "reserved",
        "recorded_at": _timestamp(),
    }
    _write_json_exclusive(pending_path, document)
    return VercelReceiptReservation(
        final_path=final_path,
        pending_path=pending_path,
        document=document,
    )


def _finalize_vercel_receipt(reservation: VercelReceiptReservation) -> None:
    document = {
        **reservation.document,
        "status": "delivered",
        "delivered_at": _timestamp(),
    }
    _write_json_exclusive(reservation.final_path, document)
    try:
        reservation.pending_path.unlink()
        _fsync_directory(reservation.pending_path.parent)
    except OSError as exc:
        raise HandoffError("Vercel receipt finalization failed") from exc


@contextmanager
def _hold_vercel_destination_locks(
    vercel_contexts: dict[str, tuple[VercelProjectSpec, Path, Path]],
) -> Iterator[None]:
    descriptors: list[int] = []
    try:
        for destination_id in sorted(vercel_contexts):
            _, _, receipt_path = vercel_contexts[destination_id]
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(receipt_path.parent, 0o700)
            lock_path = receipt_path.parent / f".{destination_id}.handoff.lock"
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor: int | None = None
            try:
                descriptor = os.open(lock_path, flags, 0o600)
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                if descriptor is not None:
                    os.close(descriptor)
                raise HandoffError("Vercel destination lock could not be acquired") from exc
            assert descriptor is not None
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _assert_same_container_shape(plaintext: Any, ciphertext: Any) -> None:
    if isinstance(plaintext, dict):
        if not isinstance(ciphertext, dict) or set(ciphertext) != set(plaintext):
            raise HandoffError("SOPS output failed the destination shape check")
        for key, value in plaintext.items():
            _assert_same_container_shape(value, ciphertext[key])
    elif isinstance(plaintext, list):
        if not isinstance(ciphertext, list) or len(ciphertext) != len(plaintext):
            raise HandoffError("SOPS output failed the destination shape check")
        for plain_item, cipher_item in zip(plaintext, ciphertext, strict=True):
            _assert_same_container_shape(plain_item, cipher_item)


def _assert_sops_destination_shape(
    *, destination: DestinationSpec, plaintext: dict[str, Any], ciphertext: dict[str, Any]
) -> None:
    sops_metadata = ciphertext.get("sops")
    if not isinstance(sops_metadata, dict) or not sops_metadata:
        raise HandoffError("SOPS output failed the destination shape check")
    encrypted_payload = {key: value for key, value in ciphertext.items() if key != "sops"}
    _assert_same_container_shape(plaintext, encrypted_payload)
    if destination.kind == "sops_k8s_secret":
        string_data = encrypted_payload.get("stringData")
        sensitive_value = (
            string_data.get(destination.fields["key"]) if isinstance(string_data, dict) else None
        )
    elif destination.kind == "sops_escrow":
        sensitive_value = encrypted_payload.get(destination.fields["secret_key"])
    else:
        sensitive_value = encrypted_payload.get(destination.fields["variable"])
    if not isinstance(sensitive_value, str) or not sensitive_value.startswith("ENC["):
        raise HandoffError("SOPS output failed the destination shape check")


def _seal_sops_document(
    *,
    destination: DestinationSpec,
    secret: bytes,
    version: str,
    repository_root: Path,
    sops_bin: str,
    document: dict[str, Any],
) -> None:
    recipients = os.environ.get("SOPS_AGE_RECIPIENTS", "").strip()
    if not recipients or "\n" in recipients or "\r" in recipients:
        raise HandoffError("SOPS_AGE_RECIPIENTS is required")
    relative_target = destination.fields["target"].format(version=version)
    target = _assert_safe_target(repository_root, relative_target)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)

    encrypted_descriptor, encrypted_name = tempfile.mkstemp(
        prefix=f".{target.name}.encrypted.", dir=target.parent
    )
    os.close(encrypted_descriptor)
    os.unlink(encrypted_name)
    encrypted_path = Path(encrypted_name)
    try:
        manifest_bytes = (
            json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        )
        command = [
            sops_bin,
            "encrypt",
            "--input-type",
            "json",
            "--output-type",
            "json",
            "--age",
            recipients,
            "--output",
            str(encrypted_path),
            "/dev/stdin",
        ]
        try:
            result = subprocess.run(
                command,
                input=manifest_bytes,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HandoffError("SOPS encryption failed") from exc
        if result.returncode != 0 or not encrypted_path.is_file():
            raise HandoffError("SOPS encryption failed")
        ciphertext = encrypted_path.read_bytes()
        try:
            secret_text = secret.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HandoffError("secret source must be UTF-8 text") from exc
        escaped_secret = json.dumps(secret_text).encode("utf-8")
        if not ciphertext or secret in ciphertext or escaped_secret in ciphertext:
            raise HandoffError("SOPS output failed the ciphertext check")
        try:
            encrypted_document = json.loads(ciphertext)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HandoffError("SOPS output failed the destination shape check") from exc
        if not isinstance(encrypted_document, dict):
            raise HandoffError("SOPS output failed the destination shape check")
        _assert_sops_destination_shape(
            destination=destination,
            plaintext=document,
            ciphertext=encrypted_document,
        )

        decrypt_command = [
            sops_bin,
            "decrypt",
            "--input-type",
            "json",
            "--output-type",
            "json",
            str(encrypted_path),
        ]
        try:
            decrypted_result = subprocess.run(
                decrypt_command,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HandoffError("SOPS verification decrypt failed") from exc
        try:
            decrypted_document = json.loads(decrypted_result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HandoffError("SOPS verification decrypt failed") from exc
        if decrypted_result.returncode != 0 or decrypted_document != document:
            raise HandoffError("SOPS verification decrypt failed")

        os.chmod(encrypted_path, 0o600)
        with encrypted_path.open("rb") as encrypted_file:
            os.fsync(encrypted_file.fileno())
        try:
            os.link(encrypted_path, target)
        except FileExistsError as exc:
            raise HandoffError("SOPS version must be new and increasing") from exc
        except OSError as exc:
            raise HandoffError("SOPS output could not be published safely") from exc
        _fsync_directory(target.parent)
    finally:
        try:
            encrypted_path.unlink()
        except FileNotFoundError:
            pass


def _seal_k8s_secret(
    *,
    destination: DestinationSpec,
    secret: bytes,
    version: str,
    repository_root: Path,
    sops_bin: str,
) -> None:
    try:
        value = secret.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HandoffError("secret source must be UTF-8 text") from exc
    document = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": destination.fields["kubernetes_secret"],
            "namespace": destination.fields["namespace"],
            "labels": {
                "app.kubernetes.io/managed-by": "exomem-secret-handoff",
                "exomem.io/secret-version": version,
            },
        },
        "type": "Opaque",
        "stringData": {destination.fields["key"]: value},
    }
    _seal_sops_document(
        destination=destination,
        secret=secret,
        version=version,
        repository_root=repository_root,
        sops_bin=sops_bin,
        document=document,
    )


def _seal_named_document(
    *,
    destination: DestinationSpec,
    secret_name: str,
    secret: bytes,
    version: str,
    repository_root: Path,
    sops_bin: str,
) -> None:
    try:
        value = secret.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HandoffError("secret source must be UTF-8 text") from exc
    if destination.kind == "sops_escrow":
        document = {
            "schema_version": 1,
            "secret_name": secret_name,
            "secret_version": version,
            destination.fields["secret_key"]: value,
        }
    else:
        document = {destination.fields["variable"]: value}
    _seal_sops_document(
        destination=destination,
        secret=secret,
        version=version,
        repository_root=repository_root,
        sops_bin=sops_bin,
        document=document,
    )


def _send_vercel_secret(
    *,
    destination: DestinationSpec,
    secret: bytes,
    vercel_bin: str,
    vercel_project: Path | None,
) -> None:
    if vercel_project is None or not vercel_project.is_dir():
        raise HandoffError("--vercel-project is required for a Vercel destination")
    command = [
        vercel_bin,
        "env",
        "add",
        destination.fields["name"],
        destination.fields["environment"],
        "--force",
        "--sensitive",
        "--yes",
        "--no-color",
        "--non-interactive",
        "--cwd",
        str(vercel_project.resolve()),
    ]
    try:
        result = subprocess.run(
            command,
            input=secret + b"\n",
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HandoffError("Vercel secret handoff failed") from exc
    if result.returncode != 0:
        raise HandoffError("Vercel secret handoff failed")


def execute_handoff(
    *,
    matrix_path: Path,
    repository_root: Path,
    secret_name: str,
    version: str,
    destination_ids: tuple[str, ...],
    source_kind: str,
    terraform_bin: str,
    sops_bin: str,
    vercel_bin: str,
    vercel_project: Path | None,
    dry_run: bool,
) -> None:
    matrix = load_matrix(matrix_path)
    secret_spec = matrix.secrets.get(secret_name)
    if secret_spec is None:
        raise HandoffError("secret is not present in the destination matrix")
    if not destination_ids or len(destination_ids) != len(set(destination_ids)):
        raise HandoffError("destinations must be a non-empty unique list")
    destinations: list[DestinationSpec] = []
    for destination_id in destination_ids:
        destination = secret_spec.destinations.get(destination_id)
        if destination is None:
            raise HandoffError(f"destination {destination_id} is not allowed for {secret_name}")
        destinations.append(destination)
    if not _VERSION.fullmatch(version):
        raise HandoffError("secret version must be v1 or a later positive integer")
    if not any(source.kind == source_kind for source in secret_spec.sources):
        raise HandoffError(f"source {source_kind} is not allowed for {secret_name}")

    vercel_contexts: dict[str, tuple[VercelProjectSpec, Path, Path]] = {}
    for destination in destinations:
        if destination.kind.startswith("sops_"):
            _assert_version_is_new_and_increasing(
                repository_root=repository_root,
                relative_template=destination.fields["target"],
                version=version,
                replacements={},
                allow_pending=False,
                label="SOPS",
            )
            continue
        project = matrix.vercel_projects[destination.fields["project"]]
        project_root = _verify_vercel_project(
            expected=project,
            vercel_project=vercel_project,
        )
        receipt_path = _assert_version_is_new_and_increasing(
            repository_root=repository_root,
            relative_template=project.receipt_policy.target,
            version=version,
            replacements={
                "secret": secret_name,
                "destination": destination.destination_id,
            },
            allow_pending=True,
            label="Vercel",
        )
        vercel_contexts[destination.destination_id] = (project, project_root, receipt_path)
    if dry_run:
        return

    secret = _read_secret(
        source_kind=source_kind,
        secret_spec=secret_spec,
        repository_root=repository_root,
        terraform_bin=terraform_bin,
    )

    local_destinations = [
        destination for destination in destinations if destination.kind.startswith("sops_")
    ]
    vercel_destinations = [
        destination for destination in destinations if destination.kind == "vercel_env"
    ]
    with _hold_vercel_destination_locks(vercel_contexts):
        for destination in vercel_destinations:
            project, _, _ = vercel_contexts[destination.destination_id]
            _assert_version_is_new_and_increasing(
                repository_root=repository_root,
                relative_template=project.receipt_policy.target,
                version=version,
                replacements={
                    "secret": secret_name,
                    "destination": destination.destination_id,
                },
                allow_pending=True,
                label="Vercel",
            )

        receipt_reservations: dict[str, VercelReceiptReservation] = {}
        for destination in vercel_destinations:
            project, _, receipt_path = vercel_contexts[destination.destination_id]
            receipt_reservations[destination.destination_id] = _reserve_vercel_receipt(
                final_path=receipt_path,
                secret_name=secret_name,
                version=version,
                destination=destination,
                project=project,
            )

        for destination in local_destinations:
            if destination.kind == "sops_k8s_secret":
                _seal_k8s_secret(
                    destination=destination,
                    secret=secret,
                    version=version,
                    repository_root=repository_root,
                    sops_bin=sops_bin,
                )
            elif destination.kind in {"sops_escrow", "sops_ansible_vars"}:
                _seal_named_document(
                    destination=destination,
                    secret_name=secret_name,
                    secret=secret,
                    version=version,
                    repository_root=repository_root,
                    sops_bin=sops_bin,
                )
        for destination in vercel_destinations:
            project, project_root, _ = vercel_contexts[destination.destination_id]
            project_root = _verify_vercel_project(
                expected=project,
                vercel_project=project_root,
            )
            _send_vercel_secret(
                destination=destination,
                secret=secret,
                vercel_bin=vercel_bin,
                vercel_project=project_root,
            )
            _finalize_vercel_receipt(receipt_reservations[destination.destination_id])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Move one secret through the versioned destination matrix without printing it."
    )
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--destination", action="append", required=True)
    parser.add_argument("--source", choices=("terraform", "stdin", "prompt"), required=True)
    parser.add_argument("--terraform-bin", default=os.environ.get("TERRAFORM_BIN", "terraform"))
    parser.add_argument("--sops-bin", default=os.environ.get("SOPS_BIN", "sops"))
    parser.add_argument("--vercel-bin", default=os.environ.get("VERCEL_BIN", "vercel"))
    parser.add_argument("--vercel-project", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        execute_handoff(
            matrix_path=args.matrix,
            repository_root=args.repository_root,
            secret_name=args.secret,
            version=args.version,
            destination_ids=tuple(args.destination),
            source_kind=args.source,
            terraform_bin=args.terraform_bin,
            sops_bin=args.sops_bin,
            vercel_bin=args.vercel_bin,
            vercel_project=args.vercel_project,
            dry_run=args.dry_run,
        )
    except HandoffError as exc:
        print(f"handoff rejected: {exc}", file=sys.stderr)
        return 2
    print("handoff policy accepted" if args.dry_run else "secret handoff completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
