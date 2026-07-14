"""Typed SQLAlchemy repositories for idempotent, fenced operations."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select, update
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
    ) & (
        Operation.fence_generation
        == select(TenantFence.fence_generation)
        .where(TenantFence.tenant_id == Operation.tenant_id)
        .scalar_subquery()
    )


def _claim_candidate_statement(claimed_at: datetime):
    return (
        select(Operation.id, Operation.tenant_id)
        .where(_claim_condition(claimed_at))
        .order_by(Operation.created_at, Operation.id)
        .limit(1)
    )


def _claim_statement(operation_id: str, claimed_at: datetime):
    return (
        select(Operation)
        .where(Operation.id == operation_id, _claim_condition(claimed_at))
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
    caller_checkpoint: str
    checkpoint: str
    progress: dict[str, Any]
    retry_after_seconds: int
    result_redacted: dict[str, Any]
    error_code: str | None
    claim_token: str | None
    claim_generation: int
    claim_expires_at: datetime | None
    created_at: datetime


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
        caller_checkpoint=operation.caller_checkpoint,
        checkpoint=operation.checkpoint,
        progress=dict(operation.progress),
        retry_after_seconds=operation.retry_after_seconds,
        result_redacted=dict(operation.result_redacted),
        error_code=operation.error_code,
        claim_token=operation.claim_token,
        claim_generation=operation.claim_generation,
        claim_expires_at=operation.claim_expires_at,
        created_at=operation.created_at,
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _require_active_claim(
    operation: Operation | None,
    *,
    worker_id: str,
    claim_token: str,
    claim_generation: int,
    now: datetime,
) -> Operation:
    if (
        operation is None
        or operation.state is not OperationState.CLAIMED
        or operation.claim_owner != worker_id
        or operation.claim_token != claim_token
        or operation.claim_generation != claim_generation
        or operation.claim_expires_at is None
        or _as_utc(operation.claim_expires_at) <= _as_utc(now)
    ):
        raise ClaimConflict("worker no longer owns an active operation claim")
    return operation


async def _database_now(session: AsyncSession, explicit: datetime | None) -> datetime:
    if explicit is not None:
        return _as_utc(explicit)
    if session.get_bind().dialect.name == "postgresql":
        current = await session.scalar(select(func.clock_timestamp()))
        if current is None:
            raise RuntimeError("PostgreSQL did not return its current clock time")
        return _as_utc(current)
    return datetime.now(UTC)


async def _lock_operation_fence_first(
    session: AsyncSession,
    operation_id: str,
) -> Operation:
    tenant_id = await session.scalar(
        select(Operation.tenant_id).where(Operation.id == operation_id)
    )
    if tenant_id is None:
        raise ClaimConflict("operation does not exist")
    fence = await session.get(TenantFence, tenant_id, with_for_update=True)
    operation = await session.get(Operation, operation_id, with_for_update=True)
    if operation is None:
        raise ClaimConflict("operation does not exist")
    if fence is None or fence.fence_generation != operation.fence_generation:
        raise StaleFence("active claim fence is stale")
    return operation


async def _lock_active_claim(
    session: AsyncSession,
    operation_id: str,
    *,
    worker_id: str,
    claim_token: str,
    claim_generation: int,
    now: datetime | None,
) -> tuple[Operation, datetime]:
    operation = await _lock_operation_fence_first(session, operation_id)
    checked_at = await _database_now(session, now)
    return (
        _require_active_claim(
            operation,
            worker_id=worker_id,
            claim_token=claim_token,
            claim_generation=claim_generation,
            now=checked_at,
        ),
        checked_at,
    )


def _require_operation_identity(
    operation: Operation,
    *,
    tenant_id: str,
    cell_id: str | None,
    provider_operation_id: str,
    provider_fence_generation: int,
) -> None:
    if (
        operation.tenant_id != tenant_id
        or operation.cell_id != cell_id
        or operation.provider_operation_id != provider_operation_id
        or operation.provider_fence_generation != provider_fence_generation
    ):
        raise ImmutableMetadataConflict("side effect does not match active operation identity")


class OperationRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        codec: EnvelopeCodec,
        claim_seconds: int = 30,
        max_failure_attempts: int = 6,
    ) -> None:
        self._sessions = session_factory
        self._codec = codec
        self.claim_seconds = claim_seconds
        self.max_failure_attempts = max_failure_attempts

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

    async def load_resource_reference(self, resource_id: str) -> str:
        async with self._sessions() as session:
            resource = await session.get(Resource, resource_id)
            if resource is None:
                raise KeyError(resource_id)
            decoded = self._codec.decrypt_json(
                resource.reference_ciphertext,
                purpose=f"resource-reference:{resource.operation_id}:{resource.kind.value}",
            )
            reference = decoded.get("reference")
            if not isinstance(reference, str):
                raise ValueError("encrypted provider reference is invalid")
            return reference

    async def list_resources(
        self,
        *,
        tenant_id: str,
        cell_id: str | None = None,
    ) -> tuple[ResourceSnapshot, ...]:
        async with self._sessions() as session:
            statement = select(Resource).where(Resource.tenant_id == tenant_id)
            if cell_id is not None:
                statement = statement.where(Resource.cell_id == cell_id)
            resources = await session.scalars(statement.order_by(Resource.created_at, Resource.id))
            return tuple(
                ResourceSnapshot(
                    resource.id,
                    resource.operation_id,
                    resource.kind,
                    resource.provider_operation_id,
                    resource.provider_fence_generation,
                )
                for resource in resources
            )

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
        tenant_id = request["tenantId"]
        external_operation_id = request["operationId"]
        fence_generation = request["fenceGeneration"]
        if not isinstance(tenant_id, str) or not isinstance(external_operation_id, str):
            raise ValueError("request identity is invalid")
        if not isinstance(fence_generation, int) or isinstance(fence_generation, bool):
            raise ValueError("request fence is invalid")
        for attempt in range(3):
            try:
                async with self._sessions.begin() as session:
                    fence = await session.get(TenantFence, tenant_id, with_for_update=True)
                    existing = await session.scalar(
                        select(Operation)
                        .where(
                            Operation.action == action_value,
                            Operation.idempotency_key == idempotency_key,
                        )
                        .with_for_update()
                    )
                    if existing is not None and existing.canonical_request_sha256 != digest:
                        raise IdempotencyConflict("idempotency key is bound to another request")
                    if fence is not None and fence_generation < fence.fence_generation:
                        raise StaleFence("request fence is older than durable tenant state")
                    if existing is not None:
                        return _operation_snapshot(existing)
                    if fence is None:
                        session.add(
                            TenantFence(
                                tenant_id=tenant_id,
                                fence_generation=fence_generation,
                            )
                        )
                    elif fence_generation > fence.fence_generation:
                        fence.fence_generation = fence_generation
                    operation = Operation(
                        action=action_value,
                        idempotency_key=idempotency_key,
                        canonical_request_sha256=digest,
                        tenant_id=tenant_id,
                        cell_id=(
                            request.get("cellId")
                            if isinstance(request.get("cellId"), str)
                            else None
                        ),
                        external_operation_id=external_operation_id,
                        fence_generation=fence_generation,
                        provider_operation_id=external_operation_id,
                        provider_fence_generation=fence_generation,
                        caller_checkpoint=str(request["checkpoint"]),
                        checkpoint="queued",
                        request_ciphertext=self._codec.encrypt_json(
                            request,
                            purpose=f"operation-request:{action_value.value}:{idempotency_key}",
                        ),
                        retry_after_seconds=retry_after_seconds,
                    )
                    session.add(operation)
                    await session.flush()
                    return _operation_snapshot(operation)
            except IntegrityError:
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
        claim_token = secrets.token_urlsafe(32)
        async with self._sessions.begin() as session:
            claimed_at = await _database_now(session, now)
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
                        checkpoint=case(
                            (Operation.checkpoint == "queued", "effect-prepared"),
                            else_=Operation.checkpoint,
                        ),
                        claim_owner=worker_id,
                        claim_token=claim_token,
                        claim_generation=Operation.claim_generation + 1,
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
                fence = await session.get(TenantFence, operation.tenant_id)
                if fence is None or fence.fence_generation != operation.fence_generation:
                    raise StaleFence("active claim fence is stale")
                return _operation_snapshot(operation)
            candidate = (await session.execute(_claim_candidate_statement(claimed_at))).first()
            if candidate is None:
                return None
            fence = await session.get(TenantFence, candidate.tenant_id, with_for_update=True)
            claimed_at = await _database_now(session, now)
            operation = await session.scalar(_claim_statement(candidate.id, claimed_at))
            if operation is None:
                return None
            if fence is None or fence.fence_generation != operation.fence_generation:
                return None
            operation.state = OperationState.CLAIMED
            if operation.checkpoint == "queued":
                operation.checkpoint = "effect-prepared"
            operation.claim_owner = worker_id
            operation.claim_token = claim_token
            operation.claim_generation += 1
            operation.claim_expires_at = claimed_at + timedelta(seconds=self.claim_seconds)
            operation.updated_at = claimed_at
            await session.flush()
            return _operation_snapshot(operation)

    async def renew_claim(
        self,
        operation_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        now: datetime | None = None,
    ) -> OperationSnapshot:
        async with self._sessions.begin() as session:
            operation, renewed_at = await _lock_active_claim(
                session,
                operation_id,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
            operation.claim_expires_at = renewed_at + timedelta(seconds=self.claim_seconds)
            await session.flush()
            return _operation_snapshot(operation)

    async def checkpoint_effect_applied(
        self,
        operation_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        now: datetime | None = None,
    ) -> None:
        async with self._sessions.begin() as session:
            operation, _ = await _lock_active_claim(
                session,
                operation_id,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
            operation.checkpoint = "effect-applied"

    async def mark_pending(
        self,
        operation_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        checkpoint: str,
        retry_after_seconds: int,
        now: datetime | None = None,
    ) -> OperationSnapshot:
        async with self._sessions.begin() as session:
            operation, pending_at = await _lock_active_claim(
                session,
                operation_id,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
            operation.state = OperationState.PENDING
            operation.checkpoint = checkpoint
            operation.progress = {
                **operation.progress,
                "pending_count": int(operation.progress.get("pending_count", 0)) + 1,
            }
            operation.retry_after_seconds = retry_after_seconds
            operation.available_at = pending_at + timedelta(seconds=retry_after_seconds)
            operation.claim_owner = None
            operation.claim_token = None
            operation.claim_expires_at = None
            await session.flush()
            return _operation_snapshot(operation)

    async def fail(
        self,
        operation_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        code: str,
        now: datetime | None = None,
    ) -> OperationSnapshot:
        async with self._sessions.begin() as session:
            operation, failed_at = await _lock_active_claim(
                session,
                operation_id,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
            operation.state = OperationState.ERROR
            operation.checkpoint = "failed"
            operation.error_code = code
            operation.claim_owner = None
            operation.claim_token = None
            operation.claim_expires_at = None
            operation.finalized_at = failed_at
            await session.flush()
            return _operation_snapshot(operation)

    async def record_retryable_failure(
        self,
        operation_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        retry_after_seconds: int,
        now: datetime | None = None,
    ) -> OperationSnapshot:
        async with self._sessions.begin() as session:
            operation, failed_at = await _lock_active_claim(
                session,
                operation_id,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
            attempts = int(operation.progress.get("failure_attempts", 0)) + 1
            operation.progress = {**operation.progress, "failure_attempts": attempts}
            operation.claim_owner = None
            operation.claim_token = None
            operation.claim_expires_at = None
            if attempts >= self.max_failure_attempts:
                operation.state = OperationState.ERROR
                operation.checkpoint = "failed"
                operation.error_code = "PROVISIONER_RETRY_EXHAUSTED"
                operation.finalized_at = failed_at
            else:
                operation.state = OperationState.PENDING
                operation.checkpoint = "retry-backoff"
                operation.retry_after_seconds = retry_after_seconds
                operation.available_at = failed_at + timedelta(seconds=retry_after_seconds)
            await session.flush()
            return _operation_snapshot(operation)

    async def complete(
        self,
        operation_id: str,
        result: dict[str, Any],
        *,
        worker_id: str,
        claim_token: str,
        claim_generation: int,
        now: datetime | None = None,
    ) -> OperationSnapshot:
        async with self._sessions.begin() as session:
            operation, completed_at = await _lock_active_claim(
                session,
                operation_id,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
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
            operation.claim_token = None
            operation.claim_expires_at = None
            operation.finalized_at = completed_at
            await session.flush()
            return _operation_snapshot(operation)

    async def record_resource(
        self,
        *,
        operation_id: str,
        worker_id: str,
        claim_token: str,
        claim_generation: int,
        now: datetime | None = None,
        tenant_id: str,
        cell_id: str | None,
        kind: ResourceKind,
        recoverable_reference: str,
        provider_operation_id: str,
        provider_fence_generation: int,
    ) -> ResourceSnapshot:
        digest = _reference_digest(recoverable_reference)
        async with self._sessions.begin() as session:
            operation, _ = await _lock_active_claim(
                session,
                operation_id,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
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
                _require_operation_identity(
                    operation,
                    tenant_id=existing.tenant_id,
                    cell_id=existing.cell_id,
                    provider_operation_id=existing.provider_operation_id,
                    provider_fence_generation=existing.provider_fence_generation,
                )
                return ResourceSnapshot(
                    existing.id,
                    existing.operation_id,
                    existing.kind,
                    existing.provider_operation_id,
                    existing.provider_fence_generation,
                )
            _require_operation_identity(
                operation,
                tenant_id=tenant_id,
                cell_id=cell_id,
                provider_operation_id=provider_operation_id,
                provider_fence_generation=provider_fence_generation,
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
        worker_id: str,
        claim_token: str,
        claim_generation: int,
        now: datetime | None = None,
        cell_id: str,
        version: int,
        credential_digest: str,
        active: bool,
    ) -> None:
        async with self._sessions.begin() as session:
            operation, _ = await _lock_active_claim(
                session,
                operation_id,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
            if operation.cell_id != cell_id:
                raise ImmutableMetadataConflict(
                    "side effect does not match active operation identity"
                )
            credentials = list(
                await session.scalars(
                    select(CredentialMetadata)
                    .where(CredentialMetadata.cell_id == cell_id)
                    .with_for_update()
                )
            )
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
                ):
                    raise ImmutableMetadataConflict("credential metadata identity is immutable")
                if existing.active and not active:
                    raise ImmutableMetadataConflict(
                        "active credential promotion cannot be reversed"
                    )
                if active and not existing.active:
                    for credential in credentials:
                        if credential.id != existing.id and credential.active:
                            credential.active = False
                    await session.flush()
                    existing.active = True
                return
            if active:
                for credential in credentials:
                    if credential.active:
                        credential.active = False
                await session.flush()
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
        worker_id: str,
        claim_token: str,
        claim_generation: int,
        now: datetime | None = None,
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
            operation, _ = await _lock_active_claim(
                session,
                operation_id,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
            existing = await session.scalar(
                select(ExportRecord).where(ExportRecord.operation_id == operation_id)
            )
            digest = _reference_digest(export_reference)
            if existing is not None:
                if (
                    existing.tenant_id != tenant_id
                    or existing.cell_id != cell_id
                    or existing.reference_digest != digest
                    or existing.archive_sha256 != archive_sha256
                    or existing.manifest_sha256 != manifest_sha256
                    or existing.archive_size != archive_size
                    or existing.provider_operation_id != provider_operation_id
                    or existing.provider_fence_generation != provider_fence_generation
                ):
                    raise ImmutableMetadataConflict("export provider metadata is immutable")
                _require_operation_identity(
                    operation,
                    tenant_id=existing.tenant_id,
                    cell_id=existing.cell_id,
                    provider_operation_id=existing.provider_operation_id,
                    provider_fence_generation=existing.provider_fence_generation,
                )
                return
            _require_operation_identity(
                operation,
                tenant_id=tenant_id,
                cell_id=cell_id,
                provider_operation_id=provider_operation_id,
                provider_fence_generation=provider_fence_generation,
            )
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
        worker_id: str,
        claim_token: str,
        claim_generation: int,
        now: datetime | None = None,
        tenant_id: str,
        cell_id: str,
        backup_reference: str,
        object_sha256: str,
        provider_operation_id: str,
        provider_fence_generation: int,
    ) -> None:
        async with self._sessions.begin() as session:
            operation, _ = await _lock_active_claim(
                session,
                operation_id,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
            existing = await session.scalar(
                select(BackupRecord).where(BackupRecord.operation_id == operation_id)
            )
            digest = _reference_digest(backup_reference)
            if existing is not None:
                if (
                    existing.tenant_id != tenant_id
                    or existing.cell_id != cell_id
                    or existing.reference_digest != digest
                    or existing.object_sha256 != object_sha256
                    or existing.provider_operation_id != provider_operation_id
                    or existing.provider_fence_generation != provider_fence_generation
                ):
                    raise ImmutableMetadataConflict("backup provider metadata is immutable")
                _require_operation_identity(
                    operation,
                    tenant_id=existing.tenant_id,
                    cell_id=existing.cell_id,
                    provider_operation_id=existing.provider_operation_id,
                    provider_fence_generation=existing.provider_fence_generation,
                )
                return
            _require_operation_identity(
                operation,
                tenant_id=tenant_id,
                cell_id=cell_id,
                provider_operation_id=provider_operation_id,
                provider_fence_generation=provider_fence_generation,
            )
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
