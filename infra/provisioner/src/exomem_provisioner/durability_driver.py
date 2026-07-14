"""Provisioner action integration for restart-safe durability workflows."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .deletion import DeletionVerificationError
from .driver import (
    DriverFinal,
    DriverPending,
    DriverRetryable,
    DriverTerminal,
    EffectContext,
    ProvisionerDriver,
)
from .durability import (
    DurabilityVerificationError,
    ExportBackupResult,
    ExportObjectUnavailable,
)
from .durability_crypto import ArchiveAuthenticationError
from .durability_repository import (
    ActiveDurabilityRun,
    DurabilityClaimConflict,
    DurabilityRepository,
    RunIdentity,
    RunKind,
    RunSnapshot,
)
from .durability_store import ProviderObjectConflict
from .kubernetes_restore import RestoreJobFailed
from .models import DurabilityRunState


class ExportWorkflowPort(Protocol):
    async def run(
        self, run: RunSnapshot, *, worker_id: str, expires_at: datetime | None = None
    ) -> ExportBackupResult: ...


class RestoreWorkflowPort(Protocol):
    async def run(self, run: RunSnapshot, **arguments: Any) -> dict[str, object]: ...


class ExportObjectPort(Protocol):
    async def release(self, reference: str, *, tenant_id: str) -> dict[str, bool]: ...

    async def download(
        self, reference: str, *, tenant_id: str, ttl_seconds: int
    ) -> dict[str, str]: ...

    async def delete(self, reference: str, *, tenant_id: str) -> dict[str, bool]: ...


class OrderedDeletionPort(Protocol):
    async def discard_candidate(self, context: EffectContext) -> DriverPending | DriverFinal: ...

    async def destroy_tenant(self, context: EffectContext) -> DriverPending | DriverFinal: ...


class DurabilityActionDriver:
    """Route hosted durability actions through the durable run ledger."""

    _RUN_ACTIONS = {"export": RunKind.USER_EXPORT, "restore": RunKind.RESTORE}

    def __init__(
        self,
        *,
        delegate: ProvisionerDriver,
        repository: DurabilityRepository,
        export_workflow: ExportWorkflowPort,
        restore_workflow: RestoreWorkflowPort,
        object_service: ExportObjectPort,
        deletion_workflow: OrderedDeletionPort | None = None,
    ) -> None:
        self._delegate = delegate
        self._repository = repository
        self._export_workflow = export_workflow
        self._restore_workflow = restore_workflow
        self._object_service = object_service
        self._deletion_workflow = deletion_workflow

    async def observed_fence(self, tenant_id: str) -> int:
        return await self._delegate.observed_fence(tenant_id)

    async def execute(
        self,
        action: str,
        request: dict[str, Any],
        context: EffectContext,
    ) -> DriverPending | DriverFinal:
        try:
            if action in self._RUN_ACTIONS:
                return await self._run_workflow(action, request, context)
            if action == "export-release":
                await self._object_service.release(
                    self._required_text(request, "releaseRef"), tenant_id=context.tenant_id
                )
                return DriverFinal(result={})
            if action == "export-download":
                result = await self._object_service.download(
                    self._required_text(request, "exportRef"),
                    tenant_id=context.tenant_id,
                    ttl_seconds=900,
                )
                return DriverFinal(result=result)
            if action == "export-delete":
                result = await self._object_service.delete(
                    self._required_text(request, "exportRef"), tenant_id=context.tenant_id
                )
                return DriverFinal(result=result)
            if action == "discard" and self._deletion_workflow is not None:
                return await self._deletion_workflow.discard_candidate(context)
            if action == "destroy" and self._deletion_workflow is not None:
                return await self._deletion_workflow.destroy_tenant(context)
        except (
            ArchiveAuthenticationError,
            DurabilityVerificationError,
            ExportObjectUnavailable,
            ProviderObjectConflict,
            DeletionVerificationError,
            KeyError,
            ValueError,
        ) as error:
            raise DriverTerminal("PROVISIONER_DURABILITY_VERIFICATION_FAILED") from error
        except (OSError, RestoreJobFailed, TimeoutError) as error:
            raise DriverRetryable("PROVISIONER_DURABILITY_PROVIDER_RETRY") from error
        return await self._delegate.execute(action, request, context)

    async def _run_workflow(
        self,
        action: str,
        request: dict[str, Any],
        context: EffectContext,
    ) -> DriverPending | DriverFinal:
        if context.cell_id is None:
            raise DriverTerminal("PROVISIONER_DURABILITY_CELL_REQUIRED")
        identity = RunIdentity(
            kind=self._RUN_ACTIONS[action],
            operation_id=context.provider_operation_id,
            tenant_id=context.tenant_id,
            cell_id=context.cell_id,
            fence_generation=context.fence_generation,
            scheduled_for=self._stable_schedule(context.provider_operation_id),
        )
        try:
            run = await self._repository.begin(identity)
            if run.status is DurabilityRunState.COMPLETE:
                result = await self._repository.load_result(run.id)
                if result is None:
                    raise DriverTerminal("PROVISIONER_DURABILITY_RESULT_MISSING")
                return self._final(action, result)
            worker_id = f"durability-{context.operation_id}"
            run = await self._repository.claim(run.id, worker_id)
        except (ActiveDurabilityRun, DurabilityClaimConflict):
            return DriverPending(checkpoint="durability-serialized", retry_after_seconds=15)

        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_claim(run, worker_id, stop_heartbeat),
            name=f"durability-claim-{run.id}",
        )
        try:
            try:
                if action == "export":
                    expiry = self._required_datetime(request, "expiresAt")
                    result = await self._export_workflow.run(
                        run, worker_id=worker_id, expires_at=expiry
                    )
                    return self._final(action, self._export_result(result))

                await self._restore_workflow.run(
                    run,
                    worker_id=worker_id,
                    source_reference=self._required_text(request, "restoreRef"),
                    expected_source_cell_id=self._required_text(request, "sourceCellId"),
                    expected_archive_sha256=self._required_text(request, "archiveSha256"),
                    expected_manifest_sha256=self._required_text(request, "manifestSha256"),
                    expected_archive_size=self._required_positive_int(request, "archiveSize"),
                )
                return DriverFinal(result={})
            except (
                ArchiveAuthenticationError,
                DurabilityVerificationError,
                ExportObjectUnavailable,
                ProviderObjectConflict,
                KeyError,
                ValueError,
            ):
                await self._fail_run(run, worker_id)
                raise
            except Exception:
                await self._release_run(run, worker_id)
                raise
        finally:
            stop_heartbeat.set()
            await heartbeat

    async def _fail_run(self, run: RunSnapshot, worker_id: str) -> None:
        try:
            await self._repository.fail(
                run.id,
                worker_id,
                claim_token=run.claim_token,
                claim_generation=run.claim_generation,
                code="PROVISIONER_DURABILITY_VERIFICATION_FAILED",
            )
        except DurabilityClaimConflict:
            return

    async def _release_run(self, run: RunSnapshot, worker_id: str) -> None:
        latest = await self._repository.get(run.id)
        if latest is None or latest.status is not DurabilityRunState.CLAIMED:
            return
        try:
            await self._repository.mark_pending(
                run.id,
                worker_id,
                claim_token=run.claim_token,
                claim_generation=run.claim_generation,
                checkpoint=latest.checkpoint,
                state=latest.state,
            )
        except DurabilityClaimConflict:
            return

    async def _renew_claim(
        self,
        run: RunSnapshot,
        worker_id: str,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.05, self._repository.lease_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    await self._repository.renew_claim(
                        run.id,
                        worker_id,
                        claim_token=run.claim_token,
                        claim_generation=run.claim_generation,
                    )
                except DurabilityClaimConflict:
                    return

    @staticmethod
    def _export_result(result: ExportBackupResult) -> dict[str, object]:
        return {
            "opaque_reference": result.opaque_reference,
            "release_reference": result.release_reference,
            "archive_sha256": result.archive_sha256,
            "manifest_sha256": result.manifest_sha256,
            "archive_size": result.archive_size,
            "encryption_scheme": result.encryption_scheme,
            "integrity_verified": result.integrity_verified,
        }

    @staticmethod
    def _final(action: str, result: dict[str, Any]) -> DriverFinal:
        if action == "restore":
            return DriverFinal(result={})
        required = {
            "opaque_reference",
            "release_reference",
            "archive_sha256",
            "manifest_sha256",
            "archive_size",
            "encryption_scheme",
            "integrity_verified",
        }
        if not required.issubset(result):
            raise DriverTerminal("PROVISIONER_DURABILITY_RESULT_INVALID")
        return DriverFinal(
            result={
                "exportRef": result["opaque_reference"],
                "releaseRef": result["release_reference"],
                "archiveSha256": result["archive_sha256"],
                "manifestSha256": result["manifest_sha256"],
                "archiveSize": result["archive_size"],
                "encryptionScheme": result["encryption_scheme"],
                "integrityVerified": result["integrity_verified"],
            }
        )

    @staticmethod
    def _stable_schedule(operation_id: str) -> datetime:
        seconds = int.from_bytes(hashlib.sha256(operation_id.encode()).digest()[:4], "big")
        return datetime(2000, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)

    @staticmethod
    def _required_text(request: dict[str, Any], field: str) -> str:
        value = request.get(field)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} is required")
        return value

    @staticmethod
    def _required_positive_int(request: dict[str, Any], field: str) -> int:
        value = request.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{field} must be positive")
        return value

    @staticmethod
    def _required_datetime(request: dict[str, Any], field: str) -> datetime:
        value = DurabilityActionDriver._required_text(request, field)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{field} must be RFC3339") from error
        if parsed.tzinfo is None:
            raise ValueError(f"{field} must include timezone")
        return parsed.astimezone(UTC)
