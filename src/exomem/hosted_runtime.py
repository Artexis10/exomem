"""Hosted tenant-cell configuration, lifecycle, and provisioning primitives.

Hosted mode is deliberately one process/container to one immutable vault.  This
module contains no public HTTP surface and no control-plane, billing, backup, or
KMS behavior; it exposes the private runtime state that later integration layers
can bind to authenticated operator routes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from . import init as init_module
from .kbdir import kb_dirname

HOSTED_PROTOCOL_VERSION = "1"
SUPPORTED_HOSTED_PROTOCOL_VERSIONS = frozenset({HOSTED_PROTOCOL_VERSION})
HOSTED_MODE_ENV = "EXOMEM_HOSTED_CELL"
_BINDING_VERSION = 1
_BINDING_FILENAME = ".exomem-hosted-cell.json"
_LIFECYCLE_STATE_VERSION = 1
_LIFECYCLE_STATE_FILENAME = "hosted-lifecycle-state.json"
_CELL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PROTOCOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_CREDENTIAL_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})
_KNOWN_FEATURES = frozenset({"diarization", "embeddings", "file-watcher", "media", "vision"})
_HOSTED_CLEARED_ENV = (
    "EXOMEM_BASE_URL",
    "EXOMEM_CF_ACCESS_AUD",
    "EXOMEM_CF_ACCESS_TEAM_DOMAIN",
    "EXOMEM_GITHUB_USERNAME",
    "EXOMEM_LARGE_UPLOAD_BASE_URL",
    "EXOMEM_REST_API_KEY",
    "EXOMEM_UPLOAD_TOKEN",
    "EXOMEM_WRITER_LEASE_PREFERRED",
    "EXOMEM_WRITER_LEASE_REPLICA_ID",
    "EXOMEM_WRITER_LEASE_TIMEOUT",
    "EXOMEM_WRITER_LEASE_TOKEN",
    "EXOMEM_WRITER_LEASE_TTL",
    "EXOMEM_WRITER_LEASE_URL",
    "EXOMEM_WRITER_LEASE_VAULT_ID",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
)

_DEFAULT_STORAGE_LIMIT_BYTES = 5 * 1024 * 1024 * 1024
_DEFAULT_UPLOAD_LIMIT_BYTES = 100 * 1024 * 1024
_DEFAULT_WORKER_LIMIT = 0


class HostedConfigError(RuntimeError):
    """Fail-closed hosted configuration/provisioning error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class HostedLifecycleError(RuntimeError):
    """Stable lifecycle-admission error; never contains vault data or secrets."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class HostedResourceLimits:
    """Provider-neutral resource bounds supplied by trusted startup config."""

    storage_bytes: int = _DEFAULT_STORAGE_LIMIT_BYTES
    upload_bytes: int = _DEFAULT_UPLOAD_LIMIT_BYTES
    worker_count: int = _DEFAULT_WORKER_LIMIT


@dataclass(frozen=True)
class HostedProcessSettings:
    """Content-private process settings applied for one hosted cell."""

    disabled_background_workers: tuple[str, ...]
    query_logging_disabled: bool = True
    usage_logging_disabled: bool = True


@dataclass(frozen=True)
class HostedCellConfig:
    """Immutable trusted binding for exactly one hosted cell and vault."""

    cell_id: str
    vault_root: Path
    state_root: Path
    log_root: Path
    service_credential: str = field(repr=False)
    protocol_version: str = HOSTED_PROTOCOL_VERSION
    feature_grants: tuple[str, ...] = ()
    resource_limits: HostedResourceLimits = field(default_factory=HostedResourceLimits)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        require_provisioned: bool = False,
    ) -> HostedCellConfig:
        """Parse explicit hosted settings without consulting dotenv or legacy files."""
        values = os.environ if env is None else env
        required = {
            "EXOMEM_HOSTED_CELL_ID": values.get("EXOMEM_HOSTED_CELL_ID", ""),
            "EXOMEM_VAULT_PATH": values.get("EXOMEM_VAULT_PATH", ""),
            "EXOMEM_HOSTED_STATE_ROOT": values.get("EXOMEM_HOSTED_STATE_ROOT", ""),
            "EXOMEM_LOG_DIR": values.get("EXOMEM_LOG_DIR", ""),
            "EXOMEM_HOSTED_SERVICE_CREDENTIAL": values.get("EXOMEM_HOSTED_SERVICE_CREDENTIAL", ""),
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise HostedConfigError(
                "HOSTED_CONFIG_MISSING",
                "required hosted configuration is incomplete: " + ", ".join(sorted(missing)),
            )

        cell_id = str(required["EXOMEM_HOSTED_CELL_ID"]).strip()
        if not _CELL_ID.fullmatch(cell_id):
            raise HostedConfigError(
                "HOSTED_CELL_ID_INVALID",
                "cell identity must be opaque ASCII letters, digits, underscores, or hyphens",
            )
        protocol = str(
            values.get("EXOMEM_HOSTED_PROTOCOL_VERSION", HOSTED_PROTOCOL_VERSION)
        ).strip()
        if not protocol or not _PROTOCOL.fullmatch(protocol):
            raise HostedConfigError("HOSTED_PROTOCOL_INVALID", "hosted protocol version is invalid")
        if protocol not in SUPPORTED_HOSTED_PROTOCOL_VERSIONS:
            raise HostedConfigError(
                "HOSTED_PROTOCOL_UNSUPPORTED",
                "hosted protocol version is not supported by this release",
            )

        vault_root = _validated_root(required["EXOMEM_VAULT_PATH"], "vault")
        state_root = _validated_root(required["EXOMEM_HOSTED_STATE_ROOT"], "state")
        log_root = _validated_root(required["EXOMEM_LOG_DIR"], "log")
        _validate_disjoint_roots(vault_root, state_root, log_root)

        grants = parse_feature_grants(values.get("EXOMEM_HOSTED_FEATURE_GRANTS", ""))
        limits = HostedResourceLimits(
            storage_bytes=_parse_limit(
                values,
                "EXOMEM_HOSTED_STORAGE_LIMIT_BYTES",
                _DEFAULT_STORAGE_LIMIT_BYTES,
                allow_zero=False,
            ),
            upload_bytes=_parse_limit(
                values,
                "EXOMEM_HOSTED_UPLOAD_LIMIT_BYTES",
                _DEFAULT_UPLOAD_LIMIT_BYTES,
                allow_zero=False,
            ),
            worker_count=_parse_limit(
                values,
                "EXOMEM_HOSTED_WORKER_LIMIT",
                _DEFAULT_WORKER_LIMIT,
                allow_zero=True,
            ),
        )
        if limits.upload_bytes > limits.storage_bytes:
            raise HostedConfigError(
                "HOSTED_LIMIT_INVALID",
                "hosted upload limit cannot exceed the hosted storage limit",
            )

        service_credential = str(required["EXOMEM_HOSTED_SERVICE_CREDENTIAL"]).strip()
        if len(service_credential.encode("utf-8")) < 32:
            raise HostedConfigError(
                "HOSTED_CREDENTIAL_WEAK",
                "hosted service credential must contain at least 32 bytes",
            )

        config = cls(
            cell_id=cell_id,
            vault_root=vault_root,
            state_root=state_root,
            log_root=log_root,
            service_credential=service_credential,
            protocol_version=protocol,
            feature_grants=grants,
            resource_limits=limits,
        )
        if require_provisioned:
            config.validate_provisioned()
        return config

    @property
    def binding_digest(self) -> str:
        payload = "\0".join(
            (
                self.cell_id,
                str(self.vault_root),
                str(self.state_root),
                str(self.log_root),
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def has_feature(self, feature: str) -> bool:
        return _normalize_feature(feature) in self.feature_grants

    def matches_service_credential(self, presented: str | None) -> bool:
        if not presented:
            return False
        expected = hashlib.sha256(self.service_credential.encode("utf-8")).digest()
        candidate = hashlib.sha256(str(presented).encode("utf-8")).digest()
        return hmac.compare_digest(expected, candidate)

    def apply_process_environment(
        self, env: MutableMapping[str, str] | None = None
    ) -> HostedProcessSettings:
        """Apply explicit process-wide gates needed by existing runtime modules.

        This is the single intentional environment-mutation boundary for hosted
        startup.  Callers may pass a private mapping for planning/tests.
        """
        target = os.environ if env is None else env
        target["EXOMEM_VAULT_PATH"] = str(self.vault_root)
        target["EXOMEM_HOSTED_STATE_ROOT"] = str(self.state_root)
        target["EXOMEM_WRITER_LEASE_STATE_DIR"] = str(self.state_root)
        target["EXOMEM_LOG_DIR"] = str(self.log_root)
        target["EXOMEM_UPLOAD_MAX_BYTES"] = str(self.resource_limits.upload_bytes)
        target["EXOMEM_DISABLE_QUERY_LOG"] = "1"
        target["EXOMEM_DISABLE_USAGE_BOOST"] = "1"
        target["EXOMEM_DISABLE_RELEVANCE_CHECK"] = "1"
        for inherited_setting in _HOSTED_CLEARED_ENV:
            target.pop(inherited_setting, None)

        workers_enabled = self.resource_limits.worker_count > 0
        _apply_disable_gate(
            target,
            workers_enabled and "embeddings" in self.feature_grants,
            "EXOMEM_DISABLE_EMBEDDINGS",
        )
        _apply_disable_gate(
            target,
            workers_enabled and "media" in self.feature_grants,
            "EXOMEM_DISABLE_MEDIA_EXTRACTION",
        )
        _apply_disable_gate(
            target,
            workers_enabled and "vision" in self.feature_grants,
            "EXOMEM_DISABLE_CLIP",
        )
        _apply_disable_gate(
            target,
            workers_enabled and "file-watcher" in self.feature_grants,
            "EXOMEM_DISABLE_FILE_WATCHER",
        )
        _apply_truthy_gate(
            target,
            workers_enabled and "diarization" in self.feature_grants,
            "EXOMEM_DIARIZE",
        )
        _apply_truthy_gate(
            target,
            workers_enabled and "vision" in self.feature_grants,
            "EXOMEM_VISION_CAPTION",
        )

        disabled_workers = tuple(
            name
            for name in ("file-watcher", "media")
            if not workers_enabled or name not in self.feature_grants
        )
        return HostedProcessSettings(disabled_background_workers=disabled_workers)

    def validate_provisioned(self) -> None:
        for kind, root in self.roots():
            if not root.exists():
                raise HostedConfigError("HOSTED_ROOT_MISSING", f"hosted {kind} root does not exist")
            if not root.is_dir():
                raise HostedConfigError(
                    "HOSTED_ROOT_NOT_DIRECTORY", f"hosted {kind} root is not a directory"
                )
            _validate_private_permissions(root, kind)
            _validate_binding_marker(root, kind, self)
        _reject_tree_symlinks(self.vault_root)
        if not _valid_vault_scaffold(self.vault_root):
            raise HostedConfigError(
                "HOSTED_SCAFFOLD_INVALID",
                "hosted vault is missing required generic scaffold or index state",
            )

    def roots(self) -> tuple[tuple[str, Path], ...]:
        return (
            ("vault", self.vault_root),
            ("state", self.state_root),
            ("log", self.log_root),
        )


@dataclass(frozen=True)
class HostedLiveness:
    live: bool
    cell_id: str
    protocol_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "live": self.live,
            "cell_id": self.cell_id,
            "protocol_version": self.protocol_version,
        }


@dataclass(frozen=True)
class HostedReadiness:
    ready: bool
    phase: str
    reason_code: str
    read_admitted: bool
    write_admitted: bool
    degraded: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "phase": self.phase,
            "reason_code": self.reason_code,
            "read_admitted": self.read_admitted,
            "write_admitted": self.write_admitted,
            "degraded": [
                {"check": check, "reason_code": reason} for check, reason in self.degraded
            ],
        }


@dataclass(frozen=True)
class HostedLifecycleSnapshot:
    phase: str
    active_reads: int
    active_mutations: int
    active_transfers: int
    reason_code: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "active_reads": self.active_reads,
            "active_mutations": self.active_mutations,
            "active_transfers": self.active_transfers,
            "reason_code": self.reason_code,
        }


class HostedCellLifecycle:
    """Thread-safe admission and lifecycle state for one immutable cell."""

    def __init__(self, config: HostedCellConfig) -> None:
        self.config = config
        self._condition = threading.Condition(threading.RLock())
        self._phase = self._load_durable_phase()
        self._vault_ready = False
        self._mutation_authority_ready = False
        self._mutation_reason = "HOSTED_MUTATION_AUTHORITY_UNAVAILABLE"
        self._service_auth_ready = False
        self._active_reads = 0
        self._active_mutations = 0
        self._active_transfers = 0
        self._worker_status: dict[str, tuple[bool, str]] = {}
        self._background_workers: list[tuple[Callable[[], Any], Callable[[], Any] | None]] = []

    def _load_durable_phase(self) -> str:
        path = self.config.state_root / _LIFECYCLE_STATE_FILENAME
        if not os.path.lexists(path):
            return "starting"
        try:
            stored = path.lstat()
            if path.is_symlink() or not path.is_file() or not stored.st_size:
                raise ValueError("invalid lifecycle state file")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HostedLifecycleError(
                "HOSTED_LIFECYCLE_STATE_INVALID",
                "durable hosted lifecycle state is invalid",
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _LIFECYCLE_STATE_VERSION
            or payload.get("cell_id") != self.config.cell_id
            or payload.get("phase") not in {"active", "quiesced", "sealed"}
        ):
            raise HostedLifecycleError(
                "HOSTED_LIFECYCLE_STATE_INVALID",
                "durable hosted lifecycle state does not match this cell",
            )
        return str(payload["phase"])

    def _persist_phase(self, phase: str) -> None:
        if phase not in {"active", "quiesced", "sealed"}:
            raise HostedLifecycleError(
                "HOSTED_LIFECYCLE_STATE_INVALID", "hosted lifecycle phase is not durable"
            )
        root = self.config.state_root
        path = root / _LIFECYCLE_STATE_FILENAME
        temp = root / f".{_LIFECYCLE_STATE_FILENAME}.tmp"
        payload = json.dumps(
            {
                "version": _LIFECYCLE_STATE_VERSION,
                "cell_id": self.config.cell_id,
                "phase": phase,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.is_symlink() or temp.is_symlink():
                raise OSError("lifecycle state path is a symbolic link")
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            _sync_directory(root)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise HostedLifecycleError(
                "HOSTED_LIFECYCLE_STATE_WRITE_FAILED",
                "durable hosted lifecycle state could not be recorded",
            ) from exc

    def liveness(self) -> HostedLiveness:
        return HostedLiveness(
            live=True,
            cell_id=self.config.cell_id,
            protocol_version=self.config.protocol_version,
        )

    def snapshot(self) -> HostedLifecycleSnapshot:
        with self._condition:
            return self._snapshot_locked()

    def readiness(self) -> HostedReadiness:
        with self._condition:
            phase_reason = {
                "starting": "HOSTED_STARTING",
                "quiescing": "HOSTED_QUIESCING",
                "quiesced": "HOSTED_QUIESCED",
                "sealed": "HOSTED_DELETION_SEALED",
            }.get(self._phase)
            read_admitted = (
                self._phase in {"active", "quiescing", "quiesced"} and self._read_ready_locked()
            )
            write_admitted = self._phase == "active" and self._core_ready_locked()
            if phase_reason is not None:
                reason = phase_reason
                ready = False
            elif not self._vault_ready:
                reason = "HOSTED_VAULT_UNAVAILABLE"
                ready = False
            elif not self._service_auth_ready:
                reason = "HOSTED_SERVICE_AUTH_UNAVAILABLE"
                ready = False
            elif not self._mutation_authority_ready:
                reason = self._mutation_reason
                ready = False
            else:
                reason = "HOSTED_READY"
                ready = True
            degraded = tuple(
                (f"worker:{name}", reason_code)
                for name, (worker_ready, reason_code) in sorted(self._worker_status.items())
                if not worker_ready
            )
            return HostedReadiness(
                ready=ready,
                phase=self._phase,
                reason_code=reason,
                read_admitted=read_admitted,
                write_admitted=write_admitted,
                degraded=degraded,
            )

    def control_plane_readiness(self) -> dict[str, Any]:
        """Return the provider-neutral readiness proof used before cell binding.

        The existing snake-case readiness fields remain the cell-local diagnostic
        contract.  This proof is deliberately explicit so a provisioner cannot
        infer mutation authority, service authentication, or worker policy from a
        single coarse ``ready`` boolean.
        """

        with self._condition:
            readiness = self.readiness()
            workers_enabled = self.config.resource_limits.worker_count > 0
            return {
                "live": True,
                "ready": readiness.ready,
                "cellId": self.config.cell_id,
                "protocolVersion": self.config.protocol_version,
                "releaseVersion": __version__,
                "serviceAuthenticated": self._service_auth_ready,
                "mutationAuthority": self._mutation_authority_ready,
                "readAdmission": readiness.read_admitted,
                "writeAdmission": readiness.write_admitted,
                "workerPolicy": {
                    "workerCount": self.config.resource_limits.worker_count,
                    "semantic": workers_enabled and "embeddings" in self.config.feature_grants,
                    "media": workers_enabled and "media" in self.config.feature_grants,
                },
                "code": "CELL_READY" if readiness.ready else readiness.reason_code,
            }

    def complete_startup(
        self,
        *,
        vault_ready: bool,
        mutation_authority_ready: bool,
        service_auth_ready: bool,
    ) -> HostedLifecycleSnapshot:
        with self._condition:
            self._vault_ready = bool(vault_ready)
            self._mutation_authority_ready = bool(mutation_authority_ready)
            self._mutation_reason = (
                "HOSTED_READY"
                if mutation_authority_ready
                else "HOSTED_MUTATION_AUTHORITY_UNAVAILABLE"
            )
            self._service_auth_ready = bool(service_auth_ready)
            if self._phase == "starting":
                self._persist_phase("active")
                self._phase = "active"
            self._condition.notify_all()
            return self._snapshot_locked()

    def set_mutation_authority(
        self, ready: bool, *, reason_code: str = "HOSTED_MUTATION_AUTHORITY_UNAVAILABLE"
    ) -> HostedLifecycleSnapshot:
        with self._condition:
            self._mutation_authority_ready = bool(ready)
            self._mutation_reason = "HOSTED_READY" if ready else reason_code
            self._condition.notify_all()
            return self._snapshot_locked()

    def set_vault_ready(self, ready: bool) -> HostedLifecycleSnapshot:
        with self._condition:
            self._vault_ready = bool(ready)
            self._condition.notify_all()
            return self._snapshot_locked()

    def set_service_auth_ready(self, ready: bool) -> HostedLifecycleSnapshot:
        with self._condition:
            self._service_auth_ready = bool(ready)
            self._condition.notify_all()
            return self._snapshot_locked()

    def set_worker_status(
        self, name: str, *, ready: bool, reason_code: str = "HOSTED_WORKER_UNAVAILABLE"
    ) -> HostedLifecycleSnapshot:
        clean = _normalize_feature(name)
        with self._condition:
            self._worker_status[clean] = (bool(ready), reason_code)
            return self._snapshot_locked()

    def register_background_stopper(self, stopper: Callable[[], Any]) -> None:
        self.register_background_worker(stopper=stopper)

    def register_background_worker(
        self,
        *,
        stopper: Callable[[], Any],
        starter: Callable[[], Any] | None = None,
    ) -> None:
        with self._condition:
            self._background_workers.append((stopper, starter))

    @contextmanager
    def admit_mutation(self) -> Iterator[None]:
        with self._condition:
            if self._phase != "active" or not self._core_ready_locked():
                raise HostedLifecycleError(
                    "HOSTED_MUTATION_NOT_ADMITTED",
                    "cell lifecycle does not currently admit mutations",
                )
            self._active_mutations += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_mutations -= 1
                self._condition.notify_all()

    def require_read_admission(self) -> None:
        with self._condition:
            self._require_read_admission_locked()

    @contextmanager
    def admit_read(self) -> Iterator[None]:
        """Hold read admission across snapshot coordination and leaf execution."""

        with self._condition:
            self._require_read_admission_locked()
            self._active_reads += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_reads -= 1
                self._condition.notify_all()

    @contextmanager
    def admit_transfer(self) -> Iterator[None]:
        """Keep deletion sealing closed until an admitted download finishes."""

        with self._condition:
            self._require_read_admission_locked(message="cell lifecycle does not admit transfers")
            self._active_transfers += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_transfers -= 1
                self._condition.notify_all()

    def quiesce(self, *, timeout: float) -> HostedLifecycleSnapshot:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            if self._phase == "sealed":
                return self._snapshot_locked()
            if self._phase == "quiesced":
                return self._snapshot_locked()
            self._phase = "quiescing"
            stoppers = tuple(stopper for stopper, _starter in self._background_workers)
            self._condition.notify_all()

        for stopper in stoppers:
            try:
                stopper()
            except Exception as exc:  # noqa: BLE001 - lifecycle must fail closed
                raise HostedLifecycleError(
                    "HOSTED_BACKGROUND_STOP_FAILED",
                    "a hosted background writer could not stop safely",
                ) from exc

        with self._condition:
            while self._active_mutations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HostedLifecycleError(
                        "HOSTED_QUIESCE_TIMEOUT",
                        "hosted cell did not drain mutations before its deadline",
                    )
                self._condition.wait(remaining)
            self._persist_phase("quiesced")
            self._phase = "quiesced"
            self._condition.notify_all()
            return self._snapshot_locked()

    def resume(self) -> HostedLifecycleSnapshot:
        with self._condition:
            if self._phase == "sealed":
                raise HostedLifecycleError("HOSTED_DELETION_SEALED", "sealed cell cannot resume")
            if self._phase == "starting":
                raise HostedLifecycleError(
                    "HOSTED_STARTUP_INCOMPLETE",
                    "cell cannot resume before startup completes",
                )
            if self._phase == "active":
                return self._snapshot_locked()
            if self._phase == "quiescing" and self._active_mutations:
                raise HostedLifecycleError(
                    "HOSTED_LIFECYCLE_BUSY",
                    "cell cannot resume while admitted mutations are draining",
                )
            started_stoppers: list[Callable[[], Any]] = []

            def rollback_started_workers() -> None:
                for stopper in reversed(started_stoppers):
                    try:
                        stopper()
                    except Exception:  # noqa: BLE001 - admission remains closed after rollback
                        pass

            try:
                for stopper, starter in self._background_workers:
                    if starter is None:
                        continue
                    started_stoppers.append(stopper)
                    starter()
            except Exception as exc:  # noqa: BLE001 - resume must remain closed
                rollback_started_workers()
                raise HostedLifecycleError(
                    "HOSTED_BACKGROUND_START_FAILED",
                    "a hosted background writer could not restart safely",
                ) from exc
            try:
                self._persist_phase("active")
            except HostedLifecycleError:
                rollback_started_workers()
                raise
            self._phase = "active"
            self._condition.notify_all()
            return self._snapshot_locked()

    def seal_for_deletion(self) -> HostedLifecycleSnapshot:
        with self._condition:
            if self._phase == "sealed":
                return self._snapshot_locked()
            if self._phase != "quiesced" or self._active_mutations:
                raise HostedLifecycleError(
                    "HOSTED_NOT_QUIESCED",
                    "cell must be quiesced before deletion sealing",
                )
            if self._active_reads:
                raise HostedLifecycleError(
                    "HOSTED_READ_IN_FLIGHT",
                    "cell has an active read",
                )
            if self._active_transfers:
                raise HostedLifecycleError(
                    "HOSTED_TRANSFER_IN_FLIGHT",
                    "cell has an active transfer",
                )
            self._persist_phase("sealed")
            self._phase = "sealed"
            self._condition.notify_all()
            return self._snapshot_locked()

    def _core_ready_locked(self) -> bool:
        return self._vault_ready and self._mutation_authority_ready and self._service_auth_ready

    def _read_ready_locked(self) -> bool:
        return self._vault_ready and self._service_auth_ready

    def _require_read_admission_locked(
        self,
        *,
        message: str = "cell lifecycle does not currently admit reads",
    ) -> None:
        if (
            self._phase not in {"active", "quiescing", "quiesced"}
            or not self._read_ready_locked()
        ):
            raise HostedLifecycleError("HOSTED_READ_NOT_ADMITTED", message)

    def _snapshot_locked(self) -> HostedLifecycleSnapshot:
        reason = self.readiness().reason_code
        return HostedLifecycleSnapshot(
            phase=self._phase,
            active_reads=self._active_reads,
            active_mutations=self._active_mutations,
            active_transfers=self._active_transfers,
            reason_code=reason,
        )


@dataclass(frozen=True)
class HostedProvisionResult:
    cell_id: str
    status: str
    lifecycle_status: str
    runtime_version: str
    protocol_version: str
    capabilities: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "status": self.status,
            "lifecycle_status": self.lifecycle_status,
            "runtime_version": self.runtime_version,
            "protocol_version": self.protocol_version,
            "capabilities": list(self.capabilities),
        }


def hosted_mode_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    raw = str(values.get(HOSTED_MODE_ENV, "")).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise HostedConfigError("HOSTED_MODE_INVALID", "hosted mode flag must be an explicit boolean")


def hosted_mutation_guard(vault_root: Path) -> AbstractContextManager[None]:
    """Late-bind the shared mutation coordinator without duplicating its lock."""

    from .writer_lease import get_manager

    manager = get_manager()
    guard = getattr(manager, "mutation_guard", None)
    if not callable(guard):
        raise HostedLifecycleError(
            "HOSTED_MUTATION_AUTHORITY_UNAVAILABLE",
            "shared vault mutation authority is unavailable",
        )
    return guard(vault_root)


def parse_feature_grants(raw: str | None) -> tuple[str, ...]:
    values = {_normalize_feature(part) for part in str(raw or "").split(",") if part.strip()}
    unknown = values - _KNOWN_FEATURES
    if unknown:
        raise HostedConfigError(
            "HOSTED_FEATURE_UNKNOWN", "hosted feature grants contain an unknown capability"
        )
    if "vision" in values and not {"embeddings", "media"}.issubset(values):
        raise HostedConfigError(
            "HOSTED_FEATURE_DEPENDENCY",
            "vision requires both embeddings and media grants",
        )
    if "diarization" in values and "media" not in values:
        raise HostedConfigError("HOSTED_FEATURE_DEPENDENCY", "diarization requires the media grant")
    return tuple(sorted(values))


def provision_hosted_cell(config: HostedCellConfig) -> HostedProvisionResult:
    """Create or converge one tenant-owned vault/state/log set.

    The canonical vault is initialized in a deterministic sibling staging root
    and atomically published. Existing unowned or incompatible data is never
    overlaid. External account, backup, billing, storage, and KMS resources are
    intentionally outside this operation.
    """
    _preflight_provisioning(config)
    existing = _is_complete_provisioning(config)
    if existing:
        return _provision_result(config, "existing")

    _ensure_owned_root(config.state_root, "state", config)
    _ensure_owned_root(config.log_root, "log", config)

    if config.vault_root.exists() and _marker_path(config.vault_root).exists():
        _validate_binding_marker(config.vault_root, "vault", config)
        if _valid_vault_scaffold(config.vault_root):
            config.validate_provisioned()
            return _provision_result(config, "existing")

    stage = _staging_root(config)
    if stage.exists():
        _validate_binding_marker(stage, "vault", config)
        if not _valid_vault_scaffold(stage):
            shutil.rmtree(stage)
    if not stage.exists():
        stage.mkdir(mode=0o700, parents=False)
        _write_binding_marker(stage, "vault", config)
        init_module.init_vault(stage)
        _sync_tree(stage)

    if config.vault_root.exists():
        # Preflight permits only an empty, unowned target here.
        config.vault_root.rmdir()
    _promote_staged_vault(stage, config.vault_root)
    _sync_directory(config.vault_root.parent)
    config.validate_provisioned()
    return _provision_result(config, "provisioned")


def _promote_staged_vault(stage: Path, destination: Path) -> None:
    """Atomic publication seam kept small for crash/restart tests."""
    os.replace(stage, destination)


def _provision_result(config: HostedCellConfig, status: str) -> HostedProvisionResult:
    return HostedProvisionResult(
        cell_id=config.cell_id,
        status=status,
        lifecycle_status="stopped",
        runtime_version=__version__,
        protocol_version=config.protocol_version,
        capabilities=config.feature_grants,
    )


def _preflight_provisioning(config: HostedCellConfig) -> None:
    _validate_disjoint_roots(config.vault_root, config.state_root, config.log_root)
    for _kind, root in config.roots():
        _reject_symlink_components(root)

    vault = config.vault_root
    if vault.exists():
        if not vault.is_dir():
            raise HostedConfigError(
                "HOSTED_PROVISIONING_CONFLICT",
                "assigned vault root is not an empty or owned directory",
            )
        if _marker_path(vault).exists():
            _validate_binding_marker(vault, "vault", config)
            if not _valid_vault_scaffold(vault):
                raise HostedConfigError(
                    "HOSTED_PROVISIONING_CONFLICT",
                    "owned vault root does not contain a complete scaffold",
                )
        elif any(vault.iterdir()):
            raise HostedConfigError(
                "HOSTED_PROVISIONING_CONFLICT",
                "assigned vault root contains existing unowned data",
            )

    for kind, root in (("state", config.state_root), ("log", config.log_root)):
        if not root.exists():
            continue
        if not root.is_dir():
            raise HostedConfigError(
                "HOSTED_PROVISIONING_CONFLICT",
                f"assigned {kind} root is not an owned directory",
            )
        if _marker_path(root).exists():
            _validate_binding_marker(root, kind, config)
        elif any(root.iterdir()):
            raise HostedConfigError(
                "HOSTED_PROVISIONING_CONFLICT",
                f"assigned {kind} root contains existing unowned data",
            )

    stage = _staging_root(config)
    if stage.exists():
        if stage.is_symlink() or not stage.is_dir():
            raise HostedConfigError("HOSTED_STAGING_CONFLICT", "hosted staging root is unsafe")
        _validate_binding_marker(stage, "vault", config)


def _is_complete_provisioning(config: HostedCellConfig) -> bool:
    try:
        config.validate_provisioned()
    except HostedConfigError as exc:
        if exc.code in {"HOSTED_ROOT_MISSING", "HOSTED_BINDING_MISSING"}:
            return False
        raise
    return True


def _ensure_owned_root(root: Path, kind: str, config: HostedCellConfig) -> None:
    if not root.exists():
        root.mkdir(mode=0o700, parents=True)
    if not root.is_dir():
        raise HostedConfigError(
            "HOSTED_PROVISIONING_CONFLICT", f"hosted {kind} root is not a directory"
        )
    marker = _marker_path(root)
    if marker.exists():
        _validate_binding_marker(root, kind, config)
        return
    if any(root.iterdir()):
        raise HostedConfigError(
            "HOSTED_PROVISIONING_CONFLICT",
            f"hosted {kind} root contains existing unowned data",
        )
    try:
        root.chmod(0o700)
    except OSError as exc:
        raise HostedConfigError(
            "HOSTED_ROOT_PERMISSIONS", f"hosted {kind} root cannot be made private"
        ) from exc
    _write_binding_marker(root, kind, config)


def _validated_root(raw: str, kind: str) -> Path:
    path = Path(str(raw))
    if not path.is_absolute():
        raise HostedConfigError("HOSTED_ROOT_NOT_ABSOLUTE", f"hosted {kind} root must be absolute")
    _reject_symlink_components(path)
    return path.resolve(strict=False)


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise HostedConfigError(
                "HOSTED_ROOT_SYMLINK", "hosted roots may not contain symbolic links"
            )


def _reject_tree_symlinks(root: Path) -> None:
    for current, directories, filenames in os.walk(root, followlinks=False):
        base = Path(current)
        for name in (*directories, *filenames):
            if (base / name).is_symlink():
                raise HostedConfigError(
                    "HOSTED_ROOT_SYMLINK",
                    "hosted vault content may not contain symbolic links",
                )


def _validate_private_permissions(root: Path, kind: str) -> None:
    if os.name == "nt":
        return
    try:
        mode = root.stat().st_mode & 0o777
    except OSError as exc:
        raise HostedConfigError(
            "HOSTED_ROOT_PERMISSIONS", f"hosted {kind} permissions cannot be read"
        ) from exc
    if mode & 0o077:
        raise HostedConfigError(
            "HOSTED_ROOT_PERMISSIONS", f"hosted {kind} root is not private to its owner"
        )


def _validate_disjoint_roots(*roots: Path) -> None:
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if left == right or _is_relative_to(left, right) or _is_relative_to(right, left):
                raise HostedConfigError(
                    "HOSTED_ROOT_OVERLAP",
                    "hosted vault, state, and log roots must be pairwise disjoint",
                )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _parse_limit(values: Mapping[str, str], key: str, default: int, *, allow_zero: bool) -> int:
    raw = str(values.get(key, "")).strip()
    try:
        parsed = int(raw) if raw else default
    except ValueError:
        raise HostedConfigError(
            "HOSTED_LIMIT_INVALID", "hosted resource limit must be an integer"
        ) from None
    minimum = 0 if allow_zero else 1
    if parsed < minimum:
        raise HostedConfigError(
            "HOSTED_LIMIT_INVALID", "hosted resource limit is outside its safe range"
        )
    return parsed


def _normalize_feature(feature: str) -> str:
    return str(feature).strip().lower().replace("_", "-")


def _apply_disable_gate(target: MutableMapping[str, str], enabled: bool, variable: str) -> None:
    if enabled:
        target.pop(variable, None)
    else:
        target[variable] = "1"


def _apply_truthy_gate(target: MutableMapping[str, str], enabled: bool, variable: str) -> None:
    if enabled:
        target[variable] = "1"
    else:
        target.pop(variable, None)


def _staging_root(config: HostedCellConfig) -> Path:
    safe_cell = hashlib.sha256(config.cell_id.encode("utf-8")).hexdigest()[:12]
    return config.vault_root.parent / f".{config.vault_root.name}.hosted-stage-{safe_cell}"


def _marker_path(root: Path) -> Path:
    return root / _BINDING_FILENAME


def _binding_payload(kind: str, config: HostedCellConfig) -> dict[str, Any]:
    return {
        "version": _BINDING_VERSION,
        "cell_id": config.cell_id,
        "root_kind": kind,
        "binding_digest": config.binding_digest,
    }


def _write_binding_marker(root: Path, kind: str, config: HostedCellConfig) -> None:
    path = _marker_path(root)
    temp = root / f".{_BINDING_FILENAME}.tmp"
    payload = json.dumps(_binding_payload(kind, config), sort_keys=True, separators=(",", ":"))
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    _sync_directory(root)


def _validate_binding_marker(root: Path, kind: str, config: HostedCellConfig) -> None:
    marker = _marker_path(root)
    if marker.is_symlink():
        raise HostedConfigError(
            "HOSTED_ROOT_SYMLINK", f"hosted {kind} ownership binding is a symbolic link"
        )
    if not marker.is_file():
        raise HostedConfigError(
            "HOSTED_BINDING_MISSING", f"hosted {kind} root has no ownership binding"
        )
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostedConfigError(
            "HOSTED_BINDING_INVALID", f"hosted {kind} ownership binding is invalid"
        ) from exc
    expected = _binding_payload(kind, config)
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise HostedConfigError(
            "HOSTED_BINDING_MISMATCH",
            f"hosted {kind} root is bound to a different cell or layout",
        )


def _valid_vault_scaffold(vault_root: Path) -> bool:
    kb = vault_root / kb_dirname()
    required = (
        kb / "index.md",
        kb / "log.md",
        kb / "_Schema" / "SKILL.md",
        kb / "Sources" / "index.md",
        kb / "Notes" / "index.md",
        kb / "Entities" / "index.md",
    )
    return all(path.is_file() for path in required)


def _sync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda candidate: len(candidate.parts), reverse=True):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        elif path.is_dir() and not path.is_symlink():
            _sync_directory(path)
    _sync_directory(root)


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# Hosted operator storage binding.  The legacy environment-driven runtime above
# remains available for the rollout window; operator-created cells use this
# richer storage identity and never persist release/protocol as ownership.
HOSTED_BINDING_VERSION = 2
_RUNTIME_ID_MAX = 2_147_483_647
_INIT_OPERATION_DIRECTORY = "hosted-init-operations"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID_V2 = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True, slots=True)
class HostedBindingV2:
    """Exact persistent ownership identity for one hosted cell's three roots."""

    cell_id: str
    vault_id: str
    vault_root: Path
    state_root: Path
    log_root: Path
    runtime_uid: int
    runtime_gid: int

    def __post_init__(self) -> None:
        if not _CELL_ID.fullmatch(self.cell_id) or not _CELL_ID.fullmatch(self.vault_id):
            raise HostedConfigError(
                "HOSTED_BINDING_CONFLICT", "hosted binding identity is invalid"
            )
        if not _valid_runtime_identity(self.runtime_uid) or not _valid_runtime_identity(
            self.runtime_gid
        ):
            raise HostedConfigError(
                "HOSTED_RUNTIME_ID_INVALID", "hosted runtime identity is outside its safe range"
            )
        normalized: list[Path] = []
        for kind, raw in (
            ("vault", self.vault_root),
            ("state", self.state_root),
            ("log", self.log_root),
        ):
            path = Path(raw)
            if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
                raise HostedConfigError(
                    "HOSTED_ROOT_INVALID", f"hosted {kind} root must be normalized and absolute"
                )
            _reject_symlink_components(path)
            normalized.append(path)
        _validate_disjoint_roots(*normalized)
        object.__setattr__(self, "vault_root", normalized[0])
        object.__setattr__(self, "state_root", normalized[1])
        object.__setattr__(self, "log_root", normalized[2])

    @property
    def binding_digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "binding_version": HOSTED_BINDING_VERSION,
                    "cell_id": self.cell_id,
                    "vault_id": self.vault_id,
                    "vault_root": str(self.vault_root),
                    "state_root": str(self.state_root),
                    "log_root": str(self.log_root),
                    "runtime_uid": self.runtime_uid,
                    "runtime_gid": self.runtime_gid,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def roots(self) -> tuple[tuple[str, Path], ...]:
        return (
            ("vault", self.vault_root),
            ("state", self.state_root),
            ("log", self.log_root),
        )

    def marker_payload(self, kind: str) -> dict[str, Any]:
        if kind not in {"vault", "state", "log"}:
            raise HostedConfigError("HOSTED_BINDING_CONFLICT", "hosted root kind is invalid")
        return {
            "binding_version": HOSTED_BINDING_VERSION,
            "cell_id": self.cell_id,
            "vault_id": self.vault_id,
            "vault_root": str(self.vault_root),
            "state_root": str(self.state_root),
            "log_root": str(self.log_root),
            "root_kind": kind,
            "runtime_uid": self.runtime_uid,
            "runtime_gid": self.runtime_gid,
        }


