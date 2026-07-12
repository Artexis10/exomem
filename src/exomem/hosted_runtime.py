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
_CELL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PROTOCOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
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
    active_mutations: int
    active_transfers: int
    reason_code: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "active_mutations": self.active_mutations,
            "active_transfers": self.active_transfers,
            "reason_code": self.reason_code,
        }


class HostedCellLifecycle:
    """Thread-safe admission and lifecycle state for one immutable cell."""

    def __init__(self, config: HostedCellConfig) -> None:
        self.config = config
        self._condition = threading.Condition(threading.RLock())
        self._phase = "starting"
        self._vault_ready = False
        self._mutation_authority_ready = False
        self._mutation_reason = "HOSTED_MUTATION_AUTHORITY_UNAVAILABLE"
        self._service_auth_ready = False
        self._active_mutations = 0
        self._active_transfers = 0
        self._worker_status: dict[str, tuple[bool, str]] = {}
        self._background_workers: list[tuple[Callable[[], Any], Callable[[], Any] | None]] = []

    def liveness(self) -> HostedLiveness:
        return HostedLiveness(
            live=True,
            cell_id=self.config.cell_id,
            protocol_version=self.config.protocol_version,
        )

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

    def complete_startup(
        self,
        *,
        vault_ready: bool,
        mutation_authority_ready: bool,
        service_auth_ready: bool,
    ) -> HostedLifecycleSnapshot:
        with self._condition:
            if self._phase == "sealed":
                raise HostedLifecycleError(
                    "HOSTED_DELETION_SEALED", "sealed cell cannot complete startup"
                )
            self._vault_ready = bool(vault_ready)
            self._mutation_authority_ready = bool(mutation_authority_ready)
            self._mutation_reason = (
                "HOSTED_READY"
                if mutation_authority_ready
                else "HOSTED_MUTATION_AUTHORITY_UNAVAILABLE"
            )
            self._service_auth_ready = bool(service_auth_ready)
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
            if (
                self._phase not in {"active", "quiescing", "quiesced"}
                or not self._read_ready_locked()
            ):
                raise HostedLifecycleError(
                    "HOSTED_READ_NOT_ADMITTED",
                    "cell lifecycle does not currently admit reads",
                )

    @contextmanager
    def admit_transfer(self) -> Iterator[None]:
        """Keep deletion sealing closed until an admitted download finishes."""

        with self._condition:
            if (
                self._phase not in {"active", "quiescing", "quiesced"}
                or not self._read_ready_locked()
            ):
                raise HostedLifecycleError(
                    "HOSTED_READ_NOT_ADMITTED",
                    "cell lifecycle does not currently admit transfers",
                )
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
            for _stopper, starter in self._background_workers:
                if starter is None:
                    continue
                try:
                    starter()
                except Exception as exc:  # noqa: BLE001 - resume must remain closed
                    raise HostedLifecycleError(
                        "HOSTED_BACKGROUND_START_FAILED",
                        "a hosted background writer could not restart safely",
                    ) from exc
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
            if self._active_transfers:
                raise HostedLifecycleError(
                    "HOSTED_TRANSFER_IN_FLIGHT",
                    "cell has an active transfer",
                )
            self._phase = "sealed"
            self._condition.notify_all()
            return self._snapshot_locked()

    def _core_ready_locked(self) -> bool:
        return self._vault_ready and self._mutation_authority_ready and self._service_auth_ready

    def _read_ready_locked(self) -> bool:
        return self._vault_ready and self._service_auth_ready

    def _snapshot_locked(self) -> HostedLifecycleSnapshot:
        reason = self.readiness().reason_code
        return HostedLifecycleSnapshot(
            phase=self._phase,
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
