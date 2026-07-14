"""Typed SQLAlchemy repositories for idempotent, fenced operations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .crypto import EnvelopeCodec
from .models import (
    BackupRecord,
    CredentialMetadata,
    ExportRecord,
    Operation,
    OperationAction,
    OperationState,
    Resource,
    ResourceKind,
    TenantFence,
)


class RepositoryConflict(RuntimeError):
    pass


class IdempotencyConflict(RepositoryConflict):
    pass


class StaleFence(RepositoryConflict):
    pass


class ImmutableMetadataConflict(RepositoryConflict):
    pass


class ClaimConflict(RepositoryConflict):
    pass


def canonical_request_bytes(request: dict[str, Any]) -> bytes:
    return json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_request_sha256(request: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_request_bytes(request)).hexdigest()


def _reference_digest(reference: str) -> str:
    return hashlib.sha256(reference.encode("utf-8")).hexdigest()


def _claim_condition(claimed_at: datetime):
    return or_(
        and_(
            Operation.state == OperationState.PENDING,
            Operation.available_at <= claimed_at,
        ),
        and_(
            Operation.state == OperationState.CLAIMED,
            Operation.claim_expires_at <= claimed_at,
        ),
    )


def _claim_statement(claimed_at: datetime):
    return (
        select(Operation)
        .where(_claim_condition(claimed_at))
        .order_by(Operation.created_at, Operation.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    id: str
    action: OperationAction
    idempotency_key: str
    canonical_request_sha256: str
    tenant_id: str
    cell_id: str | None
    external_operation_id: str
    fence_generation: int
    state: OperationState
    checkpoint: str
    progress: dict[str, Any]
    retry_after_seconds: int
    result_redacted: dict[str, Any]
    error_code: str | None


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    id: str
    operation_id: str
    kind: ResourceKind
    provider_operation_id: str
    provider_fence_generation: int


def _operation_snapshot(operation: Operation) -> OperationSnapshot:
    return OperationSnapshot(
        id=operation.id,
        action=operation.action,
        idempotency_key=operation.idempotency_key,
        canonical_request_sha256=operation.canonical_request_sha256,
        tenant_id=operation.tenant_id,
        cell_id=operation.cell_id,
        external_operation_id=operation.external_operation_id,
        fence_generation=operation.fence_generation,
        state=operation.state,
        checkpoint=operation.checkpoint,
        progress=dict(operation.progress),
        retry_after_seconds=operation.retry_after_seconds,
        result_redacted=dict(operation.result_redacted),
        error_code=operation.error_code,
    )


class OperationRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        codec: EnvelopeCodec,
        claim_seconds: int = 30,
    ) -> None:
        self._sessions = session_factory
        self._codec = codec
        self.claim_seconds = claim_seconds

    async def get(self, action: str, idempotency_key: str) -> OperationSnapshot | None:
        action_value = OperationAction(action)
        async with self._sessions() as session:
            operation = await session.scalar(
                select(Operation).where(
                    Operation.action == action_value,
                    Operation.idempotency_key == idempotency_key,
                )
            )
            return _operation_snapshot(operation) if operation is not None else None

    async def get_by_id(self, operation_id: str) -> OperationSnapshot | None:
        async with self._sessions() as session:
            operation = await session.get(Operation, operation_id)
            return _operation_snapshot(operation) if operation is not None else None

    async def submit(
        self,
        action: str,
        idempotency_key: str,
        request: dict[str, Any],
        *,
        retry_after_seconds: int = 2,
    ) -> OperationSnapshot:
        action_value = OperationAction(action)
        digest = canonical_request_sha256(request)
        existing = await self.get(action, idempotency_key)
        if existing is not None:
            if existing.canonical_request_sha256 != digest:
                raise IdempotencyConflict("idempotency key is bound to another request")
            return existing

        tenant_id = request["tenantId"]
        external_operation_id = request["operationId"]
        fence_generation = request["fenceGeneration"]
        if not isinstance(tenant_id, str) or not isinstance(external_operation_id, str):
            raise ValueError("request identity is invalid")
        if not isinstance(fence_generation, int) or isinstance(fence_generation, bool):
            raise ValueError("request fence is invalid")
        for attempt in range(3):
            operation = Operation(
                action=action_value,
                idempotency_key=idempotency_key,
                canonical_request_sha256=digest,
                tenant_id=tenant_id,
                cell_id=(request.get("cellId") if isinstance(request.get("cellId"), str) else None),
                external_operation_id=external_operation_id,
                fence_generation=fence_generation,
                provider_operation_id=external_operation_id,
                provider_fence_generation=fence_generation,
                checkpoint=str(request["checkpoint"]),
                request_ciphertext=self._codec.encrypt_json(
                    request,
                    purpose=f"operation-request:{action_value.value}:{idempotency_key}",
                ),
                retry_after_seconds=retry_after_seconds,
            )
            try:
                async with self._sessions.begin() as session:
                    fence = await session.get(TenantFence, tenant_id, with_for_update=True)
                    if fence is not None and fence_generation < fence.fence_generation:
                        raise StaleFence("request fence is older than durable tenant state")
                    if fence is None:
                        session.add(
                            TenantFence(
                                tenant_id=tenant_id,
                                fence_generation=fence_generation,
                            )
                        )
                    elif fence_generation > fence.fence_generation:
                        fence.fence_generation = fence_generation
                    session.add(operation)
                    await session.flush()
                    return _operation_snapshot(operation)
            except IntegrityError:
                existing = await self.get(action, idempotency_key)
                if existing is not None:
                    if existing.canonical_request_sha256 != digest:
                        raise IdempotencyConflict("concurrent idempotency conflict") from None
                    return existing
                if attempt == 2:
                    raise RepositoryConflict("tenant fence did not converge") from None
        raise RepositoryConflict("operation submission did not converge")

    async def load_request(self, operation_id: str) -> dict[str, Any]:
        async with self._sessions() as session:
            operation = await session.get(Operation, operation_id)
            if operation is None:
                raise KeyError(operation_id)
            return self._codec.decrypt_json(
                operation.request_ciphertext,
                purpose=(f"operation-request:{operation.action.value}:{operation.idempotency_key}"),
            )

    async def load_result(self, operation_id: str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            operation = await session.get(Operation, operation_id)
            if operation is None:
                raise KeyError(operation_id)
            if operation.result_ciphertext is None:
                return None
            return self._codec.decrypt_json(
                operation.result_ciphertext,
                purpose=f"operation-result:{operation.id}",
            )

    async def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> OperationSnapshot | None:
        claimed_at = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            if session.get_bind().dialect.name == "sqlite":
                candidate = (
                    select(Operation.id)
                    .where(_claim_condition(claimed_at))
                    .order_by(Operation.created_at, Operation.id)
                    .limit(1)
                    .scalar_subquery()
                )
                operation_id = await session.scalar(
                    update(Operation)
                    .where(Operation.id == candidate)
                    .values(
                        state=OperationState.CLAIMED,
                        checkpoint="effect-prepared",
                        claim_owner=worker_id,
                        claim_expires_at=claimed_at + timedelta(seconds=self.claim_seconds),
                        updated_at=claimed_at,
                    )
                    .returning(Operation.id)
                )
                if operation_id is None:
                    return None
                operation = await session.get(Operation, operation_id)
                if operation is None:
                    return None
                return _operation_snapshot(operation)
            operation = await session.scalar(_claim_statement(claimed_at))
            if operation is None:
                return None
            operation.state = OperationState.CLAIMED
            operation.checkpoint = "effect-prepared"
            operation.claim_owner = worker_id
            operation.claim_expires_at = claimed_at + timedelta(seconds=self.claim_seconds)
            await session.flush()
            return _operation_snapshot(operation)

    async def checkpoint_effect_applied(self, operation_id: str, worker_id: str) -> None:
        async with self._sessions.begin() as session:
            operation = await session.get(Operation, operation_id, with_for_update=True)
            if (
                operation is None
                or operation.state is not OperationState.CLAIMED
                or operation.claim_owner != worker_id
            ):
                raise ClaimConflict("worker no longer owns operation claim")
            operation.checkpoint = "effect-applied"

    async def mark_pending(
        self,
        operation_id: str,
        worker_id: str,
        *,
        checkpoint: str,
        retry_after_seconds: int,
        now: datetime | None = None,
    ) -> OperationSnapshot:
        pending_at = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            operation = await session.get(Operation, operation_id, with_for_update=True)
            if (
                operation is None
                or operation.state is not OperationState.CLAIMED
                or operation.claim_owner != worker_id
            ):
                raise ClaimConflict("worker no longer owns operation claim")
            operation.state = OperationState.PENDING
            operation.checkpoint = checkpoint
            operation.progress = {
                **operation.progress,
                "pending_count": int(operation.progress.get("pending_count", 0)) + 1,
            }
            operation.retry_after_seconds = retry_after_seconds
            operation.available_at = pending_at + timedelta(seconds=retry_after_seconds)
            operation.claim_owner = None
            operation.claim_expires_at = None
            await session.flush()
            return _operation_snapshot(operation)

    async def fail(
        self,
        operation_id: str,
        worker_id: str,
        *,
        code: str,
    ) -> OperationSnapshot:
        async with self._sessions.begin() as session:
            operation = await session.get(Operation, operation_id, with_for_update=True)
            if (
                operation is None
                or operation.state is not OperationState.CLAIMED
                or operation.claim_owner != worker_id
            ):
                raise ClaimConflict("worker no longer owns operation claim")
            operation.state = OperationState.ERROR
            operation.checkpoint = "failed"
            operation.error_code = code
            operation.claim_owner = None
            operation.claim_expires_at = None
            operation.finalized_at = datetime.now(UTC)
            await session.flush()
            return _operation_snapshot(operation)

    async def complete(self, operation_id: str, result: dict[str, Any]) -> OperationSnapshot:
        async with self._sessions.begin() as session:
            operation = await session.get(Operation, operation_id, with_for_update=True)
            if operation is None:
                raise KeyError(operation_id)
            if operation.state is OperationState.FINAL:
                prior = self._codec.decrypt_json(
                    operation.result_ciphertext or "",
                    purpose=f"operation-result:{operation.id}",
                )
                if prior != result:
                    raise ImmutableMetadataConflict("final operation result is immutable")
                return _operation_snapshot(operation)
            if operation.state is OperationState.ERROR:
                raise ImmutableMetadataConflict("failed operation cannot become final")
            operation.result_ciphertext = self._codec.encrypt_json(
                result,
                purpose=f"operation-result:{operation.id}",
            )
            operation.result_redacted = {
                "completed": True,
                "fields": sorted(result),
            }
            operation.state = OperationState.FINAL
            operation.checkpoint = "complete"
            operation.claim_owner = None
            operation.claim_expires_at = None
            operation.finalized_at = datetime.now(UTC)
            await session.flush()
            return _operation_snapshot(operation)

    async def record_resource(
        self,
        *,
        operation_id: str,
        tenant_id: str,
        cell_id: str | None,
        kind: ResourceKind,
        recoverable_reference: str,
        provider_operation_id: str,
        provider_fence_generation: int,
    ) -> ResourceSnapshot:
        digest = _reference_digest(recoverable_reference)
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(Resource).where(
                    Resource.operation_id == operation_id,
                    Resource.kind == kind,
                )
            )
            if existing is not None:
                if (
                    existing.tenant_id != tenant_id
                    or existing.cell_id != cell_id
                    or existing.reference_digest != digest
                    or existing.provider_operation_id != provider_operation_id
                    or existing.provider_fence_generation != provider_fence_generation
                ):
                    raise ImmutableMetadataConflict("resource provider metadata is immutable")
                return ResourceSnapshot(
                    existing.id,
                    existing.operation_id,
                    existing.kind,
                    existing.provider_operation_id,
                    existing.provider_fence_generation,
                )
            resource = Resource(
                operation_id=operation_id,
                tenant_id=tenant_id,
                cell_id=cell_id,
                kind=kind,
                reference_digest=digest,
                reference_ciphertext=self._codec.encrypt_json(
                    {"reference": recoverable_reference},
                    purpose=f"resource-reference:{operation_id}:{kind.value}",
                ),
                provider_operation_id=provider_operation_id,
                provider_fence_generation=provider_fence_generation,
            )
            session.add(resource)
            await session.flush()
            return ResourceSnapshot(
                resource.id,
                resource.operation_id,
                resource.kind,
                resource.provider_operation_id,
                resource.provider_fence_generation,
            )

    async def record_credential_metadata(
        self,
        *,
        operation_id: str,
        cell_id: str,
        version: int,
        credential_digest: str,
        active: bool,
    ) -> None:
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(CredentialMetadata).where(
                    CredentialMetadata.cell_id == cell_id,
                    CredentialMetadata.version == version,
                )
            )
            if existing is not None:
                if (
                    existing.operation_id != operation_id
                    or existing.credential_digest != credential_digest
                    or existing.active != active
                ):
                    raise ImmutableMetadataConflict("credential metadata is immutable")
                return
            session.add(
                CredentialMetadata(
                    operation_id=operation_id,
                    cell_id=cell_id,
                    version=version,
                    credential_digest=credential_digest,
                    active=active,
                )
            )

    async def record_export(
        self,
        *,
        operation_id: str,
        tenant_id: str,
        cell_id: str,
        export_reference: str,
        archive_sha256: str,
        manifest_sha256: str,
        archive_size: int,
        provider_operation_id: str,
        provider_fence_generation: int,
    ) -> None:
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(ExportRecord).where(ExportRecord.operation_id == operation_id)
            )
            digest = _reference_digest(export_reference)
            if existing is not None:
                if (
                    existing.reference_digest != digest
                    or existing.provider_operation_id != provider_operation_id
                    or existing.provider_fence_generation != provider_fence_generation
                ):
                    raise ImmutableMetadataConflict("export provider metadata is immutable")
                return
            session.add(
                ExportRecord(
                    operation_id=operation_id,
                    tenant_id=tenant_id,
                    cell_id=cell_id,
                    reference_digest=digest,
                    reference_ciphertext=self._codec.encrypt_json(
                        {"reference": export_reference},
                        purpose=f"export-reference:{operation_id}",
                    ),
                    archive_sha256=archive_sha256,
                    manifest_sha256=manifest_sha256,
                    archive_size=archive_size,
                    provider_operation_id=provider_operation_id,
                    provider_fence_generation=provider_fence_generation,
                )
            )

    async def record_backup(
        self,
        *,
        operation_id: str,
        tenant_id: str,
        cell_id: str,
        backup_reference: str,
        object_sha256: str,
        provider_operation_id: str,
        provider_fence_generation: int,
    ) -> None:
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(BackupRecord).where(BackupRecord.operation_id == operation_id)
            )
            digest = _reference_digest(backup_reference)
            if existing is not None:
                if (
                    existing.reference_digest != digest
                    or existing.provider_operation_id != provider_operation_id
                    or existing.provider_fence_generation != provider_fence_generation
                ):
                    raise ImmutableMetadataConflict("backup provider metadata is immutable")
                return
            session.add(
                BackupRecord(
                    operation_id=operation_id,
                    tenant_id=tenant_id,
                    cell_id=cell_id,
                    reference_digest=digest,
                    reference_ciphertext=self._codec.encrypt_json(
                        {"reference": backup_reference},
                        purpose=f"backup-reference:{operation_id}",
                    ),
                    object_sha256=object_sha256,
                    provider_operation_id=provider_operation_id,
                    provider_fence_generation=provider_fence_generation,
                )
            )
