#!/usr/bin/env python3
"""Validate one hosted release unit and render its inseparable deploy inputs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_FIELDS = {
    "artifact",
    "schemaVersion",
    "sourceRepository",
    "sourceCommit",
    "release",
    "hostedProtocol",
    "releaseBuildTime",
    "runtimeImage",
    "publishedTag",
    "operatorContractSha256",
    "gatewayContractSha256",
    "commandRegistry",
}
_COMMAND_FIELDS = {"name", "readOnly", "mode", "tier", "capability"}
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_COMMIT = re.compile(r"[a-f0-9]{40}\Z")
_RELEASE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\Z")
_IMAGE = re.compile(r"ghcr\.io/artexis10/exomem@sha256:[a-f0-9]{64}\Z")
_PROVISIONER_IMAGE = re.compile(r"ghcr\.io/artexis10/exomem-provisioner@sha256:[a-f0-9]{64}\Z")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}\.)+[a-z]{2,63}\Z")
_CANONICAL_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1] / "helm/platform/files/canonical-command-registry-v1.json"
)
_CANONICAL_REGISTRY = json.loads(_CANONICAL_REGISTRY_PATH.read_text(encoding="utf-8"))
_LOCK_FIELDS = {
    "artifact",
    "schemaVersion",
    "admissionMode",
    "components",
    "runtimeTarget",
    "composition",
    "rollback",
}


class ReleaseManifestError(RuntimeError):
    """The selected release is incomplete, mutable, or internally inconsistent."""


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseManifestError("deployment lock pair has duplicate JSON keys")
        result[key] = value
    return result


def _load_composer() -> Any:
    path = Path(__file__).with_name("hosted_composition_lock.py")
    spec = importlib.util.spec_from_file_location("hosted_composition_lock", path)
    if spec is None or spec.loader is None:
        raise ReleaseManifestError("deployment lock verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_pair(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseManifestError("deployment lock pair must be a regular file")
    try:
        raw = path.read_bytes()
        pair = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError("deployment lock pair is invalid JSON") from exc
    if raw != _canonical(pair):
        raise ReleaseManifestError("deployment lock pair bytes are not canonical")
    try:
        _load_composer().validate_deployment_lock_pair(pair)
    except Exception as exc:
        raise ReleaseManifestError("deployment lock pair is invalid") from exc
    return pair


def _select_member(
    pair: dict[str, Any], *, phase: str, member_sha256: str | None
) -> tuple[dict[str, Any], str]:
    if phase not in {"expand", "contract"}:
        raise ReleaseManifestError("deployment lock phase is invalid")
    members = pair.get("locks")
    if not isinstance(members, list):
        raise ReleaseManifestError("deployment lock pair members are invalid")
    selected = next(
        (
            member
            for member in members
            if isinstance(member, dict) and member.get("admissionMode") == phase
        ),
        None,
    )
    if not isinstance(selected, dict) or set(selected) != _LOCK_FIELDS:
        raise ReleaseManifestError("selected deployment lock is invalid")
    digest = hashlib.sha256(_canonical(selected)).hexdigest()
    if member_sha256 is not None and (
        not _SHA256.fullmatch(member_sha256) or member_sha256 != digest
    ):
        raise ReleaseManifestError("selected deployment lock SHA-256 does not match the pair member")
    return selected, digest


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo == UTC


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _FIELDS:
        raise ReleaseManifestError("release manifest must use the exact field set")
    if (
        manifest.get("artifact") != "exomem-hosted-release"
        or type(manifest.get("schemaVersion")) is not int
        or manifest.get("schemaVersion") != 1
    ):
        raise ReleaseManifestError("release manifest identity is invalid")
    repository = manifest.get("sourceRepository")
    parsed_repository = urlsplit(repository) if isinstance(repository, str) else None
    if (
        parsed_repository is None
        or parsed_repository.scheme != "https"
        or not parsed_repository.hostname
        or parsed_repository.username is not None
        or parsed_repository.password is not None
        or parsed_repository.query
        or parsed_repository.fragment
        or repository != "https://github.com/Artexis10/exomem"
    ):
        raise ReleaseManifestError("release source repository is invalid")
    if not isinstance(manifest.get("sourceCommit"), str) or not _COMMIT.fullmatch(
        manifest["sourceCommit"]
    ):
        raise ReleaseManifestError("release source commit is invalid")
    release = manifest.get("release")
    if not isinstance(release, str) or not _RELEASE.fullmatch(release):
        raise ReleaseManifestError("release version is invalid")
    if manifest.get("hostedProtocol") != "1":
        raise ReleaseManifestError("hosted protocol is unsupported")
    if not _timestamp(manifest.get("releaseBuildTime")):
        raise ReleaseManifestError("release build time is invalid")
    runtime_image = manifest.get("runtimeImage")
    if not isinstance(runtime_image, str) or not _IMAGE.fullmatch(runtime_image):
        raise ReleaseManifestError("runtime image must use an immutable digest")
    source_commit = manifest["sourceCommit"]
    if manifest.get("publishedTag") != (f"ghcr.io/artexis10/exomem:{source_commit}-hosted"):
        raise ReleaseManifestError("published tag is not bound to this release image")
    for field in ("operatorContractSha256", "gatewayContractSha256"):
        value = manifest.get(field)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ReleaseManifestError("release contract digest is invalid")
    registry = manifest.get("commandRegistry")
    if not isinstance(registry, list) or len(registry) != 21:
        raise ReleaseManifestError("release command registry must contain 21 commands")
    names: list[str] = []
    for command in registry:
        if not isinstance(command, dict) or set(command) != _COMMAND_FIELDS:
            raise ReleaseManifestError("release command registry shape is invalid")
        name = command.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ReleaseManifestError("release command name is invalid")
        if not isinstance(command.get("readOnly"), bool):
            raise ReleaseManifestError("release command readOnly value is invalid")
        mode = command.get("mode")
        tier = command.get("tier")
        capability = command.get("capability")
        if (
            mode not in {"read", "write"}
            or command["readOnly"] is not (mode == "read")
            or not isinstance(tier, int)
            or isinstance(tier, bool)
            or tier < 1
            or not isinstance(capability, str)
            or not capability
            or len(capability) > 64
        ):
            raise ReleaseManifestError("release command classification is invalid")
        names.append(name)
    if len(set(names)) != len(names):
        raise ReleaseManifestError("release command registry contains duplicates")
    if registry != _CANONICAL_REGISTRY:
        raise ReleaseManifestError("release command registry is not canonical")
    return manifest


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o022:
        raise ReleaseManifestError(
            "release manifest must be a regular file without group/world write access"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError("release manifest is invalid JSON") from exc
    return _validate_manifest(document)


def _write_private_json(path: Path, document: dict[str, Any]) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ReleaseManifestError("release output path is unsafe")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def prepare(
    *,
    manifest_path: Path,
    values_path: Path,
    provisioner_image: str,
    control_hostname: str,
    transfer_hostname: str,
) -> None:
    if not _PROVISIONER_IMAGE.fullmatch(provisioner_image):
        raise ReleaseManifestError("provisioner image must use its immutable digest")
    if (
        not _HOSTNAME.fullmatch(control_hostname)
        or not _HOSTNAME.fullmatch(transfer_hostname)
        or control_hostname == transfer_hostname
    ):
        raise ReleaseManifestError("release hostnames are invalid or not distinct")
    manifest = _load_manifest(manifest_path)
    manifest_json = json.dumps(manifest, separators=(",", ":"), sort_keys=True)
    _write_private_json(
        values_path,
        {
            "provisioner": {
                "image": provisioner_image,
                "releaseManifestJson": manifest_json,
                "controlHostname": control_hostname,
                "transferHostname": transfer_hostname,
            },
        },
    )


def prepare_v2(
    *,
    lock_pair_path: Path,
    values_path: Path,
    phase: str,
    member_sha256: str | None,
    control_hostname: str,
    transfer_hostname: str,
) -> None:
    """Render v2 deploy inputs from one verified, selected pair member only."""

    if (
        not _HOSTNAME.fullmatch(control_hostname)
        or not _HOSTNAME.fullmatch(transfer_hostname)
        or control_hostname == transfer_hostname
    ):
        raise ReleaseManifestError("release hostnames are invalid or not distinct")
    selected, digest = _select_member(_load_pair(lock_pair_path), phase=phase, member_sha256=member_sha256)
    _write_private_json(
        values_path,
        {
            "provisioner": {
                "deploymentLockJson": _canonical(selected).decode("utf-8"),
                "deploymentLockSha256": digest,
                "controlHostname": control_hostname,
                "transferHostname": transfer_hostname,
            }
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-pair", type=Path, required=True)
    parser.add_argument("--values-output", type=Path, required=True)
    parser.add_argument("--phase", choices=("expand", "contract"), required=True)
    parser.add_argument("--member-sha256")
    parser.add_argument("--control-hostname", required=True)
    parser.add_argument("--transfer-hostname", required=True)
    args = parser.parse_args()
    try:
        prepare_v2(
            lock_pair_path=args.lock_pair,
            values_path=args.values_output,
            phase=args.phase,
            member_sha256=args.member_sha256,
            control_hostname=args.control_hostname,
            transfer_hostname=args.transfer_hostname,
        )
    except (OSError, ReleaseManifestError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