@dataclass(frozen=True, slots=True)
class HostedMigrationLimits:
    max_entries: int = 100_000
    max_bytes: int = 10 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        for value in (self.max_entries, self.max_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise HostedConfigError(
                    "HOSTED_ROOT_UNSAFE_ENTRY", "hosted migration limits are invalid"
                )


@dataclass(frozen=True, slots=True)
class HostedInitV2Result:
    status: str
    cell_id: str
    vault_id: str
    binding_version: int
    lifecycle_status: str
    exomem_release: str
    hosted_protocol: str
    runtime_uid: int
    runtime_gid: int
    credential_version: str
    credential_revision: int
    capabilities: tuple[str, ...]

    def as_operator_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "cell_id": self.cell_id,
            "vault_id": self.vault_id,
            "binding_version": self.binding_version,
            "lifecycle_status": self.lifecycle_status,
            "exomem_release": self.exomem_release,
            "hosted_protocol": self.hosted_protocol,
            "runtime_uid": self.runtime_uid,
            "runtime_gid": self.runtime_gid,
            "credential_version": self.credential_version,
            "credential_revision": self.credential_revision,
            "capabilities": list(self.capabilities),
        }


def _valid_runtime_identity(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= _RUNTIME_ID_MAX
    )


def _v2_marker_path(root: Path) -> Path:
    return root / _BINDING_FILENAME


