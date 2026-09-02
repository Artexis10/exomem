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
from typing import Literal, Never, Protocol

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

_FIXED_MODES = (
    "preflight",
    "reopen",
    "inspect",
    "verify-recovery",
    "retarget-preflight",
    "retarget",
    "verify-retarget",
    "resume-retarget-preflight",
    "resume-retarget",
    "verify-resume-retarget",
    "retry-retarget-preflight",
    "retry-retarget",
    "verify-retry-retarget",
    "successor-retarget-preflight",
    "successor-retarget",
    "verify-successor-retarget",
)
_RECOVERY_MARKER = "_init_retry_recovery_v1"
_RETARGET_MARKER = "_runtime_retarget_recovery_v1"
_RETARGET_RESUME_MARKER = "_runtime_retarget_resume_v1"
_RETARGET_RETRY_MARKER = "_runtime_retarget_retry_v1"
_RETARGET_SUCCESSOR_MARKER = "_runtime_retarget_successor_v1"
_RECOVERY_MARKER_KEYS = frozenset(
    {"schema", "preflight_sha256", "helper_source_sha256", "claim_generation", "committed_at"}
)
_RETARGET_MARKER_KEYS = frozenset(
    {
        "schema",
        "preflight_sha256",
        "source_request_sha256",
        "target_request_sha256",
        "target_runtime_sha256",
        "helper_source_sha256",
        "claim_generation",
        "committed_at",
    }
)
_RETARGET_RESUME_MARKER_KEYS = frozenset(
    {
        "schema",
        "retarget_marker_sha256",
        "preflight_sha256",
        "helper_source_sha256",
        "claim_generation",
        "committed_at",
    }
)
_RETARGET_RETRY_MARKER_KEYS = frozenset(
    {
        "schema",
        "retarget_marker_sha256",
        "resume_marker_sha256",
        "preflight_sha256",
        "helper_source_sha256",
        "claim_generation",
        "committed_at",
    }
)
_RETARGET_SUCCESSOR_MARKER_KEYS = frozenset(
    {
        "schema",
        "prior_receipts_sha256",
        "preflight_sha256",
        "source_request_sha256",
        "target_request_sha256",
        "target_runtime_sha256",
        "helper_source_sha256",
        "claim_generation",
        "committed_at",
    }
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
    def error(self, message: str) -> Never:
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


@dataclass(frozen=True, slots=True)
class RetargetPreState:
    action: str
    state: str
    checkpoint: str
    error_code: str | None
    claim_owner: str | None
    claim_token: str | None
    claim_expires_at: datetime | None
    has_result: bool
    finalized: bool
    has_recovery_marker: bool
    has_retarget_marker: bool


@dataclass(frozen=True, slots=True)
class RetargetResumePreState:
    action: str
    state: str
    checkpoint: str
    error_code: str | None
    has_claim: bool
    has_result: bool
    finalized: bool
    has_recovery_marker: bool
    has_retarget_marker: bool
    has_resume_marker: bool


@dataclass(frozen=True, slots=True)
class RetargetRetryPreState:
    action: str
    state: str
    checkpoint: str
    error_code: str | None
    has_claim: bool
    has_result: bool
    finalized: bool
    has_recovery_marker: bool
    has_retarget_marker: bool
    has_resume_marker: bool
    has_retry_marker: bool


@dataclass(frozen=True, slots=True)
class RetargetSuccessorPreState:
    action: str
    state: str
    checkpoint: str
    error_code: str | None
    has_claim: bool
    has_result: bool
    finalized: bool
    has_recovery_marker: bool
    has_retarget_marker: bool
    has_resume_marker: bool
    has_retry_marker: bool
    has_successor_marker: bool


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


@dataclass(frozen=True, slots=True)
class _RetargetSnapshot:
    recovery: _RecoverySnapshot
    source_request: dict[str, object]
    target_request: dict[str, object]
    target_runtime_sha256: str


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


def retarget_transition_values(before: RetargetPreState, *, now: datetime) -> dict[str, object]:
    if before.has_retarget_marker:
        raise RecoveryRefusal("already retargeted")
    if (
        before.action != "provision"
        or before.checkpoint != "volume-owned"
        or before.error_code is not None
        or before.has_result
        or before.finalized
        or not before.has_recovery_marker
    ):
        raise RecoveryRefusal("retarget preflight failed")
    if before.state == "pending":
        if any(
            value is not None
            for value in (before.claim_owner, before.claim_token, before.claim_expires_at)
        ):
            raise RecoveryRefusal("retarget preflight failed")
    elif before.state == "claimed":
        if (
            not before.claim_owner
            or not before.claim_token
            or before.claim_expires_at is None
            or before.claim_expires_at > now
        ):
            raise RecoveryRefusal("active claim prevents retarget")
    else:
        raise RecoveryRefusal("already progressed")
    return {
        "state": OperationState.PENDING,
        "checkpoint": "volume-owned",
        "error_code": None,
        "claim_owner": None,
        "claim_token": None,
        "claim_expires_at": None,
        "finalized_at": None,
    }


def retarget_resume_transition_values(before: RetargetResumePreState) -> dict[str, object]:
    if before.has_resume_marker:
        raise RecoveryRefusal("already resumed")
    if (
        before.action != "provision"
        or before.state != "error"
        or before.checkpoint != "failed"
        or before.error_code != "PROVISIONER_PROVIDER_METADATA_CONFLICT"
        or before.has_claim
        or before.has_result
        or not before.finalized
        or not before.has_recovery_marker
        or not before.has_retarget_marker
    ):
        raise RecoveryRefusal("retarget resume preflight failed")
    return {
        "state": OperationState.PENDING,
        "checkpoint": "volume-owned",
        "error_code": None,
        "claim_owner": None,
        "claim_token": None,
        "claim_expires_at": None,
        "finalized_at": None,
    }


def retarget_retry_transition_values(before: RetargetRetryPreState) -> dict[str, object]:
    if before.has_retry_marker:
        raise RecoveryRefusal("already retried")
    if (
        before.action != "provision"
        or before.state != "error"
        or before.checkpoint != "failed"
        or before.error_code != "PROVISIONER_PROVIDER_METADATA_CONFLICT"
        or before.has_claim
        or before.has_result
        or not before.finalized
        or not before.has_recovery_marker
        or not before.has_retarget_marker
        or not before.has_resume_marker
    ):
        raise RecoveryRefusal("retarget retry preflight failed")
    return {
        "state": OperationState.PENDING,
        "checkpoint": "volume-owned",
        "error_code": None,
        "claim_owner": None,
        "claim_token": None,
        "claim_expires_at": None,
        "finalized_at": None,
    }


def retarget_successor_transition_values(
    before: RetargetSuccessorPreState,
) -> dict[str, object]:
    if (
        before.action != "provision"
        or before.has_claim
        or before.has_result
        or not before.has_recovery_marker
        or not before.has_retarget_marker
        or before.has_successor_marker
    ):
        raise RecoveryRefusal("successor retarget preflight failed")
    if before.state == "pending":
        valid_state = (
            before.checkpoint
            in {
                "volume-owned",
                "capacity-live-observation-mismatch",
            }
            and before.error_code is None
            and not before.finalized
        )
    elif before.state == "error":
        valid_state = (
            before.checkpoint == "failed"
            and before.error_code == "PROVISIONER_PROVIDER_METADATA_CONFLICT"
            and before.finalized
            and before.has_resume_marker
            and before.has_retry_marker
        )
    else:
        valid_state = False
    if not valid_state:
        raise RecoveryRefusal("successor retarget preflight failed")
    return {
        "state": OperationState.PENDING,
        "checkpoint": "volume-owned",
        "error_code": None,
        "claim_owner": None,
        "claim_token": None,
        "claim_expires_at": None,
        "finalized_at": None,
    }


def retarget_provision_request(
    request: dict[str, object], *, wire_protocol: str, runtime_target: dict[str, object]
) -> dict[str, object]:
    if request.get("provisionMode") != "serve" or not runtime_target:
        raise RecoveryRefusal("retarget request is invalid")
    if wire_protocol == "exomem-cell-provisioner.v1":
        release_version = runtime_target.get("releaseVersion")
        protocol_version = runtime_target.get("protocolVersion")
        if (
            "runtimeTarget" in request
            or not isinstance(request.get("releaseVersion"), str)
            or not isinstance(request.get("protocolVersion"), str)
            or not isinstance(release_version, str)
            or not isinstance(protocol_version, str)
        ):
            raise RecoveryRefusal("retarget request is invalid")
        if (
            request["releaseVersion"] == release_version
            and request["protocolVersion"] == protocol_version
        ):
            raise RecoveryRefusal("request already targets selected runtime")
        return {
            **request,
            "releaseVersion": release_version,
            "protocolVersion": protocol_version,
        }
    if wire_protocol == "exomem-cell-provisioner.v2":
        if (
            "releaseVersion" in request
            or "protocolVersion" in request
            or not isinstance(request.get("runtimeTarget"), dict)
        ):
            raise RecoveryRefusal("retarget request is invalid")
        if request["runtimeTarget"] == runtime_target:
            raise RecoveryRefusal("request already targets selected runtime")
        return {**request, "runtimeTarget": dict(runtime_target)}
    raise RecoveryRefusal("retarget request is invalid")


def request_targets_selected_runtime(
    request: dict[str, object], *, wire_protocol: str, runtime_target: dict[str, object]
) -> bool:
    if wire_protocol == "exomem-cell-provisioner.v1":
        return (
            "runtimeTarget" not in request
            and request.get("releaseVersion") == runtime_target.get("releaseVersion")
            and request.get("protocolVersion") == runtime_target.get("protocolVersion")
        )
    if wire_protocol == "exomem-cell-provisioner.v2":
        return (
            "releaseVersion" not in request
            and "protocolVersion" not in request
            and request.get("runtimeTarget") == runtime_target
        )
    return False


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


def retarget_marker(
    *,
    preflight_sha256: str,
    source_request_sha256: str,
    target_request_sha256: str,
    target_runtime_sha256: str,
    helper_source_sha256: str,
    claim_generation: int,
    committed_at: datetime,
) -> dict[str, object]:
    marker = {
        "schema": 1,
        "preflight_sha256": preflight_sha256,
        "source_request_sha256": source_request_sha256,
        "target_request_sha256": target_request_sha256,
        "target_runtime_sha256": target_runtime_sha256,
        "helper_source_sha256": helper_source_sha256,
        "claim_generation": claim_generation,
        "committed_at": _json_value(committed_at),
    }
    return parse_retarget_marker(marker)


def parse_retarget_marker(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _RETARGET_MARKER_KEYS:
        raise RecoveryRefusal("retarget marker is invalid")
    if (
        value.get("schema") != 1
        or not isinstance(value.get("claim_generation"), int)
        or value["claim_generation"] < 0
        or not isinstance(value.get("committed_at"), str)
        or any(
            not isinstance(value.get(key), str) or len(value[key]) != 64
            for key in _RETARGET_MARKER_KEYS
            if key.endswith("sha256")
        )
    ):
        raise RecoveryRefusal("retarget marker is invalid")
    try:
        datetime.fromisoformat(value["committed_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise RecoveryRefusal("retarget marker is invalid") from error
    return dict(value)


def retarget_resume_marker(
    *,
    retarget_marker_sha256: str,
    preflight_sha256: str,
    helper_source_sha256: str,
    claim_generation: int,
    committed_at: datetime,
) -> dict[str, object]:
    marker = {
        "schema": 1,
        "retarget_marker_sha256": retarget_marker_sha256,
        "preflight_sha256": preflight_sha256,
        "helper_source_sha256": helper_source_sha256,
        "claim_generation": claim_generation,
        "committed_at": _json_value(committed_at),
    }
    return parse_retarget_resume_marker(marker)


def parse_retarget_resume_marker(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _RETARGET_RESUME_MARKER_KEYS:
        raise RecoveryRefusal("retarget resume marker is invalid")
    if (
        value.get("schema") != 1
        or not isinstance(value.get("claim_generation"), int)
        or value["claim_generation"] < 0
        or not isinstance(value.get("committed_at"), str)
        or any(
            not isinstance(value.get(key), str) or len(value[key]) != 64
            for key in _RETARGET_RESUME_MARKER_KEYS
            if key.endswith("sha256")
        )
    ):
        raise RecoveryRefusal("retarget resume marker is invalid")
    try:
        datetime.fromisoformat(value["committed_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise RecoveryRefusal("retarget resume marker is invalid") from error
    return dict(value)


def retarget_retry_marker(
    *,
    retarget_marker_sha256: str,
    resume_marker_sha256: str,
    preflight_sha256: str,
    helper_source_sha256: str,
    claim_generation: int,
    committed_at: datetime,
) -> dict[str, object]:
    marker = {
        "schema": 1,
        "retarget_marker_sha256": retarget_marker_sha256,
        "resume_marker_sha256": resume_marker_sha256,
        "preflight_sha256": preflight_sha256,
        "helper_source_sha256": helper_source_sha256,
        "claim_generation": claim_generation,
        "committed_at": _json_value(committed_at),
    }
    return parse_retarget_retry_marker(marker)


def parse_retarget_retry_marker(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _RETARGET_RETRY_MARKER_KEYS:
        raise RecoveryRefusal("retarget retry marker is invalid")
    if (
        value.get("schema") != 1
        or not isinstance(value.get("claim_generation"), int)
        or value["claim_generation"] < 0
        or not isinstance(value.get("committed_at"), str)
        or any(
            not isinstance(value.get(key), str) or len(value[key]) != 64
            for key in _RETARGET_RETRY_MARKER_KEYS
            if key.endswith("sha256")
        )
    ):
        raise RecoveryRefusal("retarget retry marker is invalid")
    try:
        datetime.fromisoformat(value["committed_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise RecoveryRefusal("retarget retry marker is invalid") from error
    return dict(value)


def retarget_successor_marker(
    *,
    prior_receipts_sha256: str,
    preflight_sha256: str,
    source_request_sha256: str,
    target_request_sha256: str,
    target_runtime_sha256: str,
    helper_source_sha256: str,
    claim_generation: int,
    committed_at: datetime,
) -> dict[str, object]:
    marker = {
        "schema": 1,
        "prior_receipts_sha256": prior_receipts_sha256,
        "preflight_sha256": preflight_sha256,
        "source_request_sha256": source_request_sha256,
        "target_request_sha256": target_request_sha256,
        "target_runtime_sha256": target_runtime_sha256,
        "helper_source_sha256": helper_source_sha256,
        "claim_generation": claim_generation,
        "committed_at": _json_value(committed_at),
    }
    return parse_retarget_successor_marker(marker)


def parse_retarget_successor_marker(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _RETARGET_SUCCESSOR_MARKER_KEYS:
        raise RecoveryRefusal("successor retarget marker is invalid")
    if (
        value.get("schema") != 1
        or not isinstance(value.get("claim_generation"), int)
        or value["claim_generation"] < 0
        or not isinstance(value.get("committed_at"), str)
        or any(
            not isinstance(value.get(key), str) or len(value[key]) != 64
            for key in _RETARGET_SUCCESSOR_MARKER_KEYS
            if key.endswith("sha256")
        )
    ):
        raise RecoveryRefusal("successor retarget marker is invalid")
    try:
        datetime.fromisoformat(value["committed_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise RecoveryRefusal("successor retarget marker is invalid") from error
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
            "canonical_request_sha256",
            "request_ciphertext",
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
        source_deployment_lock: DeploymentLock | None = None,
        observer: RecoveryLiveObserver,
        runtime_selection: Literal["active", "rollback"] | None = None,
    ) -> None:
        self._sessions = sessions
        self._codec = codec
        self._database_name = database_name
        self._database_role = database_role
        self._database_schema = database_schema
        self._database_lock_timeout_seconds = database_lock_timeout_seconds
        self._deployment_lock = deployment_lock
        self._source_deployment_lock = source_deployment_lock or deployment_lock
        self._runtime_selection = runtime_selection
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

    async def retarget_preflight(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                now = await self._database_now(session)
                snapshot = await self._retarget_preflight(session, operation_id, now=now)
                observation = await self._observer.observe(
                    snapshot.recovery.operation, snapshot.recovery.resources
                )
                validate_live_observation(observation)
                return {
                    "status": "retarget-ready",
                    "state": snapshot.recovery.operation.state.value,
                    "checkpoint": snapshot.recovery.operation.checkpoint,
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
        except Exception as error:
            raise RecoveryRefusal("retarget-preflight-failed") from error

    async def retarget(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                await self._require_database_identity(session)
                current = await session.get(Operation, operation_id, with_for_update=True)
                if current is None:
                    raise RecoveryRefusal("operation is unavailable")
                existing_marker = current.progress.get(_RETARGET_MARKER)
                if existing_marker is not None:
                    return self._retarget_result(current, existing_marker, "already-retargeted")
                now = await self._database_now(session)
                snapshot = await self._retarget_preflight_locked(session, current, now=now)
                first = await self._observer.observe(
                    snapshot.recovery.operation, snapshot.recovery.resources
                )
                validate_live_observation(first)
                second = await self._observer.observe(
                    snapshot.recovery.operation, snapshot.recovery.resources
                )
                validate_live_observation(second)
                if first != second:
                    raise RecoveryRefusal("live recovery preflight failed")
                operation = snapshot.recovery.operation
                evidence_digest = canonical_sha256(
                    {
                        "operation_sha256": snapshot.recovery.operation_sha256,
                        "preserved_sha256": snapshot.recovery.preserved_sha256,
                        "source_request_sha256": canonical_request_sha256(snapshot.source_request),
                        "target_request_sha256": canonical_request_sha256(snapshot.target_request),
                        "request_ciphertext_sha256": (snapshot.recovery.request_ciphertext_sha256),
                        "resources_sha256": snapshot.recovery.resources_sha256,
                        "reservation_sha256": snapshot.recovery.reservation_sha256,
                        "tenant_fence_sha256": snapshot.recovery.tenant_fence_sha256,
                        "first_observation_sha256": canonical_sha256(asdict(first)),
                        "second_observation_sha256": canonical_sha256(asdict(second)),
                    }
                )
                target_sha256 = canonical_request_sha256(snapshot.target_request)
                marker = retarget_marker(
                    preflight_sha256=evidence_digest,
                    source_request_sha256=operation.canonical_request_sha256,
                    target_request_sha256=target_sha256,
                    target_runtime_sha256=snapshot.target_runtime_sha256,
                    helper_source_sha256=self._helper_source_sha256,
                    claim_generation=operation.claim_generation,
                    committed_at=now,
                )
                progress = {**operation.progress, _RETARGET_MARKER: marker}
                transition = retarget_transition_values(
                    self._retarget_pre_state(operation), now=now
                )
                purpose = f"operation-request:{operation.action.value}:{operation.idempotency_key}"
                target_ciphertext = self._codec.encrypt_json(
                    snapshot.target_request, purpose=purpose
                )
                updated = await session.scalar(
                    update(Operation)
                    .where(
                        Operation.id == operation.id,
                        Operation.action == OperationAction.PROVISION,
                        Operation.state == operation.state,
                        Operation.checkpoint == operation.checkpoint,
                        Operation.error_code.is_(None),
                        Operation.claim_owner == operation.claim_owner,
                        Operation.claim_token == operation.claim_token,
                        Operation.claim_expires_at == operation.claim_expires_at,
                        Operation.result_ciphertext.is_(None),
                        cast(Operation.result_redacted, JSONB) == cast({}, JSONB),
                        Operation.finalized_at.is_(None),
                        Operation.external_operation_id == operation.provider_operation_id,
                        Operation.fence_generation == operation.provider_fence_generation,
                        Operation.canonical_request_sha256 == operation.canonical_request_sha256,
                        Operation.request_ciphertext == operation.request_ciphertext,
                        Operation.claim_generation == operation.claim_generation,
                        Operation.updated_at == operation.updated_at,
                        cast(Operation.progress, JSONB) == cast(operation.progress, JSONB),
                        cast(Operation.progress, JSONB).has_key(_RECOVERY_MARKER),
                        ~cast(Operation.progress, JSONB).has_key(_RETARGET_MARKER),
                    )
                    .values(
                        **transition,
                        canonical_request_sha256=target_sha256,
                        request_ciphertext=target_ciphertext,
                        available_at=now,
                        updated_at=now,
                        progress=progress,
                    )
                    .returning(Operation)
                )
                if updated is None:
                    reread = await session.get(Operation, operation_id, with_for_update=True)
                    if reread is not None and _RETARGET_MARKER in reread.progress:
                        return self._retarget_result(
                            reread, reread.progress[_RETARGET_MARKER], "already-retargeted"
                        )
                    raise RecoveryRefusal("already progressed")
                await session.flush()
                return self._retarget_result(updated, marker, "retargeted")
        except RecoveryRefusal:
            raise
        except Exception as error:
            raise RecoveryRefusal("retarget-failed") from error

    async def verify_retarget(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                await self._require_database_identity(session)
                operation = await session.get(Operation, operation_id)
                if operation is None:
                    raise RecoveryRefusal("operation is unavailable")
                marker = operation.progress.get(_RETARGET_MARKER)
                if marker is None:
                    raise RecoveryRefusal("retarget attribution is unavailable")
                return self._retarget_result(operation, marker, "retarget-verified")
        except RecoveryRefusal:
            raise
        except Exception as error:
            raise RecoveryRefusal("retarget-verification-failed") from error

    async def resume_retarget_preflight(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                snapshot = await self._retarget_resume_preflight(session, operation_id)
                observation = await self._observer.observe(
                    snapshot.operation, snapshot.resources
                )
                validate_live_observation(observation)
                return {
                    "status": "retarget-resume-ready",
                    "state": snapshot.operation.state.value,
                    "checkpoint": snapshot.operation.checkpoint,
                    "error_code": snapshot.operation.error_code,
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
        except Exception as error:
            raise RecoveryRefusal("retarget-resume-preflight-failed") from error

    async def resume_retarget(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                await self._require_database_identity(session)
                current = await session.get(Operation, operation_id, with_for_update=True)
                if current is None:
                    raise RecoveryRefusal("operation is unavailable")
                existing = current.progress.get(_RETARGET_RESUME_MARKER)
                if existing is not None:
                    return self._retarget_resume_result(
                        current, existing, "already-resumed"
                    )
                snapshot = await self._retarget_resume_preflight_locked(session, current)
                first = await self._observer.observe(snapshot.operation, snapshot.resources)
                validate_live_observation(first)
                second = await self._observer.observe(snapshot.operation, snapshot.resources)
                validate_live_observation(second)
                if first != second:
                    raise RecoveryRefusal("live recovery preflight failed")
                now = await self._database_now(session)
                retarget = parse_retarget_marker(current.progress.get(_RETARGET_MARKER))
                evidence_digest = self._preflight_evidence_digest(snapshot, first, second)
                marker = retarget_resume_marker(
                    retarget_marker_sha256=canonical_sha256(retarget),
                    preflight_sha256=evidence_digest,
                    helper_source_sha256=self._helper_source_sha256,
                    claim_generation=current.claim_generation,
                    committed_at=now,
                )
                progress = {**current.progress, _RETARGET_RESUME_MARKER: marker}
                transition = retarget_resume_transition_values(
                    self._retarget_resume_pre_state(current)
                )
                updated = await session.scalar(
                    update(Operation)
                    .where(
                        Operation.id == current.id,
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
                        Operation.canonical_request_sha256 == current.canonical_request_sha256,
                        Operation.request_ciphertext == current.request_ciphertext,
                        Operation.claim_generation == current.claim_generation,
                        Operation.updated_at == current.updated_at,
                        cast(Operation.progress, JSONB) == cast(current.progress, JSONB),
                        cast(Operation.progress, JSONB).has_key(_RECOVERY_MARKER),
                        cast(Operation.progress, JSONB).has_key(_RETARGET_MARKER),
                        ~cast(Operation.progress, JSONB).has_key(_RETARGET_RESUME_MARKER),
                    )
                    .values(
                        **transition,
                        available_at=now,
                        updated_at=now,
                        progress=progress,
                    )
                    .returning(Operation)
                )
                if updated is None:
                    reread = await session.get(Operation, operation_id, with_for_update=True)
                    if reread is not None and _RETARGET_RESUME_MARKER in reread.progress:
                        return self._retarget_resume_result(
                            reread,
                            reread.progress[_RETARGET_RESUME_MARKER],
                            "already-resumed",
                        )
                    raise RecoveryRefusal("already progressed")
                await session.flush()
                return self._retarget_resume_result(updated, marker, "retarget-resumed")
        except RecoveryRefusal:
            raise
        except Exception as error:
            raise RecoveryRefusal("retarget-resume-failed") from error

    async def verify_resume_retarget(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                await self._require_database_identity(session)
                operation = await session.get(Operation, operation_id)
                if operation is None:
                    raise RecoveryRefusal("operation is unavailable")
                marker = operation.progress.get(_RETARGET_RESUME_MARKER)
                if marker is None:
                    raise RecoveryRefusal("retarget resume attribution is unavailable")
                return self._retarget_resume_result(
                    operation, marker, "retarget-resume-verified"
                )
        except RecoveryRefusal:
            raise
        except Exception as error:
            raise RecoveryRefusal("retarget-resume-verification-failed") from error

    async def retry_retarget_preflight(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                snapshot = await self._retarget_retry_preflight(session, operation_id)
                observation = await self._observer.observe(
                    snapshot.operation, snapshot.resources
                )
                validate_live_observation(observation)
                return {
                    "status": "retarget-retry-ready",
                    "state": snapshot.operation.state.value,
                    "checkpoint": snapshot.operation.checkpoint,
                    "error_code": snapshot.operation.error_code,
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
        except Exception as error:
            raise RecoveryRefusal("retarget-retry-preflight-failed") from error

    async def retry_retarget(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                await self._require_database_identity(session)
                current = await session.get(Operation, operation_id, with_for_update=True)
                if current is None:
                    raise RecoveryRefusal("operation is unavailable")
                existing = current.progress.get(_RETARGET_RETRY_MARKER)
                if existing is not None:
                    return self._retarget_retry_result(current, existing, "already-retried")
                snapshot = await self._retarget_retry_preflight_locked(session, current)
                first = await self._observer.observe(snapshot.operation, snapshot.resources)
                validate_live_observation(first)
                second = await self._observer.observe(snapshot.operation, snapshot.resources)
                validate_live_observation(second)
                if first != second:
                    raise RecoveryRefusal("live recovery preflight failed")
                now = await self._database_now(session)
                retarget = parse_retarget_marker(current.progress.get(_RETARGET_MARKER))
                resume = parse_retarget_resume_marker(
                    current.progress.get(_RETARGET_RESUME_MARKER)
                )
                evidence_digest = self._preflight_evidence_digest(snapshot, first, second)
                marker = retarget_retry_marker(
                    retarget_marker_sha256=canonical_sha256(retarget),
                    resume_marker_sha256=canonical_sha256(resume),
                    preflight_sha256=evidence_digest,
                    helper_source_sha256=self._helper_source_sha256,
                    claim_generation=current.claim_generation,
                    committed_at=now,
                )
                progress = {**current.progress, _RETARGET_RETRY_MARKER: marker}
                transition = retarget_retry_transition_values(
                    self._retarget_retry_pre_state(current)
                )
                updated = await session.scalar(
                    update(Operation)
                    .where(
                        Operation.id == current.id,
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
                        Operation.canonical_request_sha256 == current.canonical_request_sha256,
                        Operation.request_ciphertext == current.request_ciphertext,
                        Operation.claim_generation == current.claim_generation,
                        Operation.updated_at == current.updated_at,
                        cast(Operation.progress, JSONB) == cast(current.progress, JSONB),
                        cast(Operation.progress, JSONB).has_key(_RECOVERY_MARKER),
                        cast(Operation.progress, JSONB).has_key(_RETARGET_MARKER),
                        cast(Operation.progress, JSONB).has_key(_RETARGET_RESUME_MARKER),
                        ~cast(Operation.progress, JSONB).has_key(_RETARGET_RETRY_MARKER),
                    )
                    .values(
                        **transition,
                        available_at=now,
                        updated_at=now,
                        progress=progress,
                    )
                    .returning(Operation)
                )
                if updated is None:
                    reread = await session.get(Operation, operation_id, with_for_update=True)
                    if reread is not None and _RETARGET_RETRY_MARKER in reread.progress:
                        return self._retarget_retry_result(
                            reread,
                            reread.progress[_RETARGET_RETRY_MARKER],
                            "already-retried",
                        )
                    raise RecoveryRefusal("already progressed")
                await session.flush()
                return self._retarget_retry_result(updated, marker, "retarget-retried")
        except RecoveryRefusal:
            raise
        except Exception as error:
            raise RecoveryRefusal("retarget-retry-failed") from error

    async def verify_retry_retarget(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                await self._require_database_identity(session)
                operation = await session.get(Operation, operation_id)
                if operation is None:
                    raise RecoveryRefusal("operation is unavailable")
                marker = operation.progress.get(_RETARGET_RETRY_MARKER)
                if marker is None:
                    raise RecoveryRefusal("retarget retry attribution is unavailable")
                return self._retarget_retry_result(
                    operation, marker, "retarget-retry-verified"
                )
        except RecoveryRefusal:
            raise
        except Exception as error:
            raise RecoveryRefusal("retarget-retry-verification-failed") from error

    async def successor_retarget_preflight(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                now = await self._database_now(session)
                snapshot = await self._retarget_successor_preflight(
                    session, operation_id, now=now
                )
                observation = await self._observer.observe(
                    snapshot.recovery.operation, snapshot.recovery.resources
                )
                validate_live_observation(observation)
                return {
                    "status": "successor-retarget-ready",
                    "state": snapshot.recovery.operation.state.value,
                    "checkpoint": snapshot.recovery.operation.checkpoint,
                    "error_code": snapshot.recovery.operation.error_code,
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
        except Exception as error:
            raise RecoveryRefusal("successor-retarget-preflight-failed") from error

    async def successor_retarget(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                await self._require_database_identity(session)
                current = await session.get(Operation, operation_id, with_for_update=True)
                if current is None:
                    raise RecoveryRefusal("operation is unavailable")
                existing = current.progress.get(_RETARGET_SUCCESSOR_MARKER)
                if existing is not None:
                    return self._retarget_successor_result(
                        current, existing, "already-successor-retargeted"
                    )
                now = await self._database_now(session)
                snapshot = await self._retarget_successor_preflight_locked(
                    session, current, now=now
                )
                first = await self._observer.observe(
                    snapshot.recovery.operation, snapshot.recovery.resources
                )
                validate_live_observation(first)
                second = await self._observer.observe(
                    snapshot.recovery.operation, snapshot.recovery.resources
                )
                validate_live_observation(second)
                if first != second:
                    raise RecoveryRefusal("live recovery preflight failed")
                prior_receipts_sha256 = self._prior_retarget_receipts_sha256(current)
                evidence_digest = canonical_sha256(
                    {
                        "operation_sha256": snapshot.recovery.operation_sha256,
                        "preserved_sha256": snapshot.recovery.preserved_sha256,
                        "prior_receipts_sha256": prior_receipts_sha256,
                        "source_request_sha256": canonical_request_sha256(
                            snapshot.source_request
                        ),
                        "target_request_sha256": canonical_request_sha256(
                            snapshot.target_request
                        ),
                        "request_ciphertext_sha256": (
                            snapshot.recovery.request_ciphertext_sha256
                        ),
                        "resources_sha256": snapshot.recovery.resources_sha256,
                        "reservation_sha256": snapshot.recovery.reservation_sha256,
                        "tenant_fence_sha256": snapshot.recovery.tenant_fence_sha256,
                        "first_observation_sha256": canonical_sha256(asdict(first)),
                        "second_observation_sha256": canonical_sha256(asdict(second)),
                    }
                )
                target_sha256 = canonical_request_sha256(snapshot.target_request)
                marker = retarget_successor_marker(
                    prior_receipts_sha256=prior_receipts_sha256,
                    preflight_sha256=evidence_digest,
                    source_request_sha256=current.canonical_request_sha256,
                    target_request_sha256=target_sha256,
                    target_runtime_sha256=snapshot.target_runtime_sha256,
                    helper_source_sha256=self._helper_source_sha256,
                    claim_generation=current.claim_generation,
                    committed_at=now,
                )
                progress = {**current.progress, _RETARGET_SUCCESSOR_MARKER: marker}
                transition = retarget_successor_transition_values(
                    self._retarget_successor_pre_state(current)
                )
                purpose = f"operation-request:{current.action.value}:{current.idempotency_key}"
                target_ciphertext = self._codec.encrypt_json(
                    snapshot.target_request, purpose=purpose
                )
                updated = await session.scalar(
                    update(Operation)
                    .where(
                        Operation.id == current.id,
                        Operation.action == OperationAction.PROVISION,
                        Operation.state == current.state,
                        Operation.checkpoint == current.checkpoint,
                        Operation.error_code == current.error_code,
                        Operation.claim_owner.is_(None),
                        Operation.claim_token.is_(None),
                        Operation.claim_expires_at.is_(None),
                        Operation.result_ciphertext.is_(None),
                        cast(Operation.result_redacted, JSONB) == cast({}, JSONB),
                        Operation.finalized_at == current.finalized_at,
                        Operation.external_operation_id == current.provider_operation_id,
                        Operation.fence_generation == current.provider_fence_generation,
                        Operation.canonical_request_sha256 == current.canonical_request_sha256,
                        Operation.request_ciphertext == current.request_ciphertext,
                        Operation.claim_generation == current.claim_generation,
                        Operation.updated_at == current.updated_at,
                        cast(Operation.progress, JSONB) == cast(current.progress, JSONB),
                        cast(Operation.progress, JSONB).has_key(_RECOVERY_MARKER),
                        cast(Operation.progress, JSONB).has_key(_RETARGET_MARKER),
                        ~cast(Operation.progress, JSONB).has_key(_RETARGET_SUCCESSOR_MARKER),
                    )
                    .values(
                        **transition,
                        canonical_request_sha256=target_sha256,
                        request_ciphertext=target_ciphertext,
                        available_at=now,
                        updated_at=now,
                        progress=progress,
                    )
                    .returning(Operation)
                )
                if updated is None:
                    reread = await session.get(Operation, operation_id, with_for_update=True)
                    if reread is not None and _RETARGET_SUCCESSOR_MARKER in reread.progress:
                        return self._retarget_successor_result(
                            reread,
                            reread.progress[_RETARGET_SUCCESSOR_MARKER],
                            "already-successor-retargeted",
                        )
                    raise RecoveryRefusal("already progressed")
                await session.flush()
                return self._retarget_successor_result(
                    updated, marker, "successor-retargeted"
                )
        except RecoveryRefusal:
            raise
        except Exception as error:
            raise RecoveryRefusal("successor-retarget-failed") from error

    async def verify_successor_retarget(self, operation_id: str) -> dict[str, object]:
        try:
            async with self._sessions.begin() as session:
                await self._require_database_identity(session)
                operation = await session.get(Operation, operation_id)
                if operation is None:
                    raise RecoveryRefusal("operation is unavailable")
                marker = operation.progress.get(_RETARGET_SUCCESSOR_MARKER)
                if marker is None:
                    raise RecoveryRefusal("successor retarget attribution is unavailable")
                return self._retarget_successor_result(
                    operation, marker, "successor-retarget-verified"
                )
        except RecoveryRefusal:
            raise
        except Exception as error:
            raise RecoveryRefusal("successor-retarget-verification-failed") from error

    async def _retarget_resume_preflight(
        self, session: AsyncSession, operation_id: str
    ) -> _RecoverySnapshot:
        await self._require_database_identity(session)
        operation = await session.get(Operation, operation_id, with_for_update=True)
        if operation is None:
            raise RecoveryRefusal("operation is unavailable")
        return await self._retarget_resume_preflight_locked(session, operation)

    async def _retarget_resume_preflight_locked(
        self, session: AsyncSession, operation: Operation
    ) -> _RecoverySnapshot:
        fence = await session.get(TenantFence, operation.tenant_id, with_for_update=True)
        if (
            fence is None
            or fence.fence_generation != operation.fence_generation
            or operation.cell_id is None
            or operation.external_operation_id != operation.provider_operation_id
            or operation.fence_generation != operation.provider_fence_generation
        ):
            raise RecoveryRefusal("retarget resume preflight failed")
        retarget_resume_transition_values(self._retarget_resume_pre_state(operation))
        parse_recovery_marker(operation.progress.get(_RECOVERY_MARKER))
        retarget = parse_retarget_marker(operation.progress.get(_RETARGET_MARKER))
        if operation.canonical_request_sha256 != retarget["target_request_sha256"]:
            raise RecoveryRefusal("retarget resume preflight failed")
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
                request,
                wire_protocol=operation.wire_protocol.value,
                selection=self._runtime_selection,
            )
        ):
            raise RecoveryRefusal("retarget resume preflight failed")
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

    async def _retarget_retry_preflight(
        self, session: AsyncSession, operation_id: str
    ) -> _RecoverySnapshot:
        await self._require_database_identity(session)
        operation = await session.get(Operation, operation_id, with_for_update=True)
        if operation is None:
            raise RecoveryRefusal("operation is unavailable")
        return await self._retarget_retry_preflight_locked(session, operation)

    async def _retarget_retry_preflight_locked(
        self, session: AsyncSession, operation: Operation
    ) -> _RecoverySnapshot:
        fence = await session.get(TenantFence, operation.tenant_id, with_for_update=True)
        if (
            fence is None
            or fence.fence_generation != operation.fence_generation
            or operation.cell_id is None
            or operation.external_operation_id != operation.provider_operation_id
            or operation.fence_generation != operation.provider_fence_generation
        ):
            raise RecoveryRefusal("retarget retry preflight failed")
        retarget_retry_transition_values(self._retarget_retry_pre_state(operation))
        parse_recovery_marker(operation.progress.get(_RECOVERY_MARKER))
        retarget = parse_retarget_marker(operation.progress.get(_RETARGET_MARKER))
        resume = parse_retarget_resume_marker(operation.progress.get(_RETARGET_RESUME_MARKER))
        if (
            resume["retarget_marker_sha256"] != canonical_sha256(retarget)
            or operation.canonical_request_sha256 != retarget["target_request_sha256"]
        ):
            raise RecoveryRefusal("retarget retry preflight failed")
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
                request,
                wire_protocol=operation.wire_protocol.value,
                selection=self._runtime_selection,
            )
        ):
            raise RecoveryRefusal("retarget retry preflight failed")
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

    async def _retarget_successor_preflight(
        self, session: AsyncSession, operation_id: str, *, now: datetime
    ) -> _RetargetSnapshot:
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
            raise RecoveryRefusal("successor retarget preflight failed")
        return await self._retarget_successor_preflight_locked(
            session, operation, now=now, fence=fence
        )

    async def _retarget_successor_preflight_locked(
        self,
        session: AsyncSession,
        operation: Operation,
        *,
        now: datetime,
        fence: TenantFence | None = None,
    ) -> _RetargetSnapshot:
        if fence is None:
            fence = await session.get(TenantFence, operation.tenant_id, with_for_update=True)
        if (
            fence is None
            or fence.fence_generation != operation.fence_generation
            or operation.cell_id is None
            or operation.external_operation_id != operation.provider_operation_id
            or operation.fence_generation != operation.provider_fence_generation
        ):
            raise RecoveryRefusal("successor retarget preflight failed")
        retarget_successor_transition_values(self._retarget_successor_pre_state(operation))
        self._prior_retarget_receipts_sha256(operation)
        await self._lock_conflicts(
            session,
            operation,
            allow_expired_current=True,
            now=now,
        )
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
            or not self._source_deployment_lock.matches_runtime_request(
                request,
                wire_protocol=operation.wire_protocol.value,
                selection=self._runtime_selection,
            )
        ):
            raise RecoveryRefusal("successor retarget preflight failed")
        selected = self._deployment_lock.selected_runtime(self._runtime_selection)
        target_runtime = selected.runtimeTarget.model_dump(mode="json")
        if selected.compatibilityDigest is not None:
            target_runtime["compatibilityDigest"] = selected.compatibilityDigest
        target_request = retarget_provision_request(
            request,
            wire_protocol=operation.wire_protocol.value,
            runtime_target=target_runtime,
        )
        resources = await self._resources(session, operation)
        reservation = await self._reservation(session, operation)
        recovery = _RecoverySnapshot(
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
        return _RetargetSnapshot(
            recovery=recovery,
            source_request=request,
            target_request=target_request,
            target_runtime_sha256=canonical_sha256(target_runtime),
        )

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

    async def _retarget_preflight(
        self, session: AsyncSession, operation_id: str, *, now: datetime
    ) -> _RetargetSnapshot:
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
            raise RecoveryRefusal("retarget preflight failed")
        return await self._retarget_preflight_locked(session, operation, now=now, fence=fence)

    async def _retarget_preflight_locked(
        self,
        session: AsyncSession,
        operation: Operation,
        *,
        now: datetime,
        fence: TenantFence | None = None,
    ) -> _RetargetSnapshot:
        if fence is None:
            fence = await session.get(TenantFence, operation.tenant_id, with_for_update=True)
        if (
            fence is None
            or fence.fence_generation != operation.fence_generation
            or operation.cell_id is None
            or operation.external_operation_id != operation.provider_operation_id
            or operation.fence_generation != operation.provider_fence_generation
        ):
            raise RecoveryRefusal("retarget preflight failed")
        parse_recovery_marker(operation.progress.get(_RECOVERY_MARKER))
        retarget_transition_values(self._retarget_pre_state(operation), now=now)
        await self._lock_conflicts(
            session,
            operation,
            allow_expired_current=True,
            now=now,
        )
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
            or not self._source_deployment_lock.matches_runtime_request(
                request,
                wire_protocol=operation.wire_protocol.value,
                selection=self._runtime_selection,
            )
        ):
            raise RecoveryRefusal("retarget preflight failed")
        selected = self._deployment_lock.selected_runtime(self._runtime_selection)
        target_runtime = selected.runtimeTarget.model_dump(mode="json")
        if selected.compatibilityDigest is not None:
            target_runtime["compatibilityDigest"] = selected.compatibilityDigest
        target_request = retarget_provision_request(
            request,
            wire_protocol=operation.wire_protocol.value,
            runtime_target=target_runtime,
        )
        resources = await self._resources(session, operation)
        reservation = await self._reservation(session, operation)
        recovery = _RecoverySnapshot(
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
        return _RetargetSnapshot(
            recovery=recovery,
            source_request=request,
            target_request=target_request,
            target_runtime_sha256=canonical_sha256(target_runtime),
        )

    async def _database_now(self, session: AsyncSession) -> datetime:
        now = await session.scalar(select(func.clock_timestamp()))
        if not isinstance(now, datetime):
            raise RecoveryRefusal("database clock is unavailable")
        return now

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
                request,
                wire_protocol=operation.wire_protocol.value,
                selection=self._runtime_selection,
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
    def _retarget_pre_state(operation: Operation) -> RetargetPreState:
        return RetargetPreState(
            action=operation.action.value,
            state=operation.state.value,
            checkpoint=operation.checkpoint,
            error_code=operation.error_code,
            claim_owner=operation.claim_owner,
            claim_token=operation.claim_token,
            claim_expires_at=operation.claim_expires_at,
            has_result=(operation.result_ciphertext is not None or operation.result_redacted != {}),
            finalized=operation.finalized_at is not None,
            has_recovery_marker=_RECOVERY_MARKER in operation.progress,
            has_retarget_marker=_RETARGET_MARKER in operation.progress,
        )

    @staticmethod
    def _retarget_resume_pre_state(operation: Operation) -> RetargetResumePreState:
        return RetargetResumePreState(
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
            has_recovery_marker=_RECOVERY_MARKER in operation.progress,
            has_retarget_marker=_RETARGET_MARKER in operation.progress,
            has_resume_marker=_RETARGET_RESUME_MARKER in operation.progress,
        )

    @staticmethod
    def _retarget_retry_pre_state(operation: Operation) -> RetargetRetryPreState:
        return RetargetRetryPreState(
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
            has_recovery_marker=_RECOVERY_MARKER in operation.progress,
            has_retarget_marker=_RETARGET_MARKER in operation.progress,
            has_resume_marker=_RETARGET_RESUME_MARKER in operation.progress,
            has_retry_marker=_RETARGET_RETRY_MARKER in operation.progress,
        )

    @staticmethod
    def _retarget_successor_pre_state(operation: Operation) -> RetargetSuccessorPreState:
        return RetargetSuccessorPreState(
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
            has_recovery_marker=_RECOVERY_MARKER in operation.progress,
            has_retarget_marker=_RETARGET_MARKER in operation.progress,
            has_resume_marker=_RETARGET_RESUME_MARKER in operation.progress,
            has_retry_marker=_RETARGET_RETRY_MARKER in operation.progress,
            has_successor_marker=_RETARGET_SUCCESSOR_MARKER in operation.progress,
        )

    @staticmethod
    def _prior_retarget_receipts_sha256(
        operation: Operation, *, request_sha256: str | None = None
    ) -> str:
        recovery = parse_recovery_marker(operation.progress.get(_RECOVERY_MARKER))
        retarget = parse_retarget_marker(operation.progress.get(_RETARGET_MARKER))
        if (
            request_sha256 or operation.canonical_request_sha256
        ) != retarget["target_request_sha256"]:
            raise RecoveryRefusal("successor retarget preflight failed")
        resume_value = operation.progress.get(_RETARGET_RESUME_MARKER)
        retry_value = operation.progress.get(_RETARGET_RETRY_MARKER)
        resume_sha256 = ""
        retry_sha256 = ""
        ordered_receipts = [recovery, retarget]
        if resume_value is not None:
            resume = parse_retarget_resume_marker(resume_value)
            if resume["retarget_marker_sha256"] != canonical_sha256(retarget):
                raise RecoveryRefusal("successor retarget preflight failed")
            resume_sha256 = canonical_sha256(resume)
            ordered_receipts.append(resume)
        if retry_value is not None:
            if resume_value is None:
                raise RecoveryRefusal("successor retarget preflight failed")
            retry = parse_retarget_retry_marker(retry_value)
            if (
                retry["retarget_marker_sha256"] != canonical_sha256(retarget)
                or retry["resume_marker_sha256"] != resume_sha256
            ):
                raise RecoveryRefusal("successor retarget preflight failed")
            retry_sha256 = canonical_sha256(retry)
            ordered_receipts.append(retry)
        claim_generations = [int(receipt["claim_generation"]) for receipt in ordered_receipts]
        committed_at = [
            RecoveryService._receipt_committed_at(receipt) for receipt in ordered_receipts
        ]
        retry_generation = int(retry["claim_generation"]) if retry_value is not None else None
        retry_committed_at = (
            RecoveryService._receipt_committed_at(retry) if retry_value is not None else None
        )
        after_retry_terminal = (
            retry_generation is not None
            and (
                operation.state is OperationState.ERROR
                or operation.checkpoint == "capacity-live-observation-mismatch"
            )
        )
        if (
            claim_generations != sorted(claim_generations)
            or claim_generations[-1] > operation.claim_generation
            or committed_at != sorted(committed_at)
            or committed_at[-1] > operation.updated_at
            or (
                after_retry_terminal
                and operation.claim_generation <= retry_generation
            )
            or (
                operation.state is OperationState.ERROR
                and (
                    operation.finalized_at is None
                    or retry_committed_at is None
                    or operation.finalized_at < retry_committed_at
                )
            )
        ):
            raise RecoveryRefusal("successor retarget preflight failed")
        return canonical_sha256(
            {
                "recovery": canonical_sha256(recovery),
                "retarget": canonical_sha256(retarget),
                "resume": resume_sha256,
                "retry": retry_sha256,
            }
        )

    @staticmethod
    def _receipt_committed_at(receipt: dict[str, object]) -> datetime:
        value = datetime.fromisoformat(str(receipt["committed_at"]).replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise RecoveryRefusal("successor retarget preflight failed")
        return value

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

    async def _lock_conflicts(
        self,
        session: AsyncSession,
        operation: Operation,
        *,
        allow_expired_current: bool = False,
        now: datetime | None = None,
    ) -> None:
        lock = await session.get(CellOperationLock, operation.cell_id, with_for_update=True)
        if lock is not None and not (
            allow_expired_current
            and now is not None
            and lock.tenant_id == operation.tenant_id
            and lock.operation_id == operation.external_operation_id
            and lock.fence_generation == operation.fence_generation
            and lock.lease_expires_at <= now
        ):
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

    @staticmethod
    def _retarget_result(
        operation: Operation, marker_value: object, status: str
    ) -> dict[str, object]:
        marker = parse_retarget_marker(marker_value)
        if operation.canonical_request_sha256 != marker["target_request_sha256"]:
            raise RecoveryRefusal("retarget attribution is unavailable")
        if operation.state not in {
            OperationState.PENDING,
            OperationState.CLAIMED,
            OperationState.ERROR,
            OperationState.FINAL,
        }:
            raise RecoveryRefusal("retarget attribution is unavailable")
        return {
            "status": status,
            "state": operation.state.value,
            "checkpoint": operation.checkpoint,
            "recovery_digest": canonical_sha256(marker),
        }

    @staticmethod
    def _retarget_resume_result(
        operation: Operation, marker_value: object, status: str
    ) -> dict[str, object]:
        marker = parse_retarget_resume_marker(marker_value)
        retarget = parse_retarget_marker(operation.progress.get(_RETARGET_MARKER))
        if (
            marker["retarget_marker_sha256"] != canonical_sha256(retarget)
            or operation.canonical_request_sha256 != retarget["target_request_sha256"]
        ):
            raise RecoveryRefusal("retarget resume attribution is unavailable")
        if operation.state not in {
            OperationState.PENDING,
            OperationState.CLAIMED,
            OperationState.ERROR,
            OperationState.FINAL,
        }:
            raise RecoveryRefusal("retarget resume attribution is unavailable")
        return {
            "status": status,
            "state": operation.state.value,
            "checkpoint": operation.checkpoint,
            "error_code": operation.error_code,
            "recovery_digest": canonical_sha256(marker),
        }

    @staticmethod
    def _retarget_retry_result(
        operation: Operation, marker_value: object, status: str
    ) -> dict[str, object]:
        marker = parse_retarget_retry_marker(marker_value)
        retarget = parse_retarget_marker(operation.progress.get(_RETARGET_MARKER))
        resume = parse_retarget_resume_marker(operation.progress.get(_RETARGET_RESUME_MARKER))
        if (
            marker["retarget_marker_sha256"] != canonical_sha256(retarget)
            or marker["resume_marker_sha256"] != canonical_sha256(resume)
            or operation.canonical_request_sha256 != retarget["target_request_sha256"]
        ):
            raise RecoveryRefusal("retarget retry attribution is unavailable")
        if operation.state not in {
            OperationState.PENDING,
            OperationState.CLAIMED,
            OperationState.ERROR,
            OperationState.FINAL,
        }:
            raise RecoveryRefusal("retarget retry attribution is unavailable")
        return {
            "status": status,
            "state": operation.state.value,
            "checkpoint": operation.checkpoint,
            "error_code": operation.error_code,
            "recovery_digest": canonical_sha256(marker),
        }

    def _retarget_successor_result(
        self, operation: Operation, marker_value: object, status: str
    ) -> dict[str, object]:
        marker = parse_retarget_successor_marker(marker_value)
        purpose = f"operation-request:{operation.action.value}:{operation.idempotency_key}"
        request = self._codec.decrypt_json(operation.request_ciphertext, purpose=purpose)
        selected = self._deployment_lock.selected_runtime(self._runtime_selection)
        target_runtime = selected.runtimeTarget.model_dump(mode="json")
        if selected.compatibilityDigest is not None:
            target_runtime["compatibilityDigest"] = selected.compatibilityDigest
        if (
            operation.canonical_request_sha256 != marker["target_request_sha256"]
            or canonical_request_sha256(request) != operation.canonical_request_sha256
            or request.get("tenantId") != operation.tenant_id
            or request.get("cellId") != operation.cell_id
            or request.get("operationId") != operation.external_operation_id
            or request.get("fenceGeneration") != operation.fence_generation
            or request.get("checkpoint") != operation.caller_checkpoint
            or request.get("provisionMode") != "serve"
            or operation.external_operation_id != operation.provider_operation_id
            or operation.fence_generation != operation.provider_fence_generation
            or not request_targets_selected_runtime(
                request,
                wire_protocol=operation.wire_protocol.value,
                runtime_target=target_runtime,
            )
            or marker["target_runtime_sha256"] != canonical_sha256(target_runtime)
            or marker["helper_source_sha256"] != self._helper_source_sha256
            or int(marker["claim_generation"]) > operation.claim_generation
            or self._receipt_committed_at(marker) > operation.updated_at
            or marker["prior_receipts_sha256"]
            != self._prior_retarget_receipts_sha256(
                operation, request_sha256=str(marker["source_request_sha256"])
            )
        ):
            raise RecoveryRefusal("successor retarget attribution is unavailable")
        if operation.state not in {
            OperationState.PENDING,
            OperationState.CLAIMED,
            OperationState.ERROR,
            OperationState.FINAL,
        }:
            raise RecoveryRefusal("successor retarget attribution is unavailable")
        return {
            "status": status,
            "state": operation.state.value,
            "checkpoint": operation.checkpoint,
            "error_code": operation.error_code,
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
            source_deployment_lock=settings.source_deployment_lock,
            runtime_selection=settings.runtime_selection,
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
            case "retarget-preflight":
                return await service.retarget_preflight(operation_id)
            case "retarget":
                return await service.retarget(operation_id)
            case "verify-retarget":
                return await service.verify_retarget(operation_id)
            case "resume-retarget-preflight":
                return await service.resume_retarget_preflight(operation_id)
            case "resume-retarget":
                return await service.resume_retarget(operation_id)
            case "verify-resume-retarget":
                return await service.verify_resume_retarget(operation_id)
            case "retry-retarget-preflight":
                return await service.retry_retarget_preflight(operation_id)
            case "retry-retarget":
                return await service.retry_retarget(operation_id)
            case "verify-retry-retarget":
                return await service.verify_retry_retarget(operation_id)
            case "successor-retarget-preflight":
                return await service.successor_retarget_preflight(operation_id)
            case "successor-retarget":
                return await service.successor_retarget(operation_id)
            case "verify-successor-retarget":
                return await service.verify_successor_retarget(operation_id)
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
