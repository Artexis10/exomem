"""Typed SQLAlchemy repositories for idempotent, fenced operations."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy import and_, case, func, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .crypto import EnvelopeCodec
from .driver import DriverTerminal
from .governance_migration_checkpoint import CHECKPOINT_VERSION as GOVERNANCE_CHECKPOINT_VERSION
from .governance_migration_checkpoint import MigrationCheckpoint
from .governance_migration_checkpoint import _binding as _canonical_binding
from .models import (
    BackupRecord,
    CapacityDestructiveFence,
    CapacityLedger,
    CapacityReleaseReason,
    CapacityReservation,
    CellOperationLock,
    CredentialMetadata,
    ExportRecord,
    Operation,
    OperationAction,
    OperationState,
    Resource,
    ResourceKind,
    TenantFence,
    WireProtocol,
)
from .provider_identity import cell_resource_name
from .wire_protocol import (
    FORWARD_ONLY_ACTIONS,
    RUNTIME_IDENTITY_FIELDS,
    SINGLE_PHASE_ACTIONS,
    WIRE_PROTOCOL_V1,
    WIRE_PROTOCOL_V2,
)

GOVERNANCE_PROVISION_CHECKPOINT_VERSION = "gpi1"
GOVERNANCE_PROVISION_CHECKPOINT_PHASES = frozenset({"initializing", "complete", "drained"})
INITIAL_RETRY_AFTER_SECONDS = 2
_GOVERNANCE_RECOVERY_MARKER = "_governance_recovery_v1"
_GOVERNANCE_RECOVERY_DOMAIN = b"exomem.hosted-governance-recovery-snapshot.v1\0"
_GOVERNANCE_RECOVERY_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# "Ineligible" must never be expressible by a caller. A shared ``None`` would
# compare equal to a ``None`` digest and requeue exactly the rows this refuses.
_GOVERNANCE_RECOVERY_INELIGIBLE = object()


class RepositoryConflict(RuntimeError):
    pass


class IdempotencyConflict(RepositoryConflict):
    pass


class AdmissionRejected(RepositoryConflict):
    pass


class StaleFence(RepositoryConflict):
    pass


class ImmutableMetadataConflict(RepositoryConflict):
    pass


class ClaimConflict(RepositoryConflict):
    pass


def _holds_governance_checkpoint(operation: Operation, checkpoint: str) -> bool:
    if operation.wire_protocol != WireProtocol.V2:
        return False
    if checkpoint.startswith(GOVERNANCE_CHECKPOINT_VERSION + ":"):
        return operation.action in {OperationAction.PROVISION, OperationAction.ROLLFORWARD}
    return operation.action == OperationAction.PROVISION and checkpoint.startswith(
        GOVERNANCE_PROVISION_CHECKPOINT_VERSION + ":"
    )


def _foreign_governance_barrier(operation: Any):
    # This is a denial marker, not effect authority. Even malformed progress
    # must keep successors fenced until explicit verified recovery resolves it.
    barrier = Operation.__table__.alias("governance_barrier").c
    return (
        select(barrier.id)
        .where(
            barrier.id != operation.id,
            barrier.tenant_id == operation.tenant_id,
            or_(barrier.cell_id == operation.cell_id, operation.action == OperationAction.DESTROY),
            barrier.wire_protocol == WireProtocol.V2,
            or_(
                and_(
                    barrier.action.in_({OperationAction.PROVISION, OperationAction.ROLLFORWARD}),
                    barrier.checkpoint.startswith(GOVERNANCE_CHECKPOINT_VERSION + ":"),
                ),
                and_(
                    barrier.action == OperationAction.PROVISION,
                    barrier.checkpoint.startswith(GOVERNANCE_PROVISION_CHECKPOINT_VERSION + ":"),
                ),
            ),
            barrier.state.in_(
                {OperationState.PENDING, OperationState.CLAIMED, OperationState.ERROR}
            ),
        )
        .exists()
    )


def _terminal_failure_checkpoint(operation: Operation) -> str:
    # Failure state and recovery progress are distinct. An irreversible
    # governance enrollment must not lose its plan/PVC/target binding merely
    # because retries ran out or foreign evidence requires operator review.
    # Preserve even a malformed migration hint for inspection; no recovery
    # path may trust it without decoding and rechecking live authority.
    if _holds_governance_checkpoint(operation, operation.checkpoint):
        return operation.checkpoint
    return "failed"


def _is_effect_free_terminal(operation: Operation) -> bool:
    """Whether a terminal row still describes work that was submitted and never done.

    Only a single-phase action qualifies -- one whose dispatch settles or raises
    without parking on a checkpoint of its own. A terminal failure collapses any
    non-governance checkpoint to "failed", so for a multi-phase action the row no
    longer says how far the attempt got: a rollforward that failed while releasing
    its maintenance lease, with the new image already live and serving, reads
    exactly like one that never started. Restarting that would close the routes of
    a healthy cell to redo work it had already finished.

    Within the single-phase set, failure state and retained progress are still
    distinct. A governance checkpoint, a counted retry attempt, a stored result, a
    live claim, provider identity drift -- any of them means the attempt reached
    something, and only a reviewed recovery may restart it.
    """

    return (
        operation.action.value in SINGLE_PHASE_ACTIONS
        and operation.state is OperationState.ERROR
        and operation.checkpoint == "failed"
        and not operation.progress
        and operation.result_ciphertext is None
        and not operation.result_redacted
        and operation.claim_owner is None
        and operation.claim_token is None
        and operation.claim_expires_at is None
        and operation.finalized_at is not None
        and operation.external_operation_id == operation.provider_operation_id
        and operation.fence_generation == operation.provider_fence_generation
    )


def _retained_governance_checkpoint(operation: Operation) -> bool:
    # A retained hint is preserved for inspection even when malformed, so a
    # recovery path must decode it here rather than trust the denial prefix.
    checkpoint = operation.checkpoint
    if not _holds_governance_checkpoint(operation, checkpoint):
        return False
    if checkpoint.startswith(GOVERNANCE_CHECKPOINT_VERSION + ":"):
        try:
            MigrationCheckpoint.decode(checkpoint)
        except DriverTerminal:
            return False
        return True
    _, _, remainder = checkpoint.partition(":")
    phase, _, binding = remainder.partition(":")
    return phase in GOVERNANCE_PROVISION_CHECKPOINT_PHASES and _canonical_binding(binding)


def _snapshot_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    if isinstance(value, dict):
        return {str(key): _snapshot_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(item) for item in value]
    return value


def _governance_recovery_digest(operation: Operation, fence_generation: int) -> str:
    # Cover every stored column, not only the eligibility predicates: a row
    # mutated between preflight and resume must never requeue under the digest
    # the operator actually read and approved.
    payload: dict[str, Any] = {
        column.name: _snapshot_value(getattr(operation, column.name))
        for column in Operation.__table__.columns
    }
    payload["tenant_fence_generation"] = fence_generation
    return hashlib.sha256(
        _GOVERNANCE_RECOVERY_DOMAIN
        + json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _governance_recovery_snapshot(
    operation: Operation,
    fence: TenantFence | None,
    request: dict[str, Any] | None,
) -> str:
    """Digest exactly one terminal, unclaimed, current-fence, same-operation row."""
    if (
        fence is None
        or operation.state is not OperationState.ERROR
        or operation.finalized_at is None
        or operation.error_code is None
        or operation.claim_owner is not None
        or operation.claim_token is not None
        or operation.claim_expires_at is not None
        or operation.result_ciphertext is not None
        or operation.result_redacted != {}
        or operation.external_operation_id != operation.provider_operation_id
        or operation.fence_generation != operation.provider_fence_generation
        or operation.fence_generation != fence.fence_generation
        or request is None
        or canonical_request_sha256(request) != operation.canonical_request_sha256
        or not _retained_governance_checkpoint(operation)
    ):
        raise RepositoryConflict("operation is not an eligible governance recovery")
    return _governance_recovery_digest(operation, fence.fence_generation)


# One restart per row. A caller that must retry gets its work re-attempted once;
# if it fails again the row stays terminal, so the refusal reaches the caller and
# counts against its own budget instead of hiding a repeatable failure behind a
# perpetual pending.
_REPLAY_RESTART_MARKER = "_replay_restart_v1"

LegacyTarget = tuple[tuple[str, str], ...]


def legacy_target_key(target: Mapping[str, object]) -> LegacyTarget:
    """Reduce a runtime target to the six identity fields a legacy contract carries."""

    return tuple((field, str(target.get(field))) for field in RUNTIME_IDENTITY_FIELDS)


def legacy_targets_from(contracts: Iterable[Mapping[str, object]]) -> frozenset[LegacyTarget]:
    return frozenset(legacy_target_key(contract) for contract in contracts)


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    """Immutable deployment admission inputs supplied by the selected lock."""

    mode: str
    legacy_catalog: frozenset[tuple[str, str]]
    forward_target: dict[str, str]
    # Exact six-field identities of the cataloged legacy contracts. A v2 request
    # names its runtime by these fields plus a compatibility digest, so a live
    # legacy cell is admitted by identity, never by release label alone.
    legacy_targets: frozenset[LegacyTarget] = frozenset()


def _admit_submission(
    *,
    policy: AdmissionPolicy | None,
    wire_protocol: str,
    request: dict[str, Any],
    existing: Operation | None,
    action: str | None = None,
) -> None:
    if wire_protocol not in {WIRE_PROTOCOL_V1, WIRE_PROTOCOL_V2}:
        raise AdmissionRejected("unsupported wire protocol")
    if policy is None:
        return
    if policy.mode not in {"expand", "contract"}:
        raise AdmissionRejected("invalid admission mode")
    if wire_protocol == WIRE_PROTOCOL_V1:
        if existing is None and policy.mode == "contract":
            raise AdmissionRejected("fresh v1 is not admitted in contract mode")
        if "releaseVersion" not in request or "protocolVersion" not in request:
            return
        identity = (str(request["releaseVersion"]), str(request["protocolVersion"]))
        if existing is None or existing.state is not OperationState.FINAL:
            if identity not in policy.legacy_catalog:
                raise AdmissionRejected("legacy runtime is not cataloged")
        return
    if "runtimeTarget" not in request:
        return
    target = request["runtimeTarget"]
    if target == policy.forward_target:
        return
    if (
        action not in FORWARD_ONLY_ACTIONS
        and isinstance(target, Mapping)
        and legacy_target_key(target) in policy.legacy_targets
    ):
        # A cell still on a cataloged legacy release keeps renewing, checking and
        # stopping through the expand window; only the actions that place a runtime
        # image must name the forward target. Contract mode refuses only a fresh
        # legacy submission, mirroring v1: an operation already accepted may be
        # replayed for its acknowledgement until it settles.
        if policy.mode == "expand" or existing is not None:
            return
        raise AdmissionRejected("fresh legacy runtime is not admitted in contract mode")
    raise AdmissionRejected("runtime target does not match deployment lock")


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


def _claim_condition(
    claimed_at: datetime,
    *,
    include_checkpoints: frozenset[str] | None = None,
    exclude_checkpoints: frozenset[str] = frozenset(),
    allowed_actions: frozenset[OperationAction] | None = None,
    excluded_actions: frozenset[OperationAction] = frozenset(),
):
    claimable = or_(
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
    if include_checkpoints is not None:
        claimable &= Operation.checkpoint.in_(include_checkpoints)
    if exclude_checkpoints:
        claimable &= Operation.checkpoint.not_in(exclude_checkpoints)
    cell_available = or_(
        Operation.cell_id.is_(None),
        ~select(CellOperationLock.cell_id)
        .where(
            CellOperationLock.cell_id == Operation.cell_id,
            CellOperationLock.lease_expires_at > claimed_at,
            CellOperationLock.operation_id != Operation.external_operation_id,
        )
        .exists(),
    )
    action_scope = true()
    if allowed_actions is not None:
        action_scope = Operation.action.in_(allowed_actions)
    if excluded_actions:
        action_scope = action_scope & Operation.action.not_in(excluded_actions)
    return claimable & cell_available & action_scope & ~_foreign_governance_barrier(Operation)


def _claim_candidate_statement(
    claimed_at: datetime,
    *,
    include_checkpoints: frozenset[str] | None = None,
    exclude_checkpoints: frozenset[str] = frozenset(),
    allowed_actions: frozenset[OperationAction] | None = None,
    excluded_actions: frozenset[OperationAction] = frozenset(),
):
    return (
        select(Operation.id, Operation.tenant_id)
        .where(
            _claim_condition(
                claimed_at,
                include_checkpoints=include_checkpoints,
                exclude_checkpoints=exclude_checkpoints,
                allowed_actions=allowed_actions,
                excluded_actions=excluded_actions,
            )
        )
        .order_by(Operation.created_at, Operation.id)
        .limit(1)
    )


def _claim_statement(
    operation_id: str,
    claimed_at: datetime,
    *,
    include_checkpoints: frozenset[str] | None = None,
    exclude_checkpoints: frozenset[str] = frozenset(),
    allowed_actions: frozenset[OperationAction] | None = None,
    excluded_actions: frozenset[OperationAction] = frozenset(),
):
    return (
        select(Operation)
        .where(
            Operation.id == operation_id,
            _claim_condition(
                claimed_at,
                include_checkpoints=include_checkpoints,
                exclude_checkpoints=exclude_checkpoints,
                allowed_actions=allowed_actions,
                excluded_actions=excluded_actions,
            ),
        )
        .with_for_update(skip_locked=True)
    )


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    id: str
    action: OperationAction
    idempotency_key: str
    wire_protocol: str
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


@dataclass(frozen=True, slots=True)
class FleetOperationSnapshot:
    """Content-free operation identity used by the fleet observation command."""

    external_operation_id: str
    action: OperationAction
    state: OperationState
    cell_id: str
    runtime_identity: dict[str, str] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RollforwardEvidenceSnapshot:
    """Content-free proof returned for one completed runtime rollforward."""

    external_operation_id: str
    cell_id: str
    before_vault_sha256: str
    after_vault_sha256: str
    evidence_sha256: str


def _operation_snapshot(operation: Operation) -> OperationSnapshot:
    return OperationSnapshot(
        id=operation.id,
        action=operation.action,
        idempotency_key=operation.idempotency_key,
        wire_protocol=str(operation.wire_protocol),
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


async def _acquire_cell_operation_lock(
    session: AsyncSession,
    operation: Operation,
    *,
    checked_at: datetime,
    lease_expires_at: datetime,
) -> bool:
    if await session.scalar(select(_foreign_governance_barrier(operation))):
        return False
    if operation.cell_id is None:
        return True
    lock = await session.get(CellOperationLock, operation.cell_id, with_for_update=True)
    if lock is None:
        session.add(
            CellOperationLock(
                cell_id=operation.cell_id,
                tenant_id=operation.tenant_id,
                operation_id=operation.external_operation_id,
                fence_generation=operation.fence_generation,
                lease_expires_at=lease_expires_at,
                updated_at=checked_at,
            )
        )
        await session.flush()
        return True
    if (
        lock.lease_expires_at is not None
        and _as_utc(lock.lease_expires_at) > checked_at
        and lock.operation_id != operation.external_operation_id
    ):
        return False
    lock.tenant_id = operation.tenant_id
    lock.operation_id = operation.external_operation_id
    lock.fence_generation = operation.fence_generation
    lock.lease_expires_at = lease_expires_at
    lock.updated_at = checked_at
    await session.flush()
    return True


async def _require_cell_operation_lock(
    session: AsyncSession,
    operation: Operation,
    *,
    checked_at: datetime,
) -> CellOperationLock | None:
    if operation.cell_id is None:
        return None
    lock = await session.get(CellOperationLock, operation.cell_id, with_for_update=True)
    if (
        lock is None
        or lock.tenant_id != operation.tenant_id
        or lock.operation_id != operation.external_operation_id
        or lock.fence_generation != operation.fence_generation
        or _as_utc(lock.lease_expires_at) <= checked_at
    ):
        raise ClaimConflict("worker no longer owns the shared cell operation lock")
    return lock


async def _release_cell_operation_lock(
    session: AsyncSession,
    operation: Operation,
) -> None:
    if operation.cell_id is None:
        return
    lock = await session.get(CellOperationLock, operation.cell_id, with_for_update=True)
    if lock is not None and lock.operation_id == operation.external_operation_id:
        await session.delete(lock)


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
    active = _require_active_claim(
        operation,
        worker_id=worker_id,
        claim_token=claim_token,
        claim_generation=claim_generation,
        now=checked_at,
    )
    await _require_cell_operation_lock(session, active, checked_at=checked_at)
    if await session.scalar(select(_foreign_governance_barrier(active))):
        raise ClaimConflict("operation overlaps unfinished governance migration")
    return active, checked_at


async def _release_completed_capacity(
    session: AsyncSession,
    operation: Operation,
    result: dict[str, Any],
    *,
    completed_at: datetime,
) -> None:
    proof_fields = {
        OperationAction.DISCARD: {
            "computeDestroyed",
            "storageDestroyed",
            "keysDestroyed",
        },
        OperationAction.DESTROY: {
            "computeDestroyed",
            "storageDestroyed",
            "keysDestroyed",
            "tenantResourcesDestroyed",
        },
    }
    required = proof_fields.get(operation.action)
    if (
        required is None
        or set(result) != required
        or any(result[key] is not True for key in required)
    ):
        return
    if operation.action is OperationAction.DISCARD and operation.cell_id is None:
        raise ImmutableMetadataConflict("discard capacity release has no authenticated cell")
    reason = (
        CapacityReleaseReason.DISCARD
        if operation.action is OperationAction.DISCARD
        else CapacityReleaseReason.DESTROY
    )
    ledger = await session.get(CapacityLedger, 1, with_for_update=True)
    if ledger is None:
        raise ImmutableMetadataConflict("capacity ledger singleton is missing")
    statement = select(CapacityReservation).where(
        CapacityReservation.tenant_id == operation.tenant_id,
        CapacityReservation.released_at.is_(None),
    )
    if operation.action is OperationAction.DISCARD:
        statement = statement.where(CapacityReservation.cell_id == operation.cell_id)
    reservations = list(
        await session.scalars(statement.order_by(CapacityReservation.id).with_for_update())
    )
    if operation.action is OperationAction.DISCARD and any(
        reservation.resource_name != cell_resource_name(operation.cell_id)
        for reservation in reservations
    ):
        raise ImmutableMetadataConflict("discard reservation resource identity differs")
    if any(
        reservation.reserving_fence_generation > operation.fence_generation
        or (
            reservation.reserving_fence_generation == operation.fence_generation
            and not (
                operation.action is OperationAction.DISCARD
                and reservation.resource_name == cell_resource_name(operation.cell_id)
                and reservation.reserving_provider_operation_id == operation.external_operation_id
            )
        )
        for reservation in reservations
    ):
        raise StaleFence("destructive operation cannot release an equal-or-newer reservation")
    for reservation in reservations:
        reservation.released_at = completed_at
        reservation.releasing_operation_id = operation.id
        reservation.releasing_provider_operation_id = operation.external_operation_id
        reservation.releasing_fence_generation = operation.fence_generation
        reservation.release_reason = reason
    session.add(
        CapacityDestructiveFence(
            tenant_id=operation.tenant_id,
            cell_id=(operation.cell_id if reason is CapacityReleaseReason.DISCARD else None),
            release_reason=reason,
            destructive_operation_id=operation.id,
            provider_operation_id=operation.external_operation_id,
            fence_generation=operation.fence_generation,
            completed_at=completed_at,
        )
    )
    ledger.revision += 1
    ledger.updated_at = completed_at


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

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._sessions

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
        wire_protocol: str = WIRE_PROTOCOL_V1,
        admission: AdmissionPolicy | None = None,
        fresh_rejection: str | None = None,
        retry_after_seconds: int = INITIAL_RETRY_AFTER_SECONDS,
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
                    if existing is not None and str(existing.wire_protocol) != wire_protocol:
                        raise IdempotencyConflict(
                            "idempotency key is bound to another wire protocol"
                        )
                    if existing is not None and existing.canonical_request_sha256 != digest:
                        raise IdempotencyConflict("idempotency key is bound to another request")
                    if fence is not None and fence_generation < fence.fence_generation:
                        raise StaleFence("request fence is older than durable tenant state")
                    if existing is None and fresh_rejection is not None:
                        raise AdmissionRejected(fresh_rejection)
                    _admit_submission(
                        policy=admission,
                        wire_protocol=wire_protocol,
                        request=request,
                        existing=existing,
                        action=action_value.value,
                    )
                    if existing is not None:
                        if _is_effect_free_terminal(existing) and not await session.scalar(
                            select(func.count())
                            .select_from(Resource)
                            .where(Resource.operation_id == existing.id)
                        ):
                            # The caller is asking for exactly this work again and the
                            # row proves the first attempt did nothing. Answering the
                            # replay with a permanent refusal strands it: a control
                            # plane whose own contract requires it to retry has no
                            # other move, and no operator stands in that loop.
                            restarted_at = datetime.now(UTC)
                            existing.state = OperationState.PENDING
                            existing.checkpoint = "queued"
                            existing.error_code = None
                            existing.finalized_at = None
                            existing.available_at = restarted_at
                            existing.retry_after_seconds = retry_after_seconds
                            # Recording the restart also spends it: the predicate
                            # requires empty progress, so this row can never be
                            # restarted a second time.
                            existing.progress = {
                                **existing.progress,
                                _REPLAY_RESTART_MARKER: restarted_at.isoformat(),
                            }
                            await session.flush()
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
                        wire_protocol=WireProtocol(wire_protocol),
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

    async def list_fleet_operation_observations(
        self,
    ) -> tuple[FleetOperationSnapshot, ...]:
        """Decrypt requests internally and return no credential-bearing fields."""

        observations: list[FleetOperationSnapshot] = []
        async with self._sessions() as session:
            operations = await session.scalars(
                select(Operation)
                .where(Operation.cell_id.is_not(None))
                .order_by(Operation.created_at, Operation.id)
            )
            for operation in operations:
                if operation.cell_id is None:  # pragma: no cover - narrowed by SQL
                    continue
                request = self._codec.decrypt_json(
                    operation.request_ciphertext,
                    purpose=(
                        f"operation-request:{operation.action.value}:{operation.idempotency_key}"
                    ),
                )
                runtime_identity: dict[str, str] | None = None
                target = request.get("runtimeTarget")
                if isinstance(target, dict) and all(
                    isinstance(key, str) and isinstance(value, str) for key, value in target.items()
                ):
                    runtime_identity = dict(target)
                elif isinstance(request.get("releaseVersion"), str) and isinstance(
                    request.get("protocolVersion"), str
                ):
                    runtime_identity = {
                        "releaseVersion": request["releaseVersion"],
                        "protocolVersion": request["protocolVersion"],
                    }
                observations.append(
                    FleetOperationSnapshot(
                        external_operation_id=operation.external_operation_id,
                        action=operation.action,
                        state=operation.state,
                        cell_id=operation.cell_id,
                        runtime_identity=runtime_identity,
                        created_at=operation.created_at,
                    )
                )
        return tuple(observations)

    async def load_rollforward_evidence(
        self,
        external_operation_id: str,
    ) -> RollforwardEvidenceSnapshot:
        """Return only the fixed digest proof for one completed rollforward."""

        async with self._sessions() as session:
            operations = tuple(
                await session.scalars(
                    select(Operation).where(
                        Operation.action == OperationAction.ROLLFORWARD,
                        Operation.external_operation_id == external_operation_id,
                    )
                )
            )
            if len(operations) != 1:
                raise KeyError(external_operation_id)
            operation = operations[0]
            if (
                operation.state is not OperationState.FINAL
                or operation.cell_id is None
                or operation.result_ciphertext is None
            ):
                raise ValueError("rollforward evidence is unavailable")
            result = self._codec.decrypt_json(
                operation.result_ciphertext,
                purpose=f"operation-result:{operation.id}",
            )
            required = {
                "code",
                "beforeVaultSha256",
                "afterVaultSha256",
                "evidenceSha256",
            }
            if set(result) != required or result.get("code") != "rollforward_preserved":
                raise ValueError("rollforward evidence is invalid")
            digests = (
                result["beforeVaultSha256"],
                result["afterVaultSha256"],
                result["evidenceSha256"],
            )
            if any(
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                for digest in digests
            ):
                raise ValueError("rollforward evidence is invalid")
            return RollforwardEvidenceSnapshot(
                external_operation_id=operation.external_operation_id,
                cell_id=operation.cell_id,
                before_vault_sha256=digests[0],
                after_vault_sha256=digests[1],
                evidence_sha256=digests[2],
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
        include_checkpoints: frozenset[str] | None = None,
        exclude_checkpoints: frozenset[str] = frozenset(),
        allowed_actions: frozenset[OperationAction] | None = None,
        excluded_actions: frozenset[OperationAction] = frozenset(),
    ) -> OperationSnapshot | None:
        if allowed_actions is not None and not allowed_actions:
            raise ValueError("allowed action claim scope cannot be empty")
        if allowed_actions is not None and allowed_actions & excluded_actions:
            raise ValueError("claim action scopes overlap")
        claim_token = secrets.token_urlsafe(32)
        async with self._sessions.begin() as session:
            claimed_at = await _database_now(session, now)
            if session.get_bind().dialect.name == "sqlite":
                candidate = (
                    select(Operation.id)
                    .where(
                        _claim_condition(
                            claimed_at,
                            include_checkpoints=include_checkpoints,
                            exclude_checkpoints=exclude_checkpoints,
                            allowed_actions=allowed_actions,
                            excluded_actions=excluded_actions,
                        )
                    )
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
                if not await _acquire_cell_operation_lock(
                    session,
                    operation,
                    checked_at=claimed_at,
                    lease_expires_at=claimed_at + timedelta(seconds=self.claim_seconds),
                ):
                    operation.state = OperationState.PENDING
                    operation.claim_owner = None
                    operation.claim_token = None
                    operation.claim_expires_at = None
                    operation.available_at = claimed_at + timedelta(seconds=1)
                    return None
                return _operation_snapshot(operation)
            candidate = (
                await session.execute(
                    _claim_candidate_statement(
                        claimed_at,
                        include_checkpoints=include_checkpoints,
                        exclude_checkpoints=exclude_checkpoints,
                        allowed_actions=allowed_actions,
                        excluded_actions=excluded_actions,
                    )
                )
            ).first()
            if candidate is None:
                return None
            fence = await session.get(TenantFence, candidate.tenant_id, with_for_update=True)
            claimed_at = await _database_now(session, now)
            operation = await session.scalar(
                _claim_statement(
                    candidate.id,
                    claimed_at,
                    include_checkpoints=include_checkpoints,
                    exclude_checkpoints=exclude_checkpoints,
                    allowed_actions=allowed_actions,
                    excluded_actions=excluded_actions,
                )
            )
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
            if not await _acquire_cell_operation_lock(
                session,
                operation,
                checked_at=claimed_at,
                lease_expires_at=operation.claim_expires_at,
            ):
                # The candidate predicate and lock acquisition are separate
                # statements. A concurrent claimant may win that race, so do
                # not commit an operation which appears CLAIMED without owning
                # the shared per-cell lease.
                operation.state = OperationState.PENDING
                operation.claim_owner = None
                operation.claim_token = None
                operation.claim_expires_at = None
                operation.available_at = claimed_at + timedelta(seconds=1)
                operation.updated_at = claimed_at
                return None
            await session.flush()
            return _operation_snapshot(operation)

    async def resume_claim(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        include_checkpoints: frozenset[str] | None = None,
        exclude_checkpoints: frozenset[str] = frozenset(),
        allowed_actions: frozenset[OperationAction] | None = None,
        excluded_actions: frozenset[OperationAction] = frozenset(),
    ) -> OperationSnapshot | None:
        """Resume the one unexpired claim already bound to ``worker_id``."""

        if allowed_actions is not None and not allowed_actions:
            raise ValueError("allowed action claim scope cannot be empty")
        if allowed_actions is not None and allowed_actions & excluded_actions:
            raise ValueError("claim action scopes overlap")
        async with self._sessions.begin() as session:
            checked_at = await _database_now(session, now)
            claim_scope = and_(
                Operation.state == OperationState.CLAIMED,
                Operation.claim_owner == worker_id,
                Operation.claim_token.is_not(None),
                Operation.claim_expires_at > checked_at,
            )
            if include_checkpoints is not None:
                claim_scope &= Operation.checkpoint.in_(include_checkpoints)
            if exclude_checkpoints:
                claim_scope &= Operation.checkpoint.not_in(exclude_checkpoints)
            if allowed_actions is not None:
                claim_scope &= Operation.action.in_(allowed_actions)
            if excluded_actions:
                claim_scope &= Operation.action.not_in(excluded_actions)
            rows = (
                await session.execute(
                    select(
                        Operation.id,
                        Operation.claim_token,
                        Operation.claim_generation,
                    )
                    .where(claim_scope)
                    .order_by(Operation.created_at, Operation.id)
                    .limit(2)
                )
            ).all()
            if not rows:
                return None
            if len(rows) != 1:
                raise ClaimConflict("worker identity owns multiple active operation claims")
            row = rows[0]
            if not isinstance(row.claim_token, str) or not row.claim_token:
                raise ClaimConflict("claimed operation has no claim token")
            operation, _ = await _lock_active_claim(
                session,
                str(row.id),
                worker_id=worker_id,
                claim_token=row.claim_token,
                claim_generation=int(row.claim_generation),
                now=checked_at,
            )
            return _operation_snapshot(operation)

    async def assert_active_claim(
        self,
        operation_id: str,
        worker_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        now: datetime | None = None,
    ) -> None:
        """Read-check claim, tenant fence and shared cell lease without renewing."""
        async with self._sessions.begin() as session:
            await _lock_active_claim(
                session,
                operation_id,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )

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
            lock = await _require_cell_operation_lock(session, operation, checked_at=renewed_at)
            if lock is not None:
                lock.lease_expires_at = operation.claim_expires_at
                lock.updated_at = renewed_at
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
            # Keep the replay identity and denial barrier through resource/
            # result recording. Only complete() clears it atomically with FINAL.
            if not _holds_governance_checkpoint(operation, operation.checkpoint):
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
        capacity_wait_reason: str | None = None,
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
            if _holds_governance_checkpoint(operation, operation.checkpoint):
                if operation.checkpoint.startswith(
                    GOVERNANCE_CHECKPOINT_VERSION + ":"
                ) and checkpoint.startswith(GOVERNANCE_PROVISION_CHECKPOINT_VERSION + ":"):
                    raise ClaimConflict("governance migration cannot return to initialization")
                if not _holds_governance_checkpoint(operation, checkpoint):
                    # Capacity waits and other scheduling outcomes must not
                    # discard the plan binding or release the denial barrier.
                    checkpoint = operation.checkpoint
            if _holds_governance_checkpoint(operation, checkpoint) and not (
                _holds_governance_checkpoint(operation, operation.checkpoint)
            ):
                # The tenant-fence row remains locked through this commit and
                # serializes with claim acquisition. No migration Job may run
                # until this initial durable barrier has actually committed.
                overlapping = await session.scalar(
                    select(Operation.id)
                    .where(
                        Operation.id != operation.id,
                        Operation.tenant_id == operation.tenant_id,
                        or_(
                            and_(
                                Operation.action == OperationAction.DESTROY,
                                Operation.state != OperationState.FINAL,
                            ),
                            and_(
                                Operation.cell_id == operation.cell_id,
                                Operation.state == OperationState.CLAIMED,
                            ),
                        ),
                    )
                    .limit(1)
                )
                if overlapping is not None:
                    raise ClaimConflict("governance migration overlaps an existing operation")
            operation.state = OperationState.PENDING
            operation.checkpoint = checkpoint
            operation.progress = {
                **operation.progress,
                "pending_count": int(operation.progress.get("pending_count", 0)) + 1,
                **(
                    {"last_capacity_wait_reason": capacity_wait_reason}
                    if capacity_wait_reason is not None
                    else {}
                ),
            }
            operation.retry_after_seconds = retry_after_seconds
            operation.available_at = pending_at + timedelta(seconds=retry_after_seconds)
            operation.claim_owner = None
            operation.claim_token = None
            operation.claim_expires_at = None
            await _release_cell_operation_lock(session, operation)
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
            operation.checkpoint = _terminal_failure_checkpoint(operation)
            operation.error_code = code
            operation.claim_owner = None
            operation.claim_token = None
            operation.claim_expires_at = None
            operation.finalized_at = failed_at
            await _release_cell_operation_lock(session, operation)
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
                operation.checkpoint = _terminal_failure_checkpoint(operation)
                operation.error_code = "PROVISIONER_RETRY_EXHAUSTED"
                operation.finalized_at = failed_at
            else:
                operation.state = OperationState.PENDING
                # Backoff is scheduling state, not protocol progress. Replacing
                # the checkpoint here loses a multi-phase operation's durable
                # source/plan binding and can restart already-applied effects.
                operation.retry_after_seconds = retry_after_seconds
                operation.available_at = failed_at + timedelta(seconds=retry_after_seconds)
            await _release_cell_operation_lock(session, operation)
            await session.flush()
            return _operation_snapshot(operation)

    def _governance_recovery_request(self, operation: Operation) -> dict[str, Any] | None:
        try:
            return self._codec.decrypt_json(
                operation.request_ciphertext,
                purpose=(f"operation-request:{operation.action.value}:{operation.idempotency_key}"),
            )
        except Exception:  # noqa: BLE001 - an undecodable envelope is only ever ineligible
            return None

    async def preflight_governance_recovery(
        self,
        operation_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        """Digest one eligible terminal governance row without writing anything."""
        # Eligibility here is structural, not scheduled: nothing about this row
        # expires, so the caller's clock only matters when the resume commits.
        del now
        async with self._sessions() as session:
            operation = await session.get(Operation, operation_id)
            if operation is None:
                raise RepositoryConflict("operation does not exist")
            fence = await session.get(TenantFence, operation.tenant_id)
            return _governance_recovery_snapshot(
                operation,
                fence,
                self._governance_recovery_request(operation),
            )

    async def resume_governance_recovery(
        self,
        operation_id: str,
        *,
        expected_digest: str,
        now: datetime | None = None,
    ) -> str:
        """Requeue the same operation at the same checkpoint under its exact digest.

        This changes scheduling only. The worker's claim, fence, PVC and custody
        rechecks remain the sole authority over the live cell, and the retained
        governance checkpoint keeps blocking successors until it completes.
        """
        if not isinstance(expected_digest, str) or (
            _GOVERNANCE_RECOVERY_DIGEST.fullmatch(expected_digest) is None
        ):
            raise RepositoryConflict("governance recovery digest is invalid")
        async with self._sessions.begin() as session:
            operation = await _lock_operation_fence_first(session, operation_id)
            resumed_at = await _database_now(session, now)
            fence = await session.get(TenantFence, operation.tenant_id)
            current: object
            try:
                current = _governance_recovery_snapshot(
                    operation,
                    fence,
                    self._governance_recovery_request(operation),
                )
            except RepositoryConflict:
                current = _GOVERNANCE_RECOVERY_INELIGIBLE
            if current != expected_digest:
                # Only the digest recorded by the latest committed requeue is an
                # acknowledgement replay. Any older one refers to a row state
                # that a later failure or edit has already replaced.
                marker = operation.progress.get(_GOVERNANCE_RECOVERY_MARKER)
                if isinstance(marker, dict) and marker.get("preflight_sha256") == expected_digest:
                    return "already-queued"
                raise RepositoryConflict("governance recovery digest does not match the row")
            if operation.cell_id is not None and (
                await session.get(CellOperationLock, operation.cell_id, with_for_update=True)
                is not None
            ):
                raise RepositoryConflict("cell operation lease is unresolved")
            if await session.scalar(select(_foreign_governance_barrier(operation))):
                raise RepositoryConflict("operation overlaps unfinished governance migration")
            overlapping = await session.scalar(
                select(Operation.id)
                .where(
                    Operation.id != operation.id,
                    Operation.tenant_id == operation.tenant_id,
                    or_(
                        and_(
                            Operation.action == OperationAction.DESTROY,
                            Operation.state != OperationState.FINAL,
                        ),
                        and_(
                            Operation.cell_id == operation.cell_id,
                            Operation.state == OperationState.CLAIMED,
                        ),
                    ),
                )
                .limit(1)
            )
            if overlapping is not None:
                raise RepositoryConflict("governance recovery overlaps an existing operation")
            operation.state = OperationState.PENDING
            operation.error_code = None
            operation.finalized_at = None
            operation.available_at = resumed_at
            operation.retry_after_seconds = INITIAL_RETRY_AFTER_SECONDS
            operation.progress = {
                **operation.progress,
                "failure_attempts": 0,
                _GOVERNANCE_RECOVERY_MARKER: {
                    "schema": 1,
                    "preflight_sha256": expected_digest,
                    "claim_generation": operation.claim_generation,
                    "committed_at": resumed_at.isoformat(),
                },
            }
            operation.updated_at = resumed_at
            await session.flush()
            return "queued"

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
            await _release_completed_capacity(
                session,
                operation,
                result,
                completed_at=completed_at,
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
            await _release_cell_operation_lock(session, operation)
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