def _read_marker_payload(
    root: Path,
    *,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    expected_mode: int | None = None,
) -> dict[str, Any]:
    marker = _v2_marker_path(root)
    try:
        descriptor = os.open(
            marker,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise HostedConfigError(
            "HOSTED_BINDING_CONFLICT", "hosted root ownership marker is unavailable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > 16_384
            or (expected_uid is not None and before.st_uid != expected_uid)
            or (expected_gid is not None and before.st_gid != expected_gid)
            or (expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode)
        ):
            raise HostedConfigError(
                "HOSTED_BINDING_CONFLICT", "hosted root ownership marker is unsafe"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(4096, 16_385 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 16_384:
                raise HostedConfigError(
                    "HOSTED_BINDING_CONFLICT", "hosted root ownership marker is unsafe"
                )
        after = os.fstat(descriptor)
    except HostedConfigError:
        raise
    except OSError as exc:
        raise HostedConfigError(
            "HOSTED_BINDING_CONFLICT", "hosted root ownership marker cannot be read safely"
        ) from exc
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
        before.st_uid,
        before.st_gid,
        stat.S_IMODE(before.st_mode),
        before.st_size,
    ) != (
        after.st_dev,
        after.st_ino,
        stat.S_IFMT(after.st_mode),
        after.st_uid,
        after.st_gid,
        stat.S_IMODE(after.st_mode),
        after.st_size,
    ) or total != before.st_size:
        raise HostedConfigError(
            "HOSTED_BINDING_CONFLICT", "hosted root ownership marker changed during read"
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate marker key")
            result[key] = value
        return result

    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HostedConfigError(
            "HOSTED_BINDING_CONFLICT", "hosted root ownership marker is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise HostedConfigError(
            "HOSTED_BINDING_CONFLICT", "hosted root ownership marker is invalid"
        )
    return payload


def _stat_no_follow(path: Path, *, kind: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise HostedConfigError(
            "HOSTED_ROOT_OWNERSHIP_MISMATCH", f"hosted {kind} ownership cannot be verified"
        ) from exc
    if stat.S_ISLNK(value.st_mode):
        raise HostedConfigError(
            "HOSTED_ROOT_UNSAFE_ENTRY", f"hosted {kind} path is a symbolic link"
        )
    return value


def _validate_v2_marker(root: Path, kind: str, binding: HostedBindingV2) -> None:
    root_stat = _stat_no_follow(root, kind=kind)
    marker = _v2_marker_path(root)
    marker_stat = _stat_no_follow(marker, kind=f"{kind} marker")
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != binding.runtime_uid
        or root_stat.st_gid != binding.runtime_gid
        or stat.S_IMODE(root_stat.st_mode) != 0o700
        or not stat.S_ISREG(marker_stat.st_mode)
        or marker_stat.st_nlink != 1
        or marker_stat.st_uid != binding.runtime_uid
        or marker_stat.st_gid != binding.runtime_gid
        or stat.S_IMODE(marker_stat.st_mode) != 0o600
    ):
        raise HostedConfigError(
            "HOSTED_ROOT_OWNERSHIP_MISMATCH",
            "hosted root ownership or private mode differs from binding",
        )
    if _read_marker_payload(
        root,
        expected_uid=binding.runtime_uid,
        expected_gid=binding.runtime_gid,
        expected_mode=0o600,
    ) != binding.marker_payload(kind):
        raise HostedConfigError(
            "HOSTED_BINDING_CONFLICT", "hosted root belongs to another identity or layout"
        )


def validate_hosted_binding_v2(
    binding: HostedBindingV2, *, require_scaffold: bool = False
) -> None:
    """Validate marker bytes plus actual no-follow ownership and modes."""

    for kind, root in binding.roots():
        _reject_symlink_components(root)
        _validate_v2_marker(root, kind, binding)
    if require_scaffold and not _valid_vault_scaffold(binding.vault_root):
        raise HostedConfigError(
            "HOSTED_PROVISIONING_CONFLICT", "hosted vault scaffold is incomplete"
        )


def _legacy_binding_payload(kind: str, binding: HostedBindingV2) -> dict[str, Any]:
    digest = hashlib.sha256(
        "\0".join(
            (
                binding.cell_id,
                str(binding.vault_root),
                str(binding.state_root),
                str(binding.log_root),
            )
        ).encode("utf-8")
    ).hexdigest()
    return {
        "version": 1,
        "cell_id": binding.cell_id,
        "root_kind": kind,
        "binding_digest": digest,
    }


def _root_binding_generation(root: Path, kind: str, binding: HostedBindingV2) -> int | None:
    if not os.path.lexists(root):
        return None
    root_stat = _stat_no_follow(root, kind=kind)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise HostedConfigError(
            "HOSTED_PROVISIONING_CONFLICT", f"hosted {kind} root is not a directory"
        )
    marker = _v2_marker_path(root)
    if not os.path.lexists(marker):
        if any(root.iterdir()):
            raise HostedConfigError(
                "HOSTED_PROVISIONING_CONFLICT", f"hosted {kind} root contains unowned data"
            )
        return None
    payload = _read_marker_payload(root)
    if payload == binding.marker_payload(kind):
        _validate_v2_marker(root, kind, binding)
        return 2
    if payload == _legacy_binding_payload(kind, binding):
        return 1
    raise HostedConfigError(
        "HOSTED_BINDING_CONFLICT", f"hosted {kind} root has a foreign ownership binding"
    )


def _tree_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_nlink,
        value.st_size,
    )


def _preflight_migration_tree(
    root: Path, limits: HostedMigrationLimits
) -> list[tuple[Path, tuple[int, int, int, int, int], int]]:
    entries: list[tuple[Path, tuple[int, int, int, int, int], int]] = []
    total_bytes = 0
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        current_stat = _stat_no_follow(base, kind="migration")
        if not stat.S_ISDIR(current_stat.st_mode):
            raise HostedConfigError(
                "HOSTED_ROOT_UNSAFE_ENTRY", "hosted migration encountered an unsafe directory"
            )
        entries.append((base, _tree_signature(current_stat), stat.S_IMODE(current_stat.st_mode)))
        for name in (*directory_names, *file_names):
            child = base / name
            child_stat = _stat_no_follow(child, kind="migration")
            if stat.S_ISDIR(child_stat.st_mode):
                continue
            if not stat.S_ISREG(child_stat.st_mode) or child_stat.st_nlink != 1:
                raise HostedConfigError(
                    "HOSTED_ROOT_UNSAFE_ENTRY", "hosted migration encountered an unsafe entry"
                )
            total_bytes += child_stat.st_size
            entries.append(
                (child, _tree_signature(child_stat), stat.S_IMODE(child_stat.st_mode))
            )
        if len(entries) > limits.max_entries or total_bytes > limits.max_bytes:
            raise HostedConfigError(
                "HOSTED_ROOT_UNSAFE_ENTRY", "hosted migration exceeds its bounded limits"
            )
    if len(entries) > limits.max_entries:
        raise HostedConfigError(
            "HOSTED_ROOT_UNSAFE_ENTRY", "hosted migration exceeds its bounded limits"
        )
    return entries


def _converge_tree_ownership(
    entries: list[tuple[Path, tuple[int, int, int, int, int], int]],
    binding: HostedBindingV2,
) -> None:
    root = entries[0][0]
    expected = {
        path.relative_to(root).parts: (signature, original_mode)
        for path, signature, original_mode in entries
    }
    visited: set[tuple[str, ...]] = set()
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    def converge_descriptor(descriptor: int, relative: tuple[str, ...]) -> None:
        current = os.fstat(descriptor)
        recorded = expected.get(relative)
        if recorded is None or _tree_signature(current) != recorded[0]:
            raise HostedConfigError(
                "HOSTED_ROOT_UNSAFE_ENTRY", "hosted migration entry changed during traversal"
            )
        visited.add(relative)
        os.fchown(descriptor, binding.runtime_uid, binding.runtime_gid)
        original_mode = recorded[1]
        os.fchmod(
            descriptor,
            (
                0o700
                if relative == ()
                else ((original_mode & 0o700) | 0o100)
            )
            if stat.S_ISDIR(current.st_mode)
            else original_mode & 0o700,
        )
        os.fsync(descriptor)

    def walk(directory_fd: int, relative: tuple[str, ...]) -> None:
        converge_descriptor(directory_fd, relative)
        for name in sorted(os.listdir(directory_fd)):
            child_relative = (*relative, name)
            try:
                child_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise HostedConfigError(
                    "HOSTED_ROOT_UNSAFE_ENTRY", "hosted migration entry changed during traversal"
                ) from exc
            flags = directory_flags if stat.S_ISDIR(child_stat.st_mode) else file_flags
            try:
                child_fd = os.open(name, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise HostedConfigError(
                    "HOSTED_ROOT_UNSAFE_ENTRY", "hosted migration entry changed during traversal"
                ) from exc
            try:
                if stat.S_ISDIR(child_stat.st_mode):
                    walk(child_fd, child_relative)
                elif stat.S_ISREG(child_stat.st_mode) and child_stat.st_nlink == 1:
                    converge_descriptor(child_fd, child_relative)
                else:
                    raise HostedConfigError(
                        "HOSTED_ROOT_UNSAFE_ENTRY", "hosted migration encountered an unsafe entry"
                    )
            finally:
                os.close(child_fd)

    try:
        root_fd = os.open(root, directory_flags)
        try:
            walk(root_fd, ())
        finally:
            os.close(root_fd)
    except HostedConfigError:
        raise
    except OSError as exc:
        raise HostedConfigError(
            "HOSTED_ROOT_OWNERSHIP_MISMATCH", "hosted migration could not converge ownership"
        ) from exc
    if visited != set(expected):
        raise HostedConfigError(
            "HOSTED_ROOT_UNSAFE_ENTRY", "hosted migration tree changed during traversal"
        )


def _write_v2_marker(root: Path, kind: str, binding: HostedBindingV2) -> None:
    marker = _v2_marker_path(root)
    temporary = root / f".{_BINDING_FILENAME}.v2-{os.getpid()}.tmp"
    payload = (
        json.dumps(binding.marker_payload(kind), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
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
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
    _sync_directory(root)


def _prepare_fresh_root(root: Path, kind: str, binding: HostedBindingV2) -> None:
    if not root.exists():
        root.mkdir(mode=0o700, parents=True)
        _sync_directory(root.parent)
    if any(root.iterdir()):
        raise HostedConfigError(
            "HOSTED_PROVISIONING_CONFLICT", f"hosted {kind} root contains unowned data"
        )
    try:
        if os.geteuid() == 0:
            os.chown(root, binding.runtime_uid, binding.runtime_gid, follow_symlinks=False)
        root.chmod(0o700, follow_symlinks=False)
    except OSError as exc:
        raise HostedConfigError(
            "HOSTED_ROOT_OWNERSHIP_MISMATCH", f"hosted {kind} ownership cannot be converged"
        ) from exc
    root_stat = root.lstat()
    if root_stat.st_uid != binding.runtime_uid or root_stat.st_gid != binding.runtime_gid:
        raise HostedConfigError(
            "HOSTED_ROOT_OWNERSHIP_MISMATCH", f"hosted {kind} root has another owner"
        )


def _default_security_bootstrap(**kwargs: Any) -> int:
    try:
        from .hosted_security import bootstrap_hosted_security
    except ImportError as exc:
        raise HostedConfigError(
            "HOSTED_SECURITY_UNAVAILABLE", "hosted security authority is unavailable"
        ) from exc
    result = bootstrap_hosted_security(**kwargs)
    revision = result if isinstance(result, int) else getattr(result, "revision", None)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise HostedConfigError(
            "HOSTED_SECURITY_UNAVAILABLE", "hosted security bootstrap returned no revision"
        )
    return revision


def _init_operation_path(binding: HostedBindingV2, operation_id: str) -> Path:
    key = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return binding.state_root / _INIT_OPERATION_DIRECTORY / f"{key}.json"


def _read_init_operation(
    binding: HostedBindingV2,
    operation_id: str | None,
    request_digest: str | None,
    active_credential_version: str,
) -> dict[str, Any] | None:
    if (operation_id is None) != (request_digest is None):
        raise HostedConfigError(
            "HOSTED_OPERATION_CONFLICT", "hosted initialization identity is incomplete"
        )
    if operation_id is None:
        return None
    if not _OPERATION_ID_V2.fullmatch(operation_id) or not _SHA256_HEX.fullmatch(
        request_digest or ""
    ):
        raise HostedConfigError(
            "HOSTED_OPERATION_CONFLICT", "hosted initialization identity is invalid"
        )
    path = _init_operation_path(binding, operation_id)
    if not os.path.lexists(path):
        return None

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate initialization proof key")
            result[key] = item
        return result

    try:
        directory = path.parent.lstat()
        if (
            stat.S_ISLNK(directory.st_mode)
            or not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != binding.runtime_uid
            or directory.st_gid != binding.runtime_gid
            or stat.S_IMODE(directory.st_mode) != 0o700
        ):
            raise ValueError("unsafe initialization proof directory")
        value = path.lstat()
        if (
            stat.S_ISLNK(value.st_mode)
            or not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or value.st_uid != binding.runtime_uid
            or value.st_gid != binding.runtime_gid
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_size > 4096
        ):
            raise ValueError("unsafe initialization proof")
        record = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HostedConfigError(
            "HOSTED_OPERATION_CONFLICT", "hosted initialization proof is invalid"
        ) from exc
    expected = {
        "schema_version": 1,
        "operation_id": operation_id,
        "request_digest": request_digest,
        "binding_digest": binding.binding_digest,
        "credential_version": active_credential_version,
    }
    if not isinstance(record, dict) or any(record.get(key) != item for key, item in expected.items()):
        raise HostedConfigError(
            "HOSTED_OPERATION_CONFLICT", "hosted initialization operation conflicts"
        )
    if set(record) != {*expected, "status", "credential_revision"}:
        raise HostedConfigError(
            "HOSTED_OPERATION_CONFLICT", "hosted initialization proof is invalid"
        )
    if record["status"] not in {"provisioned", "migrated", "existing"} or not isinstance(
        record["credential_revision"], int
    ):
        raise HostedConfigError(
            "HOSTED_OPERATION_CONFLICT", "hosted initialization proof is invalid"
        )
    return record


def _write_init_operation(
    binding: HostedBindingV2,
    *,
    operation_id: str,
    request_digest: str,
    active_credential_version: str,
    status: str,
    credential_revision: int,
) -> None:
    path = _init_operation_path(binding, operation_id)
    directory_existed = path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory = path.parent.lstat()
        if stat.S_ISLNK(directory.st_mode) or not stat.S_ISDIR(directory.st_mode):
            raise OSError("unsafe initialization proof directory")
        if os.geteuid() == 0:
            os.chown(
                path.parent,
                binding.runtime_uid,
                binding.runtime_gid,
                follow_symlinks=False,
            )
        path.parent.chmod(0o700, follow_symlinks=False)
        directory = path.parent.lstat()
        if directory.st_uid != binding.runtime_uid or directory.st_gid != binding.runtime_gid:
            raise OSError("foreign initialization proof directory")
    except OSError as exc:
        raise HostedConfigError(
            "HOSTED_OPERATION_CONFLICT", "hosted initialization proof directory is unsafe"
        ) from exc
    if not directory_existed:
        _sync_directory(path.parent.parent)
    record = {
        "schema_version": 1,
        "operation_id": operation_id,
        "request_digest": request_digest,
        "binding_digest": binding.binding_digest,
        "credential_version": active_credential_version,
        "status": status,
        "credential_revision": credential_revision,
    }
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        payload = (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
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


def initialize_hosted_cell_v2(
    binding: HostedBindingV2,
    *,
    expected_release: str,
    expected_protocol: str,
    active_credential_version: str,
    operation_id: str | None = None,
    request_digest: str | None = None,
    bootstrap_security: Callable[..., int] | None = None,
    allow_privileged_migration: bool = False,
    migration_limits: HostedMigrationLimits | None = None,
) -> HostedInitV2Result:
    """Create/converge a v2-bound cell without adopting foreign bytes."""

    if expected_release != __version__:
        raise HostedConfigError("HOSTED_RELEASE_MISMATCH", "hosted release proof differs")
    if expected_protocol not in SUPPORTED_HOSTED_PROTOCOL_VERSIONS:
        raise HostedConfigError(
            "HOSTED_PROTOCOL_UNSUPPORTED", "hosted protocol proof is unsupported"
        )
    if not _CREDENTIAL_VERSION.fullmatch(active_credential_version):
        raise HostedConfigError(
            "HOSTED_CREDENTIAL_BUNDLE_INVALID", "hosted credential version is invalid"
        )

    generations = {
        kind: _root_binding_generation(root, kind, binding) for kind, root in binding.roots()
    }
    has_v1 = 1 in generations.values()
    if has_v1 and (not allow_privileged_migration or os.geteuid() != 0):
        raise HostedConfigError(
            "HOSTED_ROOT_OWNERSHIP_MISMATCH", "version one migration requires privilege"
        )

    limits = migration_limits or HostedMigrationLimits()
    migration_entries: dict[str, list[tuple[Path, tuple[int, int, int, int, int], int]]] = {}
    for kind, root in binding.roots():
        if generations[kind] == 1:
            migration_entries[kind] = _preflight_migration_tree(root, limits)

    existing = all(generation == 2 for generation in generations.values())
    if existing:
        validate_hosted_binding_v2(binding, require_scaffold=True)
    else:
        # Preflight above covers all roots before the first mutation.
        for kind, root in binding.roots():
            generation = generations[kind]
            if generation == 1:
                _converge_tree_ownership(migration_entries[kind], binding)
                _write_v2_marker(root, kind, binding)
            elif generation is None and kind != "vault":
                _prepare_fresh_root(root, kind, binding)
                _write_v2_marker(root, kind, binding)

        vault_generation = generations["vault"]
        if vault_generation is None:
            stage = binding.vault_root.parent / (
                f".{binding.vault_root.name}.hosted-v2-stage-"
                f"{hashlib.sha256(binding.cell_id.encode()).hexdigest()[:12]}"
            )
            if stage.exists():
                _validate_v2_marker(stage, "vault", binding)
                if not _valid_vault_scaffold(stage):
                    raise HostedConfigError(
                        "HOSTED_PROVISIONING_CONFLICT", "hosted vault staging is incomplete"
                    )
            else:
                stage.mkdir(mode=0o700, parents=False)
                if os.geteuid() == 0:
                    os.chown(
                        stage,
                        binding.runtime_uid,
                        binding.runtime_gid,
                        follow_symlinks=False,
                    )
                stage.chmod(0o700, follow_symlinks=False)
                _write_v2_marker(stage, "vault", binding)
                init_module.init_vault(stage)
                _sync_tree(stage)
            if binding.vault_root.exists():
                binding.vault_root.rmdir()
            os.replace(stage, binding.vault_root)
            _sync_directory(binding.vault_root.parent)

    recorded_operation = _read_init_operation(
        binding,
        operation_id,
        request_digest,
        active_credential_version,
    )
    bootstrap = bootstrap_security or _default_security_bootstrap
    revision = bootstrap(
        binding=binding,
        active_credential_version=active_credential_version,
        operation_id=operation_id,
        request_digest=request_digest,
    )
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise HostedConfigError(
            "HOSTED_SECURITY_UNAVAILABLE", "hosted security bootstrap returned no revision"
        )
    validate_hosted_binding_v2(binding, require_scaffold=True)
    if recorded_operation is not None:
        if recorded_operation["credential_revision"] != revision:
            raise HostedConfigError(
                "HOSTED_OPERATION_CONFLICT", "hosted security replay differs from init proof"
            )
        status = recorded_operation["status"]
    else:
        status = "existing" if existing else ("migrated" if has_v1 else "provisioned")
        if operation_id is not None and request_digest is not None:
            _write_init_operation(
                binding,
                operation_id=operation_id,
                request_digest=request_digest,
                active_credential_version=active_credential_version,
                status=status,
                credential_revision=revision,
            )
    return HostedInitV2Result(
        status=status,
        cell_id=binding.cell_id,
        vault_id=binding.vault_id,
        binding_version=HOSTED_BINDING_VERSION,
        lifecycle_status="stopped",
        exomem_release=__version__,
        hosted_protocol=expected_protocol,
        runtime_uid=binding.runtime_uid,
        runtime_gid=binding.runtime_gid,
        credential_version=active_credential_version,
        credential_revision=revision,
        capabilities=("hosted-operator-v1",),
    )


def execute_hosted_init_v2(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Operator adapter kept separate from the storage primitive."""

    from .hosted_operator import OperatorFailure, canonical_request_digest

    try:
        binding = HostedBindingV2(
            cell_id=request["cell_id"],
            vault_id=request["vault_id"],
            vault_root=Path(request["vault_root"]),
            state_root=Path(request["state_root"]),
            log_root=Path(request["log_root"]),
            runtime_uid=request["runtime_uid"],
            runtime_gid=request["runtime_gid"],
        )
        result = initialize_hosted_cell_v2(
            binding,
            expected_release=request["expected_release"],
            expected_protocol=request["expected_protocol"],
            active_credential_version=request["active_credential_version"],
            operation_id=request["operation_id"],
            request_digest=canonical_request_digest(request),
            allow_privileged_migration=os.geteuid() == 0,
        )
    except HostedConfigError as exc:
        code = "HOSTED_ROOT_UNSAFE_ENTRY" if exc.code == "HOSTED_ROOT_SYMLINK" else exc.code
        code = code if code in {
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
        } else "HOSTED_OPERATOR_INTERNAL"
        raise OperatorFailure(
            code, command="init", request_id=request.get("request_id")
        ) from exc
    return "HOSTED_CELL_INITIALIZED", result.as_operator_data()
