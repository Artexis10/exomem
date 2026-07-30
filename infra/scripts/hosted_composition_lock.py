#!/usr/bin/env python3
"""Compose one verified, source-closed hosted deployment lock pair."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

MAX_EVIDENCE_BYTES = 1_048_576
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_IMAGE = re.compile(r"^ghcr\.io/artexis10/exomem@sha256:[0-9a-f]{64}$")
_PROVISIONER_IMAGE = re.compile(
    r"^ghcr\.io/artexis10/exomem-provisioner@sha256:[0-9a-f]{64}$"
)
_RUNTIME_CLOSURE = ("Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "README.md", "LICENSE", "src/**")
_PROVISIONER_CLOSURE = (
    "infra/provisioner/Dockerfile",
    "infra/provisioner/pyproject.toml",
    "infra/provisioner/uv.lock",
    "infra/provisioner/README.md",
    "infra/provisioner/alembic.ini",
    "infra/provisioner/src/**",
    "infra/provisioner/alembic/**",
    "infra/helm/cell/**",
    ".dockerignore",
)


class CompositionError(ValueError):
    """Raised when deployment-lock composition cannot establish every proof."""


def _error(message: str) -> NoReturn:
    raise CompositionError(message)


def _load_candidate_module() -> Any:
    path = Path(__file__).with_name("hosted_image_candidate.py")
    spec = importlib.util.spec_from_file_location("hosted_image_candidate", path)
    if spec is None or spec.loader is None:
        _error("candidate verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hosted_image_candidate = _load_candidate_module()


@dataclass(frozen=True)
class CandidateInput:
    candidate: Path
    sha256: str
    image_bundle: Path | None
    candidate_bundle: Path
    bundle_from_oci: bool = False


@dataclass(frozen=True)
class HashedInput:
    path: Path
    sha256: str


@dataclass(frozen=True)
class CompositionRequest:
    repository: Path
    composition_commit: str
    runtime: CandidateInput
    provisioner: CandidateInput
    forward_contract: HashedInput
    authoritative_legacy_release_set: HashedInput
    legacy_catalog: HashedInput
    legacy_contracts: tuple[HashedInput, ...]
    rollback: HashedInput
    output: Path


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _error("evidence has duplicate JSON keys")
        result[key] = value
    return result


def _read_regular(path: Path, *, label: str, maximum: int = MAX_EVIDENCE_BYTES) -> bytes:
    try:
        initial = path.lstat()
    except OSError as exc:
        raise CompositionError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(initial.st_mode):
        _error(f"{label} must not be a symlink")
    if not stat.S_ISREG(initial.st_mode):
        _error(f"{label} must be a regular file")
    if not 1 <= initial.st_size <= maximum:
        _error(f"{label} exceeds its size contract")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CompositionError(f"cannot open {label}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino)
        ):
            _error(f"{label} changed while opening")
        if not 1 <= opened.st_size <= maximum:
            _error(f"{label} exceeds its size contract")
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(maximum + 1)
    except OSError as exc:
        os.close(descriptor)
        raise CompositionError(f"cannot read {label}") from exc
    if not 1 <= len(data) <= maximum:
        _error(f"{label} exceeds its size contract")
    return data


def _load_hashed(value: HashedInput, *, label: str) -> tuple[dict[str, Any], bytes]:
    if not _SHA256.fullmatch(value.sha256):
        _error(f"{label} SHA-256 is invalid")
    raw = _read_regular(value.path, label=label)
    if hashlib.sha256(raw).hexdigest() != value.sha256:
        _error(f"{label} SHA-256 does not match")
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompositionError(f"{label} is not strict JSON") from exc
    if not isinstance(decoded, dict):
        _error(f"{label} must be a JSON object")
    return cast(dict[str, Any], decoded), raw


def _exact_object(value: object, *, label: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _error(f"{label} fields are incomplete or unknown")
    return cast(dict[str, Any], value)


def _commit(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        _error(f"{label} must be an exact commit")
    return value


def _sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _error(f"{label} must be a SHA-256 digest")
    return value


def _image(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _error(f"{label} must be an immutable approved image digest")
    return value


def _target(value: object, *, label: str) -> dict[str, str]:
    target = _exact_object(
        value,
        label=label,
        fields={
            "releaseVersion",
            "protocolVersion",
            "agentProfile",
            "gatewayContractDigest",
            "commandFingerprint",
            "schemaDigest",
        },
    )
    for field in ("releaseVersion", "protocolVersion", "agentProfile"):
        if not isinstance(target[field], str) or not target[field]:
            _error(f"{label}.{field} must be a non-empty string")
        if "placeholder" in target[field].lower() or target[field].startswith("<"):
            _error(f"{label}.{field} must not be a placeholder")
    for field in ("gatewayContractDigest", "commandFingerprint", "schemaDigest"):
        _sha256(target[field], label=f"{label}.{field}")
    return cast(dict[str, str], target)


def _contract(value: dict[str, Any], *, label: str) -> tuple[dict[str, str], str, str]:
    contract = _exact_object(
        value,
        label=label,
        fields={
            "releaseVersion",
            "protocolVersion",
            "agentProfile",
            "gatewayContractDigest",
            "commandFingerprint",
            "schemaDigest",
            "runtimeImage",
            "sourceCommit",
        },
    )
    target = _target(
        {key: contract[key] for key in _target_fields()},
        label=label,
    )
    image = _image(contract["runtimeImage"], label=f"{label}.runtimeImage", pattern=_RUNTIME_IMAGE)
    source = _commit(contract["sourceCommit"], label=f"{label}.sourceCommit")
    return target, image, source


def _target_fields() -> tuple[str, ...]:
    return (
        "releaseVersion",
        "protocolVersion",
        "agentProfile",
        "gatewayContractDigest",
        "commandFingerprint",
        "schemaDigest",
    )


def _candidate(input_value: CandidateInput, *, kind: str) -> tuple[dict[str, Any], str]:
    raw = _read_regular(input_value.candidate, label=f"{kind} candidate", maximum=128 * 1024)
    if not _SHA256.fullmatch(input_value.sha256):
        _error(f"{kind} candidate SHA-256 is invalid")
    candidate_sha256 = hashlib.sha256(raw).hexdigest()
    if candidate_sha256 != input_value.sha256:
        _error(f"{kind} candidate SHA-256 does not match")
    bundle_maximum = cast(int, hosted_image_candidate.MAX_BUNDLE_BYTES)
    candidate_bundle = _read_regular(
        input_value.candidate_bundle,
        label=f"{kind} candidate bundle",
        maximum=bundle_maximum,
    )
    image_bundle: bytes | None = None
    if input_value.bundle_from_oci:
        if input_value.image_bundle is not None:
            _error(f"{kind} candidate has conflicting image evidence")
    else:
        if input_value.image_bundle is None:
            _error(f"{kind} candidate image evidence is required")
        image_bundle = _read_regular(
            input_value.image_bundle,
            label=f"{kind} image bundle",
            maximum=bundle_maximum,
        )
    with tempfile.TemporaryDirectory(prefix="exomem-composition-") as directory:
        staged = Path(directory)
        candidate_path = staged / input_value.candidate.name
        candidate_path.write_bytes(raw)
        candidate_bundle_path = staged / input_value.candidate_bundle.name
        candidate_bundle_path.write_bytes(candidate_bundle)
        if image_bundle is not None and input_value.image_bundle is not None:
            image_bundle_path = staged / input_value.image_bundle.name
            image_bundle_path.write_bytes(image_bundle)
        else:
            image_bundle_path = None
        try:
            parsed = hosted_image_candidate.load_candidate(candidate_path)
        except Exception as exc:
            raise CompositionError(f"{kind} candidate is invalid") from exc
        if raw != _canonical(parsed):
            _error(f"{kind} candidate bytes are not canonical")
        try:
            if input_value.bundle_from_oci:
                hosted_image_candidate.verify_candidate(
                    candidate_path,
                    bundle_from_oci=True,
                    candidate_bundle=candidate_bundle_path,
                )
            else:
                assert image_bundle_path is not None
                hosted_image_candidate.verify_candidate(
                    candidate_path,
                    bundle=image_bundle_path,
                    candidate_bundle=candidate_bundle_path,
                )
        except CompositionError:
            raise
        except Exception as exc:
            raise CompositionError(f"{kind} candidate verification failed") from exc
    if parsed.get("kind") != kind:
        _error(f"{kind} candidate kind is invalid")
    return cast(dict[str, Any], parsed), candidate_sha256


def _candidate_identity(candidate: dict[str, Any], *, kind: str) -> tuple[str, str]:
    image = candidate.get("image")
    source = candidate.get("source")
    if not isinstance(image, dict) or not isinstance(source, dict):
        _error(f"{kind} candidate identity is invalid")
    pattern = _RUNTIME_IMAGE if kind == "runtime" else _PROVISIONER_IMAGE
    return (
        _image(image.get("reference"), label=f"{kind} candidate image", pattern=pattern),
        _commit(source.get("commit"), label=f"{kind} candidate source"),
    )


def _git(repository: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", os.fspath(repository), *arguments],
            check=False,
            capture_output=True,
            shell=False,
        )
    except OSError as exc:
        raise CompositionError("Git source proof could not run") from exc


def verify_source_closure(
    repository: Path, candidate_commit: str, composition_commit: str, paths: tuple[str, ...]
) -> dict[str, object]:
    """Require an unshallow, ancestor-reachable commit with no closure diff."""

    _commit(candidate_commit, label="candidate source")
    _commit(composition_commit, label="composition source")
    if not paths:
        _error("source closure is empty")
    shallow = _git(repository, ["rev-parse", "--is-shallow-repository"])
    if shallow.returncode != 0 or shallow.stdout.strip() != b"false":
        _error("Git source proof requires an unshallow repository")
    for commit in (candidate_commit, composition_commit):
        exists = _git(repository, ["cat-file", "-e", f"{commit}^{{commit}}"])
        if exists.returncode != 0:
            _error("Git source proof commit is unavailable")
    ancestry = _git(repository, ["merge-base", "--is-ancestor", candidate_commit, composition_commit])
    if ancestry.returncode != 0:
        _error("candidate source is not an ancestor of composition source")
    changes = _git(
        repository,
        [
            "diff",
            "--no-ext-diff",
            "--name-status",
            "-z",
            f"{candidate_commit}..{composition_commit}",
            "--",
            *paths,
        ],
    )
    if changes.returncode != 0:
        _error("Git source closure diff failed")
    if changes.stdout:
        _error("source closure changed after candidate publication")
    return {
        "candidateCommit": candidate_commit,
        "compositionCommit": composition_commit,
        "paths": list(paths),
    }


def _legacy_catalog(
    catalog: dict[str, Any],
    authority: dict[str, Any],
    contracts: tuple[HashedInput, ...],
) -> tuple[list[dict[str, object]], str]:
    body = _exact_object(catalog, label="legacy catalog", fields={"schemaVersion", "units"})
    if body["schemaVersion"] != 1 or not isinstance(body["units"], list) or not body["units"]:
        _error("legacy catalog is invalid")
    authority_body = _exact_object(
        authority,
        label="authoritative legacy release set",
        fields={"artifact", "schemaVersion", "units"},
    )
    if (
        authority_body["artifact"] != "exomem-hosted-authoritative-legacy-v1-release-set"
        or authority_body["schemaVersion"] != 1
        or not isinstance(authority_body["units"], list)
        or not authority_body["units"]
    ):
        _error("authoritative legacy release set is invalid")
    authoritative_units: set[tuple[str, str, str, str]] = set()
    for raw_unit in authority_body["units"]:
        unit = _exact_object(
            raw_unit,
            label="authoritative legacy release unit",
            fields={"releaseVersion", "protocolVersion", "runtimeImage", "sourceCommit"},
        )
        release = unit["releaseVersion"]
        protocol = unit["protocolVersion"]
        if not isinstance(release, str) or not release or not isinstance(protocol, str) or not protocol:
            _error("authoritative legacy release unit identity is invalid")
        authority_key = (
            release,
            protocol,
            _image(unit["runtimeImage"], label="authoritative legacy image", pattern=_RUNTIME_IMAGE),
            _commit(unit["sourceCommit"], label="authoritative legacy source"),
        )
        if authority_key in authoritative_units:
            _error("authoritative legacy release set has duplicates")
        authoritative_units.add(authority_key)
    supplied: dict[str, dict[str, Any]] = {}
    for item in contracts:
        if item.sha256 in supplied:
            _error("legacy contract evidence is duplicated")
        loaded, raw = _load_hashed(item, label="legacy contract")
        if raw != _canonical(loaded):
            _error("legacy contract evidence is not canonical")
        supplied[item.sha256] = loaded
    units: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    used: set[str] = set()
    for raw_unit in body["units"]:
        unit = _exact_object(
            raw_unit,
            label="legacy catalog unit",
            fields={"releaseVersion", "protocolVersion", "runtimeImage", "sourceCommit", "contractSha256"},
        )
        release = unit["releaseVersion"]
        protocol = unit["protocolVersion"]
        if not isinstance(release, str) or not release or not isinstance(protocol, str) or not protocol:
            _error("legacy catalog unit identity is invalid")
        catalog_key = (release, protocol)
        if catalog_key in seen:
            _error("legacy catalog has duplicate runtime units")
        seen.add(catalog_key)
        contract_sha = _sha256(unit["contractSha256"], label="legacy catalog contract")
        contract = supplied.get(contract_sha)
        if contract is None:
            _error("legacy catalog unit lacks verified contract evidence")
        target, image, source = _contract(contract, label="legacy contract")
        if (
            target["releaseVersion"] != release
            or target["protocolVersion"] != protocol
            or image != _image(unit["runtimeImage"], label="legacy catalog image", pattern=_RUNTIME_IMAGE)
            or source != _commit(unit["sourceCommit"], label="legacy catalog source")
        ):
            _error("legacy catalog contract does not match its authoritative unit")
        used.add(contract_sha)
        units.append(
            {
                "releaseVersion": release,
                "protocolVersion": protocol,
                "runtimeImage": image,
                "sourceCommit": source,
                "contractSha256": contract_sha,
                "contract": contract,
            }
        )
    if used != set(supplied):
        _error("legacy contract evidence is not authoritative")
    catalog_units = {
        (cast(str, unit["releaseVersion"]), cast(str, unit["protocolVersion"]), cast(str, unit["runtimeImage"]), cast(str, unit["sourceCommit"]))
        for unit in units
    }
    if catalog_units != authoritative_units:
        _error("legacy catalog does not exactly match the authoritative release set")
    units.sort(key=lambda unit: (cast(str, unit["releaseVersion"]), cast(str, unit["protocolVersion"])))
    release_set = [{"releaseVersion": release, "protocolVersion": protocol} for release, protocol in sorted(seen)]
    return units, hashlib.sha256(_canonical(release_set)).hexdigest()


def _rollback(value: dict[str, Any]) -> dict[str, str]:
    rollback = _exact_object(
        value,
        label="rollback evidence",
        fields={
            "provisionerImage",
            "provisionerSourceCommit",
            "v1CorpusSha256",
            "legacyManifestSha256",
            "substrateV1ConsumerCommit",
        },
    )
    return {
        "provisionerImage": _image(
            rollback["provisionerImage"], label="rollback provisioner image", pattern=_PROVISIONER_IMAGE
        ),
        "provisionerSourceCommit": _commit(
            rollback["provisionerSourceCommit"], label="rollback provisioner source"
        ),
        "v1CorpusSha256": _sha256(rollback["v1CorpusSha256"], label="rollback v1 corpus"),
        "legacyManifestSha256": _sha256(
            rollback["legacyManifestSha256"], label="rollback legacy manifest"
        ),
        "substrateV1ConsumerCommit": _commit(
            rollback["substrateV1ConsumerCommit"], label="rollback consumer source"
        ),
    }


def _validate_output(path: Path) -> None:
    if path.exists() or path.is_symlink():
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise CompositionError("cannot inspect lock output") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            _error("lock output must be a regular non-symlink file")
    parent = path.parent
    if not parent.exists():
        _error("lock output directory must already exist")
    while parent.exists():
        try:
            information = parent.lstat()
        except OSError as exc:
            raise CompositionError("cannot inspect lock output directory") from exc
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            stat.S_ISLNK(information.st_mode)
            or not stat.S_ISDIR(information.st_mode)
            or getattr(information, "st_file_attributes", 0) & reparse
        ):
            _error("lock output directory is unsafe")
        if parent.parent == parent:
            break
        parent = parent.parent


def _stage(path: Path, data: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _write_pair_atomic(path: Path, data: bytes) -> None:
    _validate_output(path)
    temporary: Path | None = None
    try:
        temporary = _stage(path, data)
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except OSError as exc:
        raise CompositionError("lock pair write failed") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_deployment_lock(value: object) -> None:
    lock = _exact_object(
        value,
        label="deployment lock",
        fields={"artifact", "schemaVersion", "admissionMode", "components", "runtimeTarget", "composition", "rollback"},
    )
    if lock["artifact"] != "exomem-hosted-deployment-lock" or lock["schemaVersion"] != 2:
        _error("deployment lock identity is invalid")
    if lock["admissionMode"] not in {"expand", "contract"}:
        _error("deployment lock admission mode is invalid")
    target = _target(lock["runtimeTarget"], label="deployment lock runtime target")
    components = _exact_object(lock["components"], label="deployment lock components", fields={"runtime", "provisioner"})
    runtime = _exact_object(
        components["runtime"], label="runtime component", fields={"image", "sourceCommit", "candidateSha256"}
    )
    provisioner = _exact_object(
        components["provisioner"],
        label="provisioner component",
        fields={"image", "sourceCommit", "candidateSha256", "wireProtocol"},
    )
    _image(runtime["image"], label="runtime component image", pattern=_RUNTIME_IMAGE)
    _commit(runtime["sourceCommit"], label="runtime component source")
    _sha256(runtime["candidateSha256"], label="runtime component candidate")
    _image(provisioner["image"], label="provisioner component image", pattern=_PROVISIONER_IMAGE)
    _commit(provisioner["sourceCommit"], label="provisioner component source")
    _sha256(provisioner["candidateSha256"], label="provisioner component candidate")
    if provisioner["wireProtocol"] != "exomem-cell-provisioner.v2":
        _error("deployment lock wire protocol is invalid")
    composition = _exact_object(
        lock["composition"],
        label="composition evidence",
        fields={"commit", "sourceClosure", "forwardContractSha256", "authoritativeLegacyReleaseSetSha256", "legacyCatalog", "legacyReleaseSetSha256"},
    )
    _commit(composition["commit"], label="composition commit")
    _sha256(composition["forwardContractSha256"], label="forward contract")
    _sha256(composition["authoritativeLegacyReleaseSetSha256"], label="authoritative legacy release set")
    _sha256(composition["legacyReleaseSetSha256"], label="legacy release set")
    if not isinstance(composition["legacyCatalog"], list) or not composition["legacyCatalog"]:
        _error("deployment lock legacy catalog is invalid")
    legacy_units: set[tuple[str, str]] = set()
    for raw_unit in composition["legacyCatalog"]:
        unit = _exact_object(
            raw_unit,
            label="deployment lock legacy unit",
            fields={
                "releaseVersion",
                "protocolVersion",
                "runtimeImage",
                "sourceCommit",
                "contractSha256",
                "contract",
            },
        )
        if not isinstance(unit["releaseVersion"], str) or not unit["releaseVersion"]:
            _error("deployment lock legacy release is invalid")
        if not isinstance(unit["protocolVersion"], str) or not unit["protocolVersion"]:
            _error("deployment lock legacy protocol is invalid")
        key = (unit["releaseVersion"], unit["protocolVersion"])
        if key in legacy_units:
            _error("deployment lock legacy catalog has duplicates")
        legacy_units.add(key)
        unit_image = _image(unit["runtimeImage"], label="deployment lock legacy image", pattern=_RUNTIME_IMAGE)
        unit_source = _commit(unit["sourceCommit"], label="deployment lock legacy source")
        _sha256(unit["contractSha256"], label="deployment lock legacy contract")
        if hashlib.sha256(_canonical(unit["contract"])).hexdigest() != unit["contractSha256"]:
            _error("deployment lock legacy contract hash is invalid")
        contract_target, contract_image, contract_source = _contract(
            cast(dict[str, Any], unit["contract"]), label="deployment lock legacy contract"
        )
        if (
            contract_target["releaseVersion"] != key[0]
            or contract_target["protocolVersion"] != key[1]
            or contract_image != unit_image
            or contract_source != unit_source
        ):
            _error("deployment lock legacy contract does not match its unit")
    release_set = [
        {"releaseVersion": release, "protocolVersion": protocol}
        for release, protocol in sorted(legacy_units)
    ]
    if hashlib.sha256(_canonical(release_set)).hexdigest() != composition["legacyReleaseSetSha256"]:
        _error("deployment lock legacy release set digest is invalid")
    closure = _exact_object(composition["sourceClosure"], label="source closure", fields={"runtime", "provisioner"})
    for name, expected_paths, component in (
        ("runtime", _RUNTIME_CLOSURE, runtime),
        ("provisioner", _PROVISIONER_CLOSURE, provisioner),
    ):
        proof = _exact_object(
            closure[name], label=f"{name} source closure", fields={"candidateCommit", "compositionCommit", "paths"}
        )
        candidate_commit = _commit(proof["candidateCommit"], label=f"{name} closure candidate")
        composition_proof = _commit(proof["compositionCommit"], label=f"{name} closure composition")
        if candidate_commit != component["sourceCommit"] or composition_proof != composition["commit"]:
            _error(f"{name} source closure commits are not bound to the lock")
        if proof["paths"] != list(expected_paths):
            _error(f"{name} source closure paths are invalid")
    _rollback(cast(dict[str, Any], lock["rollback"]))
    if target["releaseVersion"] == "placeholder":
        _error("deployment lock contains a placeholder")


def validate_deployment_lock_pair(value: object) -> None:
    pair = _exact_object(value, label="deployment lock pair", fields={"artifact", "schemaVersion", "locks"})
    if pair["artifact"] != "exomem-hosted-deployment-lock-pair" or pair["schemaVersion"] != 2:
        _error("deployment lock pair identity is invalid")
    if not isinstance(pair["locks"], list) or len(pair["locks"]) != 2:
        _error("deployment lock pair must contain exactly two locks")
    members: dict[str, dict[str, object]] = {}
    for member in pair["locks"]:
        validate_deployment_lock(member)
        lock = cast(dict[str, object], member)
        mode = cast(str, lock["admissionMode"])
        if mode in members:
            _error("deployment lock pair has duplicate admission modes")
        members[mode] = lock
    if set(members) != {"expand", "contract"}:
        _error("deployment lock pair admission modes are invalid")
    expand = {key: item for key, item in members["expand"].items() if key != "admissionMode"}
    contract = {key: item for key, item in members["contract"].items() if key != "admissionMode"}
    if expand != contract:
        _error("deployment lock pair members differ outside admission mode")


def compose_locks(request: CompositionRequest) -> dict[str, object]:
    """Verify all composition evidence and write the deterministic phase-lock pair."""

    composition_commit = _commit(request.composition_commit, label="composition commit")
    runtime_candidate, runtime_candidate_sha = _candidate(request.runtime, kind="runtime")
    provisioner_candidate, provisioner_candidate_sha = _candidate(request.provisioner, kind="provisioner")
    runtime_image, runtime_source = _candidate_identity(runtime_candidate, kind="runtime")
    provisioner_image, provisioner_source = _candidate_identity(provisioner_candidate, kind="provisioner")
    forward, _ = _load_hashed(request.forward_contract, label="forward runtime contract")
    runtime_target, contract_image, contract_source = _contract(forward, label="forward runtime contract")
    if contract_image != runtime_image or contract_source != runtime_source:
        _error("forward runtime contract does not match verified runtime candidate")
    release = runtime_candidate.get("release")
    if not isinstance(release, dict) or release.get("version") != runtime_target["releaseVersion"]:
        _error("runtime candidate release does not match forward contract")
    authority, _ = _load_hashed(request.authoritative_legacy_release_set, label="authoritative legacy release set")
    catalog, _ = _load_hashed(request.legacy_catalog, label="legacy catalog")
    legacy_catalog, release_set_sha = _legacy_catalog(catalog, authority, request.legacy_contracts)
    rollback_evidence, _ = _load_hashed(request.rollback, label="rollback evidence")
    rollback = _rollback(rollback_evidence)
    closure = {
        "runtime": verify_source_closure(
            request.repository, runtime_source, composition_commit, _RUNTIME_CLOSURE
        ),
        "provisioner": verify_source_closure(
            request.repository, provisioner_source, composition_commit, _PROVISIONER_CLOSURE
        ),
    }
    common: dict[str, object] = {
        "artifact": "exomem-hosted-deployment-lock",
        "schemaVersion": 2,
        "components": {
            "runtime": {
                "image": runtime_image,
                "sourceCommit": runtime_source,
                "candidateSha256": runtime_candidate_sha,
            },
            "provisioner": {
                "image": provisioner_image,
                "sourceCommit": provisioner_source,
                "candidateSha256": provisioner_candidate_sha,
                "wireProtocol": "exomem-cell-provisioner.v2",
            },
        },
        "runtimeTarget": runtime_target,
        "composition": {
            "commit": composition_commit,
            "sourceClosure": closure,
            "forwardContractSha256": request.forward_contract.sha256,
            "authoritativeLegacyReleaseSetSha256": request.authoritative_legacy_release_set.sha256,
            "legacyCatalog": legacy_catalog,
            "legacyReleaseSetSha256": release_set_sha,
        },
        "rollback": rollback,
    }
    expand = {**copy.deepcopy(common), "admissionMode": "expand"}
    contract = {**copy.deepcopy(common), "admissionMode": "contract"}
    pair = {"artifact": "exomem-hosted-deployment-lock-pair", "schemaVersion": 2, "locks": [expand, contract]}
    validate_deployment_lock_pair(pair)
    _write_pair_atomic(request.output, _canonical(pair))
    return pair


def _hashed_argument(value: str) -> HashedInput:
    path, separator, digest = value.rpartition("=")
    if separator != "=" or not path:
        raise argparse.ArgumentTypeError("must be PATH=SHA256")
    return HashedInput(Path(path), digest)


def _candidate_arguments(parser: argparse.ArgumentParser, name: str) -> None:
    parser.add_argument(f"--{name}-candidate", type=Path, required=True)
    parser.add_argument(f"--{name}-candidate-sha256", required=True)
    parser.add_argument(f"--{name}-candidate-bundle", type=Path, required=True)
    evidence = parser.add_mutually_exclusive_group(required=True)
    evidence.add_argument(f"--{name}-bundle", type=Path)
    evidence.add_argument(f"--{name}-bundle-from-oci", action="store_true")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--composition-commit", required=True)
    _candidate_arguments(parser, "runtime")
    _candidate_arguments(parser, "provisioner")
    parser.add_argument("--forward-contract", type=_hashed_argument, required=True)
    parser.add_argument("--authoritative-legacy-release-set", type=_hashed_argument, required=True)
    parser.add_argument("--legacy-catalog", type=_hashed_argument, required=True)
    parser.add_argument("--legacy-contract", type=_hashed_argument, action="append", required=True)
    parser.add_argument("--rollback", type=_hashed_argument, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    request = CompositionRequest(
        repository=args.repository,
        composition_commit=args.composition_commit,
        runtime=CandidateInput(
            args.runtime_candidate,
            args.runtime_candidate_sha256,
            args.runtime_bundle,
            args.runtime_candidate_bundle,
            args.runtime_bundle_from_oci,
        ),
        provisioner=CandidateInput(
            args.provisioner_candidate,
            args.provisioner_candidate_sha256,
            args.provisioner_bundle,
            args.provisioner_candidate_bundle,
            args.provisioner_bundle_from_oci,
        ),
        forward_contract=args.forward_contract,
        authoritative_legacy_release_set=args.authoritative_legacy_release_set,
        legacy_catalog=args.legacy_catalog,
        legacy_contracts=tuple(args.legacy_contract),
        rollback=args.rollback,
        output=args.output,
    )
    try:
        compose_locks(request)
    except CompositionError as exc:
        print(f"hosted composition lock: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
