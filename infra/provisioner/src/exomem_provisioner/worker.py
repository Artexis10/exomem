"""Restart-safe database-backed operation worker."""

from __future__ import annotations

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
from .repository import OperationRepository


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
        provider_fence = await self._driver.observed_fence(operation.tenant_id)
        if provider_fence > operation.fence_generation:
            await self._repository.fail(
                operation.id,
                self._worker_id,
                code="PROVISIONER_STALE_FENCE",
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
                now=now,
            )
            return True
        except DriverRetryable:
            await self._repository.mark_pending(
                operation.id,
                self._worker_id,
                checkpoint="provider-wait",
                retry_after_seconds=2,
                now=now,
            )
            return True
        except DriverTerminal as error:
            await self._repository.fail(
                operation.id,
                self._worker_id,
                code=error.code,
            )
            return True
        if isinstance(outcome, DriverPending):
            await self._repository.mark_pending(
                operation.id,
                self._worker_id,
                checkpoint=outcome.checkpoint,
                retry_after_seconds=outcome.retry_after_seconds,
                now=now,
            )
            return True
        if not isinstance(outcome, DriverFinal):
            await self._repository.fail(
                operation.id,
                self._worker_id,
                code="PROVISIONER_DRIVER_INVALID",
            )
            return True
        await self._repository.checkpoint_effect_applied(operation.id, self._worker_id)
        for resource in outcome.resources:
            await self._repository.record_resource(
                operation_id=operation.id,
                tenant_id=operation.tenant_id,
                cell_id=operation.cell_id,
                kind=resource.kind,
                recoverable_reference=resource.recoverable_reference,
                provider_operation_id=operation.external_operation_id,
                provider_fence_generation=operation.fence_generation,
            )
        await self._repository.complete(operation.id, outcome.result)
        return True
