"""Fenced provider reconciliation for isolated hosted cells.

The orchestration is deliberately provider-idempotent: every step first adopts an
exactly tagged side effect, then returns a durable pending checkpoint. PostgreSQL
is the queue and encrypted registry; provider metadata is the recovery authority
when an acknowledgement or an operational-database snapshot is lost.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .driver import (
    DriverFinal,
    DriverPending,
    DriverResource,
    DriverTerminal,
    EffectContext,
    LostAcknowledgement,
)
from .models import ResourceKind


class MetadataConflict(RuntimeError):
    """A provider object is bound to a different immutable identity."""


def _digest(value: str, *, length: int = 24) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


_OPAQUE_PROVIDER_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")
_HCLOUD_IDENTITY_CHUNK = re.compile(r"^[a-z2-7]{1,52}$")


def _validate_provider_id(value: str) -> str:
    if not _OPAQUE_PROVIDER_ID.fullmatch(value):
        raise MetadataConflict("provider identity is not a bounded opaque ID")
    return value


def _hcloud_identity_labels(prefix: str, value: str) -> dict[str, str]:
    encoded = (
        base64.b32encode(_validate_provider_id(value).encode("ascii"))
        .decode("ascii")
        .rstrip("=")
        .lower()
    )
    chunks = [encoded[offset : offset + 52] for offset in range(0, len(encoded), 52)]
    if not 1 <= len(chunks) <= 8:
        raise MetadataConflict("provider identity exceeds HCloud label capacity")
    return {
        f"{prefix}_n": str(len(chunks)),
        **{f"{prefix}_{index}": chunk for index, chunk in enumerate(chunks)},
    }


def decode_hcloud_identity(labels: dict[str, str], prefix: str) -> str:
    count_value = labels.get(f"{prefix}_n", "")
    if not count_value.isdigit() or not 1 <= int(count_value) <= 8:
        raise MetadataConflict("HCloud provider identity chunk count is invalid")
    count = int(count_value)
    expected_keys = {f"{prefix}_{index}" for index in range(count)}
    actual_keys = {key for key in labels if key.startswith(prefix + "_") and key != f"{prefix}_n"}
    if actual_keys != expected_keys:
        raise MetadataConflict("HCloud provider identity chunks are incomplete")
    chunks = [
        labels[key] for key in sorted(expected_keys, key=lambda key: int(key.rsplit("_", 1)[1]))
    ]
    if any(not _HCLOUD_IDENTITY_CHUNK.fullmatch(chunk) for chunk in chunks):
        raise MetadataConflict("HCloud provider identity chunk is invalid")
    encoded = "".join(chunks).upper()
    padded = encoded + "=" * (-len(encoded) % 8)
    try:
        decoded = base64.b32decode(padded, casefold=False).decode("ascii")
    except (ValueError, UnicodeDecodeError) as error:
        raise MetadataConflict("HCloud provider identity cannot be decoded") from error
    return _validate_provider_id(decoded)


@dataclass(frozen=True, slots=True)
class OpaqueProviderMetadata:
    tenant_id: str = field(repr=False)
    subject_id: str = field(repr=False)
    operation_id: str = field(repr=False)
    fence_generation: int

    def __post_init__(self) -> None:
        _validate_provider_id(self.tenant_id)
        _validate_provider_id(self.subject_id)
        _validate_provider_id(self.operation_id)
        if not 1 <= self.fence_generation <= 9_007_199_254_740_991:
            raise MetadataConflict("provider fence is outside the exact integer range")

    @property
    def resource_name(self) -> str:
        return f"exo-{_digest(self.subject_id, length=20)}"

    @property
    def hcloud_labels(self) -> dict[str, str]:
        return {
            "exomem_tenant": _digest(self.tenant_id),
            "exomem_subject": _digest(self.subject_id),
            "exomem_operation": _digest(self.operation_id),
            "exomem_fence": str(self.fence_generation),
            **_hcloud_identity_labels("exomem_tenant_id", self.tenant_id),
            **_hcloud_identity_labels("exomem_cell_id", self.subject_id),
            **_hcloud_identity_labels("exomem_operation_id", self.operation_id),
        }

    @property
    def kubernetes_annotations(self) -> dict[str, str]:
        return {
            "exomem.io/tenant-id": self.tenant_id,
            "exomem.io/cell-id": self.subject_id,
            "exomem.io/operation-id": self.operation_id,
            "exomem.io/tenant-digest": _digest(self.tenant_id, length=64),
            "exomem.io/subject-digest": _digest(self.subject_id, length=64),
            "exomem.io/operation-digest": _digest(self.operation_id, length=64),
            "exomem.io/fence": str(self.fence_generation),
        }

    @classmethod
    def from_hcloud_labels(cls, labels: dict[str, str]) -> OpaqueProviderMetadata:
        fence_value = labels.get("exomem_fence", "")
        if not fence_value.isdigit():
            raise MetadataConflict("HCloud provider fence label is invalid")
        metadata = cls(
            tenant_id=decode_hcloud_identity(labels, "exomem_tenant_id"),
            subject_id=decode_hcloud_identity(labels, "exomem_cell_id"),
            operation_id=decode_hcloud_identity(labels, "exomem_operation_id"),
            fence_generation=int(fence_value),
        )
        if any(labels.get(key) != value for key, value in metadata.hcloud_labels.items()):
            raise MetadataConflict("HCloud provider identity digest or chunks differ")
        return metadata

    @classmethod
    def from_kubernetes_annotations(cls, annotations: dict[str, str]) -> OpaqueProviderMetadata:
        fence_value = annotations.get("exomem.io/fence", "")
        if not fence_value.isdigit():
            raise MetadataConflict("Kubernetes provider fence annotation is invalid")
        metadata = cls(
            tenant_id=annotations.get("exomem.io/tenant-id", ""),
            subject_id=annotations.get("exomem.io/cell-id", ""),
            operation_id=annotations.get("exomem.io/operation-id", ""),
            fence_generation=int(fence_value),
        )
        if any(
            annotations.get(key) != value for key, value in metadata.kubernetes_annotations.items()
        ):
            raise MetadataConflict("Kubernetes provider identity digest differs")
        return metadata

    def require_same(self, other: OpaqueProviderMetadata) -> None:
        if self != other:
            raise MetadataConflict("provider identity metadata is immutable")


@dataclass(frozen=True, slots=True)
class LifecycleConfig:
    image: str
    chart_path: str
    chart_version: str
    helm_version: str
    control_hostname: str
    transfer_hostname: str
    browser_origin: str
    release_version: str
    protocol_version: str
    operator_contract_digest: str
    contract_digest: str
    location: str


@dataclass(frozen=True, slots=True)
class HealthObservation:
    live: bool
    ready: bool
    cell_id: str
    protocol_version: str
    release_version: str
    service_authenticated: bool
    mutation_authority: bool
    read_admission: bool
    write_admission: bool
    worker_policy: dict[str, Any]
    code: str
    contract_digest: str
    policy_admitted: bool
    admission_admitted: bool

    @classmethod
    def ready_for(
        cls,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        config: LifecycleConfig,
    ) -> HealthObservation:
        return cls(
            live=True,
            ready=True,
            cell_id=metadata.subject_id,
            protocol_version=config.protocol_version,
            release_version=config.release_version,
            service_authenticated=True,
            mutation_authority=True,
            read_admission=True,
            write_admission=True,
            worker_policy=dict(request["workerPolicy"]),
            code="CELL_READY",
            contract_digest=config.contract_digest,
            policy_admitted=True,
            admission_admitted=True,
        )

    def replace(self, **changes: Any) -> HealthObservation:
        return replace(self, **changes)

    def flattened(self) -> dict[str, Any]:
        return {
            "live": self.live,
            "ready": self.ready,
            "cellId": self.cell_id,
            "protocolVersion": self.protocol_version,
            "releaseVersion": self.release_version,
            "serviceAuthenticated": self.service_authenticated,
            "mutationAuthority": self.mutation_authority,
            "readAdmission": self.read_admission,
            "writeAdmission": self.write_admission,
            "workerPolicy": self.worker_policy,
            "code": self.code,
        }


@dataclass(frozen=True, slots=True)
class RecordedVolume:
    volume_handle: str
    pv_name: str
    location: str
    metadata: OpaqueProviderMetadata


@dataclass(frozen=True, slots=True)
class VolumeAbsenceProof:
    kubernetes_pv_absent: bool
    hcloud_volume_absent: bool


class KubernetesVolumeControl(Protocol):
    async def discover_bound_volume(
        self, metadata: OpaqueProviderMetadata
    ) -> RecordedVolume | None: ...

    async def create_static_binding(self, recorded: RecordedVolume) -> None: ...

    async def delete_claim(self, recorded: RecordedVolume) -> None: ...

    async def claim_absent(self, recorded: RecordedVolume) -> bool: ...

    async def delete_pv(self, pv_name: str) -> None: ...

    async def pv_absent(self, pv_name: str) -> bool: ...


class HCloudVolumeControl(Protocol):
    async def label_volume(self, handle: str, metadata: OpaqueProviderMetadata) -> None: ...

    async def verify_volume(
        self, handle: str, metadata: OpaqueProviderMetadata, location: str
    ) -> bool: ...

    async def delete_volume(self, handle: str) -> None: ...

    async def volume_absent(self, handle: str) -> bool: ...

    async def discover_tenant_volumes(self, tenant_id: str) -> tuple[str, ...]: ...

    async def quarantine_volume(self, handle: str) -> None: ...


class VolumeLifecycleWorker:
    """The only seam that needs PV mutation and an HCloud credential."""

    def __init__(
        self,
        kubernetes: KubernetesVolumeControl,
        hcloud: HCloudVolumeControl,
        *,
        absence_attempts: int = 20,
        absence_interval_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if absence_attempts < 1 or absence_interval_seconds < 0:
            raise ValueError("absence polling bounds are invalid")
        self._kubernetes = kubernetes
        self._hcloud = hcloud
        self._absence_attempts = absence_attempts
        self._absence_interval_seconds = absence_interval_seconds
        self._sleep = sleep

    async def _await_absence(self, check: Callable[[], Awaitable[bool]]) -> bool:
        for attempt in range(self._absence_attempts):
            if await check():
                return True
            if attempt + 1 < self._absence_attempts:
                await self._sleep(self._absence_interval_seconds)
        return False

    async def register_bound_volume(self, metadata: OpaqueProviderMetadata) -> RecordedVolume:
        recorded = await self._kubernetes.discover_bound_volume(metadata)
        if recorded is None:
            raise MetadataConflict("bound CSI volume is not yet discoverable")
        recorded.metadata.require_same(metadata)
        await self._hcloud.label_volume(recorded.volume_handle, metadata)
        if not await self._hcloud.verify_volume(
            recorded.volume_handle, metadata, recorded.location
        ):
            raise MetadataConflict("HCloud volume identity or location differs")
        return recorded

    async def rebind_static(
        self,
        recorded: RecordedVolume,
        metadata: OpaqueProviderMetadata,
        *,
        location: str,
    ) -> None:
        recorded.metadata.require_same(metadata)
        if recorded.location != location:
            raise MetadataConflict("recorded volume location differs from the recovery node")
        if not await self._hcloud.verify_volume(recorded.volume_handle, metadata, location):
            raise MetadataConflict("provider volume identity differs from the durable record")
        await self._kubernetes.create_static_binding(recorded)
        rebound = await self._kubernetes.discover_bound_volume(metadata)
        if rebound != recorded:
            raise MetadataConflict("static PV/PVC did not bind the original volume handle")

    async def quarantine_orphans(
        self,
        *,
        tenant_id: str,
        registered_handles: set[str],
    ) -> tuple[str, ...]:
        discovered = await self._hcloud.discover_tenant_volumes(tenant_id)
        orphaned = tuple(sorted(set(discovered) - registered_handles))
        for handle in orphaned:
            await self._hcloud.quarantine_volume(handle)
        return orphaned

    async def destroy_retained(self, recorded: RecordedVolume) -> VolumeAbsenceProof:
        await self._kubernetes.delete_claim(recorded)
        if not await self._await_absence(lambda: self._kubernetes.claim_absent(recorded)):
            raise MetadataConflict("retained PVC deletion did not converge")
        await self._kubernetes.delete_pv(recorded.pv_name)
        pv_absent = await self._await_absence(lambda: self._kubernetes.pv_absent(recorded.pv_name))
        await self._hcloud.delete_volume(recorded.volume_handle)
        hcloud_absent = await self._await_absence(
            lambda: self._hcloud.volume_absent(recorded.volume_handle)
        )
        proof = VolumeAbsenceProof(
            kubernetes_pv_absent=pv_absent,
            hcloud_volume_absent=hcloud_absent,
        )
        if not (proof.kubernetes_pv_absent and proof.hcloud_volume_absent):
            raise MetadataConflict("retained storage absence is not independently proven")
        return proof


class LifecyclePlane(Protocol):
    """Provider composition required by the cell lifecycle reconciler."""

    async def observed_fence(self, tenant_id: str) -> int: ...
    async def observe_operation(self, context: EffectContext) -> None: ...
    def has_namespace(self, metadata: OpaqueProviderMetadata) -> bool: ...
    async def capacity_block_reason(self, metadata: OpaqueProviderMetadata) -> str | None: ...
    async def ensure_namespace(self, metadata: OpaqueProviderMetadata) -> None: ...
    def has_release(self, metadata: OpaqueProviderMetadata) -> bool: ...
    async def install_release(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        values: dict[str, Any],
    ) -> None: ...
    async def discover_bound_volume(
        self, metadata: OpaqueProviderMetadata
    ) -> RecordedVolume | None: ...
    async def verify_volume(
        self, handle: str, metadata: OpaqueProviderMetadata, location: str
    ) -> bool: ...
    def is_initialized(self, metadata: OpaqueProviderMetadata) -> bool: ...
    async def initialize(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        config: LifecycleConfig,
    ) -> bool: ...
    async def health(
        self, metadata: OpaqueProviderMetadata, request: dict[str, Any]
    ) -> HealthObservation: ...
    async def admit_runtime(self, metadata: OpaqueProviderMetadata) -> None: ...
    def runtime_admitted(self, metadata: OpaqueProviderMetadata) -> bool: ...
    async def enable_routes(self, metadata: OpaqueProviderMetadata) -> None: ...
    async def disable_routes(self, metadata: OpaqueProviderMetadata) -> None: ...
    def routes_enabled(self, metadata: OpaqueProviderMetadata) -> tuple[bool, bool]: ...
    async def prove_external_rejection(
        self, metadata: OpaqueProviderMetadata, request: dict[str, Any]
    ) -> bool: ...
    async def acquire_maintenance(
        self, metadata: OpaqueProviderMetadata, operation_id: str
    ) -> bool: ...
    async def release_maintenance(
        self, metadata: OpaqueProviderMetadata, operation_id: str
    ) -> None: ...
    async def quiesce(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        operation_id: str,
    ) -> None: ...
    async def scale(self, metadata: OpaqueProviderMetadata, replicas: int) -> None: ...
    async def resume(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        operation_id: str,
    ) -> None: ...
    async def stage_credential(
        self,
        metadata: OpaqueProviderMetadata,
        version: int,
        credential: str,
        request: dict[str, Any],
        operation_id: str,
    ) -> None: ...
    async def credential_accepted(
        self,
        metadata: OpaqueProviderMetadata,
        version: int,
        credential: str,
        request: dict[str, Any],
        operation_id: str,
    ) -> bool: ...
    async def promote_credential(
        self,
        metadata: OpaqueProviderMetadata,
        version: int,
        request: dict[str, Any],
        operation_id: str,
    ) -> bool: ...
    async def seal(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        request: dict[str, Any],
        operation_id: str,
        created_at: str,
    ) -> None: ...
    async def discard_candidate(self, metadata: OpaqueProviderMetadata) -> dict[str, bool]: ...
    async def destroy_tenant_online(self, tenant_id: str) -> None: ...
    def retention_wait_seconds(self, tenant_id: str) -> int | None: ...
    async def destroy_expired_retention(self, tenant_id: str) -> None: ...
    def destruction_proof(self, tenant_id: str) -> dict[str, bool]: ...
    def provider_reference(self, metadata: OpaqueProviderMetadata) -> str: ...


@dataclass(slots=True)
class _Cell:
    metadata: OpaqueProviderMetadata
    request_shape: dict[str, Any]
    helm_values: dict[str, Any]
    volume_handle: str
    pv_name: str
    namespace: bool = True
    release: bool = True
    initialized: bool = False
    runtime_admitted: bool = False
    replicas: int = 1
    control_route: bool = False
    transfer_route: bool = False
    quiesced: bool = False
    sealed: bool = False
    in_flight: int = 0
    external_control_rejected: bool = False
    external_transfer_rejected: bool = False
    credential_digests: dict[int, str] = field(default_factory=dict, repr=False)
    health: HealthObservation | None = None
    seal_checkpoint: tuple[str, str] | None = None
    candidate: bool = False
    failed: bool = False


@dataclass(slots=True)
class _Volume:
    handle: str
    pv_name: str
    location: str
    metadata: OpaqueProviderMetadata | None
    labels: dict[str, str]
    quarantined: bool = False


@dataclass(slots=True)
class _Retained:
    reference: str
    metadata: OpaqueProviderMetadata
    locked_until: datetime
    retain_until: datetime


class HighFidelityProviderPlane:
    """Deterministic provider emulator retaining real identity and recovery semantics."""

    def __init__(self, *, location: str, now: datetime | None = None) -> None:
        self.location = location
        self._now = now or datetime.now(UTC)
        self._cells: dict[str, _Cell] = {}
        self._volumes: dict[str, _Volume] = {}
        self._pv_to_handle: dict[str, str] = {}
        self._tenant_fences: dict[str, int] = {}
        self._locks: dict[str, str] = {}
        self._exports: dict[str, OpaqueProviderMetadata] = {}
        self._backups: dict[str, _Retained] = {}
        self._wrapped_keys: dict[str, OpaqueProviderMetadata] = {}
        self._cell_keys: dict[str, OpaqueProviderMetadata] = {}
        self._discarded_candidates: set[str] = set()
        self._orphan_routes: dict[str, OpaqueProviderMetadata] = {}
        self._orphan_credentials: dict[str, OpaqueProviderMetadata] = {}
        self._revoked_tenants: set[str] = set()
        self._billing_stopped: set[str] = set()
        self._tickets: dict[str, tuple[str, bool]] = {}
        self._lose_after_bind = False
        self._capacity_block_reason: str | None = None

    def __repr__(self) -> str:
        return (
            "HighFidelityProviderPlane("
            f"cells={len(self._cells)}, volumes={len(self._volumes)}, "
            f"backups={len(self._backups)})"
        )

    @staticmethod
    def _key(metadata: OpaqueProviderMetadata) -> str:
        return _digest(f"{metadata.tenant_id}:{metadata.subject_id}", length=64)

    def _cell(self, metadata: OpaqueProviderMetadata) -> _Cell | None:
        return self._cells.get(self._key(metadata))

    def _observe(self, metadata: OpaqueProviderMetadata) -> None:
        current = self._tenant_fences.get(metadata.tenant_id, 0)
        if metadata.fence_generation < current:
            raise DriverTerminal("PROVISIONER_STALE_FENCE")
        self._tenant_fences[metadata.tenant_id] = max(current, metadata.fence_generation)

    async def observed_fence(self, tenant_id: str) -> int:
        observed = self._tenant_fences.get(tenant_id, 0)
        for volume in self._volumes.values():
            if volume.metadata is not None and volume.metadata.tenant_id == tenant_id:
                observed = max(observed, volume.metadata.fence_generation)
        return observed

    async def observe_operation(self, context: EffectContext) -> None:
        current = self._tenant_fences.get(context.tenant_id, 0)
        if context.fence_generation < current:
            raise DriverTerminal("PROVISIONER_STALE_FENCE")
        self._tenant_fences[context.tenant_id] = context.fence_generation

    async def capacity_block_reason(self, metadata: OpaqueProviderMetadata) -> str | None:
        return self._capacity_block_reason

    def block_capacity(self, reason: str | None) -> None:
        self._capacity_block_reason = reason

    async def ensure_namespace(self, metadata: OpaqueProviderMetadata) -> None:
        self._observe(metadata)
        cell = self._cell(metadata)
        if cell is not None:
            cell.metadata.require_same(metadata)
            return
        self._cells[self._key(metadata)] = _Cell(
            metadata=metadata,
            request_shape={},
            helm_values={},
            volume_handle="",
            pv_name="",
            release=False,
            replicas=0,
        )

    def has_namespace(self, metadata: OpaqueProviderMetadata) -> bool:
        cell = self._cell(metadata)
        return bool(cell and cell.namespace)

    def has_release(self, metadata: OpaqueProviderMetadata) -> bool:
        cell = self._cell(metadata)
        return bool(cell and cell.release)

    async def install_release(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        self._observe(metadata)
        cell = self._cell(metadata)
        if cell is None:
            raise MetadataConflict("namespace must exist before Helm reconciliation")
        if cell.release:
            cell.metadata.require_same(metadata)
            if cell.helm_values != values:
                raise MetadataConflict("fixed Helm values drifted during adoption")
            return
        handle = f"volume-{_digest(metadata.subject_id, length=20)}"
        pv_name = f"pv-{_digest(handle, length=20)}"
        cell.release = True
        cell.replicas = 1
        cell.request_shape = {
            "cellId": str(request["cellId"]),
            "protocolVersion": str(request["protocolVersion"]),
            "releaseVersion": str(request["releaseVersion"]),
            "workerPolicy": dict(request["workerPolicy"]),
        }
        cell.helm_values = json.loads(json.dumps(values))
        cell.volume_handle = handle
        cell.pv_name = pv_name
        cell.credential_digests[1] = hashlib.sha256(
            str(request["serviceCredential"]).encode("utf-8")
        ).hexdigest()
        self._cell_keys[self._key(metadata)] = metadata
        self._pv_to_handle[pv_name] = handle
        self._volumes.setdefault(
            handle,
            _Volume(handle, pv_name, self.location, None, {}),
        )
        if self._lose_after_bind:
            self._lose_after_bind = False
            raise LostAcknowledgement("CSI binding committed before acknowledgement")

    def lose_acknowledgement_after_csi_bind_once(self) -> None:
        self._lose_after_bind = True

    async def initialize(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        config: LifecycleConfig,
    ) -> bool:
        cell = self._require_cell(metadata)
        cell.initialized = True
        cell.helm_values["workloadMode"] = "serve"
        cell.health = HealthObservation.ready_for(metadata, cell.request_shape, config)
        return True

    def is_initialized(self, metadata: OpaqueProviderMetadata) -> bool:
        cell = self._cell(metadata)
        return bool(cell and cell.initialized)

    async def health(
        self, metadata: OpaqueProviderMetadata, request: dict[str, Any]
    ) -> HealthObservation:
        cell = self._require_cell(metadata)
        if cell.health is None:
            raise MetadataConflict("runtime health is unavailable")
        return cell.health

    def set_health(self, metadata: OpaqueProviderMetadata, health: HealthObservation) -> None:
        self._require_cell(metadata).health = health

    async def admit_runtime(self, metadata: OpaqueProviderMetadata) -> None:
        self._require_cell(metadata).runtime_admitted = True

    def runtime_admitted(self, metadata: OpaqueProviderMetadata) -> bool:
        cell = self._cell(metadata)
        return bool(cell and cell.runtime_admitted)

    async def enable_routes(self, metadata: OpaqueProviderMetadata) -> None:
        cell = self._require_cell(metadata)
        cell.control_route = True
        cell.transfer_route = True
        cell.external_control_rejected = False
        cell.external_transfer_rejected = False

    async def disable_routes(self, metadata: OpaqueProviderMetadata) -> None:
        cell = self._require_cell(metadata)
        cell.control_route = False
        cell.transfer_route = False

    async def prove_external_rejection(
        self, metadata: OpaqueProviderMetadata, request: dict[str, Any]
    ) -> bool:
        cell = self._require_cell(metadata)
        tickets = [
            ticket
            for ticket, (cell_key, reached) in self._tickets.items()
            if cell_key == self._key(metadata) and not reached
        ]
        if not tickets:
            return False
        if cell.control_route or cell.transfer_route:
            self._tickets[tickets[-1]] = (self._key(metadata), True)
            return False
        cell.external_control_rejected = not cell.control_route
        cell.external_transfer_rejected = not cell.transfer_route
        return cell.external_control_rejected and cell.external_transfer_rejected

    async def acquire_maintenance(
        self, metadata: OpaqueProviderMetadata, operation_id: str
    ) -> bool:
        key = self._key(metadata)
        owner = self._locks.get(key)
        if owner not in (None, operation_id):
            return False
        self._locks[key] = operation_id
        return True

    async def release_maintenance(
        self, metadata: OpaqueProviderMetadata, operation_id: str
    ) -> None:
        key = self._key(metadata)
        if self._locks.get(key) == operation_id:
            self._locks.pop(key, None)

    async def quiesce(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        operation_id: str,
    ) -> None:
        cell = self._require_cell(metadata)
        cell.in_flight = 0
        cell.quiesced = True

    async def scale(self, metadata: OpaqueProviderMetadata, replicas: int) -> None:
        cell = self._require_cell(metadata)
        if replicas == 0 and not cell.quiesced:
            raise MetadataConflict("compute cannot stop before runtime drain")
        cell.replicas = replicas

    async def resume(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        operation_id: str,
    ) -> None:
        cell = self._require_cell(metadata)
        if cell.sealed:
            raise MetadataConflict("sealed runtime cannot resume")
        cell.quiesced = False

    async def stage_credential(
        self,
        metadata: OpaqueProviderMetadata,
        version: int,
        credential: str,
        request: dict[str, Any],
        operation_id: str,
    ) -> None:
        cell = self._require_cell(metadata)
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        existing = cell.credential_digests.get(version)
        if existing is not None and existing != digest:
            raise MetadataConflict("credential version is immutable")
        cell.credential_digests[version] = digest

    async def credential_accepted(
        self,
        metadata: OpaqueProviderMetadata,
        version: int,
        credential: str,
        request: dict[str, Any],
        operation_id: str,
    ) -> bool:
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        return self._require_cell(metadata).credential_digests.get(version) == digest

    async def promote_credential(
        self,
        metadata: OpaqueProviderMetadata,
        version: int,
        request: dict[str, Any],
        operation_id: str,
    ) -> bool:
        cell = self._require_cell(metadata)
        if version not in cell.credential_digests:
            raise MetadataConflict("pending credential was not staged")
        cell.credential_digests = {version: cell.credential_digests[version]}
        return len(cell.credential_digests) == 1

    async def seal(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        request: dict[str, Any],
        operation_id: str,
        created_at: str,
    ) -> None:
        cell = self._require_cell(metadata)
        if not cell.quiesced or cell.control_route or cell.transfer_route:
            raise MetadataConflict("seal requires closed routes and drained runtime")
        checkpoint = (operation_id, created_at)
        if cell.seal_checkpoint is not None and cell.seal_checkpoint != checkpoint:
            raise MetadataConflict("seal checkpoint identity is immutable")
        cell.seal_checkpoint = checkpoint
        cell.sealed = True
        cell.credential_digests.clear()

    async def discard_candidate(self, metadata: OpaqueProviderMetadata) -> dict[str, bool]:
        key = self._key(metadata)
        cell = self._cell(metadata)
        if cell is None and key not in self._discarded_candidates:
            raise MetadataConflict("discard target is not a registered failed candidate")
        if cell is not None and (not cell.candidate or not cell.failed):
            raise MetadataConflict("discard target is not a failed candidate")
        volume_handle = cell.volume_handle if cell else None
        pv_name = cell.pv_name if cell else None
        self._cells.pop(key, None)
        if pv_name:
            self._pv_to_handle.pop(pv_name, None)
        if volume_handle:
            self._volumes.pop(volume_handle, None)
        self._locks.pop(key, None)
        self._cell_keys.pop(key, None)
        self._discarded_candidates.add(key)
        return {
            "computeDestroyed": self._cell(metadata) is None,
            "storageDestroyed": volume_handle is None or volume_handle not in self._volumes,
            "keysDestroyed": key not in self._cell_keys,
        }

    async def destroy_tenant_online(self, tenant_id: str) -> None:
        self._revoked_tenants.add(tenant_id)
        self._billing_stopped.add(tenant_id)
        for key, cell in tuple(self._cells.items()):
            if cell.metadata.tenant_id != tenant_id:
                continue
            cell.control_route = False
            cell.transfer_route = False
            cell.quiesced = True
            cell.in_flight = 0
            cell.sealed = True
            cell.credential_digests.clear()
            self._cells.pop(key, None)
            self._locks.pop(key, None)
            self._cell_keys.pop(key, None)
            if cell.pv_name:
                self._pv_to_handle.pop(cell.pv_name, None)
            if cell.volume_handle:
                self._volumes.pop(cell.volume_handle, None)
        for handle, volume in tuple(self._volumes.items()):
            if volume.metadata is not None and volume.metadata.tenant_id == tenant_id:
                self._volumes.pop(handle, None)
        for reference, metadata in tuple(self._orphan_routes.items()):
            if metadata.tenant_id == tenant_id:
                self._orphan_routes.pop(reference, None)
        for reference, metadata in tuple(self._orphan_credentials.items()):
            if metadata.tenant_id == tenant_id:
                self._orphan_credentials.pop(reference, None)

    def retention_wait_seconds(self, tenant_id: str) -> int | None:
        waits = [
            retained.locked_until
            for retained in self._backups.values()
            if retained.metadata.tenant_id == tenant_id and retained.locked_until > self._now
        ]
        if not waits:
            return None
        return max(1, int((min(waits) - self._now).total_seconds()))

    async def destroy_expired_retention(self, tenant_id: str) -> None:
        for reference, retained in tuple(self._backups.items()):
            if retained.metadata.tenant_id == tenant_id and retained.locked_until <= self._now:
                self._backups.pop(reference, None)
                self._wrapped_keys.pop(reference, None)
        for reference, metadata in tuple(self._exports.items()):
            if metadata.tenant_id == tenant_id:
                self._exports.pop(reference, None)

    def destruction_proof(self, tenant_id: str) -> dict[str, bool]:
        compute = not any(c.metadata.tenant_id == tenant_id for c in self._cells.values())
        storage = not any(
            v.metadata is not None and v.metadata.tenant_id == tenant_id
            for v in self._volumes.values()
        )
        retained = not any(b.metadata.tenant_id == tenant_id for b in self._backups.values())
        exports = not any(m.tenant_id == tenant_id for m in self._exports.values())
        keys = not any(
            metadata.tenant_id == tenant_id for metadata in self._wrapped_keys.values()
        ) and not any(metadata.tenant_id == tenant_id for metadata in self._cell_keys.values())
        routes = not any(
            metadata.tenant_id == tenant_id for metadata in self._orphan_routes.values()
        )
        credentials = not any(
            metadata.tenant_id == tenant_id for metadata in self._orphan_credentials.values()
        )
        return {
            "computeDestroyed": compute,
            "storageDestroyed": storage,
            "keysDestroyed": keys and retained,
            "tenantResourcesDestroyed": (
                compute
                and storage
                and retained
                and exports
                and keys
                and routes
                and credentials
                and tenant_id in self._revoked_tenants
                and tenant_id in self._billing_stopped
            ),
        }

    async def seed_ready_cell(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        config: LifecycleConfig,
        *,
        candidate: bool = False,
        failed: bool = False,
    ) -> None:
        await self.ensure_namespace(metadata)
        await self.install_release(metadata, request, _fixed_helm_values(metadata, request, config))
        await VolumeLifecycleWorker(self, self).register_bound_volume(metadata)
        await self.initialize(metadata, request, config)
        await self.admit_runtime(metadata)
        await self.enable_routes(metadata)
        cell = self._require_cell(metadata)
        cell.candidate = candidate
        cell.failed = failed
        self.seed_unused_transfer_ticket(
            metadata, f"maintenance-{_digest(metadata.subject_id, length=20)}"
        )

    def seed_bound_volume(
        self, metadata: OpaqueProviderMetadata, *, handle: str, pv_name: str
    ) -> None:
        self._observe(metadata)
        self._volumes[handle] = _Volume(handle, pv_name, self.location, None, {})
        self._pv_to_handle[pv_name] = handle
        self._cells[self._key(metadata)] = _Cell(
            metadata=metadata,
            request_shape={},
            helm_values={},
            volume_handle=handle,
            pv_name=pv_name,
        )

    def seed_provider_volume(self, metadata: OpaqueProviderMetadata, *, handle: str) -> None:
        self._observe(metadata)
        pv_name = f"pv-{_digest(handle, length=20)}"
        self._volumes[handle] = _Volume(
            handle, pv_name, self.location, metadata, dict(metadata.hcloud_labels)
        )

    async def discover_bound_volume(
        self, metadata: OpaqueProviderMetadata
    ) -> RecordedVolume | None:
        cell = self._cell(metadata)
        if cell is None or not cell.volume_handle:
            return None
        volume = self._volumes.get(cell.volume_handle)
        if volume is None:
            return None
        return RecordedVolume(volume.handle, volume.pv_name, volume.location, metadata)

    async def create_static_binding(self, recorded: RecordedVolume) -> None:
        volume = self._volumes.get(recorded.volume_handle)
        if volume is None:
            raise MetadataConflict("recorded HCloud volume is absent")
        if volume.location != recorded.location:
            raise MetadataConflict("recorded HCloud volume location differs")
        self._pv_to_handle[recorded.pv_name] = recorded.volume_handle
        cell = self._cell(recorded.metadata)
        if cell is None:
            cell = _Cell(
                metadata=recorded.metadata,
                request_shape={},
                helm_values={},
                volume_handle=recorded.volume_handle,
                pv_name=recorded.pv_name,
                release=False,
                replicas=0,
            )
            self._cells[self._key(recorded.metadata)] = cell
        else:
            cell.volume_handle = recorded.volume_handle
            cell.pv_name = recorded.pv_name

    async def delete_claim(self, recorded: RecordedVolume) -> None:
        self._cells.pop(self._key(recorded.metadata), None)

    async def claim_absent(self, recorded: RecordedVolume) -> bool:
        return self._cell(recorded.metadata) is None

    async def delete_pv(self, pv_name: str) -> None:
        self._pv_to_handle.pop(pv_name, None)

    async def pv_absent(self, pv_name: str) -> bool:
        return pv_name not in self._pv_to_handle

    async def label_volume(self, handle: str, metadata: OpaqueProviderMetadata) -> None:
        volume = self._volumes.get(handle)
        if volume is None:
            raise MetadataConflict("HCloud volume is absent")
        if volume.metadata is not None:
            volume.metadata.require_same(metadata)
        volume.metadata = metadata
        volume.labels = dict(metadata.hcloud_labels)
        self._observe(metadata)

    async def verify_volume(
        self, handle: str, metadata: OpaqueProviderMetadata, location: str
    ) -> bool:
        volume = self._volumes.get(handle)
        return bool(
            volume
            and volume.location == location
            and volume.metadata == metadata
            and all(
                volume.labels.get(key) == value for key, value in metadata.hcloud_labels.items()
            )
        )

    async def delete_volume(self, handle: str) -> None:
        self._volumes.pop(handle, None)

    async def volume_absent(self, handle: str) -> bool:
        return handle not in self._volumes

    async def discover_tenant_volumes(self, tenant_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                handle
                for handle, volume in self._volumes.items()
                if volume.metadata is not None and volume.metadata.tenant_id == tenant_id
            )
        )

    async def quarantine_volume(self, handle: str) -> None:
        volume = self._volumes.get(handle)
        if volume is None:
            raise MetadataConflict("cannot quarantine an absent volume")
        volume.quarantined = True
        volume.labels["exomem_quarantine"] = "true"

    def _require_cell(self, metadata: OpaqueProviderMetadata) -> _Cell:
        cell = self._cell(metadata)
        if cell is None:
            raise MetadataConflict("cell provider resources are absent")
        return cell

    def volume_labels(self, handle: str) -> dict[str, str]:
        return dict(self._volumes[handle].labels)

    def is_quarantined(self, handle: str) -> bool:
        return self._volumes[handle].quarantined

    def lose_kubernetes_state(self) -> None:
        self._cells.clear()
        self._pv_to_handle.clear()

    def bound_handle(self, metadata: OpaqueProviderMetadata) -> str | None:
        cell = self._cell(metadata)
        return cell.volume_handle if cell and cell.volume_handle else None

    def count_volumes(self, metadata: OpaqueProviderMetadata) -> int:
        return sum(1 for v in self._volumes.values() if v.metadata == metadata)

    def provider_reference(self, metadata: OpaqueProviderMetadata) -> str:
        return f"cell-{_digest(metadata.tenant_id + ':' + metadata.subject_id, length=32)}"

    def helm_values(self, metadata: OpaqueProviderMetadata) -> dict[str, Any]:
        return json.loads(json.dumps(self._require_cell(metadata).helm_values))

    def routes_enabled(self, metadata: OpaqueProviderMetadata) -> tuple[bool, bool]:
        cell = self._require_cell(metadata)
        return cell.control_route, cell.transfer_route

    def external_rejection_proved(self, metadata: OpaqueProviderMetadata) -> tuple[bool, bool]:
        cell = self._require_cell(metadata)
        return cell.external_control_rejected, cell.external_transfer_rejected

    def seed_unused_transfer_ticket(self, metadata: OpaqueProviderMetadata, ticket: str) -> None:
        self._tickets[ticket] = (self._key(metadata), False)

    def ticket_reached_cell(self, ticket: str) -> bool:
        return self._tickets[ticket][1]

    def set_in_flight(self, metadata: OpaqueProviderMetadata, count: int) -> None:
        self._require_cell(metadata).in_flight = count

    def in_flight(self, metadata: OpaqueProviderMetadata) -> int:
        return self._require_cell(metadata).in_flight

    def replicas(self, metadata: OpaqueProviderMetadata) -> int:
        return self._require_cell(metadata).replicas

    def accepted_credential_versions(self, metadata: OpaqueProviderMetadata) -> tuple[int, ...]:
        return tuple(sorted(self._require_cell(metadata).credential_digests))

    def is_sealed(self, metadata: OpaqueProviderMetadata) -> bool:
        return self._require_cell(metadata).sealed

    def seal_created_at(self, metadata: OpaqueProviderMetadata) -> str | None:
        checkpoint = self._require_cell(metadata).seal_checkpoint
        return checkpoint[1] if checkpoint is not None else None

    def cell_exists(self, metadata: OpaqueProviderMetadata) -> bool:
        return self._cell(metadata) is not None

    def seed_export(self, metadata: OpaqueProviderMetadata, reference: str) -> None:
        self._exports[reference] = metadata

    def export_exists(self, reference: str) -> bool:
        return reference in self._exports

    def seed_backup(
        self,
        metadata: OpaqueProviderMetadata,
        reference: str,
        *,
        locked_until: datetime,
        retain_until: datetime | None = None,
    ) -> None:
        self._backups[reference] = _Retained(
            reference,
            metadata,
            locked_until,
            retain_until or locked_until + timedelta(days=23),
        )
        self._wrapped_keys[reference] = metadata

    def seed_orphan_route(self, metadata: OpaqueProviderMetadata, reference: str) -> None:
        self._orphan_routes[reference] = metadata

    def seed_orphan_credential(self, metadata: OpaqueProviderMetadata, reference: str) -> None:
        self._orphan_credentials[reference] = metadata

    def online_access_revoked(self, tenant_id: str) -> bool:
        return (
            tenant_id in self._revoked_tenants
            and tenant_id in self._billing_stopped
            and not any(
                metadata.tenant_id == tenant_id for metadata in self._orphan_routes.values()
            )
            and not any(
                metadata.tenant_id == tenant_id for metadata in self._orphan_credentials.values()
            )
        )

    def backup_exists(self, reference: str) -> bool:
        return reference in self._backups

    def online_resources_absent(self, tenant_id: str) -> bool:
        proof = self.destruction_proof(tenant_id)
        return proof["computeDestroyed"] and proof["storageDestroyed"]

    def tenant_absent(self, tenant_id: str) -> bool:
        return self.destruction_proof(tenant_id)["tenantResourcesDestroyed"]

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _metadata_from_context(context: EffectContext) -> OpaqueProviderMetadata:
    if context.cell_id is None:
        raise MetadataConflict("cell action lacks a cell or candidate identity")
    return OpaqueProviderMetadata(
        tenant_id=context.tenant_id,
        subject_id=context.cell_id,
        operation_id=context.provider_operation_id,
        fence_generation=context.fence_generation,
    )


def _fixed_helm_values(
    metadata: OpaqueProviderMetadata,
    request: dict[str, Any],
    config: LifecycleConfig,
) -> dict[str, Any]:
    worker_policy = json.dumps(
        request["workerPolicy"], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "activeCredentialVersion": "1",
        "browserOrigin": config.browser_origin,
        "cellId": metadata.subject_id,
        "credentialsSecretName": "exomem-cell-credentials",
        "credentialsManagedExternally": True,
        "expectedProtocol": config.protocol_version,
        "expectedRelease": config.release_version,
        "featureGrants": "",
        "image": config.image,
        "initOperationId": metadata.operation_id,
        "initRequestId": _deterministic_uuid4(metadata.operation_id + ":init"),
        "pvcSize": "10Gi",
        "providerIdentity": {
            "tenantId": metadata.tenant_id,
            "cellId": metadata.subject_id,
            "operationId": metadata.operation_id,
            "fence": str(metadata.fence_generation),
            "operationDigest": metadata.kubernetes_annotations["exomem.io/operation-digest"],
            "subjectDigest": metadata.kubernetes_annotations["exomem.io/subject-digest"],
            "tenantDigest": metadata.kubernetes_annotations["exomem.io/tenant-digest"],
        },
        "resourceName": metadata.resource_name,
        "routes": {"controlHostname": config.control_hostname, "enabled": False},
        "runtimeGid": 10001,
        "runtimeUid": 10001,
        "storageClassName": "exomem-hcloud-encrypted-retain",
        "storageLimitBytes": 5 * 1024**3,
        "transferHostname": config.transfer_hostname,
        "uploadLimitBytes": 90 * 1024**2,
        "vaultId": metadata.subject_id,
        "workerLimit": 0,
        "workerPolicyDigest": hashlib.sha256(worker_policy).hexdigest(),
        "workloadMode": "initialize",
    }


def _deterministic_uuid4(value: str) -> str:
    raw = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    hexadecimal = raw.hex()
    return (
        f"{hexadecimal[:8]}-{hexadecimal[8:12]}-{hexadecimal[12:16]}-"
        f"{hexadecimal[16:20]}-{hexadecimal[20:]}"
    )


class CellLifecycleDriver:
    """Multi-checkpoint driver for the provider/cell lifecycle slice."""

    def __init__(
        self,
        *,
        plane: LifecyclePlane,
        volume_worker: VolumeLifecycleWorker,
        config: LifecycleConfig,
    ) -> None:
        self._plane = plane
        self._volumes = volume_worker
        self._config = config

    async def observed_fence(self, tenant_id: str) -> int:
        return await self._plane.observed_fence(tenant_id)

    async def execute(
        self,
        action: str,
        request: dict[str, Any],
        context: EffectContext,
    ) -> DriverPending | DriverFinal:
        try:
            if ("protocolVersion" in request or "releaseVersion" in request) and (
                request.get("protocolVersion") != self._config.protocol_version
                or request.get("releaseVersion") != self._config.release_version
            ):
                raise DriverTerminal("PROVISIONER_RELEASE_UNIT_MISMATCH")
            if await self.observed_fence(context.tenant_id) > context.fence_generation:
                raise DriverTerminal("PROVISIONER_STALE_FENCE")
            await self._plane.observe_operation(context)
            if action == "provision":
                return await self._provision(request, context)
            if action == "health":
                return DriverFinal(
                    await self._exact_health(_metadata_from_context(context), request)
                )
            if action in {"quiesce", "stop", "resume", "seal"}:
                return await self._lifecycle(action, request, context)
            if action == "rotate-credential":
                return await self._rotate(request, context)
            if action == "discard":
                return await self._discard(context)
            if action == "destroy":
                return await self._destroy(context)
            raise DriverTerminal("PROVISIONER_ACTION_NOT_IMPLEMENTED")
        except MetadataConflict as error:
            raise DriverTerminal("PROVISIONER_PROVIDER_METADATA_CONFLICT") from error

    async def _provision(
        self, request: dict[str, Any], context: EffectContext
    ) -> DriverPending | DriverFinal:
        metadata = _metadata_from_context(context)
        if not self._plane.has_namespace(metadata):
            capacity_reason = await self._plane.capacity_block_reason(metadata)
            if capacity_reason is not None:
                return DriverPending(f"capacity-{capacity_reason}", 300)
            await self._plane.ensure_namespace(metadata)
            return DriverPending(
                "namespace-ready",
                1,
                (DriverResource(ResourceKind.KUBERNETES_NAMESPACE, metadata.resource_name),),
            )
        if not self._plane.has_release(metadata):
            await self._plane.install_release(
                metadata, request, _fixed_helm_values(metadata, request, self._config)
            )
            return DriverPending(
                "release-applied",
                1,
                (
                    DriverResource(ResourceKind.HELM_RELEASE, metadata.resource_name),
                    DriverResource(ResourceKind.PVC, metadata.resource_name + "-data"),
                ),
            )
        if context.checkpoint not in {
            "release-applied",
            "volume-owned",
            "initializing",
            "initialized",
            "runtime-admitted",
            "routes-open",
        }:
            return DriverPending(
                "release-applied",
                1,
                (
                    DriverResource(ResourceKind.HELM_RELEASE, metadata.resource_name),
                    DriverResource(ResourceKind.PVC, metadata.resource_name + "-data"),
                ),
            )
        recorded = await self._plane.discover_bound_volume(metadata)
        if recorded is None:
            return DriverPending("csi-binding", 2)
        if not await self._plane.verify_volume(recorded.volume_handle, metadata, recorded.location):
            recorded = await self._volumes.register_bound_volume(metadata)
            return DriverPending(
                "volume-owned",
                1,
                (DriverResource(ResourceKind.VOLUME, recorded.volume_handle),),
            )
        if not self._plane.is_initialized(metadata):
            if not await self._plane.initialize(metadata, request, self._config):
                return DriverPending("initializing", 2)
            return DriverPending("initialized", 1)
        if not self._plane.runtime_admitted(metadata):
            await self._exact_health(metadata, request)
            await self._plane.admit_runtime(metadata)
            return DriverPending("runtime-admitted", 1)
        if self._plane.routes_enabled(metadata) != (True, True):
            await self._plane.enable_routes(metadata)
            return DriverPending(
                "routes-open",
                1,
                (DriverResource(ResourceKind.ROUTE, metadata.resource_name + "-routes"),),
            )
        return DriverFinal(
            {
                "providerRef": self._plane.provider_reference(metadata),
                "privateEndpoint": (
                    f"https://{self._config.control_hostname}/cells/{metadata.subject_id}"
                ),
            }
        )

    async def _exact_health(
        self, metadata: OpaqueProviderMetadata, request: dict[str, Any]
    ) -> dict[str, Any]:
        health = await self._plane.health(metadata, request)
        expected = HealthObservation.ready_for(metadata, request, self._config)
        if health != expected:
            raise DriverTerminal("PROVISIONER_RUNTIME_CONTRACT_MISMATCH")
        return health.flattened()

    async def _maintenance_checkpoint(
        self,
        metadata: OpaqueProviderMetadata,
        context: EffectContext,
        known_checkpoints: set[str],
    ) -> DriverPending | None:
        if not await self._plane.acquire_maintenance(metadata, context.provider_operation_id):
            checkpoint = (
                context.checkpoint
                if context.checkpoint in known_checkpoints
                else "maintenance-wait"
            )
            return DriverPending(checkpoint, 2)
        if context.checkpoint not in known_checkpoints or context.checkpoint == "maintenance-wait":
            return DriverPending("maintenance-acquired", 1)
        return None

    async def _lifecycle(
        self,
        action: str,
        request: dict[str, Any],
        context: EffectContext,
    ) -> DriverPending | DriverFinal:
        metadata = _metadata_from_context(context)
        operation_id = context.provider_operation_id
        if action in {"quiesce", "stop", "seal"}:
            known = {
                "maintenance-wait",
                "maintenance-acquired",
                "routes-closed",
                "runtime-drained",
                "compute-stopped",
                "runtime-sealed",
            }
            checkpoint = await self._maintenance_checkpoint(metadata, context, known)
            if checkpoint is not None:
                return checkpoint
            if context.checkpoint == "maintenance-acquired":
                await self._plane.disable_routes(metadata)
                if not await self._plane.prove_external_rejection(metadata, request):
                    raise DriverTerminal("PROVISIONER_ROUTE_CLOSURE_UNPROVEN")
                return DriverPending("routes-closed", 1)
            if context.checkpoint == "routes-closed":
                await self._plane.quiesce(metadata, request, operation_id)
                return DriverPending("runtime-drained", 1)
            if context.checkpoint == "runtime-drained" and action == "stop":
                await self._plane.scale(metadata, 0)
                return DriverPending("compute-stopped", 1)
            if context.checkpoint == "runtime-drained" and action == "seal":
                await self._plane.seal(
                    metadata,
                    request=request,
                    operation_id=operation_id,
                    created_at=context.operation_created_at,
                )
                return DriverPending("runtime-sealed", 1)
            if action == "quiesce":
                await self._plane.quiesce(metadata, request, operation_id)
            elif action == "stop":
                await self._plane.scale(metadata, 0)
            else:
                await self._plane.seal(
                    metadata,
                    request=request,
                    operation_id=operation_id,
                    created_at=context.operation_created_at,
                )
            await self._plane.release_maintenance(metadata, operation_id)
            return DriverFinal({})
        known = {
            "maintenance-wait",
            "maintenance-acquired",
            "compute-started",
            "runtime-resumed",
            "runtime-admitted",
            "routes-open",
        }
        checkpoint = await self._maintenance_checkpoint(metadata, context, known)
        if checkpoint is not None:
            return checkpoint
        if context.checkpoint == "maintenance-acquired":
            await self._plane.scale(metadata, 1)
            return DriverPending("compute-started", 1)
        if context.checkpoint == "compute-started":
            await self._plane.resume(metadata, request, operation_id)
            return DriverPending("runtime-resumed", 1)
        if context.checkpoint == "runtime-resumed":
            await self._exact_health(metadata, request)
            return DriverPending("runtime-admitted", 1)
        if context.checkpoint == "runtime-admitted":
            await self._plane.enable_routes(metadata)
            return DriverPending("routes-open", 1)
        await self._exact_health(metadata, request)
        if self._plane.routes_enabled(metadata) != (True, True):
            await self._plane.enable_routes(metadata)
        await self._plane.release_maintenance(metadata, operation_id)
        return DriverFinal({})

    async def _rotate(
        self, request: dict[str, Any], context: EffectContext
    ) -> DriverPending | DriverFinal:
        metadata = _metadata_from_context(context)
        known = {
            "maintenance-wait",
            "maintenance-acquired",
            "credential-staged",
            "credential-proved",
            "credential-promoted",
        }
        checkpoint = await self._maintenance_checkpoint(metadata, context, known)
        if checkpoint is not None:
            return checkpoint
        version = int(request["credentialVersion"])
        credential = str(request["nextCredential"])
        if context.checkpoint == "maintenance-acquired":
            await self._plane.stage_credential(
                metadata, version, credential, request, context.provider_operation_id
            )
            return DriverPending("credential-staged", 1)
        if context.checkpoint == "credential-staged":
            if not await self._plane.credential_accepted(
                metadata, version, credential, request, context.provider_operation_id
            ):
                raise DriverTerminal("PROVISIONER_PENDING_CREDENTIAL_UNPROVEN")
            return DriverPending("credential-proved", 1)
        if context.checkpoint == "credential-proved" and request["phase"] == "finalize":
            if not await self._plane.promote_credential(
                metadata, version, request, context.provider_operation_id
            ):
                raise DriverTerminal("PROVISIONER_PREVIOUS_CREDENTIAL_ACCEPTED")
            return DriverPending("credential-promoted", 1)
        previous_rejected = False
        if request["phase"] == "finalize":
            previous_rejected = await self._plane.promote_credential(
                metadata, version, request, context.provider_operation_id
            )
            if not previous_rejected:
                raise DriverTerminal("PROVISIONER_PREVIOUS_CREDENTIAL_ACCEPTED")
        await self._plane.release_maintenance(metadata, context.provider_operation_id)
        return DriverFinal({"previousCredentialRejected": previous_rejected})

    async def _discard(self, context: EffectContext) -> DriverPending | DriverFinal:
        proof = await self._plane.discard_candidate(_metadata_from_context(context))
        if not all(proof.values()):
            return DriverPending("candidate-absence-verification", 2)
        if context.checkpoint not in {
            "candidate-destroyed",
            "candidate-absence-verification",
        }:
            return DriverPending("candidate-destroyed", 1)
        return DriverFinal(proof)

    async def _destroy(self, context: EffectContext) -> DriverPending | DriverFinal:
        if context.checkpoint not in {
            "online-destroyed",
            "retained-wait",
            "retention-destroyed",
            "absence-verification",
        }:
            await self._plane.destroy_tenant_online(context.tenant_id)
            return DriverPending("online-destroyed", 1)
        await self._plane.destroy_tenant_online(context.tenant_id)
        wait = self._plane.retention_wait_seconds(context.tenant_id)
        if wait is not None:
            return DriverPending("retained-wait", min(300, wait))
        if context.checkpoint not in {"retention-destroyed", "absence-verification"}:
            await self._plane.destroy_expired_retention(context.tenant_id)
            return DriverPending("retention-destroyed", 1)
        proof = self._plane.destruction_proof(context.tenant_id)
        if not all(proof.values()):
            return DriverPending("absence-verification", 2)
        return DriverFinal(proof)
