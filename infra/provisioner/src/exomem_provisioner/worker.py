"""Restart-safe database-backed operation worker."""

from __future__ import annotations

import asyncio
from datetime import datetime

from .driver import (
    DriverFinal,
    DriverPending,
    DriverRetryable,
    DriverTerminal,
    EffectContext,
    LostAcknowledgement,
    ProvisionerDriver,
)
from .repository import ClaimConflict, OperationRepository, OperationSnapshot, StaleFence


class ProvisionerWorker:
    def __init__(
        self,
        repository: OperationRepository,
        driver: ProvisionerDriver,
        *,
        worker_id: str,
    ) -> None:
        self._repository = repository
        self._driver = driver
        self._worker_id = worker_id

    async def run_once(self, *, now: datetime | None = None) -> bool:
        operation = await self._repository.claim_next(self._worker_id, now=now)
        if operation is None:
            return False
        if operation.claim_token is None:
            raise ClaimConflict("claimed operation has no claim token")
        stop_heartbeat = asyncio.Event()
        claim_lost = asyncio.Event()
        heartbeat = (
            asyncio.create_task(
                self._renew_claim(operation, stop_heartbeat, claim_lost),
                name=f"provisioner-claim-{operation.id}",
            )
            if now is None
            else None
        )
        try:
            return await self._run_claimed(operation, claim_lost=claim_lost, now=now)
        except (ClaimConflict, StaleFence):
            return True
        finally:
            stop_heartbeat.set()
            if heartbeat is not None:
                await heartbeat

    async def _renew_claim(
        self,
        operation: OperationSnapshot,
        stop: asyncio.Event,
        lost: asyncio.Event,
    ) -> None:
        interval = max(0.05, self._repository.claim_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    await self._repository.renew_claim(
                        operation.id,
                        self._worker_id,
                        claim_token=operation.claim_token or "",
                        claim_generation=operation.claim_generation,
                    )
                except (ClaimConflict, StaleFence):
                    lost.set()
                    return

    async def _run_claimed(
        self,
        operation: OperationSnapshot,
        *,
        claim_lost: asyncio.Event,
        now: datetime | None,
    ) -> bool:
        claim = {
            "claim_token": operation.claim_token or "",
            "claim_generation": operation.claim_generation,
            "now": now,
        }
        provider_fence = await self._driver.observed_fence(operation.tenant_id)
        if claim_lost.is_set():
            return True
        if provider_fence > operation.fence_generation:
            await self._repository.fail(
                operation.id,
                self._worker_id,
                code="PROVISIONER_STALE_FENCE",
                **claim,
            )
            return True
        request = await self._repository.load_request(operation.id)
        context = EffectContext(
            operation_id=operation.id,
            provider_operation_id=operation.external_operation_id,
            tenant_id=operation.tenant_id,
            cell_id=operation.cell_id,
            fence_generation=operation.fence_generation,
        )
        try:
            outcome = await self._driver.execute(operation.action.value, request, context)
        except LostAcknowledgement:
            await self._repository.mark_pending(
                operation.id,
                self._worker_id,
                checkpoint="effect-prepared",
                retry_after_seconds=2,
                **claim,
            )
            return True
        except DriverRetryable:
            await self._repository.record_retryable_failure(
                operation.id,
                self._worker_id,
                retry_after_seconds=2,
                **claim,
            )
            return True
        except DriverTerminal as error:
            await self._repository.fail(
                operation.id,
                self._worker_id,
                code=error.code,
                **claim,
            )
            return True
        if claim_lost.is_set():
            return True
        if isinstance(outcome, DriverPending):
            await self._repository.mark_pending(
                operation.id,
                self._worker_id,
                checkpoint=outcome.checkpoint,
                retry_after_seconds=outcome.retry_after_seconds,
                **claim,
            )
            return True
        if not isinstance(outcome, DriverFinal):
            await self._repository.fail(
                operation.id,
                self._worker_id,
                code="PROVISIONER_DRIVER_INVALID",
                **claim,
            )
            return True
        await self._repository.checkpoint_effect_applied(
            operation.id,
            self._worker_id,
            **claim,
        )
        for resource in outcome.resources:
            await self._repository.record_resource(
                operation_id=operation.id,
                worker_id=self._worker_id,
                tenant_id=operation.tenant_id,
                cell_id=operation.cell_id,
                kind=resource.kind,
                recoverable_reference=resource.recoverable_reference,
                provider_operation_id=operation.external_operation_id,
                provider_fence_generation=operation.fence_generation,
                **claim,
            )
        await self._repository.complete(
            operation.id,
            outcome.result,
            worker_id=self._worker_id,
            **claim,
        )
        return True
