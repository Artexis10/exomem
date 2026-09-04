"""Restart-safe database-backed operation worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Protocol

from .capacity import CapacityIdentityConflict
from .conflict_reason import ConflictReason, coerce_conflict_reason
from .driver import (
    DriverFinal,
    DriverPending,
    DriverResource,
    DriverRetryable,
    DriverTerminal,
    EffectContext,
    LostAcknowledgement,
    ProvisionerDriver,
)
from .lifecycle import _digest
from .models import OperationAction
from .repository import (
    ClaimConflict,
    ImmutableMetadataConflict,
    OperationRepository,
    OperationSnapshot,
    StaleFence,
)
from .wire_protocol import FINAL_MODELS_BY_PROTOCOL, WIRE_PROTOCOL_V2, runtime_identity

_LOGGER = logging.getLogger(__name__)


def _log_terminal_failure(
    operation: OperationSnapshot,
    *,
    code: str,
    reason: object = None,
) -> None:
    """Name the condition behind a terminal failure where an operator will see it.

    One stable code stands for a dozen distinct provider conditions and the worker
    logged nothing at all on that path, so a terminal provision failure left only
    the code -- true of every one of them. The line carries allowlisted operational
    metadata and a closed-set condition label, never provider or caller text.

    The internal operation ID is confidential -- the recovery runbook admits it
    only through an operator-owned mode-0600 file and forbids it in any log -- so
    the line carries a digest of it instead, which correlates against the
    operations table without publishing the identity.

    `reason` is re-validated here rather than trusted: `DriverTerminal` is the
    public exception any driver raises, so an attribute set after construction
    degrades to `UNCLASSIFIED` instead of reaching the log or raising inside the
    handler that still has to fail the operation.
    """

    extra: dict[str, Any] = {
        "event": "operation_failed",
        "action": operation.action.value,
        "operation_digest": _digest(operation.id),
        "code": code,
    }
    if reason is not None:
        extra["reason"] = coerce_conflict_reason(reason)
    _LOGGER.warning("operation failed", extra=extra)


def _validate_final(
    operation: OperationSnapshot,
    request: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Reject a malformed driver final before it becomes replayable state."""

    try:
        final_model = FINAL_MODELS_BY_PROTOCOL[operation.wire_protocol][operation.action.value]
    except KeyError as error:
        raise DriverTerminal("PROVISIONER_DRIVER_INVALID") from error
    if final_model is None:
        if result:
            raise DriverTerminal("PROVISIONER_DRIVER_INVALID")
        return
    try:
        final_model.model_validate(result)
    except ValueError as error:
        raise DriverTerminal("PROVISIONER_DRIVER_INVALID") from error
    if operation.action is OperationAction.HEALTH:
        identity = runtime_identity(request)
        if (
            result.get("cellId") != request.get("cellId")
            or result.get("workerPolicy") != request.get("workerPolicy")
        ):
            raise DriverTerminal("PROVISIONER_RUNTIME_CONTRACT_MISMATCH")
        if operation.wire_protocol == WIRE_PROTOCOL_V2:
            if result.get("runtimeIdentity") != identity:
                raise DriverTerminal("PROVISIONER_RUNTIME_CONTRACT_MISMATCH")
        elif (
            result.get("releaseVersion") != identity["releaseVersion"]
            or result.get("protocolVersion") != identity["protocolVersion"]
        ):
            raise DriverTerminal("PROVISIONER_RUNTIME_CONTRACT_MISMATCH")
    if operation.action is OperationAction.ROTATE_CREDENTIAL and (
        result.get("previousCredentialRejected") != (request.get("phase") == "finalize")
    ):
        raise DriverTerminal("PROVISIONER_DRIVER_INVALID")


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

    async def _record_resources(
        self,
        operation: OperationSnapshot,
        resources: tuple[DriverResource, ...],
        *,
        claim: dict[str, Any],
        claim_lost: asyncio.Event,
    ) -> bool:
        """Record each driver resource, or terminally fail the operation.

        A recorded resource is immutable, so a provider reference that no longer
        matches one can never be reconciled by retrying -- the provider resource it
        named is gone. Letting that escape kills the process, and the restarted pod
        re-claims this same operation and re-drives its effect without bound.

        Scoped deliberately to these calls. `complete()` raises the same exception for
        unrelated capacity-ledger integrity violations, and converting those here would
        park a DISCARD or DESTROY under a provider-metadata code that `reopen()` cannot
        recover, because it admits only PROVISION.

        Returns False when the operation was failed and the caller must stop.
        """

        try:
            for resource in resources:
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
        except ImmutableMetadataConflict:
            if not claim_lost.is_set():
                _log_terminal_failure(
                    operation,
                    code="PROVISIONER_PROVIDER_METADATA_CONFLICT",
                    reason=ConflictReason.DURABLE_RESOURCE_IDENTITY_IMMUTABLE,
                )
                try:
                    await self._repository.fail(
                        operation.id,
                        self._worker_id,
                        code="PROVISIONER_PROVIDER_METADATA_CONFLICT",
                        **claim,
                    )
                except (ClaimConflict, StaleFence):
                    # The claim expired while handling the conflict. The row becomes
                    # claimable again and the next holder reaches the same outcome.
                    pass
            return False
        return True

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
            _log_terminal_failure(operation, code="PROVISIONER_STALE_FENCE")
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
                _log_terminal_failure(operation, code="PROVISIONER_CAPACITY_CONFLICT")
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
            wire_protocol=operation.wire_protocol,
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
            _log_terminal_failure(operation, code=error.code, reason=error.reason)
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
            if not await self._record_resources(
                operation, outcome.resources, claim=claim, claim_lost=claim_lost
            ):
                return True
            await self._repository.mark_pending(
                operation.id,
                self._worker_id,
                checkpoint=outcome.checkpoint,
                retry_after_seconds=outcome.retry_after_seconds,
                **claim,
            )
            return True
        if not isinstance(outcome, DriverFinal):
            _log_terminal_failure(operation, code="PROVISIONER_DRIVER_INVALID")
            await self._repository.fail(
                operation.id,
                self._worker_id,
                code="PROVISIONER_DRIVER_INVALID",
                **claim,
            )
            return True
        try:
            _validate_final(operation, request, outcome.result)
        except DriverTerminal as error:
            _log_terminal_failure(operation, code=error.code, reason=error.reason)
            await self._repository.fail(
                operation.id,
                self._worker_id,
                code=error.code,
                **claim,
            )
            return True
        await self._repository.checkpoint_effect_applied(
            operation.id,
            self._worker_id,
            **claim,
        )
        if not await self._record_resources(
            operation, outcome.resources, claim=claim, claim_lost=claim_lost
        ):
            return True
        await self._repository.complete(
            operation.id,
            outcome.result,
            worker_id=self._worker_id,
            **claim,
        )
        return True
