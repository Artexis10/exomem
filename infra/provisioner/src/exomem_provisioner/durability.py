"""Production durability orchestration over portable Exomem runtime contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .durability_crypto import ChunkedArchiveCipher, DataKeyWrapper, RecoveryIdentity
from .durability_repository import (
    ActiveDurabilityRun,
    DurabilityClaimConflict,
    DurabilityRepository,
    RecoveryObjectInput,
    RunIdentity,
    RunKind,
    RunSnapshot,
)
from .durability_store import ProviderObjectHead
from .models import DurabilityRunState
from .provider_recovery import (
    ProviderIdentityAuthenticator,
    ProviderIdentitySigner,
    ProviderReference,
)


class DurabilityVerificationError(RuntimeError):
    pass


class QuiescenceTargetExceeded(DurabilityVerificationError):
    pass


@dataclass(frozen=True, slots=True)
class PortableArchive:
    archive_path: Path
    manifest_path: Path
    archive_sha256: str
    manifest_sha256: str
    archive_size: int
    source_cell_id: str
    release_version: str
    hosted_state_included: bool


@dataclass(frozen=True, slots=True)
class ExportBackupResult:
    opaque_reference: str
    release_reference: str
    archive_sha256: str
    manifest_sha256: str
    archive_size: int
    encryption_scheme: str
    integrity_verified: bool
    quiescence_seconds: float


class RouteMaintenancePort(Protocol):
    async def close_and_verify(self, cell_id: str, operation_id: str) -> None: ...

    async def open(self, cell_id: str, operation_id: str) -> None: ...


class PortableRuntimePort(Protocol):
    async def quiesce(self, cell_id: str, operation_id: str, *, routing_stopped: bool) -> None: ...

    async def portable_export(self, cell_id: str, operation_id: str) -> PortableArchive: ...

    async def release(self, cell_id: str, operation_id: str) -> None: ...


class UploadOnlyRecoveryStore(Protocol):
    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        metadata: dict[str, str],
        retain_until: datetime | None,
    ) -> ProviderObjectHead: ...

    async def head(self, key: str) -> ProviderObjectHead | None: ...


class ExportBackupWorkflow:
    """Resumable route-stop -> snapshot -> release -> encrypt -> upload workflow."""

    def __init__(
        self,
        *,
        repository: DurabilityRepository,
        routes: RouteMaintenancePort,
        runtime: PortableRuntimePort,
        upload_store: UploadOnlyRecoveryStore,
        cipher: ChunkedArchiveCipher,
        key_wrapper: DataKeyWrapper,
        provider_identity_signer: ProviderIdentitySigner,
        provider_bucket: str,
        scratch_root: Path,
        min_archive_bytes: int = 1024,
        max_archive_bytes: int = 6 * 1024 * 1024 * 1024,
        max_quiescence_seconds: float = 120,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._routes = routes
        self._runtime = runtime
        self._upload_store = upload_store
        self._cipher = cipher
        self._key_wrapper = key_wrapper
        self._provider_identity_signer = provider_identity_signer
        self._provider_bucket = provider_bucket
        self._scratch_root = scratch_root.resolve()
        self._min_archive_bytes = min_archive_bytes
        self._max_archive_bytes = max_archive_bytes
        self._max_quiescence_seconds = max_quiescence_seconds
        self._monotonic = monotonic
        self._clock = clock

    async def run(
        self,
        run: RunSnapshot,
        *,
        worker_id: str,
        expires_at: datetime | None = None,
    ) -> ExportBackupResult:
        self._scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._scratch_root.chmod(0o700)
        paths = (
            self._scratch_root / f"{run.id}.archive",
            self._scratch_root / f"{run.id}.manifest.json",
            self._scratch_root / f"{run.id}.encrypted",
        )
        try:
            return await self._run_once(
                run,
                worker_id=worker_id,
                expires_at=expires_at,
            )
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    async def _run_once(
        self,
        run: RunSnapshot,
        *,
        worker_id: str,
        expires_at: datetime | None = None,
    ) -> ExportBackupResult:
        claim = {
            "claim_token": run.claim_token,
            "claim_generation": run.claim_generation,
        }
        identity = run.identity
        archive_path = self._scratch_root / f"{run.id}.archive"
        manifest_path = self._scratch_root / f"{run.id}.manifest.json"
        encrypted_path = self._scratch_root / f"{run.id}.encrypted"
        self._scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._scratch_root.chmod(0o700)

        state = dict(run.state)
        checkpoint = run.checkpoint
        product_expiry = self._product_expiry(run, expires_at)
        product_expiry_iso = product_expiry.isoformat() if product_expiry is not None else None
        if checkpoint != "requested" and state.get("product_expires_at") != product_expiry_iso:
            raise DurabilityVerificationError("durability expiry differs from checkpoint")
        if checkpoint == "requested":
            started = self._monotonic()
            await self._routes.close_and_verify(identity.cell_id, identity.operation_id)
            archive: PortableArchive | None = None
            failure: Exception | None = None
            try:
                await self._runtime.quiesce(
                    identity.cell_id, identity.operation_id, routing_stopped=True
                )
                archive = await self._runtime.portable_export(
                    identity.cell_id, identity.operation_id
                )
                await asyncio.to_thread(
                    self._stage_and_verify, archive, archive_path, manifest_path
                )
            except Exception as error:  # noqa: BLE001 - release routes for every provider failure
                failure = error
            try:
                await self._runtime.release(identity.cell_id, identity.operation_id)
            except Exception as error:  # noqa: BLE001 - still reopen routes after release failure
                if failure is None:
                    failure = error
            try:
                await self._routes.open(identity.cell_id, identity.operation_id)
            except Exception as error:  # noqa: BLE001 - retain first cleanup/provider failure
                if failure is None:
                    failure = error
            elapsed = self._monotonic() - started
            if failure is not None:
                await self._repository.checkpoint(
                    run.id,
                    worker_id,
                    **claim,
                    checkpoint="snapshot-failed",
                    state={"error_code": "PORTABLE_ARCHIVE_INVALID"},
                )
                raise failure
            if archive is None:
                raise DurabilityVerificationError("runtime returned no portable archive")
            state = {
                "archive_path": str(archive_path),
                "manifest_path": str(manifest_path),
                "archive_sha256": archive.archive_sha256,
                "manifest_sha256": archive.manifest_sha256,
                "archive_size": archive.archive_size,
                "release_version": archive.release_version,
                "quiescence_seconds": elapsed,
                "product_expires_at": product_expiry_iso,
            }
            updated = await self._repository.checkpoint(
                run.id,
                worker_id,
                **claim,
                checkpoint="scratch-released",
                state=state,
            )
            checkpoint = updated.checkpoint
            if elapsed > self._max_quiescence_seconds:
                await self._repository.checkpoint(
                    run.id,
                    worker_id,
                    **claim,
                    checkpoint="quiescence-target-exceeded",
                    state=state,
                )
                raise QuiescenceTargetExceeded("portable snapshot exceeded two-minute target")

        if checkpoint not in {"scratch-released", "encrypted", "uploaded"}:
            raise DurabilityVerificationError(f"unsupported durability checkpoint: {checkpoint}")
        if checkpoint == "scratch-released":
            if not archive_path.is_file() or not manifest_path.is_file():
                restarted = await self._repository.checkpoint(
                    run.id,
                    worker_id,
                    **claim,
                    checkpoint="requested",
                    state={},
                )
                return await self._run_once(
                    restarted,
                    worker_id=worker_id,
                    expires_at=expires_at,
                )
            await asyncio.to_thread(self._verify_staged_state, state, archive_path, manifest_path)
        remote_encrypted_checkpoint = False
        if checkpoint == "encrypted":
            if encrypted_path.is_file():
                await asyncio.to_thread(self._verify_encrypted_state, state, encrypted_path)
            else:
                remote_encrypted_checkpoint = True

        recovery_identity = RecoveryIdentity(
            tenant_id=identity.tenant_id,
            cell_id=identity.cell_id,
            operation_id=identity.operation_id,
            fence_generation=identity.fence_generation,
            archive_sha256=str(state["archive_sha256"]),
            manifest_sha256=str(state["manifest_sha256"]),
            archive_size=int(state["archive_size"]),
        )
        if checkpoint == "scratch-released":
            encryption = await asyncio.to_thread(
                self._cipher.encrypt,
                archive_path,
                encrypted_path,
                identity=recovery_identity,
                key_wrapper=self._key_wrapper,
            )
            protected_at = self._clock()
            provider_lock_until = (
                None if identity.kind is RunKind.USER_EXPORT else protected_at + timedelta(days=7)
            )
            object_expires_at = product_expiry or protected_at + timedelta(days=30)
            state = {
                **state,
                "wrapped_data_key": encryption.wrapped_data_key,
                "ciphertext_sha256": encryption.ciphertext_sha256,
                "ciphertext_size": encryption.ciphertext_size,
                "protected_at": protected_at.isoformat(),
                "object_lock_until": (
                    provider_lock_until.isoformat() if provider_lock_until is not None else None
                ),
                "expires_at": object_expires_at.isoformat(),
            }
            updated = await self._repository.checkpoint(
                run.id,
                worker_id,
                **claim,
                checkpoint="encrypted",
                state=state,
            )
            checkpoint = updated.checkpoint

        key = self._object_key(run)
        provider_recovery_reference = ProviderReference.b2(
            bucket=self._provider_bucket,
            key=key,
        )
        reference_prefix = "export" if identity.kind is RunKind.USER_EXPORT else "recovery"
        opaque_reference = f"{reference_prefix}_{hashlib.sha256(key.encode()).hexdigest()[:26]}"
        metadata = {
            **recovery_identity.provider_metadata(),
            "run-kind": identity.kind.value,
            "wrapped-key-reference": opaque_reference,
            "ciphertext-sha256": str(state["ciphertext_sha256"]),
            "ciphertext-size": str(state["ciphertext_size"]),
            "identity-envelope": self._provider_identity_signer.seal(
                provider="b2",
                provider_reference=provider_recovery_reference,
                tenant_id=identity.tenant_id,
                cell_id=identity.cell_id,
                operation_id=identity.operation_id,
                fence_generation=identity.fence_generation,
            ),
        }
        if product_expiry_iso is not None:
            metadata["expires-at"] = product_expiry_iso
        metadata_sha256 = hashlib.sha256(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        now = self._clock()
        raw_lock_until = state["object_lock_until"]
        lock_until = (
            datetime.fromisoformat(str(raw_lock_until)) if raw_lock_until is not None else None
        )
        protected_at = datetime.fromisoformat(str(state["protected_at"]))
        object_expires_at = datetime.fromisoformat(str(state["expires_at"]))
        if checkpoint == "encrypted":
            if remote_encrypted_checkpoint:
                receipt = await self._upload_store.head(key)
                if receipt is None:
                    restarted = await self._repository.checkpoint(
                        run.id,
                        worker_id,
                        **claim,
                        checkpoint="requested",
                        state={},
                    )
                    return await self._run_once(
                        restarted,
                        worker_id=worker_id,
                        expires_at=product_expiry,
                    )
            else:
                receipt = await self._upload_store.put_file(
                    key,
                    encrypted_path,
                    metadata=metadata,
                    retain_until=lock_until,
                )
            self._verify_remote(
                receipt,
                key=key,
                expected_size=int(state["ciphertext_size"]),
                metadata=metadata,
                lock_until=lock_until,
            )
            state = {**state, "provider_version_id": receipt.version_id or ""}
            await self._repository.checkpoint(
                run.id,
                worker_id,
                **claim,
                checkpoint="uploaded",
                state=state,
            )
        else:
            receipt = await self._upload_store.head(key)
            if receipt is None:
                raise DurabilityVerificationError("uploaded recovery object is unavailable")
            self._verify_remote(
                receipt,
                key=key,
                expected_size=int(state["ciphertext_size"]),
                metadata=metadata,
                lock_until=lock_until,
            )

        provider_reference = f"b2://{key}#{state.get('provider_version_id', '')}"
        await self._repository.record_verified_object(
            run.id,
            worker_id,
            **claim,
            value=RecoveryObjectInput(
                opaque_reference=opaque_reference,
                provider_reference=provider_reference,
                wrapped_data_key=str(state["wrapped_data_key"]),
                archive_sha256=recovery_identity.archive_sha256,
                manifest_sha256=recovery_identity.manifest_sha256,
                archive_size=recovery_identity.archive_size,
                ciphertext_sha256=str(state["ciphertext_sha256"]),
                ciphertext_size=int(state["ciphertext_size"]),
                metadata_sha256=metadata_sha256,
                object_lock_until=lock_until or protected_at,
                expires_at=object_expires_at,
            ),
            verified_at=now,
        )
        result = ExportBackupResult(
            opaque_reference=opaque_reference,
            release_reference=f"release_{run.id.replace('-', '')}",
            archive_sha256=recovery_identity.archive_sha256,
            manifest_sha256=recovery_identity.manifest_sha256,
            archive_size=recovery_identity.archive_size,
            encryption_scheme="envelope-aes-256-gcm",
            integrity_verified=True,
            quiescence_seconds=float(state["quiescence_seconds"]),
        )
        await self._repository.complete(
            run.id,
            worker_id,
            **claim,
            result=asdict(result),
            now=now,
        )
        return result

    def _stage_and_verify(
        self,
        archive: PortableArchive,
        archive_path: Path,
        manifest_path: Path,
    ) -> None:
        if archive.hosted_state_included:
            raise DurabilityVerificationError("portable archive contains hosted binding state")
        if not self._min_archive_bytes <= archive.archive_size <= self._max_archive_bytes:
            raise DurabilityVerificationError("portable archive size is implausible")
        if archive.archive_path.stat().st_size != archive.archive_size:
            raise DurabilityVerificationError("portable archive size proof differs")
        if self._sha256(archive.archive_path) != archive.archive_sha256:
            raise DurabilityVerificationError("portable archive digest differs")
        if self._sha256(archive.manifest_path) != archive.manifest_sha256:
            raise DurabilityVerificationError("portable manifest digest differs")
        self._copy_private_file(archive.archive_path, archive_path)
        self._copy_private_file(archive.manifest_path, manifest_path)
        self._ensure_scratch(archive_path)
        self._ensure_scratch(manifest_path)

    def _copy_private_file(self, source: Path, destination: Path) -> None:
        if destination.parent.resolve() != self._scratch_root:
            raise DurabilityVerificationError("scratch destination escaped bounded root")
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            os.replace(temporary, destination)
            destination.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _verify_staged_state(
        self, state: dict[str, object], archive_path: Path, manifest_path: Path
    ) -> None:
        self._ensure_scratch(archive_path)
        self._ensure_scratch(manifest_path)
        if not archive_path.is_file() or not manifest_path.is_file():
            raise DurabilityVerificationError("durable scratch checkpoint is unavailable")
        if archive_path.stat().st_size != int(state["archive_size"]):
            raise DurabilityVerificationError("staged archive size drifted")
        if self._sha256(archive_path) != state["archive_sha256"]:
            raise DurabilityVerificationError("staged archive digest drifted")
        if self._sha256(manifest_path) != state["manifest_sha256"]:
            raise DurabilityVerificationError("staged manifest digest drifted")

    def _verify_encrypted_state(self, state: dict[str, object], encrypted_path: Path) -> None:
        self._ensure_scratch(encrypted_path)
        if not encrypted_path.is_file():
            raise DurabilityVerificationError("encrypted scratch checkpoint is unavailable")
        if encrypted_path.stat().st_size != int(state["ciphertext_size"]):
            raise DurabilityVerificationError("encrypted scratch size drifted")
        if self._sha256(encrypted_path) != state["ciphertext_sha256"]:
            raise DurabilityVerificationError("encrypted scratch digest drifted")

    def _ensure_scratch(self, path: Path) -> None:
        if not path.resolve().is_relative_to(self._scratch_root):
            raise DurabilityVerificationError("scratch path escaped bounded root")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _object_key(run: RunSnapshot) -> str:
        identity = run.identity
        opaque = hashlib.sha256(
            f"{identity.tenant_id}:{identity.cell_id}:{identity.operation_id}".encode()
        ).hexdigest()
        return f"{identity.kind.value}/{opaque[:2]}/{opaque}.recovery"

    @staticmethod
    def _verify_remote(
        receipt: ProviderObjectHead,
        *,
        key: str,
        expected_size: int,
        metadata: dict[str, str],
        lock_until: datetime | None,
    ) -> None:
        retained_until = receipt.retain_until
        if retained_until is not None and retained_until.tzinfo is None:
            retained_until = retained_until.replace(tzinfo=UTC)
        retention_differs = (
            retained_until is not None
            if lock_until is None
            else retained_until is None or retained_until < lock_until
        )
        if (
            receipt.key != key
            or receipt.size != expected_size
            or receipt.metadata != metadata
            or retention_differs
        ):
            raise DurabilityVerificationError("remote provider proof differs from upload")

    def _product_expiry(self, run: RunSnapshot, value: datetime | None) -> datetime | None:
        if run.identity.kind is not RunKind.USER_EXPORT:
            if value is not None:
                raise DurabilityVerificationError(
                    "scheduled recovery backup must not have product expiry"
                )
            return None
        if value is None:
            raise DurabilityVerificationError("user export requires exact product expiry")
        normalized = (
            value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
        )
        now = self._clock()
        if normalized <= now or normalized - now > timedelta(days=30):
            raise DurabilityVerificationError("user export expiry is outside product policy")
        return normalized


class BackupScheduler:
    INTERVAL_MINUTES = 30
    WARNING_SECONDS = 45 * 60
    BLOCK_SECONDS = 60 * 60

    @staticmethod
    def slot(value: datetime) -> datetime:
        value = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        minute = 30 if value.minute >= 30 else 0
        return value.replace(minute=minute, second=0, microsecond=0)

    @classmethod
    def freshness_metrics(cls, *, age_seconds: int | None) -> dict[str, int]:
        blocked = age_seconds is None or age_seconds >= cls.BLOCK_SECONDS
        warning = age_seconds is None or age_seconds >= cls.WARNING_SECONDS
        return {
            "exomem_recovery_backup_age_seconds": age_seconds if age_seconds is not None else -1,
            "exomem_recovery_backup_warning": int(warning),
            "exomem_recovery_alpha_blocked": int(blocked),
        }


@dataclass(frozen=True, slots=True)
class BackupTarget:
    tenant_id: str
    cell_id: str
    fence_generation: int

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.cell_id or self.fence_generation < 1:
            raise ValueError("backup target identity is invalid")


@dataclass(frozen=True, slots=True)
class BackupScheduleReport:
    slot: datetime
    started: int
    completed: int
    failed: int
    deferred_busy: tuple[str, ...]
    failed_cells: tuple[str, ...]
    warning_cells: tuple[str, ...]
    alpha_blocked_cells: tuple[str, ...]
    metrics: dict[str, dict[str, int]]
    sweep_seconds: float
    peak_concurrency: int
    capacity_rpo_met: bool


class BackupTargetSource(Protocol):
    async def list_backup_targets(self) -> list[BackupTarget]: ...


class ScheduledBackupWorkflow(Protocol):
    async def run(self, run: RunSnapshot, *, worker_id: str) -> object: ...


class CentralBackupScheduler:
    """Run one globally enumerated, per-cell serialized backup for each 30-minute slot."""

    def __init__(
        self,
        *,
        repository: DurabilityRepository,
        target_source: BackupTargetSource,
        workflow: ScheduledBackupWorkflow,
        worker_id: str,
        run_kind: RunKind = RunKind.VAULT_BACKUP,
        max_concurrency: int = 4,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not worker_id:
            raise ValueError("central backup worker identity is required")
        self._repository = repository
        self._target_source = target_source
        self._workflow = workflow
        self._worker_id = worker_id
        if not 1 <= max_concurrency <= 32:
            raise ValueError("central backup concurrency must be between 1 and 32")
        self._max_concurrency = max_concurrency
        self._monotonic = monotonic
        if run_kind not in {RunKind.VAULT_BACKUP, RunKind.DATABASE_BACKUP}:
            raise ValueError("central scheduler supports backup run kinds only")
        self._run_kind = run_kind

    async def run_once(self, *, now: datetime | None = None) -> BackupScheduleReport:
        sweep_started = self._monotonic()
        checked_at = now or datetime.now(UTC)
        slot = BackupScheduler.slot(checked_at)
        targets = sorted(
            await self._target_source.list_backup_targets(),
            key=lambda value: (value.tenant_id, value.cell_id),
        )
        identities = {(target.tenant_id, target.cell_id) for target in targets}
        if len(identities) != len(targets):
            raise DurabilityVerificationError("backup target enumeration contains duplicates")

        started = 0
        completed = 0
        failed_cells: list[str] = []
        deferred_busy: list[str] = []
        peak_concurrency = 0
        active = 0
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def process(target: BackupTarget) -> None:
            nonlocal started, completed, peak_concurrency, active
            async with semaphore:
                identity = RunIdentity(
                    kind=self._run_kind,
                    operation_id=self._operation_id(target, slot),
                    tenant_id=target.tenant_id,
                    cell_id=target.cell_id,
                    fence_generation=target.fence_generation,
                    scheduled_for=slot,
                )
                try:
                    run = await self._repository.begin(identity)
                except ActiveDurabilityRun:
                    deferred_busy.append(target.cell_id)
                    return
                if run.status is DurabilityRunState.COMPLETE:
                    return
                try:
                    run = await self._repository.claim(run.id, self._worker_id)
                except (ActiveDurabilityRun, DurabilityClaimConflict):
                    deferred_busy.append(target.cell_id)
                    return
                started += 1
                active += 1
                peak_concurrency = max(peak_concurrency, active)
                stop_heartbeat = asyncio.Event()
                heartbeat = asyncio.create_task(
                    self._renew_claim(run, stop_heartbeat),
                    name=f"central-backup-claim-{run.id}",
                )
                try:
                    await self._workflow.run(run, worker_id=self._worker_id)
                    completed += 1
                except Exception:  # noqa: BLE001 - report one cell and continue the global sweep
                    failed_cells.append(target.cell_id)
                    await self._release_failed_claim(run)
                finally:
                    active -= 1
                    stop_heartbeat.set()
                    await heartbeat

        await asyncio.gather(*(process(target) for target in targets))

        metrics: dict[str, dict[str, int]] = {}
        warning_cells: list[str] = []
        blocked_cells: list[str] = []
        for target in targets:
            freshness = await self._repository.backup_freshness(
                target.cell_id,
                kind=self._run_kind,
                now=checked_at,
            )
            metrics[target.cell_id] = BackupScheduler.freshness_metrics(
                age_seconds=freshness.age_seconds
            )
            if freshness.warning:
                warning_cells.append(target.cell_id)
            if freshness.alpha_blocked:
                blocked_cells.append(target.cell_id)

        sweep_seconds = max(0.0, self._monotonic() - sweep_started)
        capacity_rpo_met = (
            sweep_seconds < BackupScheduler.INTERVAL_MINUTES * 60 and not failed_cells
        )
        return BackupScheduleReport(
            slot=slot,
            started=started,
            completed=completed,
            failed=len(failed_cells),
            deferred_busy=tuple(sorted(deferred_busy)),
            failed_cells=tuple(sorted(failed_cells)),
            warning_cells=tuple(warning_cells),
            alpha_blocked_cells=tuple(blocked_cells),
            metrics=metrics,
            sweep_seconds=sweep_seconds,
            peak_concurrency=peak_concurrency,
            capacity_rpo_met=capacity_rpo_met,
        )

    async def _renew_claim(self, run: RunSnapshot, stop: asyncio.Event) -> None:
        interval = max(0.05, self._repository.lease_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    await self._repository.renew_claim(
                        run.id,
                        self._worker_id,
                        claim_token=run.claim_token,
                        claim_generation=run.claim_generation,
                    )
                except DurabilityClaimConflict:
                    return

    async def _release_failed_claim(self, run: RunSnapshot) -> None:
        latest = await self._repository.get(run.id)
        if latest is None or latest.status is not DurabilityRunState.CLAIMED:
            return
        try:
            await self._repository.mark_pending(
                run.id,
                self._worker_id,
                claim_token=run.claim_token,
                claim_generation=run.claim_generation,
                checkpoint=latest.checkpoint,
                state=latest.state,
            )
        except DurabilityClaimConflict:
            return

    def _operation_id(self, target: BackupTarget, slot: datetime) -> str:
        digest = hashlib.sha256(
            f"{target.tenant_id}:{target.cell_id}:{slot.isoformat()}".encode()
        ).hexdigest()[:24]
        return f"{self._run_kind.value}-{slot:%Y%m%dT%H%MZ}-{digest}"


class RestoreVerificationError(DurabilityVerificationError):
    pass


@dataclass(frozen=True, slots=True)
class PortableArchiveInspection:
    manifest_sha256: str
    source_cell_id: str
    hosted_state_included: bool
    path_safe: bool
    schema_compatible: bool
    release_compatible: bool


class RestoreRecoveryStore(Protocol):
    async def head(self, key: str) -> ProviderObjectHead | None: ...

    async def download_file(self, key: str, destination: Path) -> None: ...


class OfflineRestoreRuntime(Protocol):
    async def inspect_portable_archive(self, path: Path) -> PortableArchiveInspection: ...

    async def stop_candidate(self, candidate_cell_id: str) -> None: ...

    async def offline_restore(
        self,
        candidate_cell_id: str,
        archive_path: Path,
        *,
        helper_version: str,
        release_version: str,
        operation_id: str,
        fence_generation: int,
        source_cell_id: str,
        archive_sha256: str,
        artifact_reference: str,
    ) -> None: ...

    async def authenticated_readiness(self, candidate_cell_id: str) -> bool: ...

    async def product_checks(self, candidate_cell_id: str) -> dict[str, bool]: ...

    async def finalize_candidate(self, candidate_cell_id: str) -> dict[str, bool]: ...


class RestoreWorkflow:
    """Decrypt and publish a provider recovery object into a stopped new candidate."""

    HELPER_VERSION = "1"
    REQUIRED_PRODUCT_CHECKS = frozenset(
        {"capture", "recall", "review", "export", "restart", "candidateIdentity"}
    )

    def __init__(
        self,
        *,
        repository: DurabilityRepository,
        restore_store: RestoreRecoveryStore,
        runtime: OfflineRestoreRuntime,
        cipher: ChunkedArchiveCipher,
        key_wrapper: DataKeyWrapper,
        provider_identity_verifier: ProviderIdentityAuthenticator,
        provider_bucket: str,
        scratch_root: Path,
        release_version: str,
    ) -> None:
        self._repository = repository
        self._restore_store = restore_store
        self._runtime = runtime
        self._cipher = cipher
        self._key_wrapper = key_wrapper
        self._provider_identity_verifier = provider_identity_verifier
        self._provider_bucket = provider_bucket
        self._scratch_root = scratch_root.resolve()
        self._release_version = release_version

    async def run(
        self,
        run: RunSnapshot,
        *,
        worker_id: str,
        source_reference: str,
        expected_source_cell_id: str | None = None,
        expected_archive_sha256: str | None = None,
        expected_manifest_sha256: str | None = None,
        expected_archive_size: int | None = None,
    ) -> dict[str, object]:
        if run.identity.kind is not RunKind.RESTORE:
            raise RestoreVerificationError("restore workflow requires a restore run")
        source = await self._repository.get_recovery_object(source_reference)
        if source is None or source.deleted_at is not None:
            raise RestoreVerificationError("recovery object is unavailable")
        if source.wrapped_data_key is None:
            raise RestoreVerificationError("recovery object key was destroyed")
        if source.tenant_id != run.identity.tenant_id:
            raise RestoreVerificationError("recovery object belongs to another tenant")
        if source.kind is RunKind.USER_EXPORT and source.expires_at <= datetime.now(UTC):
            raise RestoreVerificationError("user export restore source has expired")
        expected_values = (
            (expected_source_cell_id, source.cell_id),
            (expected_archive_sha256, source.archive_sha256),
            (expected_manifest_sha256, source.manifest_sha256),
            (expected_archive_size, source.archive_size),
        )
        if any(expected is not None and expected != actual for expected, actual in expected_values):
            raise RestoreVerificationError("restore request differs from recovery proof")
        if source.cell_id == run.identity.cell_id:
            raise RestoreVerificationError("restore must publish into a new candidate identity")
        if run.checkpoint == "candidate-published":
            return await self._verify_and_complete_candidate(
                run,
                worker_id=worker_id,
            )
        key, expected_version = self._provider_location(source.provider_reference)
        head = await self._restore_store.head(key)
        if head is None:
            raise RestoreVerificationError("provider object is unavailable")
        expected_metadata = self._provider_metadata(source, key=key, actual=head.metadata)
        metadata_sha256 = hashlib.sha256(
            json.dumps(expected_metadata, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            head.size != source.ciphertext_size
            or (expected_version is not None and head.version_id != expected_version)
            or head.metadata != expected_metadata
            or metadata_sha256 != source.metadata_sha256
        ):
            raise RestoreVerificationError("provider object proof differs from recovery ledger")

        self._scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._scratch_root.chmod(0o700)
        encrypted_path = self._scratch_root / f"{run.id}.restore.encrypted"
        archive_path = self._scratch_root / f"{run.id}.restore.archive"
        claim = {
            "claim_token": run.claim_token,
            "claim_generation": run.claim_generation,
        }
        try:
            await self._restore_store.download_file(key, encrypted_path)
            encrypted_path.chmod(0o600)
            if (
                encrypted_path.stat().st_size != source.ciphertext_size
                or await asyncio.to_thread(ExportBackupWorkflow._sha256, encrypted_path)
                != source.ciphertext_sha256
            ):
                raise RestoreVerificationError("downloaded ciphertext did not verify")
            source_identity = RecoveryIdentity(
                tenant_id=source.tenant_id,
                cell_id=source.cell_id,
                operation_id=source.operation_id,
                fence_generation=source.fence_generation,
                archive_sha256=source.archive_sha256,
                manifest_sha256=source.manifest_sha256,
                archive_size=source.archive_size,
            )
            await asyncio.to_thread(
                self._cipher.decrypt,
                encrypted_path,
                archive_path,
                identity=source_identity,
                wrapped_data_key=source.wrapped_data_key,
                key_wrapper=self._key_wrapper,
            )
            inspection = await self._runtime.inspect_portable_archive(archive_path)
            self._verify_inspection(inspection, source)
            await self._repository.checkpoint(
                run.id,
                worker_id,
                **claim,
                checkpoint="archive-validated",
                state={
                    "source_reference_digest": hashlib.sha256(
                        source_reference.encode()
                    ).hexdigest(),
                    "archive_sha256": source.archive_sha256,
                },
            )
            await self._runtime.stop_candidate(run.identity.cell_id)
            await self._runtime.offline_restore(
                run.identity.cell_id,
                archive_path,
                helper_version=self.HELPER_VERSION,
                release_version=self._release_version,
                operation_id=(f"{run.identity.operation_id}-baseline-{run.claim_generation}"),
                fence_generation=run.identity.fence_generation,
                source_cell_id=source.cell_id,
                archive_sha256=source.archive_sha256,
                artifact_reference=source_reference,
            )
            try:
                checks = await self._runtime.product_checks(run.identity.cell_id)
            finally:
                try:
                    await self._runtime.stop_candidate(run.identity.cell_id)
                finally:
                    await self._runtime.offline_restore(
                        run.identity.cell_id,
                        archive_path,
                        helper_version=self.HELPER_VERSION,
                        release_version=self._release_version,
                        operation_id=(
                            f"{run.identity.operation_id}-cleanup-{run.claim_generation}"
                        ),
                        fence_generation=run.identity.fence_generation,
                        source_cell_id=source.cell_id,
                        archive_sha256=source.archive_sha256,
                        artifact_reference=source_reference,
                    )
            checks.update(await self._runtime.finalize_candidate(run.identity.cell_id))
            if set(checks) != self.REQUIRED_PRODUCT_CHECKS or not all(checks.values()):
                raise RestoreVerificationError("candidate product checks failed")
            await self._repository.checkpoint(
                run.id,
                worker_id,
                **claim,
                checkpoint="candidate-published",
                state={"archive_sha256": source.archive_sha256},
            )
            return await self._verify_and_complete_candidate(
                run,
                worker_id=worker_id,
            )
        finally:
            encrypted_path.unlink(missing_ok=True)
            archive_path.unlink(missing_ok=True)

    async def _verify_and_complete_candidate(
        self,
        run: RunSnapshot,
        *,
        worker_id: str,
    ) -> dict[str, object]:
        if run.claim_token is None:
            raise RestoreVerificationError("restore run has no active claim")
        if not await self._runtime.authenticated_readiness(run.identity.cell_id):
            raise RestoreVerificationError("candidate authenticated readiness failed")
        result: dict[str, object] = {
            "restored": True,
            "candidateCellId": run.identity.cell_id,
        }
        await self._repository.complete(
            run.id,
            worker_id,
            claim_token=run.claim_token,
            claim_generation=run.claim_generation,
            result=result,
        )
        return result

    @staticmethod
    def _provider_location(reference: str) -> tuple[str, str | None]:
        if not reference.startswith("b2://") or "#" not in reference:
            raise RestoreVerificationError("provider reference is invalid")
        key, version = reference[5:].split("#", 1)
        if (
            not key
            or key.startswith("/")
            or any(part in {"", ".", ".."} for part in key.split("/"))
        ):
            raise RestoreVerificationError("provider reference is invalid")
        return key, version or None

    def _provider_metadata(
        self,
        source,
        *,
        key: str,
        actual: dict[str, str],
    ) -> dict[str, str]:
        metadata = {
            "tenant-id": source.tenant_id,
            "cell-id": source.cell_id,
            "operation-id": source.operation_id,
            "fence-generation": str(source.fence_generation),
            "archive-sha256": source.archive_sha256,
            "manifest-sha256": source.manifest_sha256,
            "archive-size": str(source.archive_size),
            "run-kind": source.kind.value,
            "ciphertext-sha256": source.ciphertext_sha256,
            "ciphertext-size": str(source.ciphertext_size),
        }
        if source.kind is RunKind.USER_EXPORT:
            metadata["expires-at"] = source.expires_at.isoformat()
        envelope = actual.get("identity-envelope")
        if envelope is None:
            raise RestoreVerificationError("provider object identity is unauthenticated")
        self._provider_identity_verifier.authenticate(
            envelope,
            provider="b2",
            provider_reference=ProviderReference.b2(bucket=self._provider_bucket, key=key),
            tenant_id=source.tenant_id,
            cell_id=source.cell_id,
            operation_id=source.operation_id,
            fence_generation=source.fence_generation,
        )
        metadata["identity-envelope"] = envelope
        return metadata

    def _verify_inspection(self, inspection: PortableArchiveInspection, source) -> None:
        if inspection.hosted_state_included:
            raise RestoreVerificationError("portable archive contains forbidden hosted state")
        if inspection.source_cell_id != source.cell_id:
            raise RestoreVerificationError("portable archive source identity differs")
        if inspection.manifest_sha256 != source.manifest_sha256:
            raise RestoreVerificationError("portable manifest digest differs")
        if not inspection.path_safe:
            raise RestoreVerificationError("portable archive contains an unsafe path")
        if not inspection.schema_compatible or not inspection.release_compatible:
            raise RestoreVerificationError("portable archive is incompatible")


class ExportObjectUnavailable(RuntimeError):
    pass


class ExportRestoreCapability(Protocol):
    async def head(self, key: str) -> ProviderObjectHead | None: ...

    async def download_file(self, key: str, destination: Path) -> None: ...


class ExportDeliveryCapability(Protocol):
    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        metadata: dict[str, str],
        retain_until: datetime | None,
    ) -> ProviderObjectHead: ...

    async def presigned_download(self, key: str, *, ttl_seconds: int) -> str: ...


class ExportDeletionCapability(Protocol):
    async def head(self, key: str) -> ProviderObjectHead | None: ...

    async def list_page(
        self, *, prefix: str, continuation_token: str | None = None
    ) -> tuple[list[str], str | None]: ...

    async def delete(self, key: str, *, version_id: str | None = None) -> None: ...

    async def absent(self, key: str) -> bool: ...


class ExportGarbageCollector:
    """One-shot GC using only ledger and delete/list/read-metadata capability."""

    def __init__(
        self,
        *,
        repository: DurabilityRepository,
        deletion_store: ExportDeletionCapability,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._deletion_store = deletion_store
        self._clock = clock

    async def run_once(
        self,
        *,
        export_limit: int = 100,
        delivery_limit: int = 1000,
    ) -> dict[str, int]:
        records = await self._repository.expired_export_objects(
            now=self._clock(),
            limit=export_limit,
        )
        for record in records:
            await self._destroy_export(record)
        deliveries = await self._destroy_expired_deliveries(limit=delivery_limit)
        return {"exportsDeleted": len(records), "deliveriesDeleted": deliveries}

    async def _destroy_export(self, record) -> None:
        key, version_id = ExportObjectService._provider_location(record.provider_reference)
        if not await self._deletion_store.absent(key):
            await self._deletion_store.delete(key, version_id=version_id)
        if not await self._deletion_store.absent(key):
            raise ExportObjectUnavailable("export deletion lacks provider absence proof")
        await self._repository.mark_recovery_object_deleted(
            record.opaque_reference,
            tenant_id=record.tenant_id,
            deleted_at=self._clock(),
        )
        erased = await self._repository.destroy_recovery_wrapped_key(
            record.opaque_reference,
            tenant_id=record.tenant_id,
            destroyed_at=self._clock(),
        )
        if erased.wrapped_data_key is not None or erased.key_destroyed_at is None:
            raise ExportObjectUnavailable("export wrapped-key destruction lacks proof")

    async def _destroy_expired_deliveries(self, *, limit: int) -> int:
        if not 1 <= limit <= 10_000:
            raise ValueError("delivery expiry batch size is invalid")
        checked_at = self._clock()
        continuation: str | None = None
        seen_tokens: set[str] = set()
        keys_to_check: list[str] = []
        while True:
            keys, next_token = await self._deletion_store.list_page(
                prefix="user-export-delivery/",
                continuation_token=continuation,
            )
            keys_to_check.extend(keys)
            if next_token is None:
                break
            if next_token in seen_tokens:
                raise ExportObjectUnavailable("delivery listing cursor did not advance")
            seen_tokens.add(next_token)
            continuation = next_token
        deleted = 0
        for key in keys_to_check:
            if deleted >= limit:
                break
            head = await self._deletion_store.head(key)
            if head is None:
                continue
            raw_expiry = head.metadata.get("expires-at")
            try:
                expires_at = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
            except ValueError as error:
                raise ExportObjectUnavailable("delivery expiry metadata is invalid") from error
            if expires_at.tzinfo is None:
                raise ExportObjectUnavailable("delivery expiry metadata is invalid")
            if expires_at > checked_at:
                continue
            await self._deletion_store.delete(key, version_id=head.version_id)
            if not await self._deletion_store.absent(key):
                raise ExportObjectUnavailable("delivery deletion lacks provider absence proof")
            deleted += 1
        return deleted


class ExportObjectService:
    """Opaque release/download/delete facade over short-lived provider capabilities."""

    def __init__(
        self,
        *,
        repository: DurabilityRepository,
        restore_store: ExportRestoreCapability,
        delivery_store: ExportDeliveryCapability,
        deletion_store: ExportDeletionCapability,
        cipher: ChunkedArchiveCipher,
        key_wrapper: DataKeyWrapper,
        scratch_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._restore_store = restore_store
        self._delivery_store = delivery_store
        self._deletion_store = deletion_store
        self._cipher = cipher
        self._key_wrapper = key_wrapper
        self._scratch_root = scratch_root.resolve()
        self._clock = clock

    async def release(self, opaque_reference: str, *, tenant_id: str) -> dict[str, bool]:
        record = await self._repository.get_recovery_object_by_release_reference(opaque_reference)
        if record is None:
            record = await self._repository.get_recovery_object(opaque_reference)
        self._require_available(record, tenant_id=tenant_id)
        # Source routing/checkpoint release happens before remote upload; this
        # action is an idempotent product acknowledgement, never a local path.
        return {"released": True}

    async def download(
        self,
        opaque_reference: str,
        *,
        tenant_id: str,
        ttl_seconds: int,
    ) -> dict[str, str]:
        record = await self._available(opaque_reference, tenant_id=tenant_id)
        key, _ = self._provider_location(record.provider_reference)
        head = await self._restore_store.head(key)
        if head is None or head.size != record.ciphertext_size:
            raise ExportObjectUnavailable("export object is unavailable")
        expires = self._clock() + timedelta(seconds=ttl_seconds)
        self._scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._scratch_root.chmod(0o700)
        encrypted_path = self._scratch_root / f"{record.id}.delivery.encrypted"
        archive_path = self._scratch_root / f"{record.id}.delivery.archive"
        try:
            await self._restore_store.download_file(key, encrypted_path)
            encrypted_path.chmod(0o600)
            if (
                encrypted_path.stat().st_size != record.ciphertext_size
                or await asyncio.to_thread(self._sha256, encrypted_path) != record.ciphertext_sha256
            ):
                raise ExportObjectUnavailable("export ciphertext did not verify")
            identity = RecoveryIdentity(
                tenant_id=record.tenant_id,
                cell_id=record.cell_id,
                operation_id=record.operation_id,
                fence_generation=record.fence_generation,
                archive_sha256=record.archive_sha256,
                manifest_sha256=record.manifest_sha256,
                archive_size=record.archive_size,
            )
            await asyncio.to_thread(
                self._cipher.decrypt,
                encrypted_path,
                archive_path,
                identity=identity,
                wrapped_data_key=record.wrapped_data_key,
                key_wrapper=self._key_wrapper,
            )
            delivery_digest = hashlib.sha256(
                f"{record.id}:{expires.isoformat()}:{secrets.token_urlsafe(32)}".encode()
            ).hexdigest()
            delivery_key = f"user-export-delivery/{delivery_digest[:2]}/{delivery_digest}.portable"
            metadata = {
                "expires-at": expires.isoformat().replace("+00:00", "Z"),
                "archive-sha256": record.archive_sha256,
                "archive-size": str(record.archive_size),
                "source-reference-sha256": hashlib.sha256(
                    record.opaque_reference.encode()
                ).hexdigest(),
            }
            receipt = await self._delivery_store.put_file(
                delivery_key,
                archive_path,
                metadata=metadata,
                retain_until=None,
            )
            if (
                receipt.key != delivery_key
                or receipt.size != record.archive_size
                or receipt.metadata != metadata
                or receipt.retain_until is not None
            ):
                raise ExportObjectUnavailable("portable export delivery proof differs")
            url = await self._delivery_store.presigned_download(
                delivery_key, ttl_seconds=ttl_seconds
            )
            return {"url": url, "expiresAt": expires.isoformat().replace("+00:00", "Z")}
        finally:
            encrypted_path.unlink(missing_ok=True)
            archive_path.unlink(missing_ok=True)

    async def delete(self, opaque_reference: str, *, tenant_id: str) -> dict[str, bool]:
        record = await self._available(opaque_reference, tenant_id=tenant_id)
        await self._destroy(record)
        return {"objectDestroyed": True}

    async def delete_expired(self, *, limit: int = 100) -> int:
        """Delete one bounded batch whose product TTL has elapsed."""

        records = await self._repository.expired_export_objects(
            now=self._clock(),
            limit=limit,
        )
        for record in records:
            await self._destroy(record)
        return len(records)

    async def delete_expired_deliveries(self, *, limit: int = 1000) -> int:
        """Delete expired plaintext deliveries and prove each provider absence.

        Delivery objects are deliberately not part of the durable ledger: they
        exist only for one short-lived presigned URL. Their signed expiry is
        provider metadata, and this bounded, paginated sweep is the active
        cleanup path. A one-day bucket lifecycle is the independent backstop.
        """

        if not 1 <= limit <= 10_000:
            raise ValueError("delivery expiry batch size is invalid")
        checked_at = self._clock()
        continuation: str | None = None
        seen_tokens: set[str] = set()
        keys_to_check: list[str] = []
        while True:
            keys, next_token = await self._deletion_store.list_page(
                prefix="user-export-delivery/",
                continuation_token=continuation,
            )
            keys_to_check.extend(keys)
            if next_token is None:
                break
            if next_token in seen_tokens:
                raise ExportObjectUnavailable("delivery listing cursor did not advance")
            seen_tokens.add(next_token)
            continuation = next_token
        deleted = 0
        for key in keys_to_check:
            if deleted >= limit:
                break
            head = await self._deletion_store.head(key)
            if head is None:
                continue
            raw_expiry = head.metadata.get("expires-at")
            try:
                expires_at = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
            except ValueError as error:
                raise ExportObjectUnavailable("delivery expiry metadata is invalid") from error
            if expires_at.tzinfo is None:
                raise ExportObjectUnavailable("delivery expiry metadata is invalid")
            if expires_at > checked_at:
                continue
            await self._deletion_store.delete(key, version_id=head.version_id)
            if not await self._deletion_store.absent(key):
                raise ExportObjectUnavailable("delivery deletion lacks provider absence proof")
            deleted += 1
        return deleted

    async def _destroy(self, record) -> None:
        key, version_id = self._provider_location(record.provider_reference)
        if not await self._deletion_store.absent(key):
            await self._deletion_store.delete(key, version_id=version_id)
        if not await self._deletion_store.absent(key):
            raise ExportObjectUnavailable("export deletion lacks provider absence proof")
        await self._repository.mark_recovery_object_deleted(
            record.opaque_reference,
            tenant_id=record.tenant_id,
            deleted_at=self._clock(),
        )
        erased = await self._repository.destroy_recovery_wrapped_key(
            record.opaque_reference,
            tenant_id=record.tenant_id,
            destroyed_at=self._clock(),
        )
        if erased.wrapped_data_key is not None or erased.key_destroyed_at is None:
            raise ExportObjectUnavailable("export wrapped-key destruction lacks proof")

    async def _available(self, opaque_reference: str, *, tenant_id: str):
        record = await self._repository.get_recovery_object(opaque_reference)
        self._require_available(record, tenant_id=tenant_id)
        return record

    def _require_available(self, record, *, tenant_id: str) -> None:
        now = self._clock()
        if (
            record is None
            or record.kind is not RunKind.USER_EXPORT
            or record.tenant_id != tenant_id
            or record.deleted_at is not None
            or record.wrapped_data_key is None
            or record.expires_at <= now
        ):
            raise ExportObjectUnavailable("export object is unavailable")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _provider_location(reference: str) -> tuple[str, str | None]:
        if not reference.startswith("b2://"):
            raise ExportObjectUnavailable("export object is unavailable")
        raw = reference[5:]
        key, separator, version = raw.partition("#")
        if (
            not key
            or key.startswith("/")
            or any(part in {"", ".", ".."} for part in key.split("/"))
        ):
            raise ExportObjectUnavailable("export object is unavailable")
        return key, version if separator and version else None
