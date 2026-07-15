"""Signed live capacity evidence and serialized PostgreSQL reservations."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .lifecycle import MetadataConflict, OpaqueProviderMetadata
from .models import (
    CapacityDestructiveFence,
    CapacityLedger,
    CapacityReleaseReason,
    CapacityReservation,
    CapacityReservationClass,
    OperationAction,
)
from .repository import (
    OperationSnapshot,
    StaleFence,
    _as_utc,
    _database_now,
    _lock_active_claim,
)

_DOMAIN = b"exomem.capacity-live-receipt.v1\0"
_RECEIPT_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_MAX_RECEIPT_BYTES = 65_536
_HCLOUD_CSI_DRIVER = "csi.hetzner.cloud"
_CELL_RESOURCE_NAME = re.compile(r"exo-[0-9a-f]{20}\Z")
_CELL_NAMESPACE_MARKERS = frozenset(
    {
        "exomem.io/tenant-cell",
        "exomem.io/cell-resource",
        "exomem.io/resource-name",
        "exomem.io/tenant-id",
        "exomem.io/cell-id",
        "exomem.io/operation-id",
        "exomem.io/fence",
        "exomem.io/provision-mode",
    }
)


class CapacityError(RuntimeError):
    pass


class CapacityReceiptError(CapacityError):
    pass


class CapacityObservationError(CapacityError):
    pass


class CapacityConflict(CapacityError):
    pass


class CapacityIdentityConflict(CapacityConflict):
    """Expected immutable reservation/fence conflict safe for terminal handling."""


class CapacityBlocked(CapacityError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CapacityObservation:
    observed_at: datetime
    cluster_uid: str
    hcloud_server_id: int
    hcloud_location: str
    user_resource_names: frozenset[str]
    recovery_resource_names: frozenset[str]
    orphan_attachment_ids: frozenset[str]
    attached_hcloud_volumes: int

    @property
    def potential_resource_names(self) -> frozenset[str]:
        return self.user_resource_names | self.recovery_resource_names


@dataclass(frozen=True, slots=True)
class VerifiedCapacityReceipt:
    receipt_id: str
    sequence: int
    observed_at: datetime
    expires_at: datetime
    cluster_uid: str
    hcloud_server_id: int
    hcloud_location: str
    active_user_cells: int
    active_recovery_cells: int
    attached_volumes: int


@dataclass(frozen=True, slots=True)
class CapacityReservationSnapshot:
    id: str
    tenant_id: str
    cell_id: str
    resource_name: str
    reservation_class: CapacityReservationClass
    reserving_operation_id: str
    provider_operation_id: str
    fence_generation: int
    released_at: datetime | None


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_contract_digest(contract: dict[str, Any] | dict[str, object]) -> str:
    return hashlib.sha256(_canonical(dict(contract))).hexdigest()


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CapacityReceiptError("capacity receipt contains duplicate fields")
        value[key] = item
    return value


def _whole_second_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise CapacityReceiptError("capacity receipt timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise CapacityReceiptError("capacity receipt timestamp is invalid") from error
    if parsed.tzinfo != UTC or parsed.microsecond:
        raise CapacityReceiptError("capacity receipt timestamp is invalid")
    return parsed


def _positive_integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CapacityReceiptError(f"{description} is invalid")
    return value


def _non_negative_integer(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CapacityReceiptError(f"{description} is invalid")
    return value


def _public_key(raw: str) -> Ed25519PublicKey:
    if "=" in raw or re.fullmatch(r"[A-Za-z0-9_-]{43}", raw) is None:
        raise CapacityReceiptError("capacity receipt public key is invalid")
    try:
        decoded = base64.b64decode(raw + "=", altchars=b"-_", validate=True)
        return Ed25519PublicKey.from_public_bytes(decoded)
    except (ValueError, binascii.Error) as error:
        raise CapacityReceiptError("capacity receipt public key is invalid") from error


def _public_key_id(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


class CapacityReceiptVerifier:
    """Verify one bounded Ed25519 receipt against fresh local observation."""

    def __init__(
        self,
        *,
        contract: dict[str, Any] | dict[str, object],
        public_key: str,
        expected_server_id: int,
        expected_location: str,
    ) -> None:
        self._contract = dict(contract)
        self._key = _public_key(public_key)
        self._server_id = _positive_integer(expected_server_id, "expected HCloud server")
        self._location = expected_location
        authentication = self._contract.get("receipt_authentication")
        limits = self._contract.get("limits")
        if (
            not isinstance(authentication, dict)
            or set(
                name
                for name in (
                    "algorithm",
                    "capacity_domain",
                    "capacity_ttl_seconds",
                    "capacity_public_key_id",
                )
                if name not in authentication
            )
            or authentication.get("algorithm") != "ed25519"
            or authentication.get("capacity_domain") != _DOMAIN[:-1].decode("ascii")
            or authentication.get("capacity_ttl_seconds") != 300
            or authentication.get("capacity_public_key_id") != _public_key_id(self._key)
            or limits
            != {
                "active_user_cells": 6,
                "active_recovery_cells": 2,
                "maximum_potential_attachments": 8,
                "provider_volume_attachment_limit": 16,
                "minimum_unused_provider_headroom": 8,
            }
        ):
            raise CapacityReceiptError("capacity contract authentication is invalid")

    def verify(
        self,
        raw: str | bytes,
        *,
        observation: CapacityObservation,
        now: datetime,
    ) -> VerifiedCapacityReceipt:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not 1 <= len(encoded) <= _MAX_RECEIPT_BYTES:
            raise CapacityReceiptError("capacity receipt size is invalid")
        try:
            receipt = json.loads(encoded.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CapacityReceiptError("capacity receipt JSON is invalid") from error
        if not isinstance(receipt, dict):
            raise CapacityReceiptError("capacity receipt shape is invalid")
        expected = {
            "schema_version",
            "issuer",
            "contract_sha256",
            "receipt_id",
            "sequence",
            "cluster_uid",
            "hcloud_server_id",
            "hcloud_location",
            "observed_at",
            "expires_at",
            "active_user_cells",
            "active_recovery_cells",
            "attached_volumes",
            "authentication",
        }
        if set(receipt) != expected:
            raise CapacityReceiptError("capacity receipt shape is invalid")
        authentication = receipt["authentication"]
        if not isinstance(authentication, dict) or set(authentication) != {
            "algorithm",
            "key_id",
            "signature",
        }:
            raise CapacityReceiptError("capacity receipt signature is invalid")
        unsigned = {key: value for key, value in receipt.items() if key != "authentication"}
        signature = authentication.get("signature")
        if (
            authentication.get("algorithm") != "ed25519"
            or authentication.get("key_id") != _public_key_id(self._key)
            or not isinstance(signature, str)
            or len(signature) != 128
        ):
            raise CapacityReceiptError("capacity receipt signature is invalid")
        try:
            self._key.verify(bytes.fromhex(signature), _DOMAIN + _canonical(unsigned))
        except (ValueError, InvalidSignature) as error:
            raise CapacityReceiptError("capacity receipt signature is invalid") from error
        receipt_id = unsigned.get("receipt_id")
        if (
            unsigned.get("schema_version") != 1
            or unsigned.get("issuer") != "exomem-live-kubernetes-hcloud-v1"
            or unsigned.get("contract_sha256") != canonical_contract_digest(self._contract)
            or not isinstance(receipt_id, str)
            or _RECEIPT_ID.fullmatch(receipt_id) is None
            or str(uuid.UUID(receipt_id)) != receipt_id
        ):
            raise CapacityReceiptError("capacity receipt identity is invalid")
        observed_at = _whole_second_timestamp(unsigned.get("observed_at"))
        expires_at = _whole_second_timestamp(unsigned.get("expires_at"))
        if (
            expires_at.timestamp() - observed_at.timestamp() != 300
            or observed_at > _as_utc(now)
            or _as_utc(now) > expires_at
        ):
            raise CapacityReceiptError("capacity receipt is stale or future")
        sequence = _positive_integer(unsigned.get("sequence"), "capacity receipt sequence")
        server_id = _positive_integer(
            unsigned.get("hcloud_server_id"), "capacity receipt HCloud server"
        )
        users = _non_negative_integer(
            unsigned.get("active_user_cells"), "capacity receipt user count"
        )
        recovery = _non_negative_integer(
            unsigned.get("active_recovery_cells"), "capacity receipt recovery count"
        )
        attached = _non_negative_integer(
            unsigned.get("attached_volumes"), "capacity receipt attachment count"
        )
        if (
            unsigned.get("cluster_uid") != observation.cluster_uid
            or server_id != self._server_id
            or server_id != observation.hcloud_server_id
            or unsigned.get("hcloud_location") != self._location
            or unsigned.get("hcloud_location") != observation.hcloud_location
            or users != len(observation.user_resource_names)
            or recovery != len(observation.recovery_resource_names)
            or attached != observation.attached_hcloud_volumes
        ):
            raise CapacityReceiptError("capacity receipt and local observation differ")
        return VerifiedCapacityReceipt(
            receipt_id=receipt_id,
            sequence=sequence,
            observed_at=observed_at,
            expires_at=expires_at,
            cluster_uid=observation.cluster_uid,
            hcloud_server_id=server_id,
            hcloud_location=self._location,
            active_user_cells=users,
            active_recovery_cells=recovery,
            attached_volumes=attached,
        )


def _attr(value: object, name: str, default: object = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _metadata(value: object) -> object:
    return _attr(value, "metadata", {})


class KubernetesCapacityObserver:
    """Build an exact local namespace/PV/PVC/attachment/node observation."""

    def __init__(
        self,
        *,
        core_v1: Any,
        storage_v1: Any,
        expected_server_id: int,
        expected_location: str,
        now: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._core = core_v1
        self._storage = storage_v1
        self._server_id = _positive_integer(expected_server_id, "expected HCloud server")
        self._location = expected_location
        self._now = now

    async def observe(self) -> CapacityObservation:
        try:
            namespaces, cluster, pvs, pvcs, attachments, nodes = await asyncio.gather(
                asyncio.to_thread(self._core.list_namespace),
                asyncio.to_thread(self._core.read_namespace, "kube-system"),
                asyncio.to_thread(self._core.list_persistent_volume),
                asyncio.to_thread(
                    self._core.list_persistent_volume_claim_for_all_namespaces
                ),
                asyncio.to_thread(self._storage.list_volume_attachment),
                asyncio.to_thread(self._core.list_node),
            )
        except Exception as error:  # noqa: BLE001 - exact provider-read boundary
            raise CapacityObservationError(
                "Kubernetes capacity observation is unavailable"
            ) from error
        cluster_uid = _attr(_metadata(cluster), "uid")
        if not isinstance(cluster_uid, str) or len(cluster_uid) < 8:
            raise CapacityReceiptError("Kubernetes cluster identity is invalid")
        expected_provider = f"hcloud://{self._server_id}"
        matched_nodes = [
            item
            for item in (_attr(nodes, "items", ()) or ())
            if _attr(_attr(item, "spec", {}), "provider_id") == expected_provider
        ]
        if len(matched_nodes) != 1:
            raise CapacityReceiptError("expected HCloud node identity is invalid")
        expected_node = _attr(_metadata(matched_nodes[0]), "name")
        if not isinstance(expected_node, str) or not expected_node:
            raise CapacityReceiptError("expected HCloud node identity is invalid")

        namespace_by_name: dict[str, OpaqueProviderMetadata] = {}
        user_names: set[str] = set()
        recovery_names: set[str] = set()
        cell_ids: set[str] = set()
        for item in (_attr(namespaces, "items", ()) or ()):
            metadata = _metadata(item)
            name = _attr(metadata, "name")
            raw_labels = _attr(metadata, "labels", {}) or {}
            raw_annotations = _attr(metadata, "annotations", {}) or {}
            if not isinstance(raw_labels, dict) or not isinstance(raw_annotations, dict):
                raise CapacityReceiptError("tenant namespace identity is invalid")
            labels = dict(raw_labels)
            annotations = dict(raw_annotations)
            candidate = (
                isinstance(name, str) and _CELL_RESOURCE_NAME.fullmatch(name) is not None
            ) or bool(_CELL_NAMESPACE_MARKERS & (labels.keys() | annotations.keys()))
            if not candidate:
                continue
            try:
                identity = OpaqueProviderMetadata.from_kubernetes_annotations(annotations)
            except (MetadataConflict, TypeError, ValueError) as error:
                raise CapacityReceiptError("tenant namespace identity is invalid") from error
            mode = annotations.get("exomem.io/provision-mode")
            if (
                labels.get("exomem.io/tenant-cell") != "true"
                or labels.get("exomem.io/cell-resource") != name
                or not isinstance(name, str)
                or name != identity.resource_name
                or annotations.get("exomem.io/resource-name") != name
                or name in namespace_by_name
                or identity.subject_id in cell_ids
                or mode not in {"serve", "restore-candidate"}
            ):
                raise CapacityReceiptError("tenant namespace identity is invalid")
            namespace_by_name[name] = identity
            cell_ids.add(identity.subject_id)
            (user_names if mode == "serve" else recovery_names).add(name)

        pv_by_name: dict[str, object] = {}
        for pv in (_attr(pvs, "items", ()) or ()):
            name = _attr(_metadata(pv), "name")
            if not isinstance(name, str) or not name or name in pv_by_name:
                raise CapacityReceiptError("persistent volume observation is ambiguous")
            pv_by_name[name] = pv
        pvc_by_identity: dict[tuple[str, str], object] = {}
        for pvc in (_attr(pvcs, "items", ()) or ()):
            metadata = _metadata(pvc)
            identity = (_attr(metadata, "namespace"), _attr(metadata, "name"))
            if (
                not all(isinstance(value, str) and value for value in identity)
                or identity in pvc_by_identity
            ):
                raise CapacityReceiptError("persistent volume claim observation is ambiguous")
            pvc_by_identity[identity] = pvc

        attached = 0
        orphan_ids: set[str] = set()
        seen_va_names: set[str] = set()
        seen_va_uids: set[str] = set()
        seen_pv_names: set[str] = set()
        seen_handles: set[str] = set()
        for attachment in (_attr(attachments, "items", ()) or ()):
            spec = _attr(attachment, "spec", {})
            status = _attr(attachment, "status", {})
            if _attr(spec, "attacher") != _HCLOUD_CSI_DRIVER or _attr(status, "attached") is not True:
                continue
            metadata = _metadata(attachment)
            va_name = _attr(metadata, "name")
            va_uid = _attr(metadata, "uid")
            pv_name = _attr(_attr(spec, "source", {}), "persistent_volume_name")
            if (
                not all(isinstance(value, str) and value for value in (va_name, va_uid, pv_name))
                or va_name in seen_va_names
                or va_uid in seen_va_uids
                or pv_name in seen_pv_names
                or _attr(spec, "node_name") != expected_node
            ):
                raise CapacityReceiptError("HCloud volume attachment observation is invalid")
            seen_va_names.add(va_name)
            seen_va_uids.add(va_uid)
            seen_pv_names.add(pv_name)
            attached += 1
            pv = pv_by_name.get(pv_name)
            if pv is None:
                orphan_ids.add(f"va:{va_uid}:{va_name}:pv:{pv_name}")
                continue
            pv_spec = _attr(pv, "spec", {})
            csi = _attr(pv_spec, "csi", {})
            handle = _attr(csi, "volume_handle")
            claim = _attr(pv_spec, "claim_ref", {})
            claim_namespace = _attr(claim, "namespace")
            claim_name = _attr(claim, "name")
            claim_uid = _attr(claim, "uid")
            if (
                _attr(csi, "driver") != _HCLOUD_CSI_DRIVER
                or not isinstance(handle, str)
                or not handle.isdigit()
                or int(handle) < 1
                or handle in seen_handles
                or not all(
                    isinstance(value, str) and value
                    for value in (claim_namespace, claim_name, claim_uid)
                )
            ):
                raise CapacityReceiptError("HCloud persistent volume observation is invalid")
            seen_handles.add(handle)
            pvc = pvc_by_identity.get((claim_namespace, claim_name))
            namespace_identity = namespace_by_name.get(claim_namespace)
            if pvc is not None:
                pvc_metadata = _metadata(pvc)
                if (
                    _attr(pvc_metadata, "uid") != claim_uid
                    or _attr(_attr(pvc, "spec", {}), "volume_name") != pv_name
                ):
                    raise CapacityReceiptError("HCloud PVC ownership observation is invalid")
            if pvc is None or namespace_identity is None:
                orphan_ids.add(f"volume:{handle}")
                continue
            if claim_name != namespace_identity.resource_name + "-data":
                raise CapacityReceiptError("HCloud PVC ownership observation is invalid")
        return CapacityObservation(
            observed_at=_as_utc(self._now()),
            cluster_uid=cluster_uid,
            hcloud_server_id=self._server_id,
            hcloud_location=self._location,
            user_resource_names=frozenset(user_names),
            recovery_resource_names=frozenset(recovery_names),
            orphan_attachment_ids=frozenset(orphan_ids),
            attached_hcloud_volumes=attached,
        )


def _class_from_request(request: dict[str, Any]) -> CapacityReservationClass:
    mode = request.get("provisionMode")
    if mode == "serve":
        return CapacityReservationClass.USER
    if mode == "restore-candidate":
        return CapacityReservationClass.RECOVERY
    raise CapacityConflict("provision mode is invalid")


def _snapshot(row: CapacityReservation) -> CapacityReservationSnapshot:
    return CapacityReservationSnapshot(
        id=row.id,
        tenant_id=row.tenant_id,
        cell_id=row.cell_id,
        resource_name=row.resource_name,
        reservation_class=row.reservation_class,
        reserving_operation_id=row.reserving_operation_id,
        provider_operation_id=row.reserving_provider_operation_id,
        fence_generation=row.reserving_fence_generation,
        released_at=row.released_at,
    )


class CapacityReservationAuthority:
    """Serialize one live-evidence admission decision on ledger row 1."""

    USER_LIMIT = 6
    RECOVERY_LIMIT = 2
    POTENTIAL_LIMIT = 8

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._sqlite_lock = asyncio.Lock()

    async def reserve(
        self,
        operation: OperationSnapshot,
        request: dict[str, Any],
        *,
        receipt: VerifiedCapacityReceipt,
        observation: CapacityObservation,
        worker_id: str,
        claim_token: str,
        claim_generation: int,
        now: datetime | None = None,
    ) -> CapacityReservationSnapshot:
        try:
            if self._sessions.kw.get("bind").dialect.name == "sqlite":
                async with self._sqlite_lock:
                    return await self._reserve(
                        operation,
                        request,
                        receipt=receipt,
                        observation=observation,
                        worker_id=worker_id,
                        claim_token=claim_token,
                        claim_generation=claim_generation,
                        now=now,
                    )
            return await self._reserve(
                operation,
                request,
                receipt=receipt,
                observation=observation,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
        except StaleFence:
            async with self._sessions() as session:
                consumed = await session.scalar(
                    select(CapacityReservation).where(
                        CapacityReservation.reserving_operation_id == operation.id,
                        CapacityReservation.released_at.is_not(None),
                    )
                )
            if consumed is not None:
                raise CapacityIdentityConflict(
                    "reserving operation was permanently released"
                ) from None
            raise

    async def _reserve(
        self,
        operation: OperationSnapshot,
        request: dict[str, Any],
        *,
        receipt: VerifiedCapacityReceipt,
        observation: CapacityObservation,
        worker_id: str,
        claim_token: str,
        claim_generation: int,
        now: datetime | None,
    ) -> CapacityReservationSnapshot:
        reservation_class = _class_from_request(request)
        if (
            operation.action is not OperationAction.PROVISION
            or request.get("tenantId") != operation.tenant_id
            or request.get("cellId") != operation.cell_id
            or request.get("operationId") != operation.external_operation_id
            or request.get("fenceGeneration") != operation.fence_generation
            or operation.cell_id is None
        ):
            raise CapacityConflict("capacity request and claimed operation differ")
        resource_name = OpaqueProviderMetadata(
            operation.tenant_id,
            operation.cell_id,
            operation.external_operation_id,
            operation.fence_generation,
        ).resource_name
        try:
            async with self._sessions.begin() as session:
                active, _ = await _lock_active_claim(
                    session,
                    operation.id,
                    worker_id=worker_id,
                    claim_token=claim_token,
                    claim_generation=claim_generation,
                    now=now,
                )
                ledger = await session.get(CapacityLedger, 1, with_for_update=True)
                if ledger is None:
                    raise CapacityConflict("capacity ledger singleton is missing")
                checked_at = await _database_now(session, now)
                if checked_at < receipt.observed_at or checked_at > receipt.expires_at:
                    raise CapacityBlocked("capacity-live-receipt-unavailable")
                observation_age = (checked_at - _as_utc(observation.observed_at)).total_seconds()
                if observation_age < 0 or observation_age > 30:
                    raise CapacityBlocked("capacity-live-observation-mismatch")
                if (
                    receipt.cluster_uid != observation.cluster_uid
                    or receipt.hcloud_server_id != observation.hcloud_server_id
                    or receipt.hcloud_location != observation.hcloud_location
                    or receipt.active_user_cells != len(observation.user_resource_names)
                    or receipt.active_recovery_cells != len(observation.recovery_resource_names)
                    or receipt.attached_volumes != observation.attached_hcloud_volumes
                ):
                    raise CapacityBlocked("capacity-live-observation-mismatch")
                users = set(observation.user_resource_names)
                recovery = set(observation.recovery_resource_names)
                if users & recovery:
                    raise CapacityBlocked("capacity-live-observation-mismatch")
                destructive = await session.scalar(
                    select(CapacityDestructiveFence)
                    .where(
                        CapacityDestructiveFence.tenant_id == active.tenant_id,
                        CapacityDestructiveFence.fence_generation
                        >= active.fence_generation,
                        or_(
                            CapacityDestructiveFence.release_reason
                            == CapacityReleaseReason.DESTROY,
                            and_(
                                CapacityDestructiveFence.release_reason
                                == CapacityReleaseReason.DISCARD,
                                CapacityDestructiveFence.cell_id == active.cell_id,
                            ),
                        ),
                    )
                    .order_by(CapacityDestructiveFence.fence_generation.desc())
                    .with_for_update()
                )
                if destructive is not None:
                    raise CapacityIdentityConflict(
                        "proof-valid destructive fence blocks capacity admission"
                    )
                existing = await session.scalar(
                    select(CapacityReservation)
                    .where(CapacityReservation.reserving_operation_id == active.id)
                    .with_for_update()
                )
                identity = (
                    active.tenant_id,
                    active.cell_id,
                    resource_name,
                    reservation_class,
                    active.external_operation_id,
                    active.fence_generation,
                )
                if existing is not None:
                    existing_identity = (
                        existing.tenant_id,
                        existing.cell_id,
                        existing.resource_name,
                        existing.reservation_class,
                        existing.reserving_provider_operation_id,
                        existing.reserving_fence_generation,
                    )
                    if existing_identity != identity or existing.released_at is not None:
                        raise CapacityIdentityConflict(
                            "reserving operation identity is immutable"
                        )
                reservations = list(
                    await session.scalars(
                        select(CapacityReservation)
                        .where(CapacityReservation.released_at.is_(None))
                        .order_by(CapacityReservation.id)
                        .with_for_update()
                    )
                )
                for reserved in reservations:
                    observed_user = reserved.resource_name in users
                    observed_recovery = reserved.resource_name in recovery
                    if (
                        observed_user
                        and reserved.reservation_class is not CapacityReservationClass.USER
                    ) or (
                        observed_recovery
                        and reserved.reservation_class is not CapacityReservationClass.RECOVERY
                    ):
                        raise CapacityBlocked("capacity-live-observation-mismatch")
                if existing is not None:
                    return _snapshot(existing)
                if any(
                    (
                        reserved.tenant_id == active.tenant_id
                        and reserved.cell_id == active.cell_id
                    )
                    or reserved.resource_name == resource_name
                    for reserved in reservations
                ):
                    raise CapacityIdentityConflict(
                        "active capacity reservation identity conflicts"
                    )
                potential = set(observation.potential_resource_names)
                for reserved in reservations:
                    potential.add(reserved.resource_name)
                    target = (
                        users
                        if reserved.reservation_class is CapacityReservationClass.USER
                        else recovery
                    )
                    target.add(reserved.resource_name)
                (users if reservation_class is CapacityReservationClass.USER else recovery).add(
                    resource_name
                )
                potential.add(resource_name)
                if len(users) > self.USER_LIMIT:
                    raise CapacityBlocked("capacity-user-exhausted")
                if len(recovery) > self.RECOVERY_LIMIT:
                    raise CapacityBlocked("capacity-recovery-exhausted")
                if len(potential) + len(observation.orphan_attachment_ids) > self.POTENTIAL_LIMIT:
                    raise CapacityBlocked("capacity-attachment-headroom-exhausted")
                reserved = CapacityReservation(
                    tenant_id=active.tenant_id,
                    cell_id=active.cell_id,
                    resource_name=resource_name,
                    reservation_class=reservation_class,
                    reserving_operation_id=active.id,
                    reserving_provider_operation_id=active.external_operation_id,
                    reserving_fence_generation=active.fence_generation,
                    reserved_at=checked_at,
                )
                session.add(reserved)
                ledger.revision += 1
                ledger.updated_at = checked_at
                await session.flush()
                return _snapshot(reserved)
        except CapacityBlocked:
            raise

    async def require_active(
        self,
        *,
        internal_operation_id: str,
        tenant_id: str,
        cell_id: str,
        provider_operation_id: str,
        fence_generation: int,
        reservation_class: CapacityReservationClass,
    ) -> CapacityReservationSnapshot:
        resource_name = OpaqueProviderMetadata(
            tenant_id, cell_id, provider_operation_id, fence_generation
        ).resource_name
        async with self._sessions() as session:
            row = await session.scalar(
                select(CapacityReservation).where(
                    CapacityReservation.reserving_operation_id == internal_operation_id,
                    CapacityReservation.tenant_id == tenant_id,
                    CapacityReservation.cell_id == cell_id,
                    CapacityReservation.resource_name == resource_name,
                    CapacityReservation.reservation_class == reservation_class,
                    CapacityReservation.reserving_provider_operation_id == provider_operation_id,
                    CapacityReservation.reserving_fence_generation == fence_generation,
                    CapacityReservation.released_at.is_(None),
                )
            )
            if row is None:
                raise CapacityConflict("exact active capacity reservation is absent")
            return _snapshot(row)


def load_capacity_contract(path: str | Path) -> dict[str, Any]:
    contract_path = Path(path)
    if contract_path.is_symlink() or not contract_path.is_file():
        raise CapacityReceiptError("capacity contract is unavailable")
    raw = contract_path.read_bytes()
    if not 1 <= len(raw) <= _MAX_RECEIPT_BYTES:
        raise CapacityReceiptError("capacity contract size is invalid")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CapacityReceiptError("capacity contract JSON is invalid") from error
    if not isinstance(value, dict):
        raise CapacityReceiptError("capacity contract shape is invalid")
    return value


class LiveCapacityAdmission:
    """Collect local evidence, verify the collector receipt, then reserve."""

    def __init__(
        self,
        *,
        core_v1: Any,
        storage_v1: Any,
        sessions: async_sessionmaker[AsyncSession],
        contract: dict[str, Any],
        public_key: str,
        receipt_namespace: str,
        receipt_config_map: str,
        expected_server_id: int,
        expected_location: str,
        now: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._core = core_v1
        self._receipt_namespace = receipt_namespace
        self._receipt_config_map = receipt_config_map
        self._now = now
        self._observer = KubernetesCapacityObserver(
            core_v1=core_v1,
            storage_v1=storage_v1,
            expected_server_id=expected_server_id,
            expected_location=expected_location,
            now=now,
        )
        self._verifier = CapacityReceiptVerifier(
            contract=contract,
            public_key=public_key,
            expected_server_id=expected_server_id,
            expected_location=expected_location,
        )
        self._reservations = CapacityReservationAuthority(sessions)

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
    ) -> str | None:
        if (
            provider_operation_id != operation.external_operation_id
            or provider_fence_generation != operation.fence_generation
        ):
            raise CapacityConflict("worker provider identity differs from claimed operation")
        try:
            observation = await self._observer.observe()
        except (CapacityObservationError, CapacityReceiptError):
            return "capacity-live-observation-mismatch"
        try:
            config_map = await asyncio.to_thread(
                self._core.read_namespaced_config_map,
                self._receipt_config_map,
                self._receipt_namespace,
            )
            data = dict(_attr(config_map, "data", {}) or {})
            raw_receipt = data.get("receipt.json")
            if not isinstance(raw_receipt, str) or not raw_receipt:
                return "capacity-live-receipt-unavailable"
        except Exception:  # noqa: BLE001 - provider errors become a content-free pending reason
            return "capacity-live-receipt-unavailable"
        try:
            receipt = self._verifier.verify(
                raw_receipt,
                observation=observation,
                now=_as_utc(now) if now is not None else _as_utc(self._now()),
            )
        except CapacityReceiptError as error:
            if "observation differ" in str(error):
                return "capacity-live-observation-mismatch"
            return "capacity-live-receipt-invalid"
        try:
            await self._reservations.reserve(
                operation,
                request,
                receipt=receipt,
                observation=observation,
                worker_id=worker_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
                now=now,
            )
        except CapacityBlocked as error:
            return error.reason
        return None

    async def require_active(self, **identity: Any) -> CapacityReservationSnapshot:
        return await self._reservations.require_active(**identity)
