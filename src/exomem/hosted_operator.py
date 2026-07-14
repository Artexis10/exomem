"""Versioned JSON-only operator CLI for hosted cell lifecycle work.

This module intentionally has no server imports.  It is safe to load from the
ordinary CLI dispatcher and delegates command execution through narrow seams.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, TextIO

CONTRACT_VERSION = 1
BINDING_VERSION = 2
REQUEST_MAX_BYTES = 65_536
STDOUT_MAX_BYTES = 65_536
OFFLINE_REQUEST_PATHS = {
    "init": Path("/run/exomem/operator-requests/init.json"),
    "restore-candidate": Path("/run/exomem/operator-requests/restore-candidate.json"),
}
LIVE_COMMANDS = frozenset({"credential", "probe"})
COMMANDS = (*OFFLINE_REQUEST_PATHS, *sorted(LIVE_COMMANDS))

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CREDENTIAL_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PROTOCOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")

_ERROR_CLASSES: dict[str, tuple[int, bool, str, str]] = {
    "input": (2, False, "hosted operator request is invalid", "fix-request"),
    "conflict": (
        3,
        False,
        "hosted operator state conflicts with the request",
        "inspect-content-free-state",
    ),
    "unavailable": (
        4,
        True,
        "hosted operator dependency is temporarily unavailable",
        "retry-with-the-same-request",
    ),
    "integrity": (
        5,
        False,
        "hosted data integrity verification failed",
        "quarantine-and-investigate",
    ),
    "internal": (
        6,
        False,
        "hosted operator failed safely",
        "inspect-redacted-operator-logs",
    ),
}

_STABLE_ERROR_CLASSES = {
    "HOSTED_OPERATOR_CONTRACT_INVALID": "input",
    "HOSTED_RELEASE_MISMATCH": "input",
    "HOSTED_PROTOCOL_UNSUPPORTED": "input",
    "HOSTED_RUNTIME_ID_INVALID": "input",
    "HOSTED_ROOT_INVALID": "input",
    "HOSTED_ROOT_OVERLAP": "input",
    "HOSTED_CREDENTIAL_BUNDLE_INVALID": "input",
    "HOSTED_CREDENTIAL_WEAK": "input",
    "HOSTED_PROBE_TRANSPORT_INVALID": "input",
    "HOSTED_ROOT_UNSAFE_ENTRY": "conflict",
    "HOSTED_ROOT_OWNERSHIP_MISMATCH": "conflict",
    "HOSTED_BINDING_CONFLICT": "conflict",
    "HOSTED_PROVISIONING_CONFLICT": "conflict",
    "HOSTED_OPERATION_CONFLICT": "conflict",
    "HOSTED_CREDENTIAL_STATE_INVALID": "conflict",
    "HOSTED_CREDENTIAL_TRANSITION_INVALID": "conflict",
    "HOSTED_CREDENTIAL_REVISION_CONFLICT": "conflict",
    "HOSTED_CREDENTIAL_PROOF_REQUIRED": "conflict",
    "HOSTED_CREDENTIAL_PROOF_STALE": "conflict",
    "HOSTED_RESTORE_NOT_OFFLINE": "conflict",
    "HOSTED_RESTORE_TARGET_CONFLICT": "conflict",
    "HOSTED_RESTORE_IDENTITY_CONFLICT": "conflict",
    "HOSTED_RESTORE_JOURNAL_CONFLICT": "conflict",
    "HOSTED_PROBE_REDIRECT": "conflict",
    "HOSTED_PROBE_RESPONSE_TOO_LARGE": "conflict",
    "HOSTED_PROBE_MEDIA_INVALID": "conflict",
    "HOSTED_PROBE_SCHEMA_INVALID": "conflict",
    "HOSTED_PROBE_AUTH_FAILED": "conflict",
    "HOSTED_PROBE_CONTRACT_MISMATCH": "conflict",
    "HOSTED_SECURITY_UNAVAILABLE": "unavailable",
    "HOSTED_RESTORE_BUSY": "unavailable",
    "HOSTED_PROBE_UNAVAILABLE": "unavailable",
    "HOSTED_PROBE_TIMEOUT": "unavailable",
    "HOSTED_ARTIFACT_DIGEST_MISMATCH": "integrity",
    "HOSTED_ARCHIVE_INVALID": "integrity",
    "HOSTED_ARCHIVE_UNSAFE_ENTRY": "integrity",
    "HOSTED_ARCHIVE_RUNTIME_STATE": "integrity",
    "HOSTED_ARCHIVE_INTEGRITY_FAILURE": "integrity",
    "HOSTED_RESTORE_CANONICAL_INTEGRITY": "integrity",
    "HOSTED_OPERATOR_INTERNAL": "internal",
}

_COMMAND_ERRORS = {
    "init": {
        "HOSTED_OPERATOR_CONTRACT_INVALID",
        "HOSTED_RELEASE_MISMATCH",
        "HOSTED_PROTOCOL_UNSUPPORTED",
        "HOSTED_RUNTIME_ID_INVALID",
        "HOSTED_ROOT_INVALID",
        "HOSTED_ROOT_OVERLAP",
        "HOSTED_ROOT_UNSAFE_ENTRY",
        "HOSTED_ROOT_OWNERSHIP_MISMATCH",
        "HOSTED_BINDING_CONFLICT",
        "HOSTED_PROVISIONING_CONFLICT",
        "HOSTED_CREDENTIAL_BUNDLE_INVALID",
        "HOSTED_CREDENTIAL_WEAK",
        "HOSTED_OPERATION_CONFLICT",
        "HOSTED_SECURITY_UNAVAILABLE",
        "HOSTED_OPERATOR_INTERNAL",
    },
    "restore-candidate": {
        "HOSTED_OPERATOR_CONTRACT_INVALID",
        "HOSTED_RESTORE_NOT_OFFLINE",
        "HOSTED_RESTORE_BUSY",
        "HOSTED_RESTORE_TARGET_CONFLICT",
        "HOSTED_RESTORE_IDENTITY_CONFLICT",
        "HOSTED_ARTIFACT_DIGEST_MISMATCH",
        "HOSTED_ARCHIVE_INVALID",
        "HOSTED_ARCHIVE_UNSAFE_ENTRY",
        "HOSTED_ARCHIVE_RUNTIME_STATE",
        "HOSTED_ARCHIVE_INTEGRITY_FAILURE",
        "HOSTED_RESTORE_JOURNAL_CONFLICT",
        "HOSTED_RESTORE_CANONICAL_INTEGRITY",
        "HOSTED_CREDENTIAL_BUNDLE_INVALID",
        "HOSTED_OPERATION_CONFLICT",
        "HOSTED_SECURITY_UNAVAILABLE",
        "HOSTED_OPERATOR_INTERNAL",
    },
    "credential": {
        "HOSTED_OPERATOR_CONTRACT_INVALID",
        "HOSTED_CREDENTIAL_BUNDLE_INVALID",
        "HOSTED_CREDENTIAL_WEAK",
        "HOSTED_CREDENTIAL_STATE_INVALID",
        "HOSTED_CREDENTIAL_TRANSITION_INVALID",
        "HOSTED_CREDENTIAL_REVISION_CONFLICT",
        "HOSTED_CREDENTIAL_PROOF_REQUIRED",
        "HOSTED_CREDENTIAL_PROOF_STALE",
        "HOSTED_OPERATION_CONFLICT",
        "HOSTED_SECURITY_UNAVAILABLE",
        "HOSTED_OPERATOR_INTERNAL",
    },
    "probe": {
        "HOSTED_OPERATOR_CONTRACT_INVALID",
        "HOSTED_CREDENTIAL_BUNDLE_INVALID",
        "HOSTED_CREDENTIAL_STATE_INVALID",
        "HOSTED_PROBE_TRANSPORT_INVALID",
        "HOSTED_PROBE_UNAVAILABLE",
        "HOSTED_PROBE_TIMEOUT",
        "HOSTED_PROBE_REDIRECT",
        "HOSTED_PROBE_RESPONSE_TOO_LARGE",
        "HOSTED_PROBE_MEDIA_INVALID",
        "HOSTED_PROBE_SCHEMA_INVALID",
        "HOSTED_PROBE_AUTH_FAILED",
        "HOSTED_PROBE_CONTRACT_MISMATCH",
        "HOSTED_SECURITY_UNAVAILABLE",
        "HOSTED_OPERATOR_INTERNAL",
    },
}

_COMMON_FIELDS = {"request_id"}
_MUTATING_FIELDS = _COMMON_FIELDS | {"operation_id"}
_COMMAND_FIELDS = {
    "init": _MUTATING_FIELDS
    | {
        "cell_id",
        "vault_id",
        "vault_root",
        "state_root",
        "log_root",
        "expected_release",
        "expected_protocol",
        "runtime_uid",
        "runtime_gid",
        "active_credential_version",
    },
    "restore-candidate": _MUTATING_FIELDS
    | {
        "artifact_reference",
        "archive_path",
        "expected_archive_sha256",
        "source_cell_id",
        "source_vault_id",
        "target_cell_id",
        "target_vault_id",
        "target_vault_root",
        "target_state_root",
        "target_log_root",
        "expected_release",
        "expected_protocol",
        "runtime_uid",
        "runtime_gid",
        "active_credential_version",
        "routing_stopped",
        "workload_stopped",
    },
    "credential": _MUTATING_FIELDS
    | {"cell_id", "vault_id", "state_root", "action", "expected_revision"},
    "probe": _MUTATING_FIELDS
    | {
        "cell_id",
        "vault_id",
        "state_root",
        "selected_credential_version",
        "expected_release",
        "expected_protocol",
        "expected_worker_policy_digest",
        "expected_revision",
        "port",
    },
}

_SUCCESS_FIELDS = {
    "init": {
        "status",
        "cell_id",
        "vault_id",
        "binding_version",
        "lifecycle_status",
        "exomem_release",
        "hosted_protocol",
        "runtime_uid",
        "runtime_gid",
        "credential_version",
        "credential_revision",
        "capabilities",
    },
    "restore-candidate": {
        "status",
        "artifact_reference_digest",
        "archive_sha256",
        "manifest_sha256",
        "source_cell_id",
        "source_vault_id",
        "target_cell_id",
        "target_vault_id",
        "binding_version",
        "exomem_release",
        "hosted_protocol",
        "journal_phase",
        "derived_state",
        "derived_error_code",
        "credential_version",
        "credential_revision",
    },
    "credential": {
        "phase",
        "revision",
        "active_version",
        "pending_version",
        "preferred_version",
        "rotation_id",
        "proof_valid_until",
    },
    "probe": {
        "cell_id",
        "vault_id",
        "exomem_release",
        "hosted_protocol",
        "authenticated_credential_version",
        "security_revision",
        "service_authenticated",
        "mutation_authority",
        "admission_phase",
        "read_admission",
        "write_admission",
        "worker_policy_digest",
        "proof_recorded",
        "proof_valid_until",
    },
}


class OperatorFailure(RuntimeError):
    """A stable, content-free operator outcome."""

    def __init__(
        self,
        code: str,
        *,
        command: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.code = code if code in _STABLE_ERROR_CLASSES else "HOSTED_OPERATOR_INTERNAL"
        self.command = command if command in COMMANDS else None
        self.request_id = request_id if _is_uuid_v4(request_id) else None
        super().__init__(self.code)

    @property
    def error_class(self) -> str:
        return _STABLE_ERROR_CLASSES[self.code]

    @property
    def exit_status(self) -> int:
        return _ERROR_CLASSES[self.error_class][0]

    def envelope(self) -> dict[str, Any]:
        _status, retryable, message, action = _ERROR_CLASSES[self.error_class]
        return {
            "contract_version": CONTRACT_VERSION,
            "ok": False,
            "command": self.command,
            "request_id": self.request_id,
            "error": {
                "code": self.code,
                "message": message,
                "retryable": retryable,
                "operator_action": action,
            },
        }


Handler = Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]


def _fail(
    code: str = "HOSTED_OPERATOR_CONTRACT_INVALID",
    *,
    command: str | None = None,
    request_id: str | None = None,
) -> None:
    raise OperatorFailure(code, command=command, request_id=request_id)


def _is_uuid_v4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _json_no_duplicates(raw: bytes) -> Any:
    def object_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result

    try:
        text = raw.decode("utf-8")
        decoder = json.JSONDecoder(object_pairs_hook=object_hook)
        value, end = decoder.raw_decode(text)
    except OperatorFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail()
    if text[end:].strip():
        _fail()
    return value


def _valid_string(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _valid_runtime_id(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= 2_147_483_647
    )


def _valid_revision(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= 9_223_372_036_854_775_807
    )


def _validate_root(value: object, *, code: str = "HOSTED_ROOT_INVALID") -> Path:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        _fail(code)
    path = Path(value)
    if not path.is_absolute() or Path(os.path.normpath(value)) != path or "\x00" in value:
        _fail(code)
    return path


def _roots_overlap(*paths: Path) -> bool:
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            try:
                left.relative_to(right)
            except ValueError:
                pass
            else:
                return True
            try:
                right.relative_to(left)
            except ValueError:
                pass
            else:
                return True
    return False


def _validate_common(command: str, request: dict[str, Any]) -> None:
    request_id = request.get("request_id")
    if not _is_uuid_v4(request_id):
        _fail(command=command)
    operation_id = request.get("operation_id")
    if not _valid_string(operation_id, _OPERATION_ID):
        _fail(command=command, request_id=request_id)

    for field in ("cell_id", "vault_id", "source_cell_id", "source_vault_id", "target_cell_id", "target_vault_id"):
        if field in request and not _valid_string(request[field], _OPAQUE_ID):
            _fail(command=command, request_id=request_id)
    for field in ("active_credential_version", "pending_version", "selected_credential_version"):
        if field in request and not _valid_string(request[field], _CREDENTIAL_VERSION):
            _fail("HOSTED_CREDENTIAL_BUNDLE_INVALID", command=command, request_id=request_id)
    for field in ("expected_protocol",):
        if field in request and not _valid_string(request[field], _PROTOCOL):
            code = "HOSTED_PROTOCOL_UNSUPPORTED" if command == "init" else "HOSTED_OPERATOR_CONTRACT_INVALID"
            _fail(code, command=command, request_id=request_id)
    if "expected_release" in request:
        release = request["expected_release"]
        if not isinstance(release, str) or not 1 <= len(release.encode("utf-8")) <= 64:
            code = "HOSTED_RELEASE_MISMATCH" if command == "init" else "HOSTED_OPERATOR_CONTRACT_INVALID"
            _fail(code, command=command, request_id=request_id)
    for field in ("runtime_uid", "runtime_gid"):
        if field in request and not _valid_runtime_id(request[field]):
            code = "HOSTED_RUNTIME_ID_INVALID" if command == "init" else "HOSTED_OPERATOR_CONTRACT_INVALID"
            _fail(code, command=command, request_id=request_id)
    if "expected_revision" in request and not _valid_revision(request["expected_revision"]):
        _fail(command=command, request_id=request_id)


def _validate_command(command: str, request: dict[str, Any]) -> None:
    request_id = request.get("request_id")
    allowed = set(_COMMAND_FIELDS[command])
    if command == "credential" and request.get("action") == "stage":
        allowed.add("pending_version")
    if set(request) != allowed:
        _fail(command=command, request_id=request_id)
    _validate_common(command, request)

    if command == "init":
        roots = tuple(_validate_root(request[name]) for name in ("vault_root", "state_root", "log_root"))
        if _roots_overlap(*roots):
            _fail("HOSTED_ROOT_OVERLAP", command=command, request_id=request_id)
    elif command == "restore-candidate":
        roots = tuple(
            _validate_root(request[name], code="HOSTED_OPERATOR_CONTRACT_INVALID")
            for name in ("target_vault_root", "target_state_root", "target_log_root")
        )
        archive = _validate_root(request["archive_path"], code="HOSTED_OPERATOR_CONTRACT_INVALID")
        if _roots_overlap(*roots):
            _fail(command=command, request_id=request_id)
        if any(_roots_overlap(archive, root) for root in roots):
            _fail(command=command, request_id=request_id)
        if not _valid_string(request["artifact_reference"], _ARTIFACT_REFERENCE):
            _fail(command=command, request_id=request_id)
        if not _valid_string(request["expected_archive_sha256"], _SHA256):
            _fail(command=command, request_id=request_id)
        if request["target_cell_id"] == request["source_cell_id"]:
            _fail("HOSTED_RESTORE_IDENTITY_CONFLICT", command=command, request_id=request_id)
        if request["target_vault_id"] != request["source_vault_id"]:
            _fail("HOSTED_RESTORE_IDENTITY_CONFLICT", command=command, request_id=request_id)
        if request["routing_stopped"] is not True or request["workload_stopped"] is not True:
            _fail("HOSTED_RESTORE_NOT_OFFLINE", command=command, request_id=request_id)
    elif command == "credential":
        _validate_root(request["state_root"], code="HOSTED_OPERATOR_CONTRACT_INVALID")
        action = request.get("action")
        if action not in {"stage", "promote", "abort", "finalize"}:
            _fail(command=command, request_id=request_id)
        if action == "stage":
            if not _valid_string(request.get("pending_version"), _CREDENTIAL_VERSION):
                _fail("HOSTED_CREDENTIAL_BUNDLE_INVALID", command=command, request_id=request_id)
        elif "pending_version" in request:
            _fail(command=command, request_id=request_id)
    elif command == "probe":
        _validate_root(request["state_root"], code="HOSTED_OPERATOR_CONTRACT_INVALID")
        if not _valid_string(request["expected_worker_policy_digest"], _SHA256):
            _fail(command=command, request_id=request_id)
        port = request["port"]
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            _fail("HOSTED_PROBE_TRANSPORT_INVALID", command=command, request_id=request_id)


def decode_request(command: str, raw: bytes) -> dict[str, Any]:
    """Decode one bounded, duplicate-free request and enforce its exact schema."""

    if command not in COMMANDS or len(raw) > REQUEST_MAX_BYTES:
        _fail(command=command)
    value = _json_no_duplicates(raw)
    if not isinstance(value, dict):
        _fail(command=command)
    _validate_command(command, value)
    return value


def _read_bounded(stream: BinaryIO) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = stream.read(min(8192, REQUEST_MAX_BYTES + 1 - total))
            if not isinstance(chunk, bytes):
                _fail()
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > REQUEST_MAX_BYTES:
                _fail()
    except OSError:
        _fail()
    return b"".join(chunks)


def read_live_request(command: str, stdin: BinaryIO) -> dict[str, Any]:
    if command not in LIVE_COMMANDS:
        _fail()
    if hasattr(stdin, "isatty") and stdin.isatty():
        _fail()
    return decode_request(command, _read_bounded(stdin))


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        try:
            current_stat = current.lstat()
        except OSError:
            _fail()
        if stat.S_ISLNK(current_stat.st_mode):
            _fail()


def _descriptor_facts(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_uid,
        stat.S_IMODE(value.st_mode),
        value.st_size,
    )


def _validate_request_descriptor(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != 0
        or stat.S_IMODE(value.st_mode) not in {0o400, 0o440, 0o444}
        or value.st_mode & 0o222
        or value.st_nlink != 1
        or value.st_size > REQUEST_MAX_BYTES
    ):
        _fail()


def read_offline_request(
    command: str,
    path: Path | str,
    *,
    expected_path: Path | str | None = None,
) -> dict[str, Any]:
    """Read one fixed root-owned request generation from a single no-follow FD."""

    if command not in OFFLINE_REQUEST_PATHS:
        _fail()
    supplied = Path(path)
    expected = Path(expected_path) if expected_path is not None else OFFLINE_REQUEST_PATHS[command]
    if (
        not supplied.is_absolute()
        or Path(os.path.normpath(str(supplied))) != supplied
        or supplied != expected
    ):
        _fail()
    _reject_symlink_components(supplied)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(supplied, flags)
    except OSError:
        _fail()
    try:
        before = os.fstat(descriptor)
        _validate_request_descriptor(before)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(8192, REQUEST_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > REQUEST_MAX_BYTES:
                _fail()
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        _validate_request_descriptor(after)
        if _descriptor_facts(before) != _descriptor_facts(after):
            _fail()
        if len(raw) != before.st_size or len(raw) > REQUEST_MAX_BYTES:
            _fail()
    except OSError:
        _fail()
    finally:
        os.close(descriptor)
    return decode_request(command, raw)


def canonical_request_digest(request: Mapping[str, Any]) -> str:
    payload = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _nullable_credential_version(value: object) -> bool:
    return value is None or _valid_string(value, _CREDENTIAL_VERSION)


def _utc_rfc3339_or_null(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)


def _validate_success(
    command: str,
    request: Mapping[str, Any],
    code: object,
    data: object,
) -> dict[str, Any]:
    """Fail closed before any handler-supplied value reaches stdout."""

    if not isinstance(data, dict) or set(data) != _SUCCESS_FIELDS[command]:
        _fail("HOSTED_OPERATOR_INTERNAL", command=command, request_id=request.get("request_id"))
    expected_code: str
    if command == "credential":
        expected_code = {
            "stage": "HOSTED_CREDENTIAL_STAGED",
            "promote": "HOSTED_CREDENTIAL_PROMOTED",
            "abort": "HOSTED_CREDENTIAL_ABORTED",
            "finalize": "HOSTED_CREDENTIAL_FINALIZED",
        }[request["action"]]
    else:
        expected_code = {
            "init": "HOSTED_CELL_INITIALIZED",
            "restore-candidate": "HOSTED_RESTORE_CANDIDATE_READY",
            "probe": "HOSTED_PROBE_READY",
        }[command]
    if code != expected_code:
        _fail("HOSTED_OPERATOR_INTERNAL", command=command, request_id=request.get("request_id"))

    valid = True
    if command == "init":
        capabilities = data["capabilities"]
        valid = (
            data["status"] in {"provisioned", "migrated", "existing"}
            and data["cell_id"] == request["cell_id"]
            and data["vault_id"] == request["vault_id"]
            and data["binding_version"] == BINDING_VERSION
            and data["lifecycle_status"] == "stopped"
            and data["exomem_release"] == request["expected_release"]
            and data["hosted_protocol"] == request["expected_protocol"]
            and data["runtime_uid"] == request["runtime_uid"]
            and data["runtime_gid"] == request["runtime_gid"]
            and data["credential_version"] == request["active_credential_version"]
            and _positive_integer(data["credential_revision"])
            and isinstance(capabilities, list)
            and len(capabilities) == len(set(capabilities))
            and all(_valid_string(item, _OPAQUE_ID) for item in capabilities)
        )
    elif command == "restore-candidate":
        ready = data["derived_state"] == "ready" and data["derived_error_code"] is None
        degraded = (
            data["derived_state"] == "degraded"
            and isinstance(data["derived_error_code"], str)
            and bool(data["derived_error_code"])
        )
        valid = (
            data["status"] in {"ready", "degraded"}
            and ((data["status"] == "ready" and ready) or (data["status"] == "degraded" and degraded))
            and all(
                _valid_string(data[field], _SHA256)
                for field in (
                    "artifact_reference_digest",
                    "archive_sha256",
                    "manifest_sha256",
                )
            )
            and data["archive_sha256"] == request["expected_archive_sha256"]
            and data["source_cell_id"] == request["source_cell_id"]
            and data["source_vault_id"] == request["source_vault_id"]
            and data["target_cell_id"] == request["target_cell_id"]
            and data["target_vault_id"] == request["target_vault_id"]
            and data["binding_version"] == BINDING_VERSION
            and data["exomem_release"] == request["expected_release"]
            and data["hosted_protocol"] == request["expected_protocol"]
            and data["journal_phase"] == "complete"
            and data["credential_version"] == request["active_credential_version"]
            and _positive_integer(data["credential_revision"])
        )
    elif command == "credential":
        action = request["action"]
        valid = (
            data["phase"] in {"stable", "staged", "promoted"}
            and data["revision"] == request["expected_revision"] + 1
            and _valid_string(data["active_version"], _CREDENTIAL_VERSION)
            and _nullable_credential_version(data["pending_version"])
            and _valid_string(data["preferred_version"], _CREDENTIAL_VERSION)
            and (data["rotation_id"] is None or _is_uuid_v4(data["rotation_id"]))
            and _utc_rfc3339_or_null(data["proof_valid_until"])
        )
        if action == "stage":
            valid = valid and (
                data["phase"] == "staged"
                and data["pending_version"] == request["pending_version"]
                and data["preferred_version"] == data["active_version"]
                and data["rotation_id"] is not None
                and data["proof_valid_until"] is None
            )
        elif action == "promote":
            valid = valid and (
                data["phase"] == "promoted"
                and data["pending_version"] is not None
                and data["preferred_version"] == data["pending_version"]
                and data["rotation_id"] is not None
                and data["proof_valid_until"] is not None
            )
        elif action == "abort":
            valid = valid and (
                data["phase"] == "stable"
                and data["pending_version"] is None
                and data["preferred_version"] == data["active_version"]
                and data["rotation_id"] is None
                and data["proof_valid_until"] is None
            )
        else:
            valid = valid and (
                data["phase"] == "stable"
                and data["pending_version"] is None
                and data["preferred_version"] == data["active_version"]
                and data["rotation_id"] is None
                and data["proof_valid_until"] is None
            )
    else:
        valid = (
            data["cell_id"] == request["cell_id"]
            and data["vault_id"] == request["vault_id"]
            and data["exomem_release"] == request["expected_release"]
            and data["hosted_protocol"] == request["expected_protocol"]
            and data["authenticated_credential_version"]
            == request["selected_credential_version"]
            and data["security_revision"] == request["expected_revision"]
            and data["service_authenticated"] is True
            and data["mutation_authority"] is True
            and data["admission_phase"] == "active"
            and data["read_admission"] is True
            and data["write_admission"] is True
            and data["worker_policy_digest"] == request["expected_worker_policy_digest"]
            and isinstance(data["proof_recorded"], bool)
            and _utc_rfc3339_or_null(data["proof_valid_until"])
            and (
                (not data["proof_recorded"] and data["proof_valid_until"] is None)
                or (data["proof_recorded"] and data["proof_valid_until"] is not None)
            )
        )
    if not valid:
        _fail("HOSTED_OPERATOR_INTERNAL", command=command, request_id=request.get("request_id"))
    return data


def _default_handlers() -> dict[str, Handler]:
    from .hosted_restore import execute_restore_candidate
    from .hosted_runtime import execute_hosted_init_v2

    handlers: dict[str, Handler] = {
        "init": execute_hosted_init_v2,
        "restore-candidate": execute_restore_candidate,
    }
    try:
        from .hosted_security import execute_credential_operator, execute_probe_operator
    except ImportError:
        return handlers
    handlers["credential"] = execute_credential_operator
    handlers["probe"] = execute_probe_operator
    return handlers


def _parse_invocation(argv: list[str]) -> tuple[str, str]:
    if len(argv) != 5 or argv[0] not in COMMANDS:
        _fail()
    command = argv[0]
    expected_request = (
        str(OFFLINE_REQUEST_PATHS[command]) if command in OFFLINE_REQUEST_PATHS else "-"
    )
    if argv[1:] != ["--contract-version", "1", "--request-file", expected_request]:
        _fail()
    return command, expected_request


def _write_envelope(stream: TextIO, envelope: Mapping[str, Any]) -> None:
    rendered = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    if len(rendered.encode("utf-8")) > STDOUT_MAX_BYTES:
        rendered = json.dumps(
            OperatorFailure("HOSTED_OPERATOR_INTERNAL").envelope(),
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
    stream.write(rendered)
    stream.flush()


def main(
    argv: list[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    handlers: Mapping[str, Handler] | None = None,
) -> int:
    """Run one exact hosted operator invocation."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    input_stream = stdin if stdin is not None else sys.stdin.buffer
    output_stream = stdout if stdout is not None else sys.stdout
    _ = stderr  # Modeled outcomes deliberately never write standard error.
    command: str | None = None
    request_id: str | None = None
    try:
        command, request_source = _parse_invocation(arguments)
        if command in LIVE_COMMANDS:
            request = read_live_request(command, input_stream)
        else:
            request = read_offline_request(command, request_source)
        request_id = request["request_id"]
        selected = dict(handlers) if handlers is not None else _default_handlers()
        handler = selected.get(command)
        if handler is None:
            _fail("HOSTED_SECURITY_UNAVAILABLE", command=command, request_id=request_id)
        code, data = handler(request)
        data = _validate_success(command, request, code, data)
        envelope = {
            "contract_version": CONTRACT_VERSION,
            "ok": True,
            "command": command,
            "request_id": request_id,
            "code": code,
            "data": data,
        }
        _write_envelope(output_stream, envelope)
        return 0
    except OperatorFailure as error:
        if error.command is None and command is not None:
            error.command = command
        if error.request_id is None and request_id is not None:
            error.request_id = request_id
        if error.command is not None and error.code not in _COMMAND_ERRORS[error.command]:
            error = OperatorFailure(
                "HOSTED_OPERATOR_INTERNAL",
                command=error.command,
                request_id=error.request_id,
            )
        _write_envelope(output_stream, error.envelope())
        return error.exit_status
    except Exception:  # noqa: BLE001 - the public operator result is always redacted
        error = OperatorFailure(
            "HOSTED_OPERATOR_INTERNAL", command=command, request_id=request_id
        )
        _write_envelope(output_stream, error.envelope())
        return error.exit_status


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
