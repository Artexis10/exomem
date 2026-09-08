"""Offline executor for accepted native-owner maintenance reviews."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import plistlib
import re
import shlex
import socket
import stat
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from dotenv import dotenv_values

from . import mutation_lock, state_migration
from .governance import authorization_custody
from .native_owner_maintenance_runner import require_supported_platform
from .native_owner_reviews import OwnerReviewStore

_MANAGED = "EXOMEM_OWNER_MAINTENANCE_MANAGED"
_SERVICE_UNIT = "EXOMEM_OWNER_SERVICE_UNIT"
_OWNER_ID = "EXOMEM_GITHUB_USER_ID"
_CUSTODY_NAMES = frozenset(
    {
        authorization_custody.KEYRING_FILE_ENV,
        authorization_custody.CONTROL_FILE_ENV,
        authorization_custody.MEMBERSHIP_FILE_ENV,
        authorization_custody.REPLICA_ID_ENV,
    }
)
_REQUEST_ID = re.compile(r"owner-review-[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class NativeOwnerMaintenanceUnavailable(RuntimeError):
    """The selected service or stop proof cannot be trusted."""


class NativeOwnerMaintenanceDenied(RuntimeError):
    """No current owner acceptance authorizes this maintenance."""


@dataclass(frozen=True, slots=True)
class ServiceBinding:
    unit_file: Path
    service_id: str
    interpreter: Path
    port: int
    vault_root: Path
    state_root: Path
    binding_path: Path
    environment_digest: str
    unit_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "vault_root": str(self.vault_root),
            "state_root": str(self.state_root),
            "binding_path": str(self.binding_path),
            "port": self.port,
            "environment_digest": self.environment_digest,
            "service_unit_digest": self.unit_digest,
        }


@dataclass(frozen=True, slots=True)
class MaintenancePreflight:
    binding: ServiceBinding
    review_id: str
    owner_id: str
    action: str
    custody_environment: Mapping[str, str]
    source_environment_digest: str
    target_environment_digest: str
    source_unit_digest: str
    state: str
    result: Mapping[str, Any] | None


def _absolute(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise NativeOwnerMaintenanceUnavailable
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise NativeOwnerMaintenanceUnavailable
    return path.resolve(strict=False)


def _absolute_executable(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise NativeOwnerMaintenanceUnavailable
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise NativeOwnerMaintenanceUnavailable
    return Path(os.path.abspath(path))


def _systemd_parts(unit: Path) -> tuple[dict[str, str], Path, list[str], str]:
    try:
        text = unit.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise NativeOwnerMaintenanceUnavailable from None
    environment_lines = re.findall(r"(?m)^EnvironmentFile=([^\n]+)$", text)
    commands = re.findall(r"(?m)^ExecStart=([^\n]+)$", text)
    if len(environment_lines) != 1 or len(commands) != 1:
        raise NativeOwnerMaintenanceUnavailable
    encoded = environment_lines[0].strip()
    if encoded.startswith("-") or any(character in encoded for character in "\"'%$"):
        raise NativeOwnerMaintenanceUnavailable
    decoded = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda match: chr(int(match.group(1), 16)), encoded)
    if "\\" in decoded:
        raise NativeOwnerMaintenanceUnavailable
    binding_path = _absolute(decoded)
    try:
        info = os.lstat(binding_path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
            raise NativeOwnerMaintenanceUnavailable
        raw = binding_path.read_bytes()
        names = []
        for line in raw.decode("utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*=", line)
            if match is None:
                raise NativeOwnerMaintenanceUnavailable
            names.append(line[: match.end()].split("=", 1)[0].strip())
        if len(names) != len(set(names)):
            raise NativeOwnerMaintenanceUnavailable
        parsed = dotenv_values(binding_path, interpolate=False)
        argv = shlex.split(commands[0], posix=True)
    except (OSError, UnicodeError, ValueError):
        raise NativeOwnerMaintenanceUnavailable from None
    if any(value is None for value in parsed.values()):
        raise NativeOwnerMaintenanceUnavailable
    environment = {str(key): str(value) for key, value in parsed.items()}
    service_id = unit.name.removesuffix(".service")
    if not service_id or unit.suffix != ".service":
        raise NativeOwnerMaintenanceUnavailable
    return environment, binding_path, argv, hashlib.sha256(raw).hexdigest()


def _launchd_parts(unit: Path) -> tuple[dict[str, str], Path, list[str], str, str]:
    try:
        raw = unit.read_bytes()
        payload = plistlib.loads(raw)
    except (OSError, plistlib.InvalidFileException, ValueError):
        raise NativeOwnerMaintenanceUnavailable from None
    if not isinstance(payload, dict):
        raise NativeOwnerMaintenanceUnavailable
    environment = payload.get("EnvironmentVariables")
    argv = payload.get("ProgramArguments")
    label = payload.get("Label")
    if (
        not isinstance(environment, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
        )
        or not isinstance(argv, list)
        or not all(isinstance(value, str) for value in argv)
        or not isinstance(label, str)
        or re.fullmatch(r"[A-Za-z0-9_.@-]+", label) is None
    ):
        raise NativeOwnerMaintenanceUnavailable
    return dict(environment), unit, list(argv), hashlib.sha256(raw).hexdigest(), label


def _unit_parts(unit_file: Path) -> tuple[dict[str, str], Path, list[str], str, str]:
    try:
        unit = Path(unit_file).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise NativeOwnerMaintenanceUnavailable from None
    if sys.platform == "darwin" or unit.suffix == ".plist":
        environment, binding, argv, digest, service_id = _launchd_parts(unit)
        return environment, binding, argv, digest, service_id
    if os.name == "nt":
        raise NativeOwnerMaintenanceUnavailable("Windows owner maintenance is unsupported")
    environment, binding, argv, digest = _systemd_parts(unit)
    return environment, binding, argv, digest, unit.name.removesuffix(".service")


def load_service_environment(unit_file: Path) -> dict[str, str]:
    """Parse the managed environment as data, without evaluating its contents."""

    environment, _binding, _argv, _digest, _service_id = _unit_parts(unit_file)
    return dict(environment)


def service_environment_digest(unit_file: Path) -> str:
    """Digest the exact managed environment source bytes."""

    _environment, _binding, _argv, digest, _service_id = _unit_parts(unit_file)
    return digest


def _planned_environment_bytes(binding: ServiceBinding, custody: Mapping[str, str]) -> bytes:
    current = load_service_environment(binding.unit_file)
    if all(current.get(name) == value for name, value in custody.items()):
        return binding.binding_path.read_bytes()
    if any(name in current for name in custody):
        raise NativeOwnerMaintenanceUnavailable
    try:
        if binding.binding_path == binding.unit_file:
            payload = plistlib.loads(binding.unit_file.read_bytes())
            environment = payload.get("EnvironmentVariables")
            if not isinstance(environment, dict):
                raise NativeOwnerMaintenanceUnavailable
            environment.update(custody)
            return plistlib.dumps(payload, sort_keys=False)
        raw = binding.binding_path.read_bytes()
        additions = _render_custody_lines(custody)
        return raw + (b"" if raw.endswith(b"\n") else b"\n") + additions
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise NativeOwnerMaintenanceUnavailable from None


def planned_environment_digest(unit_file: Path, custody_environment: Mapping[str, str]) -> str:
    """Digest the exact environment bytes that maintenance would publish."""

    binding = service_binding(unit_file)
    custody = _custody_environment(custody_environment)
    return hashlib.sha256(_planned_environment_bytes(binding, custody)).hexdigest()


def service_binding(unit_file: Path) -> ServiceBinding:
    unit = Path(unit_file).expanduser().resolve(strict=True)
    environment, binding_path, argv, digest, service_id = _unit_parts(unit)
    if len(argv) < 3 or argv[1:3] != ["-m", "exomem"]:
        raise NativeOwnerMaintenanceUnavailable
    interpreter = _absolute_executable(argv[0])
    ports = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--port"]
    if len(ports) != 1 or not ports[0].isdigit() or not 1 <= int(ports[0]) <= 65535:
        raise NativeOwnerMaintenanceUnavailable
    vault = _absolute(environment.get("EXOMEM_VAULT_PATH"))
    state = _absolute(environment.get("EXOMEM_STATE_ROOT"))
    if _absolute(environment.get(_SERVICE_UNIT)) != unit:
        raise NativeOwnerMaintenanceUnavailable
    owner = environment.get(_OWNER_ID, "")
    if not owner.isdigit() or int(owner) < 1:
        raise NativeOwnerMaintenanceUnavailable
    try:
        unit_digest = hashlib.sha256(unit.read_bytes()).hexdigest()
    except OSError:
        raise NativeOwnerMaintenanceUnavailable from None
    return ServiceBinding(
        unit,
        service_id,
        interpreter,
        int(ports[0]),
        vault,
        state,
        binding_path,
        digest,
        unit_digest,
    )


def _runtime_version() -> str:
    try:
        return importlib.metadata.version("exomem")
    except importlib.metadata.PackageNotFoundError:
        raise NativeOwnerMaintenanceUnavailable from None


def _custody_environment(value: object) -> Mapping[str, str]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _CUSTODY_NAMES
        or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items())
    ):
        raise NativeOwnerMaintenanceDenied
    values = dict(value)
    for name in _CUSTODY_NAMES - {authorization_custody.REPLICA_ID_ENV}:
        _absolute(values[name])
    authorization_custody._bounded_identifier(  # noqa: SLF001
        values[authorization_custody.REPLICA_ID_ENV]
    )
    return MappingProxyType(values)


def _render_custody_lines(values: Mapping[str, str]) -> bytes:
    return "".join(
        f'{name}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"\n'
        for name, value in sorted(values.items())
    ).encode("utf-8")


def _matches_reviewed_environment(
    binding: ServiceBinding,
    expected_source_digest: object,
    expected_target_digest: object,
    custody: Mapping[str, str],
    *,
    allow_target: bool,
) -> bool:
    if (
        not isinstance(expected_source_digest, str)
        or _SHA256.fullmatch(expected_source_digest) is None
        or not isinstance(expected_target_digest, str)
        or _SHA256.fullmatch(expected_target_digest) is None
    ):
        return False
    if binding.environment_digest == expected_source_digest:
        try:
            return (
                hashlib.sha256(_planned_environment_bytes(binding, custody)).hexdigest()
                == expected_target_digest
            )
        except NativeOwnerMaintenanceUnavailable:
            return False
    return allow_target and binding.environment_digest == expected_target_digest


def _matches_reviewed_unit(
    binding: ServiceBinding,
    expected_source_digest: object,
    expected_target_environment_digest: object,
    *,
    allow_target: bool,
) -> bool:
    if (
        not isinstance(expected_source_digest, str)
        or _SHA256.fullmatch(expected_source_digest) is None
    ):
        return False
    if binding.unit_digest == expected_source_digest:
        return True
    return (
        allow_target
        and binding.binding_path == binding.unit_file
        and isinstance(expected_target_environment_digest, str)
        and _SHA256.fullmatch(expected_target_environment_digest) is not None
        and binding.unit_digest == expected_target_environment_digest
    )


def preflight(
    unit_file: Path, request_id: str, *, now: int, resume: bool = False
) -> MaintenancePreflight:
    binding = service_binding(unit_file)
    if _REQUEST_ID.fullmatch(request_id) is None:
        raise NativeOwnerMaintenanceDenied
    environment = load_service_environment(unit_file)
    owner_id = f"github:{environment[_OWNER_ID]}"
    review = OwnerReviewStore(binding.vault_root).get(request_id, owner_id=owner_id, now=now)
    allowed_states = {"accepted", "applying", "completed"} if resume else {"accepted"}
    expired_recovery = (
        resume
        and review.state in {"applying", "completed"}
        and isinstance(review.started_at, int)
        and review.started_at < review.expires_at
    )
    body = review.body
    if not isinstance(body, Mapping):
        raise NativeOwnerMaintenanceDenied
    custody = _custody_environment(body.get("custody_environment"))
    if (
        review.state not in allowed_states
        or (review.expired and not expired_recovery)
        or review.action not in {"migration", "activation"}
        or body.get("service_unit") != str(binding.unit_file)
        or body.get("service_interpreter") != str(binding.interpreter)
        or not _matches_reviewed_unit(
            binding,
            body.get("service_unit_digest"),
            body.get("service_target_environment_digest"),
            allow_target=resume and review.state == "completed",
        )
        or not _matches_reviewed_environment(
            binding,
            body.get("service_environment_digest"),
            body.get("service_target_environment_digest"),
            custody,
            allow_target=resume and review.state == "completed",
        )
        or body.get("runtime_version") != _runtime_version()
    ):
        raise NativeOwnerMaintenanceDenied
    return MaintenancePreflight(
        binding,
        request_id,
        owner_id,
        review.action,
        custody,
        str(body["service_environment_digest"]),
        str(body["service_target_environment_digest"]),
        str(body["service_unit_digest"]),
        review.state,
        getattr(review, "result", None),
    )


def exec_managed_phase(unit_file: Path, argv: Sequence[str]) -> None:
    binding = service_binding(unit_file)
    environment = load_service_environment(unit_file)
    environment[_MANAGED] = "1"
    os.execve(
        str(binding.interpreter),
        [str(binding.interpreter), "-m", "exomem.native_owner_maintenance", *argv],
        environment,
    )


def _verify_interpreter(binding: ServiceBinding) -> None:
    try:
        if binding.interpreter.parent.name == "bin" and binding.interpreter.name == "python":
            selected_prefix = binding.interpreter.parent.parent
        elif (
            binding.interpreter.parent.name == "Scripts"
            and binding.interpreter.name.lower() == "python.exe"
        ):
            selected_prefix = binding.interpreter.parent.parent
        else:
            raise NativeOwnerMaintenanceUnavailable
        if not binding.interpreter.samefile(sys.executable) or not selected_prefix.samefile(
            sys.prefix
        ):
            raise NativeOwnerMaintenanceUnavailable
    except OSError:
        raise NativeOwnerMaintenanceUnavailable from None


def _verify_receipt(path: Path, preflighted: MaintenancePreflight) -> None:
    try:
        retained = mutation_lock.retain_regular_file(path)
        try:
            info = os.fstat(retained.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not 1 <= info.st_size <= 64 * 1024
                or not authorization_custody._file_is_owner_protected(  # noqa: SLF001
                    retained.fd, info
                )
            ):
                raise NativeOwnerMaintenanceUnavailable
            raw = os.read(retained.fd, 64 * 1024 + 1)
            if len(raw) != info.st_size or not mutation_lock._same_file_entry(  # noqa: SLF001
                retained.directory, path.name, retained.fd
            ):
                raise NativeOwnerMaintenanceUnavailable
        finally:
            retained.close()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeError):
        raise NativeOwnerMaintenanceUnavailable from None
    binding = preflighted.binding
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not isinstance(payload, dict)
        or payload.get("service_id") != binding.service_id
        or payload.get("binding_path") != str(binding.binding_path)
        or payload.get("state_root") != str(binding.state_root)
        or payload.get("vault_root") != str(binding.vault_root)
        or payload.get("target_port") != binding.port
        or payload.get("phase") != "stopped"
    ):
        raise NativeOwnerMaintenanceUnavailable
    pids = payload.get("captured_pids")
    if not isinstance(pids, list) or not pids:
        raise NativeOwnerMaintenanceUnavailable
    for pid in pids:
        if not isinstance(pid, int) or pid < 1:
            raise NativeOwnerMaintenanceUnavailable
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        raise NativeOwnerMaintenanceUnavailable
    try:
        with socket.create_connection(("127.0.0.1", binding.port), timeout=0.2):
            raise NativeOwnerMaintenanceUnavailable
    except ConnectionRefusedError:
        pass


def _persist_custody(preflighted: MaintenancePreflight) -> None:
    binding = preflighted.binding
    current_binding = service_binding(binding.unit_file)
    if (
        current_binding.unit_file != binding.unit_file
        or current_binding.service_id != binding.service_id
        or current_binding.interpreter != binding.interpreter
        or current_binding.port != binding.port
        or current_binding.vault_root != binding.vault_root
        or current_binding.state_root != binding.state_root
        or current_binding.binding_path != binding.binding_path
        or not _matches_reviewed_unit(
            current_binding,
            preflighted.source_unit_digest,
            preflighted.target_environment_digest,
            allow_target=True,
        )
        or not _matches_reviewed_environment(
            current_binding,
            preflighted.source_environment_digest,
            preflighted.target_environment_digest,
            preflighted.custody_environment,
            allow_target=True,
        )
    ):
        raise NativeOwnerMaintenanceUnavailable
    current = load_service_environment(binding.unit_file)
    for name, value in preflighted.custody_environment.items():
        if name in current and current[name] != value:
            raise NativeOwnerMaintenanceUnavailable
    if all(current.get(name) == value for name, value in preflighted.custody_environment.items()):
        return
    if current_binding.environment_digest != preflighted.source_environment_digest:
        raise NativeOwnerMaintenanceUnavailable
    encoded = _planned_environment_bytes(current_binding, preflighted.custody_environment)
    if hashlib.sha256(encoded).hexdigest() != preflighted.target_environment_digest:
        raise NativeOwnerMaintenanceUnavailable
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{binding.binding_path.name}.", dir=binding.binding_path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, binding.binding_path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def apply_maintenance(
    unit_file: Path, request_id: str, receipt_path: Path, *, now: int, resume: bool = False
) -> dict[str, Any]:
    checked = preflight(unit_file, request_id, now=now, resume=resume)
    _verify_interpreter(checked.binding)
    _verify_receipt(Path(receipt_path), checked)
    if checked.state == "completed":
        _persist_custody(checked)
        return {
            "status": "completed",
            "review_id": request_id,
            "terminal": checked.result,
        }
    authority = state_migration.assert_offline_migration_authority(
        source=f"native owner maintenance receipt {request_id}"
    )
    os.environ.update(checked.custody_environment)
    from .native_owner_control import NativeOwnerControl

    terminal = NativeOwnerControl(checked.binding.vault_root).apply_maintenance(
        request_id,
        owner_id=checked.owner_id,
        offline_authority=authority,
        now=now,
    )
    _persist_custody(checked)
    return {"status": "completed", "review_id": request_id, "terminal": terminal}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("metadata", "preflight", "apply"))
    parser.add_argument("--unit-file", type=Path, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--resume", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    require_supported_platform()
    forwarded = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get(_MANAGED) != "1":
        exec_managed_phase(args.unit_file, forwarded)
        return 1
    now = int(time.time())
    if args.phase == "metadata":
        binding = service_binding(args.unit_file)
        _verify_interpreter(binding)
        print(json.dumps(binding.as_dict(), sort_keys=True))
    elif args.phase == "preflight":
        checked = preflight(args.unit_file, args.request_id, now=now, resume=args.resume)
        _verify_interpreter(checked.binding)
        print(json.dumps({"status": "accepted", "review_id": args.request_id}))
    else:
        if args.receipt is None:
            raise NativeOwnerMaintenanceUnavailable
        result = apply_maintenance(
            args.unit_file, args.request_id, args.receipt, now=now, resume=args.resume
        )
        print(json.dumps({"status": result["status"], "review_id": args.request_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
