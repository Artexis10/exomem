"""Restart-safe database-backed operation worker."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Protocol

from .capacity import CapacityIdentityConflict
from .driver import (
    DriverFinal,
    DriverPending,
    DriverRetryable,
    DriverTerminal,
    EffectContext,
    LostAcknowledgement,
    ProvisionerDriver,
)
from .models import OperationAction
from .repository import ClaimConflict, OperationRepository, OperationSnapshot, StaleFence


class CapacityAdmission(Protocol):
    async def admit(
        self,
        operation: OperationSnapshot,
        request: dict[str, Any],
        *,
        worker_id: str,
        claim_token: str,
        claim_generation: int,
        provider_operation_id: str,
        provider_fence_generation: int,
        now: datetime | None,
    ) -> str | None: ...


class ProvisionerWorker:
    def __init__(
        self,
        repository: OperationRepository,
        driver: ProvisionerDriver,
        *,
        worker_id: str,
        include_checkpoints: frozenset[str] | None = None,
        exclude_checkpoints: frozenset[str] = frozenset(),
        allowed_actions: frozenset[OperationAction] | None = None,
        excluded_actions: frozenset[OperationAction] = frozenset(),
        resume_claim: bool = False,
        capacity_admission: CapacityAdmission | None = None,
    ) -> None:
        if allowed_actions is not None and allowed_actions & excluded_actions:
            raise ValueError("worker action scopes overlap")
        provision_capable = (
            (allowed_actions is None or OperationAction.PROVISION in allowed_actions)
            and OperationAction.PROVISION not in excluded_actions
        )
        if provision_capable and capacity_admission is None:
            raise ValueError("PROVISION-capable worker requires capacity admission")
        self._repository = repository
        self._driver = driver
        self._worker_id = worker_id
        self._include_checkpoints = include_checkpoints
        self._exclude_checkpoints = exclude_checkpoints
        self._allowed_actions = allowed_actions
        self._excluded_actions = excluded_actions
        self._resume_claim = resume_claim
        self._capacity_admission = capacity_admission

    async def run_once(self, *, now: datetime | None = None) -> bool:
        claim_method = (
            self._repository.resume_claim
            if self._resume_claim
            else self._repository.claim_next
        )
        operation = await claim_method(
            self._worker_id,
            now=now,
            include_checkpoints=self._include_checkpoints,
            exclude_checkpoints=self._exclude_checkpoints,
            allowed_actions=self._allowed_actions,
            excluded_actions=self._excluded_actions,
        )
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
        if operation.action is OperationAction.PROVISION:
            if self._capacity_admission is None:
                raise RuntimeError("PROVISION-capable worker has no capacity admission")
            try:
                block_reason = await self._capacity_admission.admit(
                    operation,
                    request,
                    worker_id=self._worker_id,
                    claim_token=claim["claim_token"],
                    claim_generation=claim["claim_generation"],
                    provider_operation_id=operation.external_operation_id,
                    provider_fence_generation=operation.fence_generation,
                    now=now,
                )
            except CapacityIdentityConflict:
                if claim_lost.is_set():
                    return True
                await self._repository.fail(
                    operation.id,
                    self._worker_id,
                    code="PROVISIONER_CAPACITY_CONFLICT",
                    **claim,
                )
                return True
            if claim_lost.is_set():
                return True
            if block_reason is not None:
                await self._repository.mark_pending(
                    operation.id,
                    self._worker_id,
                    checkpoint=block_reason,
                    retry_after_seconds=300,
                    **claim,
                )
                return True
        context = EffectContext(
            operation_id=operation.id,
            provider_operation_id=operation.external_operation_id,
            tenant_id=operation.tenant_id,
            cell_id=operation.cell_id,
            fence_generation=operation.fence_generation,
            checkpoint=operation.checkpoint,
            operation_created_at=operation.created_at.isoformat().replace("+00:00", "Z"),
        )
        try:
            outcome = await self._driver.execute(operation.action.value, request, context)
        except LostAcknowledgement:
            await self._repository.mark_pending(
                operation.id,
                self._worker_id,
                checkpoint=operation.checkpoint,
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
