"""Restart-safe durability ledger and immutable recovery-object registry."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .crypto import EnvelopeCodec
from .models import (
    CellOperationLock,
    DurabilityRun,
    DurabilityRunKind,
    DurabilityRunState,
    ExportDelivery,
    Operation,
    OperationState,
    ProviderDisposition,
    ProviderObservation,
    RecoveryObject,
    TenantFence,
)

RunKind = DurabilityRunKind


class DurabilityConflict(RuntimeError):
    pass


class ActiveDurabilityRun(DurabilityConflict):
    pass


class DurabilityClaimConflict(DurabilityConflict):
    pass


class ImmutableRecoveryConflict(DurabilityConflict):
    pass


@dataclass(frozen=True, slots=True)
class RunIdentity:
    kind: RunKind
    operation_id: str
    tenant_id: str
    cell_id: str
    fence_generation: int
    scheduled_for: datetime


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    id: str
    identity: RunIdentity
    status: DurabilityRunState
    checkpoint: str
    state: dict[str, Any]
    claim_token: str
    claim_generation: int
    claim_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class RecoveryObjectInput:
    opaque_reference: str
    provider_reference: str
    wrapped_data_key: str | None
    archive_sha256: str
    manifest_sha256: str
    archive_size: int
    ciphertext_sha256: str
    ciphertext_size: int
    metadata_sha256: str
    object_lock_until: datetime
    expires_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RecoveryObjectSnapshot:
    id: str
    run_id: str
    kind: RunKind
    tenant_id: str
    cell_id: str
    operation_id: str
    fence_generation: int
    opaque_reference: str
    provider_reference: str
    wrapped_data_key: str | None
    archive_sha256: str
    manifest_sha256: str
    archive_size: int
    ciphertext_sha256: str
    ciphertext_size: int
    metadata_sha256: str
    object_lock_until: datetime
    expires_at: datetime
    verified_at: datetime
    deleted_at: datetime | None
    key_destroyed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExportDeliverySnapshot:
    id: str
    source_object_id: str
    tenant_id: str
    cell_id: str
    operation_id: str
    fence_generation: int
    provider_reference: str
    expires_at: datetime
    verified_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class BackupFreshness:
    age_seconds: int | None
    warning: bool
    alpha_blocked: bool


@dataclass(frozen=True, slots=True)
class RediscoveryObservationInput:
    provider: str
    provider_reference: str
    tenant_id: str
    cell_id: str | None
    operation_id: str
    fence_generation: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class RediscoveryObservationSnapshot:
    provider: str
    operation_id: str
    tenant_id: str
    cell_id: str | None
    fence_generation: int
    disposition: str


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _now(value: datetime | None) -> datetime:
    return _utc(value) if value is not None else datetime.now(UTC)


async def _database_now(session: AsyncSession, explicit: datetime | None) -> datetime:
    if explicit is not None:
        return _utc(explicit)
    if session.get_bind().dialect.name == "postgresql":
        current = await session.scalar(select(func.clock_timestamp()))
        if current is None:
            raise RuntimeError("PostgreSQL did not return its current clock time")
        return _utc(current)
    return datetime.now(UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DurabilityRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        codec: EnvelopeCodec,
        lease_seconds: int = 60,
    ) -> None:
        self._sessions = session_factory
        self._codec = codec
        self._lease_seconds = lease_seconds

    @property
    def lease_seconds(self) -> float:
        return self._lease_seconds

    async def begin(self, identity: RunIdentity) -> RunSnapshot:
        if identity.fence_generation < 1:
            raise ValueError("durability run fence must be positive")
        try:
            async with self._sessions.begin() as session:
                fence = await session.get(TenantFence, identity.tenant_id, with_for_update=True)
                if fence is None:
                    session.add(
                        TenantFence(
                            tenant_id=identity.tenant_id,
                            fence_generation=identity.fence_generation,
                        )
                    )
                    await session.flush()
                elif identity.fence_generation < fence.fence_generation:
                    raise ImmutableRecoveryConflict("durability fence is older than tenant state")
                elif identity.fence_generation > fence.fence_generation:
                    fence.fence_generation = identity.fence_generation
                existing = await session.scalar(
                    select(DurabilityRun).where(DurabilityRun.operation_id == identity.operation_id)
                )
                if existing is not None:
                    self._require_identity(existing, identity)
                    return self._snapshot(existing, self._load_run_state(existing))
                active = await session.scalar(
                    select(DurabilityRun).where(
                        DurabilityRun.cell_id == identity.cell_id,
                        DurabilityRun.state.in_(
                            (DurabilityRunState.PENDING, DurabilityRunState.CLAIMED)
                        ),
                    )
                )
                if active is not None:
                    raise ActiveDurabilityRun("cell already has active durability work")
                run = DurabilityRun(
                    kind=identity.kind,
                    operation_id=identity.operation_id,
                    tenant_id=identity.tenant_id,
                    cell_id=identity.cell_id,
                    fence_generation=identity.fence_generation,
                    scheduled_for=_utc(identity.scheduled_for),
                    state=DurabilityRunState.PENDING,
                    checkpoint="requested",
                    state_ciphertext=self._codec.encrypt_json(
                        {}, purpose=f"durability-run:{identity.operation_id}"
                    ),
                )
                session.add(run)
                await session.flush()
                return self._snapshot(run, {})
        except IntegrityError:
            async with self._sessions() as session:
                replay = await session.scalar(
                    select(DurabilityRun).where(DurabilityRun.operation_id == identity.operation_id)
                )
                if replay is not None:
                    self._require_identity(replay, identity)
                    return self._snapshot(replay, self._load_run_state(replay))
            raise ActiveDurabilityRun("cell already has active durability work") from None

    async def claim(
        self,
        run_id: str,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> RunSnapshot:
        async with self._sessions.begin() as session:
            claimed_at = await _database_now(session, now)
            tenant_id = await session.scalar(
                select(DurabilityRun.tenant_id).where(DurabilityRun.id == run_id)
            )
            if tenant_id is None:
                raise KeyError(run_id)
            fence = await session.get(TenantFence, tenant_id, with_for_update=True)
            run = await session.get(DurabilityRun, run_id, with_for_update=True)
            if run is None:
                raise KeyError(run_id)
            if run.state in (DurabilityRunState.COMPLETE, DurabilityRunState.ERROR):
                raise DurabilityClaimConflict("durability run is terminal")
            if fence is None or fence.fence_generation != run.fence_generation:
                raise DurabilityClaimConflict("durability run fence is stale")
            if (
                run.state is DurabilityRunState.CLAIMED
                and run.claim_expires_at is not None
                and _utc(run.claim_expires_at) > claimed_at
            ):
                raise DurabilityClaimConflict("durability run already has an active claim")
            run.state = DurabilityRunState.CLAIMED
            run.claim_owner = worker_id
            run.claim_token = secrets.token_urlsafe(32)
            run.claim_generation += 1
            run.claim_expires_at = claimed_at + timedelta(seconds=self._lease_seconds)
            run.updated_at = claimed_at
            lock = await session.get(CellOperationLock, run.cell_id, with_for_update=True)
            if (
                lock is not None
                and _utc(lock.lease_expires_at) > claimed_at
                and lock.operation_id != run.operation_id
            ):
                raise ActiveDurabilityRun("cell operation lock is already held")
            if lock is None:
                session.add(
                    CellOperationLock(
                        cell_id=run.cell_id,
                        tenant_id=run.tenant_id,
                        operation_id=run.operation_id,
                        fence_generation=run.fence_generation,
                        lease_expires_at=run.claim_expires_at,
                        updated_at=claimed_at,
                    )
                )
            else:
                lock.tenant_id = run.tenant_id
                lock.operation_id = run.operation_id
                lock.fence_generation = run.fence_generation
                lock.lease_expires_at = max(_utc(lock.lease_expires_at), run.claim_expires_at)
                lock.updated_at = claimed_at
            await session.flush()
            return self._snapshot(run, self._load_run_state(run))

    async def checkpoint(
        self,
        run_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        checkpoint: str,
        state: dict[str, Any],
        now: datetime | None = None,
    ) -> RunSnapshot:
        async with self._sessions.begin() as session:
            checked_at = await _database_now(session, now)
            run = await self._active_claim(
                session,
                run_id,
                worker_id,
                claim_token,
                claim_generation,
                checked_at=checked_at,
            )
            run.checkpoint = checkpoint
            run.state_ciphertext = self._codec.encrypt_json(
                state, purpose=f"durability-run:{run.operation_id}"
            )
            run.updated_at = checked_at
            await session.flush()
            return self._snapshot(run, state)

    async def renew_claim(
        self,
        run_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        now: datetime | None = None,
    ) -> RunSnapshot:
        async with self._sessions.begin() as session:
            renewed_at = await _database_now(session, now)
            run = await self._active_claim(
                session,
                run_id,
                worker_id,
                claim_token,
                claim_generation,
                checked_at=renewed_at,
            )
            run.claim_expires_at = renewed_at + timedelta(seconds=self._lease_seconds)
            run.updated_at = renewed_at
            lock = await self._active_cell_lock(session, run, renewed_at)
            lock.lease_expires_at = max(_utc(lock.lease_expires_at), run.claim_expires_at)
            lock.updated_at = renewed_at
            await session.flush()
            return self._snapshot(run, self._load_run_state(run))

    async def mark_pending(
        self,
        run_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        checkpoint: str,
        state: dict[str, Any],
        now: datetime | None = None,
    ) -> RunSnapshot:
        snapshot = await self.checkpoint(
            run_id,
            worker_id,
            claim_token=claim_token,
            claim_generation=claim_generation,
            checkpoint=checkpoint,
            state=state,
            now=now,
        )
        async with self._sessions.begin() as session:
            run = await session.get(DurabilityRun, run_id, with_for_update=True)
            if run is None or run.claim_token != claim_token:
                raise DurabilityClaimConflict("durability claim was lost")
            run.state = DurabilityRunState.PENDING
            run.claim_owner = None
            run.claim_token = None
            run.claim_expires_at = None
            await self._release_uncoordinated_cell_lock(session, run)
            await session.flush()
            return self._snapshot(run, snapshot.state)

    async def complete(
        self,
        run_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        result: dict[str, Any],
        now: datetime | None = None,
    ) -> RunSnapshot:
        async with self._sessions.begin() as session:
            completed_at = await _database_now(session, now)
            run = await self._active_claim(
                session,
                run_id,
                worker_id,
                claim_token,
                claim_generation,
                checked_at=completed_at,
            )
            run.state = DurabilityRunState.COMPLETE
            run.checkpoint = "complete"
            run.result_ciphertext = self._codec.encrypt_json(
                result, purpose=f"durability-result:{run.operation_id}"
            )
            run.claim_owner = None
            run.claim_token = None
            run.claim_expires_at = None
            run.completed_at = completed_at
            await self._release_uncoordinated_cell_lock(session, run)
            await session.flush()
            return self._snapshot(run, self._load_run_state(run))

    async def fail(
        self,
        run_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        code: str,
        now: datetime | None = None,
    ) -> RunSnapshot:
        """End a run with one bounded, content-free failure code."""

        if not code or len(code) > 128:
            raise ValueError("durability failure code is invalid")
        async with self._sessions.begin() as session:
            failed_at = await _database_now(session, now)
            run = await self._active_claim(
                session,
                run_id,
                worker_id,
                claim_token,
                claim_generation,
                checked_at=failed_at,
            )
            state = {"error_code": code}
            run.state = DurabilityRunState.ERROR
            run.checkpoint = "error"
            run.state_ciphertext = self._codec.encrypt_json(
                state,
                purpose=f"durability-run:{run.operation_id}",
            )
            run.claim_owner = None
            run.claim_token = None
            run.claim_expires_at = None
            run.completed_at = failed_at
            run.updated_at = failed_at
            await self._release_uncoordinated_cell_lock(session, run)
            await session.flush()
            return self._snapshot(run, state)

    async def get(self, run_id: str) -> RunSnapshot | None:
        async with self._sessions() as session:
            run = await session.get(DurabilityRun, run_id)
            return self._snapshot(run, self._load_run_state(run)) if run is not None else None

    async def load_result(self, run_id: str) -> dict[str, Any] | None:
        """Return a completed result for lost-acknowledgement replay."""

        async with self._sessions() as session:
            run = await session.get(DurabilityRun, run_id)
            if run is None or run.result_ciphertext is None:
                return None
            return self._codec.decrypt_json(
                run.result_ciphertext,
                purpose=f"durability-result:{run.operation_id}",
            )

    async def record_verified_object(
        self,
        run_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        value: RecoveryObjectInput,
        verified_at: datetime | None = None,
    ) -> RecoveryObjectSnapshot:
        async with self._sessions.begin() as session:
            checked_at = await _database_now(session, verified_at)
            run = await self._active_claim(
                session,
                run_id,
                worker_id,
                claim_token,
                claim_generation,
                checked_at=checked_at,
            )
            existing = await session.scalar(
                select(RecoveryObject).where(RecoveryObject.run_id == run.id)
            )
            if existing is not None:
                snapshot = self._object_snapshot(existing)
                if snapshot != self._expected_object_snapshot(
                    existing.id, run, value, snapshot.verified_at
                ):
                    raise ImmutableRecoveryConflict("recovery object metadata is immutable")
                return snapshot
            record = RecoveryObject(
                run_id=run.id,
                kind=run.kind,
                tenant_id=run.tenant_id,
                cell_id=run.cell_id,
                operation_id=run.operation_id,
                fence_generation=run.fence_generation,
                opaque_reference_digest=_digest(value.opaque_reference),
                secret_ciphertext=self._codec.encrypt_json(
                    {
                        "opaque_reference": value.opaque_reference,
                        "provider_reference": value.provider_reference,
                        "wrapped_data_key": value.wrapped_data_key,
                    },
                    purpose=f"recovery-object:{run.operation_id}",
                ),
                archive_sha256=value.archive_sha256,
                manifest_sha256=value.manifest_sha256,
                archive_size=value.archive_size,
                ciphertext_sha256=value.ciphertext_sha256,
                ciphertext_size=value.ciphertext_size,
                metadata_sha256=value.metadata_sha256,
                object_lock_until=_utc(value.object_lock_until),
                expires_at=_utc(value.expires_at),
                verified_at=checked_at,
            )
            session.add(record)
            await session.flush()
            return self._object_snapshot(record)

    async def get_recovery_object(self, opaque_reference: str) -> RecoveryObjectSnapshot | None:
        async with self._sessions() as session:
            record = await session.scalar(
                select(RecoveryObject).where(
                    RecoveryObject.opaque_reference_digest == _digest(opaque_reference)
                )
            )
            return self._object_snapshot(record) if record is not None else None

    async def get_recovery_object_by_release_reference(
        self, release_reference: str
    ) -> RecoveryObjectSnapshot | None:
        if not release_reference.startswith("release_"):
            return None
        try:
            run_id = str(uuid.UUID(hex=release_reference.removeprefix("release_")))
        except ValueError:
            return None
        async with self._sessions() as session:
            record = await session.scalar(
                select(RecoveryObject).where(RecoveryObject.run_id == run_id)
            )
            return self._object_snapshot(record) if record is not None else None

    async def expired_export_objects(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[RecoveryObjectSnapshot]:
        if not 1 <= limit <= 1000:
            raise ValueError("expired export batch size is invalid")
        async with self._sessions() as session:
            checked_at = await _database_now(session, now)
            records = (
                await session.scalars(
                    select(RecoveryObject)
                    .where(
                        RecoveryObject.kind == RunKind.USER_EXPORT,
                        RecoveryObject.expires_at <= checked_at,
                        RecoveryObject.deleted_at.is_(None),
                    )
                    .order_by(RecoveryObject.expires_at, RecoveryObject.id)
                    .limit(limit)
                )
            ).all()
            return [self._object_snapshot(record) for record in records]

    async def mark_recovery_object_deleted(
        self,
        opaque_reference: str,
        *,
        tenant_id: str,
        deleted_at: datetime | None = None,
    ) -> RecoveryObjectSnapshot:
        async with self._sessions.begin() as session:
            checked_at = await _database_now(session, deleted_at)
            record = await session.scalar(
                select(RecoveryObject)
                .where(RecoveryObject.opaque_reference_digest == _digest(opaque_reference))
                .with_for_update()
            )
            if record is None or record.tenant_id != tenant_id:
                raise KeyError(opaque_reference)
            if record.deleted_at is None:
                record.deleted_at = checked_at
                await session.flush()
            return self._object_snapshot(record)

    async def tenant_recovery_objects(self, tenant_id: str) -> list[RecoveryObjectSnapshot]:
        async with self._sessions() as session:
            records = (
                await session.scalars(
                    select(RecoveryObject)
                    .where(RecoveryObject.tenant_id == tenant_id)
                    .order_by(RecoveryObject.verified_at, RecoveryObject.id)
                )
            ).all()
            return [self._object_snapshot(record) for record in records]

    async def record_export_delivery(
        self,
        *,
        source_object_id: str,
        tenant_id: str,
        provider_reference: str,
        expires_at: datetime,
        verified_at: datetime | None = None,
    ) -> ExportDeliverySnapshot:
        async with self._sessions.begin() as session:
            checked_at = await _database_now(session, verified_at)
            source = await session.get(RecoveryObject, source_object_id, with_for_update=True)
            if (
                source is None
                or source.tenant_id != tenant_id
                or source.kind is not RunKind.USER_EXPORT
                or source.deleted_at is not None
                or source.key_destroyed_at is not None
            ):
                raise ImmutableRecoveryConflict("export delivery source is unavailable")
            expiry = _utc(expires_at)
            if expiry <= checked_at or expiry > _utc(source.expires_at):
                raise ImmutableRecoveryConflict("export delivery expiry is outside source bounds")
            reference_digest = _digest(provider_reference)
            existing = await session.scalar(
                select(ExportDelivery).where(
                    ExportDelivery.provider_reference_digest == reference_digest
                )
            )
            if existing is not None:
                snapshot = self._delivery_snapshot(existing)
                if (
                    snapshot.source_object_id != source.id
                    or snapshot.tenant_id != source.tenant_id
                    or snapshot.cell_id != source.cell_id
                    or snapshot.operation_id != source.operation_id
                    or snapshot.fence_generation != source.fence_generation
                    or snapshot.provider_reference != provider_reference
                    or snapshot.expires_at != expiry
                ):
                    raise ImmutableRecoveryConflict("export delivery identity is immutable")
                return snapshot
            record_id = str(uuid.uuid4())
            record = ExportDelivery(
                id=record_id,
                source_object_id=source.id,
                tenant_id=source.tenant_id,
                cell_id=source.cell_id,
                operation_id=source.operation_id,
                fence_generation=source.fence_generation,
                provider_reference_digest=reference_digest,
                provider_reference_ciphertext=self._codec.encrypt_json(
                    {"provider_reference": provider_reference},
                    purpose=f"export-delivery:{record_id}",
                ),
                expires_at=expiry,
                verified_at=checked_at,
            )
            session.add(record)
            await session.flush()
            return self._delivery_snapshot(record)

    async def tenant_export_deliveries(self, tenant_id: str) -> list[ExportDeliverySnapshot]:
        async with self._sessions() as session:
            records = (
                await session.scalars(
                    select(ExportDelivery)
                    .where(ExportDelivery.tenant_id == tenant_id)
                    .order_by(ExportDelivery.verified_at, ExportDelivery.id)
                )
            ).all()
            return [self._delivery_snapshot(record) for record in records]

    async def expired_export_deliveries(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1000,
    ) -> list[ExportDeliverySnapshot]:
        if not 1 <= limit <= 10_000:
            raise ValueError("expired delivery batch size is invalid")
        async with self._sessions() as session:
            checked_at = await _database_now(session, now)
            records = (
                await session.scalars(
                    select(ExportDelivery)
                    .where(
                        ExportDelivery.expires_at <= checked_at,
                        ExportDelivery.deleted_at.is_(None),
                    )
                    .order_by(ExportDelivery.expires_at, ExportDelivery.id)
                    .limit(limit)
                )
            ).all()
            return [self._delivery_snapshot(record) for record in records]

    async def mark_export_delivery_deleted(
        self,
        reference: str,
        *,
        tenant_id: str,
        deleted_at: datetime | None = None,
    ) -> ExportDeliverySnapshot:
        async with self._sessions.begin() as session:
            checked_at = await _database_now(session, deleted_at)
            record = await session.get(ExportDelivery, reference, with_for_update=True)
            if record is None or record.tenant_id != tenant_id:
                raise KeyError(reference)
            if record.deleted_at is None:
                record.deleted_at = checked_at
                await session.flush()
            return self._delivery_snapshot(record)

    async def destroy_recovery_wrapped_key(
        self,
        opaque_reference: str,
        *,
        tenant_id: str,
        destroyed_at: datetime | None = None,
    ) -> RecoveryObjectSnapshot:
        async with self._sessions.begin() as session:
            checked_at = await _database_now(session, destroyed_at)
            record = await session.scalar(
                select(RecoveryObject)
                .where(RecoveryObject.opaque_reference_digest == _digest(opaque_reference))
                .with_for_update()
            )
            if record is None or record.tenant_id != tenant_id:
                raise KeyError(opaque_reference)
            if record.deleted_at is None:
                raise ImmutableRecoveryConflict(
                    "wrapped key cannot be destroyed before provider absence proof"
                )
            if record.key_destroyed_at is None:
                secret = self._codec.decrypt_json(
                    record.secret_ciphertext,
                    purpose=f"recovery-object:{record.operation_id}",
                )
                record.secret_ciphertext = self._codec.encrypt_json(
                    {
                        "opaque_reference": secret["opaque_reference"],
                        "provider_reference": secret["provider_reference"],
                    },
                    purpose=f"recovery-object:{record.operation_id}",
                )
                record.key_destroyed_at = checked_at
                await session.flush()
            return self._object_snapshot(record)

    async def backup_freshness(
        self,
        cell_id: str,
        *,
        kind: RunKind = RunKind.VAULT_BACKUP,
        now: datetime | None = None,
    ) -> BackupFreshness:
        if kind not in {RunKind.VAULT_BACKUP, RunKind.DATABASE_BACKUP}:
            raise ValueError("freshness applies only to backup run kinds")
        async with self._sessions() as session:
            checked_at = await _database_now(session, now)
            verified_at = await session.scalar(
                select(RecoveryObject.verified_at)
                .where(
                    RecoveryObject.cell_id == cell_id,
                    RecoveryObject.kind == kind,
                    RecoveryObject.deleted_at.is_(None),
                )
                .order_by(RecoveryObject.verified_at.desc())
                .limit(1)
            )
        if verified_at is None:
            return BackupFreshness(age_seconds=None, warning=True, alpha_blocked=True)
        age = max(0, int((checked_at - _utc(verified_at)).total_seconds()))
        return BackupFreshness(
            age_seconds=age,
            warning=age >= 45 * 60,
            alpha_blocked=age >= 60 * 60,
        )

    async def tenant_fence(self, tenant_id: str) -> int | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(TenantFence.fence_generation).where(TenantFence.tenant_id == tenant_id)
            )

    async def reconcile_provider_observations(
        self,
        observations: list[RediscoveryObservationInput],
        *,
        maximum_fences: dict[str, int],
    ) -> list[RediscoveryObservationSnapshot]:
        computed: dict[str, int] = {}
        for value in observations:
            computed[value.tenant_id] = max(
                computed.get(value.tenant_id, 0), value.fence_generation
            )
        if computed != maximum_fences:
            raise ImmutableRecoveryConflict("provider maximum fence proof differs")
        snapshots: list[RediscoveryObservationSnapshot] = []
        async with self._sessions.begin() as session:
            # Raise durable tenant guards to the fully scanned provider maximum
            # before classifying any side effect for adoption.
            for tenant_id in sorted(maximum_fences):
                fence = await session.get(TenantFence, tenant_id, with_for_update=True)
                maximum = maximum_fences[tenant_id]
                if fence is None:
                    session.add(TenantFence(tenant_id=tenant_id, fence_generation=maximum))
                elif maximum > fence.fence_generation:
                    fence.fence_generation = maximum
            await session.flush()

            for value in observations:
                reference_digest = _digest(value.provider_reference)
                existing = await session.scalar(
                    select(ProviderObservation).where(
                        ProviderObservation.provider == value.provider,
                        ProviderObservation.reference_digest == reference_digest,
                    )
                )
                if existing is not None:
                    if (
                        existing.tenant_id != value.tenant_id
                        or existing.cell_id != value.cell_id
                        or existing.operation_id != value.operation_id
                        or existing.fence_generation != value.fence_generation
                    ):
                        raise ImmutableRecoveryConflict(
                            "provider observation identity is immutable"
                        )
                    disposition = existing.disposition
                else:
                    known_operation = await session.scalar(
                        select(Operation).where(
                            Operation.external_operation_id == value.operation_id,
                            Operation.tenant_id == value.tenant_id,
                        )
                    )
                    known_durability = await session.scalar(
                        select(DurabilityRun).where(
                            DurabilityRun.operation_id == value.operation_id,
                            DurabilityRun.tenant_id == value.tenant_id,
                        )
                    )
                    operation_matches = known_operation is not None and (
                        known_operation.cell_id == value.cell_id
                        and known_operation.fence_generation == value.fence_generation
                    )
                    durability_matches = known_durability is not None and (
                        known_durability.cell_id == value.cell_id
                        and known_durability.fence_generation == value.fence_generation
                    )
                    disposition = (
                        ProviderDisposition.ADOPTED
                        if operation_matches or durability_matches
                        else ProviderDisposition.QUARANTINED
                    )
                    session.add(
                        ProviderObservation(
                            provider=value.provider,
                            reference_digest=reference_digest,
                            reference_ciphertext=self._codec.encrypt_json(
                                {"reference": value.provider_reference},
                                purpose=f"provider-observation:{value.provider}:{reference_digest}",
                            ),
                            tenant_id=value.tenant_id,
                            cell_id=value.cell_id,
                            operation_id=value.operation_id,
                            fence_generation=value.fence_generation,
                            disposition=disposition,
                            observed_at=_utc(value.observed_at),
                        )
                    )
                snapshots.append(
                    RediscoveryObservationSnapshot(
                        provider=value.provider,
                        operation_id=value.operation_id,
                        tenant_id=value.tenant_id,
                        cell_id=value.cell_id,
                        fence_generation=value.fence_generation,
                        disposition=disposition.value,
                    )
                )
        return snapshots

    async def _active_claim(
        self,
        session: AsyncSession,
        run_id: str,
        worker_id: str,
        claim_token: str,
        claim_generation: int,
        *,
        checked_at: datetime,
    ) -> DurabilityRun:
        tenant_id = await session.scalar(
            select(DurabilityRun.tenant_id).where(DurabilityRun.id == run_id)
        )
        if tenant_id is None:
            raise DurabilityClaimConflict("durability run does not exist")
        fence = await session.get(TenantFence, tenant_id, with_for_update=True)
        run = await session.get(DurabilityRun, run_id, with_for_update=True)
        if (
            run is None
            or run.state is not DurabilityRunState.CLAIMED
            or run.claim_owner != worker_id
            or run.claim_token != claim_token
            or run.claim_generation != claim_generation
            or run.claim_expires_at is None
            or _utc(run.claim_expires_at) <= checked_at
        ):
            raise DurabilityClaimConflict("worker no longer owns durability claim")
        if fence is None or fence.fence_generation != run.fence_generation:
            raise DurabilityClaimConflict("durability run fence is stale")
        await self._active_cell_lock(session, run, checked_at)
        return run

    @staticmethod
    async def _active_cell_lock(
        session: AsyncSession,
        run: DurabilityRun,
        checked_at: datetime,
    ) -> CellOperationLock:
        lock = await session.get(CellOperationLock, run.cell_id, with_for_update=True)
        if (
            lock is None
            or lock.tenant_id != run.tenant_id
            or lock.operation_id != run.operation_id
            or lock.fence_generation != run.fence_generation
            or _utc(lock.lease_expires_at) <= checked_at
        ):
            raise DurabilityClaimConflict("worker no longer owns shared cell operation lock")
        return lock

    @staticmethod
    async def _release_uncoordinated_cell_lock(
        session: AsyncSession,
        run: DurabilityRun,
    ) -> None:
        outer = await session.scalar(
            select(Operation.id).where(
                Operation.external_operation_id == run.operation_id,
                Operation.state == OperationState.CLAIMED,
            )
        )
        if outer is not None:
            return
        lock = await session.get(CellOperationLock, run.cell_id, with_for_update=True)
        if lock is not None and lock.operation_id == run.operation_id:
            await session.delete(lock)

    def _load_run_state(self, run: DurabilityRun) -> dict[str, Any]:
        return self._codec.decrypt_json(
            run.state_ciphertext, purpose=f"durability-run:{run.operation_id}"
        )

    def _snapshot(self, run: DurabilityRun, state: dict[str, Any]) -> RunSnapshot:
        return RunSnapshot(
            id=run.id,
            identity=RunIdentity(
                kind=run.kind,
                operation_id=run.operation_id,
                tenant_id=run.tenant_id,
                cell_id=run.cell_id,
                fence_generation=run.fence_generation,
                scheduled_for=_utc(run.scheduled_for),
            ),
            status=run.state,
            checkpoint=run.checkpoint,
            state=state,
            claim_token=run.claim_token or "",
            claim_generation=run.claim_generation,
            claim_expires_at=(
                _utc(run.claim_expires_at) if run.claim_expires_at is not None else None
            ),
        )

    @staticmethod
    def _require_identity(run: DurabilityRun, identity: RunIdentity) -> None:
        if (
            run.kind != identity.kind
            or run.operation_id != identity.operation_id
            or run.tenant_id != identity.tenant_id
            or run.cell_id != identity.cell_id
            or run.fence_generation != identity.fence_generation
            or _utc(run.scheduled_for) != _utc(identity.scheduled_for)
        ):
            raise ImmutableRecoveryConflict("durability operation identity is immutable")

    def _object_snapshot(self, record: RecoveryObject) -> RecoveryObjectSnapshot:
        secret = self._codec.decrypt_json(
            record.secret_ciphertext, purpose=f"recovery-object:{record.operation_id}"
        )
        return RecoveryObjectSnapshot(
            id=record.id,
            run_id=record.run_id,
            kind=record.kind,
            tenant_id=record.tenant_id,
            cell_id=record.cell_id,
            operation_id=record.operation_id,
            fence_generation=record.fence_generation,
            opaque_reference=str(secret["opaque_reference"]),
            provider_reference=str(secret["provider_reference"]),
            wrapped_data_key=(
                str(secret["wrapped_data_key"]) if "wrapped_data_key" in secret else None
            ),
            archive_sha256=record.archive_sha256,
            manifest_sha256=record.manifest_sha256,
            archive_size=record.archive_size,
            ciphertext_sha256=record.ciphertext_sha256,
            ciphertext_size=record.ciphertext_size,
            metadata_sha256=record.metadata_sha256,
            object_lock_until=_utc(record.object_lock_until),
            expires_at=_utc(record.expires_at),
            verified_at=_utc(record.verified_at),
            deleted_at=_utc(record.deleted_at) if record.deleted_at is not None else None,
            key_destroyed_at=(
                _utc(record.key_destroyed_at) if record.key_destroyed_at is not None else None
            ),
        )

    def _delivery_snapshot(self, record: ExportDelivery) -> ExportDeliverySnapshot:
        secret = self._codec.decrypt_json(
            record.provider_reference_ciphertext,
            purpose=f"export-delivery:{record.id}",
        )
        return ExportDeliverySnapshot(
            id=record.id,
            source_object_id=record.source_object_id,
            tenant_id=record.tenant_id,
            cell_id=record.cell_id,
            operation_id=record.operation_id,
            fence_generation=record.fence_generation,
            provider_reference=str(secret["provider_reference"]),
            expires_at=_utc(record.expires_at),
            verified_at=_utc(record.verified_at),
            deleted_at=_utc(record.deleted_at) if record.deleted_at is not None else None,
        )

    def _expected_object_snapshot(
        self,
        record_id: str,
        run: DurabilityRun,
        value: RecoveryObjectInput,
        verified_at: datetime,
    ) -> RecoveryObjectSnapshot:
        return RecoveryObjectSnapshot(
            id=record_id,
            run_id=run.id,
            kind=run.kind,
            tenant_id=run.tenant_id,
            cell_id=run.cell_id,
            operation_id=run.operation_id,
            fence_generation=run.fence_generation,
            opaque_reference=value.opaque_reference,
            provider_reference=value.provider_reference,
            wrapped_data_key=value.wrapped_data_key,
            archive_sha256=value.archive_sha256,
            manifest_sha256=value.manifest_sha256,
            archive_size=value.archive_size,
            ciphertext_sha256=value.ciphertext_sha256,
            ciphertext_size=value.ciphertext_size,
            metadata_sha256=value.metadata_sha256,
            object_lock_until=_utc(value.object_lock_until),
            expires_at=_utc(value.expires_at),
            verified_at=verified_at,
            deleted_at=None,
            key_destroyed_at=None,
        )
