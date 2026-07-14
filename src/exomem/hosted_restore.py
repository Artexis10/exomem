"""Crash-recoverable offline restore for a new hosted target cell."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from . import hosted_portability as portability
from .hosted_operator import OperatorFailure, canonical_request_digest, decode_request
from .hosted_runtime import (
    HOSTED_BINDING_VERSION,
    SUPPORTED_HOSTED_PROTOCOL_VERSIONS,
    HostedBindingV2,
    HostedConfigError,
    _sync_directory,
    _validate_v2_marker,
    _write_v2_marker,
    validate_hosted_binding_v2,
)

_LIFETIME_LOCK = ".exomem-hosted-lifetime.lock"
_JOURNAL_DIRECTORY = "restore-journal"
_JOURNAL_VERSION = 1
_PHASES = (
    "roots_bound",
    "archive_prepared",
    "canonical_published",
    "derived_ready",
    "derived_degraded",
    "complete",
)


class HostedRestoreCrash(RuntimeError):
    """Test seam representing process death at a named durable boundary."""


@dataclass(frozen=True, slots=True)
class HostedRestoreResult:
    status: str
    artifact_reference_digest: str
    archive_sha256: str
    manifest_sha256: str
    source_cell_id: str
    source_vault_id: str
    target_cell_id: str
    target_vault_id: str
    binding_version: int
    exomem_release: str
    hosted_protocol: str
    journal_phase: str
    derived_state: str
    derived_error_code: str | None
    credential_version: str
    credential_revision: int

    def as_operator_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "artifact_reference_digest": self.artifact_reference_digest,
            "archive_sha256": self.archive_sha256,
            "manifest_sha256": self.manifest_sha256,
            "source_cell_id": self.source_cell_id,
            "source_vault_id": self.source_vault_id,
            "target_cell_id": self.target_cell_id,
            "target_vault_id": self.target_vault_id,
            "binding_version": self.binding_version,
            "exomem_release": self.exomem_release,
            "hosted_protocol": self.hosted_protocol,
            "journal_phase": self.journal_phase,
            "derived_state": self.derived_state,
            "derived_error_code": self.derived_error_code,
            "credential_version": self.credential_version,
            "credential_revision": self.credential_revision,
        }


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _event(hook: Callable[[str], None] | None, name: str) -> None:
    if hook is not None:
        hook(name)


def _ensure_private_directory(path: Path, binding: HostedBindingV2) -> None:
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
        _sync_directory(path.parent)
    try:
        value = path.lstat()
    except OSError as exc:
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT")
    try:
        if os.geteuid() == 0:
            os.chown(
                path,
                binding.runtime_uid,
                binding.runtime_gid,
                follow_symlinks=False,
            )
        path.chmod(0o700, follow_symlinks=False)
    except OSError as exc:
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc
    value = path.lstat()
    if value.st_uid != binding.runtime_uid or value.st_gid != binding.runtime_gid:
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT")


@contextmanager
def acquire_hosted_lifetime_lock(
    state_root: Path | str,
    *,
    binding: HostedBindingV2 | None = None,
) -> Iterator[None]:
    """Acquire the nonblocking lock shared by target restore and server lifetime."""

    root = Path(state_root)
    if not root.is_absolute():
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT")
    if not root.exists():
        root.mkdir(mode=0o700, parents=True)
        _sync_directory(root.parent)
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise OperatorFailure("HOSTED_RESTORE_BUSY") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise OperatorFailure("HOSTED_RESTORE_BUSY")
    lock = root / _LIFETIME_LOCK
    base_flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        try:
            descriptor = os.open(lock, base_flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(lock, base_flags)
        lock_stat = os.fstat(descriptor)
        expected_uid = binding.runtime_uid if binding is not None else os.getuid()
        expected_gid = binding.runtime_gid if binding is not None else os.getgid()
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_nlink != 1
            or (
                not created
                and (
                    lock_stat.st_uid != expected_uid
                    or lock_stat.st_gid != expected_gid
                    or stat.S_IMODE(lock_stat.st_mode) != 0o600
                )
            )
        ):
            raise OSError("unsafe lifetime lock")
        if created:
            os.fchmod(descriptor, 0o600)
            if binding is not None and os.geteuid() == 0:
                os.fchown(descriptor, binding.runtime_uid, binding.runtime_gid)
            os.fsync(descriptor)
            _sync_directory(root)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise OperatorFailure("HOSTED_RESTORE_BUSY") from exc
    except OSError as exc:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise OperatorFailure("HOSTED_RESTORE_BUSY") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _target_preflight(binding: HostedBindingV2) -> None:
    for kind, root in binding.roots():
        if not os.path.lexists(root):
            continue
        try:
            root_stat = root.lstat()
        except OSError as exc:
            raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT")
        marker = root / ".exomem-hosted-cell.json"
        if os.path.lexists(marker):
            try:
                _validate_v2_marker(root, kind, binding)
            except HostedConfigError as exc:
                raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc
            continue
        allowed = {_LIFETIME_LOCK} if kind == "state" else set()
        if {entry.name for entry in root.iterdir()} - allowed:
            raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT")


def _ensure_runtime_root(binding: HostedBindingV2, kind: str, root: Path) -> None:
    marker = root / ".exomem-hosted-cell.json"
    if os.path.lexists(marker):
        try:
            _validate_v2_marker(root, kind, binding)
        except HostedConfigError as exc:
            raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc
        return
    allowed = {_LIFETIME_LOCK} if kind == "state" else set()
    if {entry.name for entry in root.iterdir()} - allowed:
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT")
    _ensure_private_directory(root, binding)
    try:
        _write_v2_marker(root, kind, binding)
        _validate_v2_marker(root, kind, binding)
    except HostedConfigError as exc:
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc


def _hash_archive(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise OperatorFailure("HOSTED_ARCHIVE_INVALID")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            before = os.fstat(descriptor)
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OperatorFailure:
        raise
    except OSError as exc:
        raise OperatorFailure("HOSTED_ARCHIVE_INVALID") from exc
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise OperatorFailure("HOSTED_ARCHIVE_INTEGRITY_FAILURE")
    return digest.hexdigest()


def _verify_archive(request: Mapping[str, Any]) -> portability.VerifiedArchive:
    archive = Path(request["archive_path"])
    if _hash_archive(archive) != request["expected_archive_sha256"]:
        raise OperatorFailure("HOSTED_ARTIFACT_DIGEST_MISMATCH")
    try:
        verified = portability.verify_export_archive(
            archive,
            expected_cell_id=request["source_cell_id"],
            expected_vault_id=request["source_vault_id"],
        )
    except portability.PortabilityError as exc:
        if exc.code in {
            "DISALLOWED_ARTIFACT",
            "ARTIFACT_CLASS_MISMATCH",
        }:
            code = "HOSTED_ARCHIVE_RUNTIME_STATE"
        elif exc.code in {
            "UNSAFE_ARCHIVE_PATH",
            "UNSAFE_ARCHIVE_ENTRY",
            "DUPLICATE_ARCHIVE_PATH",
            "CASE_COLLISION",
            "PREFIX_PATH_COLLISION",
        }:
            code = "HOSTED_ARCHIVE_UNSAFE_ENTRY"
        elif exc.code in {"CELL_BINDING_MISMATCH", "VAULT_BINDING_MISMATCH"}:
            code = "HOSTED_ARCHIVE_INTEGRITY_FAILURE"
        else:
            code = "HOSTED_ARCHIVE_INVALID"
        raise OperatorFailure(code) from exc
    if verified.archive_sha256 != request["expected_archive_sha256"]:
        raise OperatorFailure("HOSTED_ARTIFACT_DIGEST_MISMATCH")
    return verified


def _journal_path(binding: HostedBindingV2, operation_id: str) -> Path:
    operation_key = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return binding.state_root / _JOURNAL_DIRECTORY / f"{operation_key}.json"


def _journal_identity(
    request: Mapping[str, Any],
    binding: HostedBindingV2,
    verified: portability.VerifiedArchive,
) -> dict[str, Any]:
    return {
        "journal_version": _JOURNAL_VERSION,
        "operation_id": request["operation_id"],
        "request_digest": canonical_request_digest(request),
        "artifact_reference": request["artifact_reference"],
        "artifact_reference_digest": hashlib.sha256(
            request["artifact_reference"].encode("utf-8")
        ).hexdigest(),
        "archive_sha256": verified.archive_sha256,
        "manifest_sha256": verified.manifest["overall_digest"]["value"],
        "source_cell_id": request["source_cell_id"],
        "source_vault_id": request["source_vault_id"],
        "target_cell_id": request["target_cell_id"],
        "target_vault_id": request["target_vault_id"],
        "binding_digest": binding.binding_digest,
        "credential_version": request["active_credential_version"],
    }


def _read_journal(
    path: Path, identity: Mapping[str, Any], binding: HostedBindingV2
) -> dict[str, Any] | None:
    if not os.path.lexists(path):
        return None
    try:
        value = path.lstat()
        if (
            stat.S_ISLNK(value.st_mode)
            or not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or value.st_uid != binding.runtime_uid
            or value.st_gid != binding.runtime_gid
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_size > 65_536
        ):
            raise ValueError("unsafe journal")

        def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate journal key")
                result[key] = item
            return result

        record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OperatorFailure("HOSTED_RESTORE_JOURNAL_CONFLICT") from exc
    if not isinstance(record, dict) or any(record.get(key) != item for key, item in identity.items()):
        raise OperatorFailure("HOSTED_RESTORE_JOURNAL_CONFLICT")
    if record.get("phase") not in _PHASES:
        raise OperatorFailure("HOSTED_RESTORE_JOURNAL_CONFLICT")
    return record


def _write_journal(
    path: Path,
    record: Mapping[str, Any],
    binding: HostedBindingV2,
) -> None:
    directory_existed = path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _ensure_private_directory(path.parent, binding)
    if not directory_existed:
        _sync_directory(path.parent.parent)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        payload = _canonical_bytes(record)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
        if os.geteuid() == 0:
            os.fchown(descriptor, binding.runtime_uid, binding.runtime_gid)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    _sync_directory(path.parent)


def _advance(
    path: Path,
    record: dict[str, Any],
    phase: str,
    binding: HostedBindingV2,
    hook: Callable[[str], None] | None,
    **updates: Any,
) -> dict[str, Any]:
    next_record = {**record, **updates, "phase": phase}
    _write_journal(path, next_record, binding)
    _event(hook, f"journal:{phase}")
    return next_record


def _staging_path(binding: HostedBindingV2, operation_id: str) -> Path:
    suffix = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:16]
    return binding.vault_root.parent / f".{binding.vault_root.name}.restore-{suffix}"


def _verify_published(
    binding: HostedBindingV2, manifest: Mapping[str, Any]
) -> None:
    try:
        _validate_v2_marker(binding.vault_root, "vault", binding)
        portability._verify_staged_files(
            binding.vault_root, manifest, allow_derived_extras=True
        )
    except (HostedConfigError, portability.PortabilityError) as exc:
        raise OperatorFailure("HOSTED_RESTORE_CANONICAL_INTEGRITY") from exc


def _prepare_archive(
    request: Mapping[str, Any],
    binding: HostedBindingV2,
    verified: portability.VerifiedArchive,
    staging: Path,
) -> portability.PreparedRestore:
    if os.path.lexists(staging):
        try:
            staging_stat = staging.lstat()
            if stat.S_ISLNK(staging_stat.st_mode) or not stat.S_ISDIR(staging_stat.st_mode):
                raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT")
            marker = staging / ".exomem-hosted-cell.json"
            if os.path.lexists(marker):
                _validate_v2_marker(staging, "vault", binding)
                portability._verify_staged_files(
                    staging, verified.manifest, allow_derived_extras=True
                )
            else:
                # A process may die after the verified extraction rename but
                # before its target-binding marker. Exact manifest bytes prove
                # this deterministic operation-owned staging generation.
                portability._verify_staged_files(staging, verified.manifest)
                _write_v2_marker(staging, "vault", binding)
                _validate_v2_marker(staging, "vault", binding)
            return portability.PreparedRestore(
                staging_root=staging,
                source_archive=verified.archive_path,
                archive_sha256=verified.archive_sha256,
                manifest=verified.manifest,
                context=portability.PortabilityContext(
                    cell_id=binding.cell_id,
                    vault_id=binding.vault_id,
                    operation_id=request["operation_id"],
                    created_at=verified.manifest["created_at"],
                    operator_authorized=True,
                    lifecycle_state="restore-staging",
                    routing_stopped=True,
                    active_mutations=0,
                    background_writers_stopped=True,
                    reads_allowed=False,
                ),
            )
        except OperatorFailure:
            raise
        except (HostedConfigError, portability.PortabilityError, OSError) as exc:
            raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc
    try:
        prepared = portability.prepare_restore(
            verified.archive_path,
            staging,
            context=portability.PortabilityContext(
                cell_id=binding.cell_id,
                vault_id=binding.vault_id,
                operation_id=request["operation_id"],
                created_at=verified.manifest["created_at"],
                operator_authorized=True,
                lifecycle_state="restore-staging",
                routing_stopped=True,
                active_mutations=0,
                background_writers_stopped=True,
                reads_allowed=False,
            ),
            expected_source_cell_id=request["source_cell_id"],
            expected_source_vault_id=request["source_vault_id"],
        )
        _write_v2_marker(staging, "vault", binding)
        _validate_v2_marker(staging, "vault", binding)
        portability._verify_staged_files(staging, prepared.manifest, allow_derived_extras=True)
    except HostedConfigError as exc:
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc
    except portability.PortabilityError as exc:
        raise OperatorFailure("HOSTED_ARCHIVE_INTEGRITY_FAILURE") from exc
    return prepared


def _publish_staging(
    binding: HostedBindingV2,
    staging: Path,
    manifest: Mapping[str, Any],
) -> None:
    target = binding.vault_root
    if staging.parent != target.parent:
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT")
    try:
        parent_stat = target.parent.stat()
        staging_stat = staging.lstat()
    except OSError as exc:
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc
    if staging_stat.st_dev != parent_stat.st_dev:
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT")
    if os.path.lexists(target):
        try:
            target_stat = target.lstat()
            if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISDIR(target_stat.st_mode):
                raise OSError("unsafe target")
            if any(target.iterdir()):
                raise OSError("nonempty target")
            target.rmdir()
        except OSError as exc:
            raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc
    try:
        os.replace(staging, target)
        _sync_directory(target.parent)
    except OSError as exc:
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc
    _verify_published(binding, manifest)


def _bootstrap_security(
    binding: HostedBindingV2,
    request: Mapping[str, Any],
    bootstrap_security: Callable[..., int] | None,
) -> int:
    if bootstrap_security is None:
        try:
            from .hosted_security import bootstrap_hosted_security
        except ImportError as exc:
            raise OperatorFailure("HOSTED_SECURITY_UNAVAILABLE") from exc
        bootstrap_security = bootstrap_hosted_security
    try:
        result = bootstrap_security(
            binding=binding,
            active_credential_version=request["active_credential_version"],
            operation_id=request["operation_id"],
            request_digest=canonical_request_digest(request),
        )
    except OperatorFailure:
        raise
    except Exception as exc:
        raise OperatorFailure("HOSTED_SECURITY_UNAVAILABLE") from exc
    revision = result if isinstance(result, int) else getattr(result, "revision", None)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise OperatorFailure("HOSTED_SECURITY_UNAVAILABLE")
    return revision


def _result_from_record(record: Mapping[str, Any]) -> HostedRestoreResult:
    derived_state = record.get("derived_state")
    derived_error = record.get("derived_error_code")
    if derived_state not in {"ready", "degraded"}:
        raise OperatorFailure("HOSTED_RESTORE_JOURNAL_CONFLICT")
    revision = record.get("credential_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise OperatorFailure("HOSTED_RESTORE_JOURNAL_CONFLICT")
    return HostedRestoreResult(
        status=derived_state,
        artifact_reference_digest=record["artifact_reference_digest"],
        archive_sha256=record["archive_sha256"],
        manifest_sha256=record["manifest_sha256"],
        source_cell_id=record["source_cell_id"],
        source_vault_id=record["source_vault_id"],
        target_cell_id=record["target_cell_id"],
        target_vault_id=record["target_vault_id"],
        binding_version=HOSTED_BINDING_VERSION,
        exomem_release=__version__,
        hosted_protocol=record["hosted_protocol"],
        journal_phase="complete",
        derived_state=derived_state,
        derived_error_code=derived_error,
        credential_version=record["credential_version"],
        credential_revision=revision,
    )


def restore_candidate(
    raw_request: Mapping[str, Any],
    *,
    bootstrap_security: Callable[..., int] | None = None,
    rebuild_derived: Callable[[Path], None] | None = None,
    crash_hook: Callable[[str], None] | None = None,
) -> HostedRestoreResult:
    """Restore one pinned archive into a new, exclusively locked target cell."""

    encoded = _canonical_bytes(raw_request)
    request = decode_request("restore-candidate", encoded)
    if request["expected_release"] != __version__:
        raise OperatorFailure("HOSTED_OPERATOR_CONTRACT_INVALID")
    if request["expected_protocol"] not in SUPPORTED_HOSTED_PROTOCOL_VERSIONS:
        raise OperatorFailure("HOSTED_OPERATOR_CONTRACT_INVALID")
    verified = _verify_archive(request)
    try:
        binding = HostedBindingV2(
            cell_id=request["target_cell_id"],
            vault_id=request["target_vault_id"],
            vault_root=Path(request["target_vault_root"]),
            state_root=Path(request["target_state_root"]),
            log_root=Path(request["target_log_root"]),
            runtime_uid=request["runtime_uid"],
            runtime_gid=request["runtime_gid"],
        )
    except HostedConfigError as exc:
        raise OperatorFailure("HOSTED_RESTORE_TARGET_CONFLICT") from exc
    _target_preflight(binding)
    identity = _journal_identity(request, binding, verified)
    journal_path = _journal_path(binding, request["operation_id"])
    staging = _staging_path(binding, request["operation_id"])

    with acquire_hosted_lifetime_lock(binding.state_root, binding=binding):
        _target_preflight(binding)
        _ensure_private_directory(binding.state_root, binding)
        _ensure_runtime_root(binding, "state", binding.state_root)
        _event(crash_hook, "state_bound")
        _ensure_private_directory(binding.log_root, binding)
        _ensure_runtime_root(binding, "log", binding.log_root)
        _event(crash_hook, "log_bound")
        _event(crash_hook, "roots_bound")

        record = _read_journal(journal_path, identity, binding)
        if record is None:
            record = _advance(
                journal_path,
                {**identity, "hosted_protocol": request["expected_protocol"]},
                "roots_bound",
                binding,
                crash_hook,
            )
        if record["phase"] == "complete":
            validate_hosted_binding_v2(binding, require_scaffold=True)
            _verify_published(binding, verified.manifest)
            return _result_from_record(record)

        if record["phase"] == "roots_bound":
            prepared = _prepare_archive(request, binding, verified, staging)
            _event(crash_hook, "archive_prepared")
            record = _advance(
                journal_path,
                record,
                "archive_prepared",
                binding,
                crash_hook,
            )
        else:
            prepared = portability.PreparedRestore(
                staging_root=staging,
                source_archive=verified.archive_path,
                archive_sha256=verified.archive_sha256,
                manifest=verified.manifest,
                context=portability.PortabilityContext(
                    cell_id=binding.cell_id,
                    vault_id=binding.vault_id,
                    operation_id=request["operation_id"],
                    created_at=verified.manifest["created_at"],
                    operator_authorized=True,
                    lifecycle_state="restore-staging",
                    routing_stopped=True,
                    active_mutations=0,
                    background_writers_stopped=True,
                    reads_allowed=False,
                ),
            )

        if record["phase"] == "archive_prepared":
            if os.path.lexists(binding.vault_root):
                # Crash after rename and before the journal advance: exact
                # binding plus every manifest path/byte is the commit proof.
                _verify_published(binding, verified.manifest)
                if os.path.lexists(staging):
                    raise OperatorFailure("HOSTED_RESTORE_CANONICAL_INTEGRITY")
            else:
                _publish_staging(binding, staging, verified.manifest)
                _event(crash_hook, "canonical_renamed")
            record = _advance(
                journal_path,
                record,
                "canonical_published",
                binding,
                crash_hook,
            )

        if record["phase"] == "canonical_published":
            derived_state = "ready"
            derived_error: str | None = None
            if rebuild_derived is not None:
                try:
                    rebuild_derived(binding.vault_root)
                except Exception:  # noqa: BLE001 - optional derived work may degrade
                    derived_state = "degraded"
                    derived_error = "DERIVED_REBUILD_FAILED"
            try:
                _verify_published(binding, verified.manifest)
            except OperatorFailure as exc:
                try:
                    portability._repair_canonical_from_archive(prepared, binding.vault_root)
                except Exception as repair_error:
                    raise OperatorFailure("HOSTED_RESTORE_CANONICAL_INTEGRITY") from repair_error
                raise OperatorFailure("HOSTED_RESTORE_CANONICAL_INTEGRITY") from exc
            _event(crash_hook, "derived_rebuilt")
            record = _advance(
                journal_path,
                record,
                "derived_ready" if derived_state == "ready" else "derived_degraded",
                binding,
                crash_hook,
                derived_state=derived_state,
                derived_error_code=derived_error,
            )

        if record["phase"] in {"derived_ready", "derived_degraded"}:
            revision = _bootstrap_security(binding, request, bootstrap_security)
            _verify_published(binding, verified.manifest)
            record = _advance(
                journal_path,
                record,
                "complete",
                binding,
                crash_hook,
                credential_revision=revision,
            )
            _event(crash_hook, "proof_written")

        validate_hosted_binding_v2(binding, require_scaffold=True)
        _verify_published(binding, verified.manifest)
        return _result_from_record(record)


def execute_restore_candidate(
    request: dict[str, Any],
    *,
    bootstrap_security: Callable[..., int] | None = None,
    rebuild_derived: Callable[[Path], None] | None = None,
    crash_hook: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    result = restore_candidate(
        request,
        bootstrap_security=bootstrap_security,
        rebuild_derived=rebuild_derived,
        crash_hook=crash_hook,
    )
    return "HOSTED_RESTORE_CANDIDATE_READY", result.as_operator_data()
