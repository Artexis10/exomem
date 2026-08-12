"""Fail-closed operator recovery for one hosted init-retry false negative."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from sqlalchemy import cast, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import DeploymentLock
from .crypto import EnvelopeCodec
from .database import DATABASE_REVISION
from .database_bootstrap import database_lock_key
from .models import (
    CapacityDestructiveFence,
    CapacityLedger,
    CapacityReservation,
    CellOperationLock,
    Operation,
    OperationAction,
    OperationState,
    Resource,
    ResourceKind,
    TenantFence,
)
from .provider_identity import cell_resource_name
from .repository import canonical_request_sha256

_FIXED_MODES = ("preflight", "reopen", "inspect", "verify-recovery")
_RECOVERY_MARKER = "_init_retry_recovery_v1"
_RECOVERY_MARKER_KEYS = frozenset(
    {"schema", "preflight_sha256", "helper_source_sha256", "claim_generation", "committed_at"}
)
_OUTPUT_KEYS = frozenset(
    {
        "status",
        "refusal",
        "state",
        "checkpoint",
        "error_code",
        "resource_kind_counts",
        "active_reservation",
        "final_proof",
        "recovery_digest",
    }
)


class RecoveryRefusal(RuntimeError):
    """A content-free no-op refusal."""


class _RecoveryArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise RecoveryRefusal("command arguments are invalid")


def _json_value(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError("recovery value is not canonical JSON")


def canonical_receipt_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    if not isinstance(value, dict):
        raise TypeError("recovery hashes require object values")
    return hashlib.sha256(canonical_receipt_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class LiveObservation:
    namespace_present: bool
    release_present: bool
    pvc_bound: bool
    volume_present: bool
    init_job_present: bool
    init_complete: bool
    init_failed_only: bool
    terminating: bool
    runtime_admitted: bool
    routes_present: int
    identity_digest: str = ""


@dataclass(frozen=True, slots=True)
class OperationPreState:
    action: str
    state: str
    checkpoint: str
    error_code: str | None
    has_claim: bool
    has_result: bool
    finalized: bool


class RecoveryLiveObserver(Protocol):
    async def observe(
        self,
        operation: Operation,
        resources: tuple[Resource, ...],
    ) -> LiveObservation: ...


@dataclass(frozen=True, slots=True)
class _RecoverySnapshot:
    operation: Operation
    resources: tuple[Resource, ...]
    reservation: CapacityReservation
    operation_sha256: str
    preserved_sha256: str
    request_sha256: str
    request_ciphertext_sha256: str
    resources_sha256: str
    reservation_sha256: str
    tenant_fence_sha256: str


def read_operation_identity(*, stdin: str | None = None) -> str:
    if stdin is None:
        raise RecoveryRefusal("operation identity source is invalid")
    raw = stdin
    if raw.count("\n") != 1 or not raw.endswith("\n"):
        raise RecoveryRefusal("operation identity is invalid")
    value = raw[:-1]
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise RecoveryRefusal("operation identity is invalid") from error
    if str(parsed) != value:
        raise RecoveryRefusal("operation identity is invalid")
    return value


def require_postgresql(dialect: str) -> None:
    if dialect != "postgresql":
        raise RecoveryRefusal("PostgreSQL is required")


def validate_live_observation(observation: LiveObservation) -> None:
    if (
        not observation.namespace_present
        or not observation.release_present
        or not observation.pvc_bound
        or not observation.volume_present
        or (observation.init_job_present and not observation.init_complete)
        or observation.init_failed_only
        or observation.terminating
        or observation.runtime_admitted
        or observation.routes_present != 0
        or (observation.identity_digest and len(observation.identity_digest) != 64)
    ):
        raise RecoveryRefusal("live recovery preflight failed")


def recovery_transition_values(before: OperationPreState) -> dict[str, object]:
    if (
        before.action != "provision"
        or before.state != "error"
        or before.checkpoint != "failed"
        or before.error_code != "PROVISIONER_PROVIDER_METADATA_CONFLICT"
        or before.has_claim
        or before.has_result
        or not before.finalized
    ):
        if before.state in {"pending", "claimed", "final"}:
            raise RecoveryRefusal("already progressed")
        raise RecoveryRefusal("recovery preflight failed")
    return {
        "state": OperationState.PENDING,
        "checkpoint": "volume-owned",
        "error_code": None,
        "claim_owner": None,
        "claim_token": None,
        "claim_expires_at": None,
        "finalized_at": None,
    }


def recovery_marker(
    *,
    preflight_sha256: str,
    helper_source_sha256: str,
    claim_generation: int,
    committed_at: datetime,
) -> dict[str, object]:
    marker = {
        "schema": 1,
        "preflight_sha256": preflight_sha256,
        "helper_source_sha256": helper_source_sha256,
        "claim_generation": claim_generation,
        "committed_at": _json_value(committed_at),
    }
    return parse_recovery_marker(marker)


def parse_recovery_marker(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _RECOVERY_MARKER_KEYS:
        raise RecoveryRefusal("recovery marker is invalid")
    if (
        value.get("schema") != 1
        or not isinstance(value.get("claim_generation"), int)
        or value["claim_generation"] < 0
        or not isinstance(value.get("committed_at"), str)
        or any(
            not isinstance(value.get(key), str) or len(value[key]) != 64
            for key in ("preflight_sha256", "helper_source_sha256")
        )
    ):
        raise RecoveryRefusal("recovery marker is invalid")
    try:
        datetime.fromisoformat(value["committed_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise RecoveryRefusal("recovery marker is invalid") from error
    return dict(value)


def _hash_model(model: object, *, excluded: frozenset[str] = frozenset()) -> str:
    table = model.__table__  # type: ignore[attr-defined]
    return canonical_sha256(
        {
            column.name: getattr(model, column.name)
            for column in table.columns
            if column.name not in excluded
        }
    )


class RecoveryService:
    """The only recovery mutation, bound to a one-way operation progress marker."""

    _CHANGED_COLUMNS = frozenset(
        {
            "state",
            "checkpoint",
            "error_code",
            "claim_owner",
            "claim_token",
            "claim_expires_at",
            "finalized_at",
            "available_at",
            "updated_at",
            "progress",
        }
    )

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        codec: EnvelopeCodec,
        database_name: str,
        database_role: str,
        database_schema: str,
        database_lock_timeout_seconds: int,
        deployment_lock: DeploymentLock,
        observer: RecoveryLiveObserver,
    ) -> None:
        self._sessions = sessions
        self._codec = codec
        self._database_name = database_name
        self._database_role = database_role
        self._database_schema = database_schema
        self._database_lock_timeout_seconds = database_lock_timeout_seconds
        self._deployment_lock = deployment_lock
        self._observer = observer
        self._helper_source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    async def preflight(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                snapshot = await self._preflight(session, operation_id)
                observation = await self._observer.observe(snapshot.operation, snapshot.resources)
                validate_live_observation(observation)
                return {
                    "status": "ready",
                    "state": snapshot.operation.state.value,
                    "checkpoint": snapshot.operation.checkpoint,
                    "resource_kind_counts": {
                        **{
                            kind.value: 1
                            for kind in (
                                ResourceKind.HELM_RELEASE,
                                ResourceKind.KUBERNETES_NAMESPACE,
                                ResourceKind.PVC,
                                ResourceKind.VOLUME,
                            )
                        },
                        ResourceKind.ROUTE.value: 0,
                        "provider-object": 0,
                    },
                    "active_reservation": True,
                }
        except RecoveryRefusal:
            raise
        except Exception as error:  # content-free boundary for database/provider failures
            raise RecoveryRefusal("preflight-failed") from error

    async def reopen(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                await self._require_database_identity(session)
                current = await session.get(Operation, operation_id, with_for_update=True)
                if current is None:
                    raise RecoveryRefusal("operation is unavailable")
                marker = current.progress.get(_RECOVERY_MARKER)
                if marker is not None:
                    return self._recovery_result(current, marker, "already-recovered")
                snapshot = await self._preflight(session, operation_id)
                first = await self._observer.observe(snapshot.operation, snapshot.resources)
                validate_live_observation(first)
                second = await self._observer.observe(snapshot.operation, snapshot.resources)
                validate_live_observation(second)
                if first != second:
                    raise RecoveryRefusal("live recovery preflight failed")
                now = await session.scalar(select(func.clock_timestamp()))
                if not isinstance(now, datetime):
                    raise RecoveryRefusal("database clock is unavailable")
                before = snapshot.operation
                evidence_digest = self._preflight_evidence_digest(snapshot, first, second)
                progress = {
                    **before.progress,
                    _RECOVERY_MARKER: recovery_marker(
                        preflight_sha256=evidence_digest,
                        helper_source_sha256=self._helper_source_sha256,
                        claim_generation=before.claim_generation,
                        committed_at=now,
                    ),
                }
                updated = await session.scalar(
                    update(Operation)
                    .where(
                        Operation.id == before.id,
                        Operation.action == OperationAction.PROVISION,
                        Operation.state == OperationState.ERROR,
                        Operation.checkpoint == "failed",
                        Operation.error_code == "PROVISIONER_PROVIDER_METADATA_CONFLICT",
                        Operation.claim_owner.is_(None),
                        Operation.claim_token.is_(None),
                        Operation.claim_expires_at.is_(None),
                        Operation.result_ciphertext.is_(None),
                        cast(Operation.result_redacted, JSONB) == cast({}, JSONB),
                        Operation.finalized_at.is_not(None),
                        Operation.external_operation_id == Operation.provider_operation_id,
                        Operation.fence_generation == Operation.provider_fence_generation,
                        Operation.canonical_request_sha256 == before.canonical_request_sha256,
                        Operation.claim_generation == before.claim_generation,
                        Operation.updated_at == before.updated_at,
                        cast(Operation.progress, JSONB) == cast(before.progress, JSONB),
                        ~cast(Operation.progress, JSONB).has_key(_RECOVERY_MARKER),
                    )
                    .values(
                        state=OperationState.PENDING,
                        checkpoint="volume-owned",
                        error_code=None,
                        claim_owner=None,
                        claim_token=None,
                        claim_expires_at=None,
                        finalized_at=None,
                        available_at=now,
                        updated_at=now,
                        progress=progress,
                    )
                    .returning(Operation)
                )
                if updated is None:
                    reread = await session.get(Operation, operation_id, with_for_update=True)
                    if reread is not None and _RECOVERY_MARKER in reread.progress:
                        return self._recovery_result(
                            reread, reread.progress[_RECOVERY_MARKER], "already-recovered"
                        )
                    raise RecoveryRefusal("already progressed")
                await session.flush()
                return self._recovery_result(updated, progress[_RECOVERY_MARKER], "reopened")
        except RecoveryRefusal:
            raise
        except Exception as error:  # marker and transition roll back together
            raise RecoveryRefusal("recovery-failed") from error

    async def inspect(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                await self._require_database_identity(session)
                operation = await session.get(Operation, operation_id)
                if operation is None:
                    raise RecoveryRefusal("operation is unavailable")
                resources = tuple(
                    await session.scalars(
                        select(Resource).where(Resource.operation_id == operation.id)
                    )
                )
                reservation = await session.scalar(
                    select(CapacityReservation).where(
                        CapacityReservation.reserving_operation_id == operation.id,
                        CapacityReservation.released_at.is_(None),
                    )
                )
                return {
                    "status": "inspected",
                    "state": operation.state.value,
                    "checkpoint": operation.checkpoint,
                    "error_code": operation.error_code,
                    "resource_kind_counts": {
                        kind.value: sum(item.kind is kind for item in resources)
                        for kind in ResourceKind
                    },
                    "active_reservation": reservation is not None,
                    "final_proof": self._final_proof(operation),
                }
        except RecoveryRefusal:
            raise
        except Exception as error:
            raise RecoveryRefusal("inspect-failed") from error

    async def verify_recovery(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                await self._require_database_identity(session)
                operation = await session.get(Operation, operation_id)
                if operation is None:
                    raise RecoveryRefusal("operation is unavailable")
                marker = operation.progress.get(_RECOVERY_MARKER)
                if marker is not None:
                    return self._recovery_result(operation, marker, "verified")
                try:
                    recovery_transition_values(self._operation_pre_state(operation))
                except RecoveryRefusal as error:
                    raise RecoveryRefusal("recovery attribution is unavailable") from error
                return {
                    "status": "not-run",
                    "state": operation.state.value,
                    "checkpoint": operation.checkpoint,
                }
        except RecoveryRefusal:
            raise
        except Exception as error:
            raise RecoveryRefusal("recovery-verification-failed") from error

    async def _preflight(self, session: AsyncSession, operation_id: str) -> _RecoverySnapshot:
        await self._require_database_identity(session)
        initial = await session.get(Operation, operation_id)
        if initial is None:
            raise RecoveryRefusal("operation is unavailable")
        fence = await session.get(TenantFence, initial.tenant_id, with_for_update=True)
        operation = await session.get(Operation, operation_id, with_for_update=True)
        if (
            operation is None
            or fence is None
            or fence.tenant_id != operation.tenant_id
            or fence.fence_generation != operation.fence_generation
        ):
            raise RecoveryRefusal("recovery preflight failed")
        return await self._preflight_locked(session, operation, fence)

    async def _require_database_identity(self, session: AsyncSession) -> None:
        require_postgresql(session.get_bind().dialect.name)
        key = database_lock_key(self._database_name, self._database_schema)
        deadline = asyncio.get_running_loop().time() + self._database_lock_timeout_seconds
        while True:
            acquired = await session.scalar(select(func.pg_try_advisory_xact_lock(key)))
            if acquired is True:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise RecoveryRefusal("database recovery lock timed out")
            await asyncio.sleep(0.2)
        revision_rows = list(await session.scalars(text("SELECT version_num FROM alembic_version")))
        if revision_rows != [DATABASE_REVISION]:
            raise RecoveryRefusal("database revision is invalid")
        role = await session.scalar(select(func.current_user()))
        schema = await session.scalar(select(func.current_schema()))
        if role != self._database_role or schema != self._database_schema:
            raise RecoveryRefusal("database identity is invalid")

    async def _preflight_locked(
        self, session: AsyncSession, operation: Operation, fence: TenantFence
    ) -> _RecoverySnapshot:
        transition = recovery_transition_values(self._operation_pre_state(operation))
        if _RECOVERY_MARKER in operation.progress:
            raise RecoveryRefusal("already progressed")
        if (
            operation.cell_id is None
            or operation.external_operation_id != operation.provider_operation_id
            or operation.fence_generation != operation.provider_fence_generation
        ):
            raise RecoveryRefusal("recovery preflight failed")
        del transition
        await self._lock_conflicts(session, operation)
        request = self._codec.decrypt_json(
            operation.request_ciphertext,
            purpose=f"operation-request:{operation.action.value}:{operation.idempotency_key}",
        )
        if (
            canonical_request_sha256(request) != operation.canonical_request_sha256
            or request.get("tenantId") != operation.tenant_id
            or request.get("cellId") != operation.cell_id
            or request.get("operationId") != operation.external_operation_id
            or request.get("fenceGeneration") != operation.fence_generation
            or request.get("checkpoint") != operation.caller_checkpoint
            or request.get("provisionMode") != "serve"
            or not self._deployment_lock.matches_runtime_request(
                request, wire_protocol=operation.wire_protocol.value
            )
        ):
            raise RecoveryRefusal("recovery preflight failed")
        resources = await self._resources(session, operation)
        reservation = await self._reservation(session, operation)
        return _RecoverySnapshot(
            operation=operation,
            resources=resources,
            reservation=reservation,
            operation_sha256=_hash_model(operation),
            preserved_sha256=_hash_model(operation, excluded=self._CHANGED_COLUMNS),
            request_sha256=operation.canonical_request_sha256,
            request_ciphertext_sha256=hashlib.sha256(
                operation.request_ciphertext.encode()
            ).hexdigest(),
            resources_sha256=canonical_sha256(
                {item.id: _hash_model(item) for item in sorted(resources, key=lambda item: item.id)}
            ),
            reservation_sha256=_hash_model(reservation),
            tenant_fence_sha256=_hash_model(fence),
        )

    @staticmethod
    def _operation_pre_state(operation: Operation) -> OperationPreState:
        return OperationPreState(
            action=operation.action.value,
            state=operation.state.value,
            checkpoint=operation.checkpoint,
            error_code=operation.error_code,
            has_claim=any(
                value is not None
                for value in (
                    operation.claim_owner,
                    operation.claim_token,
                    operation.claim_expires_at,
                )
            ),
            has_result=(operation.result_ciphertext is not None or operation.result_redacted != {}),
            finalized=operation.finalized_at is not None,
        )

    @staticmethod
    def _preflight_evidence_digest(
        snapshot: _RecoverySnapshot, first: LiveObservation, second: LiveObservation
    ) -> str:
        return canonical_sha256(
            {
                "operation_sha256": snapshot.operation_sha256,
                "preserved_sha256": snapshot.preserved_sha256,
                "request_sha256": snapshot.request_sha256,
                "request_ciphertext_sha256": snapshot.request_ciphertext_sha256,
                "resources_sha256": snapshot.resources_sha256,
                "reservation_sha256": snapshot.reservation_sha256,
                "tenant_fence_sha256": snapshot.tenant_fence_sha256,
                "first_observation_sha256": canonical_sha256(asdict(first)),
                "second_observation_sha256": canonical_sha256(asdict(second)),
            }
        )

    async def _lock_conflicts(self, session: AsyncSession, operation: Operation) -> None:
        lock = await session.get(CellOperationLock, operation.cell_id, with_for_update=True)
        if lock is not None:
            raise RecoveryRefusal("recovery preflight failed")
        if await session.get(CapacityLedger, 1, with_for_update=True) is None:
            raise RecoveryRefusal("recovery preflight failed")
        destructive = tuple(
            await session.scalars(
                select(CapacityDestructiveFence)
                .where(
                    CapacityDestructiveFence.tenant_id == operation.tenant_id,
                    CapacityDestructiveFence.fence_generation >= operation.fence_generation,
                )
                .with_for_update()
            )
        )
        if destructive:
            raise RecoveryRefusal("recovery preflight failed")
        conflicts = tuple(
            await session.scalars(
                select(Operation)
                .where(
                    Operation.id != operation.id,
                    Operation.state.in_((OperationState.PENDING, OperationState.CLAIMED)),
                    (Operation.tenant_id == operation.tenant_id)
                    | (Operation.cell_id == operation.cell_id),
                )
                .with_for_update()
            )
        )
        if conflicts:
            raise RecoveryRefusal("recovery preflight failed")

    async def _resources(self, session: AsyncSession, operation: Operation) -> tuple[Resource, ...]:
        scoped = tuple(
            await session.scalars(
                select(Resource)
                .where(
                    Resource.tenant_id == operation.tenant_id, Resource.cell_id == operation.cell_id
                )
                .order_by(Resource.created_at, Resource.id)
                .with_for_update()
            )
        )
        resources = tuple(item for item in scoped if item.operation_id == operation.id)
        expected = {
            ResourceKind.HELM_RELEASE,
            ResourceKind.KUBERNETES_NAMESPACE,
            ResourceKind.PVC,
            ResourceKind.VOLUME,
        }
        if (
            {item.kind for item in resources} != expected
            or len(resources) != len(expected)
            or any(item.operation_id != operation.id for item in scoped)
        ):
            raise RecoveryRefusal("recovery preflight failed")
        for resource in resources:
            if (
                resource.provider_operation_id != operation.external_operation_id
                or resource.provider_fence_generation != operation.fence_generation
            ):
                raise RecoveryRefusal("recovery preflight failed")
            reference = self._codec.decrypt_json(
                resource.reference_ciphertext,
                purpose=f"resource-reference:{resource.operation_id}:{resource.kind.value}",
            ).get("reference")
            if (
                not isinstance(reference, str)
                or hashlib.sha256(reference.encode()).hexdigest() != resource.reference_digest
            ):
                raise RecoveryRefusal("recovery preflight failed")
        return resources

    async def _reservation(
        self, session: AsyncSession, operation: Operation
    ) -> CapacityReservation:
        reservations = tuple(
            await session.scalars(
                select(CapacityReservation)
                .where(
                    CapacityReservation.reserving_operation_id == operation.id,
                    CapacityReservation.tenant_id == operation.tenant_id,
                    CapacityReservation.cell_id == operation.cell_id,
                )
                .with_for_update()
            )
        )
        if len(reservations) != 1:
            raise RecoveryRefusal("recovery preflight failed")
        reservation = reservations[0]
        if (
            reservation.reservation_class.value != "USER"
            or reservation.resource_name != cell_resource_name(operation.cell_id)
            or reservation.reserving_provider_operation_id != operation.external_operation_id
            or reservation.reserving_fence_generation != operation.fence_generation
            or reservation.released_at is not None
            or any(
                value is not None
                for value in (
                    reservation.releasing_operation_id,
                    reservation.releasing_provider_operation_id,
                    reservation.releasing_fence_generation,
                    reservation.release_reason,
                )
            )
        ):
            raise RecoveryRefusal("recovery preflight failed")
        return reservation

    @staticmethod
    def _final_proof(operation: Operation) -> bool:
        result = operation.result_redacted
        return (
            operation.state is OperationState.FINAL
            and operation.checkpoint == "complete"
            and operation.result_ciphertext is not None
            and result
            == {
                "completed": True,
                "fields": ["privateEndpoint", "providerRef"],
            }
            and operation.finalized_at is not None
        )

    @staticmethod
    def _recovery_result(
        operation: Operation, marker_value: object, status: str
    ) -> dict[str, object]:
        marker = parse_recovery_marker(marker_value)
        if operation.state is OperationState.ERROR:
            return {
                "status": "recovered-then-failed",
                "state": operation.state.value,
                "checkpoint": operation.checkpoint,
                "error_code": operation.error_code,
                "recovery_digest": canonical_sha256(marker),
            }
        if operation.state not in {
            OperationState.PENDING,
            OperationState.CLAIMED,
            OperationState.FINAL,
        }:
            raise RecoveryRefusal("recovery attribution is unavailable")
        return {
            "status": status,
            "state": operation.state.value,
            "checkpoint": operation.checkpoint,
            "recovery_digest": canonical_sha256(marker),
        }


@dataclass(frozen=True, slots=True)
class _ProductionRecoveryObserver:
    """Compose existing authenticated Kubernetes/PV/HCloud readers without mutations."""

    registry: object
    cell: object
    volumes: object
    hcloud: object
    location: str
    codec: EnvelopeCodec

    async def observe(
        self,
        operation: Operation,
        resources: tuple[Resource, ...],
    ) -> LiveObservation:
        from .lifecycle import OpaqueProviderMetadata

        if operation.cell_id is None:
            raise RecoveryRefusal("live recovery preflight failed")
        metadata = OpaqueProviderMetadata(
            tenant_id=operation.tenant_id,
            subject_id=operation.cell_id,
            operation_id=operation.external_operation_id,
            fence_generation=operation.fence_generation,
        )
        references: dict[ResourceKind, str] = {}
        for resource in resources:
            value = self.codec.decrypt_json(
                resource.reference_ciphertext,
                purpose=f"resource-reference:{resource.operation_id}:{resource.kind.value}",
            ).get("reference")
            if not isinstance(value, str):
                raise RecoveryRefusal("live recovery preflight failed")
            references[resource.kind] = value
        if references != {
            ResourceKind.KUBERNETES_NAMESPACE: metadata.resource_name,
            ResourceKind.HELM_RELEASE: metadata.resource_name,
            ResourceKind.PVC: metadata.resource_name + "-data",
            ResourceKind.VOLUME: references.get(ResourceKind.VOLUME, ""),
        }:
            raise RecoveryRefusal("live recovery preflight failed")
        from .lifecycle import RecordedVolume

        expected_volume = RecordedVolume.from_recoverable_reference(
            references[ResourceKind.VOLUME], metadata
        )
        snapshot = await self.registry.inspect(metadata, metadata)  # type: ignore[attr-defined]
        registry_digest = await self.registry.authenticate_recovery_record(metadata)  # type: ignore[attr-defined]
        volume_observation = await self.volumes.observe_recovery_bound_volume(metadata)  # type: ignore[attr-defined]
        volume_present = (
            volume_observation is not None
            and (
                volume_observation.recorded.volume_handle == expected_volume.volume_handle
                and volume_observation.recorded.pv_name == expected_volume.pv_name
                and volume_observation.recorded.location == expected_volume.location
                and volume_observation.recorded.metadata == expected_volume.metadata
                and volume_observation.recorded.pv_recovery_envelope
                == expected_volume.pv_recovery_envelope
                and volume_observation.recorded.pvc_recovery_envelope
                == expected_volume.pvc_recovery_envelope
            )
            and await self.hcloud.verify_recovery_volume(  # type: ignore[attr-defined]
                expected_volume.volume_handle,
                metadata,
                self.location,
                expected_volume.hcloud_recovery_envelope,
            )
        )
        return LiveObservation(
            namespace_present=snapshot.namespace,
            release_present=snapshot.release,
            pvc_bound=await self.cell.volume_claim_bound(metadata),  # type: ignore[attr-defined]
            volume_present=volume_present,
            init_job_present=snapshot.init_job_present,
            init_complete=snapshot.init_complete,
            init_failed_only=snapshot.init_failed,
            terminating=False,
            runtime_admitted=snapshot.runtime_admitted,
            routes_present=sum(snapshot.routes),
            identity_digest=canonical_sha256(
                {
                    "kubernetes": registry_digest,
                    "pv": (
                        volume_observation.stability_digest
                        if volume_observation is not None
                        else ""
                    ),
                }
            ),
        )


def emit_result(payload: dict[str, object]) -> int:
    if set(payload) - _OUTPUT_KEYS or not isinstance(payload.get("status"), str):
        raise RecoveryRefusal("recovery output is invalid")
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = _RecoveryArgumentParser(add_help=False)
    parser.add_argument("mode", choices=_FIXED_MODES)
    parser.add_argument("--stdin", action="store_true", required=True)
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    from hcloud import Client as HCloudClient
    from kubernetes import client, config

    from .adapters import HCloudVolumeAdapter, KubernetesCellAdapter, KubernetesVolumeAdapter
    from .crypto import AesGcmEnvelopeCodec
    from .database import ProvisionerDatabase
    from .live import KubernetesProviderRegistry
    from .provider_identity import ProviderRecoveryIdentityVerifier
    from .recovery_settings import RecoverySettings, load_recovery_settings

    database: ProvisionerDatabase | None = None
    api_client: object | None = None
    try:
        settings: RecoverySettings = load_recovery_settings()
        database = ProvisionerDatabase(settings)
        config.load_incluster_config()
        api_client = client.ApiClient()
        identity = ProviderRecoveryIdentityVerifier.from_public_key(
            settings.provider_recovery_public_key
        )
        core = client.CoreV1Api(api_client)
        observer = _ProductionRecoveryObserver(
            registry=KubernetesProviderRegistry(
                core_v1=core,
                apps_v1=client.AppsV1Api(api_client),
                batch_v1=client.BatchV1Api(api_client),
                custom_objects=client.CustomObjectsApi(api_client),
                identity_verifier=identity,
            ),
            cell=KubernetesCellAdapter(
                core_v1=core,
                apps_v1=client.AppsV1Api(api_client),
                identity_verifier=identity,
            ),
            volumes=KubernetesVolumeAdapter(
                core_v1=core,
                storage_class_name="exomem-hcloud-encrypted-retain",
                encryption_secret_name="recovery-observer-does-not-read-secrets",
                encryption_secret_namespace="recovery-observer-does-not-read-secrets",
                identity_verifier=identity,
            ),
            hcloud=HCloudVolumeAdapter(
                client=HCloudClient(token=settings.hcloud_token.get_secret_value()),
                identity_verifier=identity,
            ),
            location=settings.hcloud_location,
            codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        )
        service = RecoveryService(
            sessions=database.session_factory,
            codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
            database_name=settings.database_name,
            database_role=settings.database_role,
            database_schema=settings.database_schema,
            database_lock_timeout_seconds=settings.database_lock_timeout_seconds,
            deployment_lock=settings.deployment_lock,
            observer=observer,
        )
        operation_id = read_operation_identity(stdin=sys.stdin.read())
        match args.mode:
            case "preflight":
                return await service.preflight(operation_id)
            case "reopen":
                return await service.reopen(operation_id)
            case "inspect":
                return await service.inspect(operation_id)
            case "verify-recovery":
                return await service.verify_recovery(operation_id)
        raise RecoveryRefusal("recovery mode is invalid")
    except RecoveryRefusal:
        raise
    except Exception as error:
        raise RecoveryRefusal("recovery command failed") from error
    finally:
        if database is not None:
            await database.dispose()
        if api_client is not None:
            await asyncio.to_thread(api_client.close)  # type: ignore[attr-defined]


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values == ["--help"]:
        print(
            "exomem-provisioner-recover-init-retry - recover one proven hosted init retry "
            "false-negative; configuration is supplied through environment variables"
        )
        return 0
    try:
        arguments = _parser().parse_args(values)
        return emit_result(asyncio.run(_run(arguments)))
    except RecoveryRefusal as error:
        emit_result({"status": "refused", "refusal": str(error)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
