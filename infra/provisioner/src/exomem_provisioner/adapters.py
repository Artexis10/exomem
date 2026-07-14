"""Live provider adapter seams for Kubernetes, Helm, HCloud, and Traefik."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .credentials import validate_machine_credential
from .lifecycle import (
    HealthObservation,
    LifecycleConfig,
    MetadataConflict,
    OpaqueProviderMetadata,
    RecordedVolume,
    _digest,
)
from .provider_identity import (
    ProviderIdentityConflict,
    ProviderRecoveryIdentityVerifier,
    ProviderReference,
    chunk_hcloud_identity_envelope,
    decode_hcloud_identity_envelope,
)


def _api_status(error: Exception) -> int | None:
    status = getattr(error, "status", None)
    return status if isinstance(status, int) else None


def _require_annotations(
    actual: dict[str, str] | None,
    metadata: OpaqueProviderMetadata,
) -> None:
    values = actual or {}
    if any(values.get(key) != value for key, value in metadata.kubernetes_annotations.items()):
        raise MetadataConflict("Kubernetes object identity annotations differ")


def _require_cell_identity(
    actual: dict[str, str] | None,
    metadata: OpaqueProviderMetadata,
) -> None:
    values = actual or {}
    expected = metadata.kubernetes_annotations
    for key in (
        "exomem.io/tenant-id",
        "exomem.io/cell-id",
        "exomem.io/tenant-digest",
        "exomem.io/subject-digest",
    ):
        if values.get(key) != expected[key]:
            raise MetadataConflict("Kubernetes cell identity annotations differ")


class KubernetesVolumeAdapter:
    """Narrow official-client adapter used only by the privileged volume worker."""

    def __init__(
        self,
        *,
        core_v1: Any,
        storage_class_name: str,
        encryption_secret_name: str,
        encryption_secret_namespace: str,
        identity_verifier: ProviderRecoveryIdentityVerifier | None = None,
    ) -> None:
        self._core = core_v1
        self._storage_class_name = storage_class_name
        self._encryption_secret_name = encryption_secret_name
        self._encryption_secret_namespace = encryption_secret_namespace
        self._identity_verifier = identity_verifier

    @classmethod
    def from_in_cluster(
        cls,
        *,
        storage_class_name: str,
        encryption_secret_name: str,
        encryption_secret_namespace: str,
    ) -> KubernetesVolumeAdapter:
        from kubernetes import client, config

        config.load_incluster_config()
        return cls(
            core_v1=client.CoreV1Api(),
            storage_class_name=storage_class_name,
            encryption_secret_name=encryption_secret_name,
            encryption_secret_namespace=encryption_secret_namespace,
        )

    async def discover_bound_volume(
        self, metadata: OpaqueProviderMetadata
    ) -> RecordedVolume | None:
        namespace = metadata.resource_name
        claim_name = metadata.resource_name + "-data"
        try:
            pvc = await asyncio.to_thread(
                self._core.read_namespaced_persistent_volume_claim,
                claim_name,
                namespace,
            )
        except Exception as error:
            if _api_status(error) == 404:
                return None
            raise
        pvc_annotations = dict(getattr(pvc.metadata, "annotations", None) or {})
        _require_annotations(pvc_annotations, metadata)
        pvc_envelope = str(pvc_annotations.get("exomem.io/recovery-envelope", ""))
        if self._identity_verifier is not None:
            try:
                self._identity_verifier.authenticate(
                    pvc_envelope,
                    provider="kubernetes",
                    provider_reference=ProviderReference.kubernetes(
                        provider="kubernetes",
                        api_version="v1",
                        kind="PersistentVolumeClaim",
                        namespace=namespace,
                        name=claim_name,
                    ),
                    tenant_id=metadata.tenant_id,
                    cell_id=metadata.subject_id,
                    operation_id=metadata.operation_id,
                    fence_generation=metadata.fence_generation,
                )
            except ProviderIdentityConflict as error:
                raise MetadataConflict(
                    "PVC provider recovery identity did not authenticate"
                ) from error
        pv_name = getattr(pvc.spec, "volume_name", None)
        if not isinstance(pv_name, str) or not pv_name:
            return None
        pv = await asyncio.to_thread(self._core.read_persistent_volume, pv_name)
        csi = getattr(pv.spec, "csi", None)
        handle = getattr(csi, "volume_handle", None)
        if not isinstance(handle, str) or not handle:
            raise MetadataConflict("bound PV has no CSI volumeHandle")
        location = self._location(pv)
        annotations = dict(getattr(pv.metadata, "annotations", None) or {})
        identity_keys = set(metadata.kubernetes_annotations)
        if identity_keys.intersection(annotations):
            _require_annotations(annotations, metadata)
        else:
            await asyncio.to_thread(
                self._core.patch_persistent_volume,
                pv_name,
                {"metadata": {"annotations": metadata.kubernetes_annotations}},
            )
        pv_envelope = str(annotations.get("exomem.io/recovery-envelope", ""))
        if self._identity_verifier is not None and pv_envelope:
            try:
                self._identity_verifier.authenticate(
                    pv_envelope,
                    provider="kubernetes",
                    provider_reference=ProviderReference.kubernetes(
                        provider="kubernetes",
                        api_version="v1",
                        kind="PersistentVolume",
                        namespace="",
                        name=pv_name,
                    ),
                    tenant_id=metadata.tenant_id,
                    cell_id=metadata.subject_id,
                    operation_id=metadata.operation_id,
                    fence_generation=metadata.fence_generation,
                )
            except ProviderIdentityConflict as error:
                raise MetadataConflict(
                    "PV provider recovery identity did not authenticate"
                ) from error
        return RecordedVolume(
            handle,
            pv_name,
            location,
            metadata,
            pv_recovery_envelope=pv_envelope,
            pvc_recovery_envelope=pvc_envelope,
        )

    async def label_bound_volume(self, recorded: RecordedVolume, recovery_envelope: str) -> None:
        annotations = {
            **recorded.metadata.kubernetes_annotations,
            "exomem.io/recovery-envelope": recovery_envelope,
        }
        await asyncio.to_thread(
            self._core.patch_persistent_volume,
            recorded.pv_name,
            {"metadata": {"annotations": annotations}},
        )

    @staticmethod
    def _location(pv: Any) -> str:
        affinity = getattr(getattr(pv.spec, "node_affinity", None), "required", None)
        terms = getattr(affinity, "node_selector_terms", ()) or ()
        locations: set[str] = set()
        for term in terms:
            for expression in getattr(term, "match_expressions", ()) or ():
                if getattr(expression, "key", "") in {
                    "topology.kubernetes.io/zone",
                    "csi.hetzner.cloud/location",
                }:
                    locations.update(getattr(expression, "values", ()) or ())
        if len(locations) != 1:
            raise MetadataConflict("bound PV has no unique Hetzner location")
        return locations.pop()

    async def create_static_binding(self, recorded: RecordedVolume) -> None:
        metadata = recorded.metadata
        namespace = metadata.resource_name
        pv = {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {
                "name": recorded.pv_name,
                "annotations": {
                    **metadata.kubernetes_annotations,
                    "exomem.io/recovery-envelope": recorded.pv_recovery_envelope,
                },
                "labels": {"exomem.io/resource-name": metadata.resource_name},
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "capacity": {"storage": "10Gi"},
                "csi": {
                    "driver": "csi.hetzner.cloud",
                    "fsType": "ext4",
                    "nodePublishSecretRef": {
                        "name": self._encryption_secret_name,
                        "namespace": self._encryption_secret_namespace,
                    },
                    "volumeHandle": recorded.volume_handle,
                },
                "claimRef": {
                    "name": metadata.resource_name + "-data",
                    "namespace": namespace,
                },
                "nodeAffinity": {
                    "required": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "topology.kubernetes.io/zone",
                                        "operator": "In",
                                        "values": [recorded.location],
                                    }
                                ]
                            }
                        ]
                    }
                },
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": self._storage_class_name,
                "volumeMode": "Filesystem",
            },
        }
        pvc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": metadata.resource_name + "-data",
                "namespace": namespace,
                "annotations": {
                    **metadata.kubernetes_annotations,
                    "exomem.io/recovery-envelope": recorded.pvc_recovery_envelope,
                },
                "labels": {"exomem.io/resource-name": metadata.resource_name},
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "10Gi"}},
                "storageClassName": self._storage_class_name,
                "volumeMode": "Filesystem",
                "volumeName": recorded.pv_name,
            },
        }
        try:
            await asyncio.to_thread(self._core.create_persistent_volume, pv)
        except Exception as error:
            if _api_status(error) != 409:
                raise
        try:
            await asyncio.to_thread(
                self._core.create_namespaced_persistent_volume_claim,
                namespace,
                pvc,
            )
        except Exception as error:
            if _api_status(error) != 409:
                raise

    async def delete_claim(self, recorded: RecordedVolume) -> None:
        try:
            await asyncio.to_thread(
                self._core.delete_namespaced_persistent_volume_claim,
                recorded.metadata.resource_name + "-data",
                recorded.metadata.resource_name,
                {"propagationPolicy": "Foreground"},
            )
        except Exception as error:
            if _api_status(error) != 404:
                raise

    async def claim_absent(self, recorded: RecordedVolume) -> bool:
        try:
            await asyncio.to_thread(
                self._core.read_namespaced_persistent_volume_claim,
                recorded.metadata.resource_name + "-data",
                recorded.metadata.resource_name,
            )
        except Exception as error:
            if _api_status(error) == 404:
                return True
            raise
        return False

    async def delete_pv(self, pv_name: str) -> None:
        try:
            await asyncio.to_thread(
                self._core.delete_persistent_volume,
                pv_name,
                {"propagationPolicy": "Foreground"},
            )
        except Exception as error:
            if _api_status(error) != 404:
                raise

    async def pv_absent(self, pv_name: str) -> bool:
        try:
            await asyncio.to_thread(self._core.read_persistent_volume, pv_name)
        except Exception as error:
            if _api_status(error) == 404:
                return True
            raise
        return False


class KubernetesCellAdapter:
    """Official-client mutations used by routine lifecycle reconciliation."""

    _CREDENTIAL_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    _CREDENTIAL = re.compile(r"^[A-Za-z0-9_-]{43}$")

    def __init__(
        self,
        *,
        core_v1: Any,
        apps_v1: Any,
        identity_verifier: ProviderRecoveryIdentityVerifier | None = None,
    ) -> None:
        self._core = core_v1
        self._apps = apps_v1
        self._identity_verifier = identity_verifier

    async def volume_claim_bound(self, metadata: OpaqueProviderMetadata) -> bool:
        namespace = metadata.resource_name
        name = metadata.resource_name + "-data"
        try:
            pvc = await asyncio.to_thread(
                self._core.read_namespaced_persistent_volume_claim,
                name,
                namespace,
            )
        except Exception as error:
            if _api_status(error) == 404:
                return False
            raise
        annotations = dict(getattr(pvc.metadata, "annotations", None) or {})
        _require_annotations(annotations, metadata)
        if self._identity_verifier is not None:
            try:
                self._identity_verifier.authenticate(
                    str(annotations.get("exomem.io/recovery-envelope", "")),
                    provider="kubernetes",
                    provider_reference=ProviderReference.kubernetes(
                        provider="kubernetes",
                        api_version="v1",
                        kind="PersistentVolumeClaim",
                        namespace=namespace,
                        name=name,
                    ),
                    tenant_id=metadata.tenant_id,
                    cell_id=metadata.subject_id,
                    operation_id=metadata.operation_id,
                    fence_generation=metadata.fence_generation,
                )
            except ProviderIdentityConflict as error:
                raise MetadataConflict(
                    "PVC provider recovery identity did not authenticate"
                ) from error
        volume_name = getattr(pvc.spec, "volume_name", None)
        return isinstance(volume_name, str) and bool(volume_name)

    def __repr__(self) -> str:
        return "KubernetesCellAdapter()"

    @classmethod
    def from_in_cluster(cls) -> KubernetesCellAdapter:
        from kubernetes import client, config

        config.load_incluster_config()
        return cls(core_v1=client.CoreV1Api(), apps_v1=client.AppsV1Api())

    async def write_credential_bundle(
        self,
        metadata: OpaqueProviderMetadata,
        credentials: dict[str, str],
        *,
        lifecycle_annotations: dict[str, str] | None = None,
    ) -> None:
        try:
            valid_credentials = all(
                self._CREDENTIAL.fullmatch(value) and validate_machine_credential(value)
                for value in credentials.values()
            )
        except ValueError:
            valid_credentials = False
        if (
            not 1 <= len(credentials) <= 2
            or any(not self._CREDENTIAL_VERSION.fullmatch(version) for version in credentials)
            or not valid_credentials
        ):
            raise MetadataConflict("cell credential bundle shape is invalid")
        bundle = json.dumps(
            {"schema_version": 1, "credentials": credentials},
            sort_keys=True,
            separators=(",", ":"),
        )
        annotations = dict(metadata.kubernetes_annotations)
        annotations.update(lifecycle_annotations or {})
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "exomem-cell-credentials",
                "namespace": metadata.resource_name,
                "annotations": annotations,
                "labels": {"exomem.io/resource-name": metadata.resource_name},
            },
            "type": "Opaque",
            "immutable": False,
            "stringData": {"credentials.json": bundle},
        }
        try:
            await asyncio.to_thread(
                self._core.patch_namespaced_secret,
                "exomem-cell-credentials",
                metadata.resource_name,
                body,
            )
        except Exception as error:
            if _api_status(error) != 404:
                raise
            await asyncio.to_thread(
                self._core.create_namespaced_secret,
                metadata.resource_name,
                body,
            )

    async def read_credential_bundle(
        self, metadata: OpaqueProviderMetadata
    ) -> tuple[dict[str, str], dict[str, str]]:
        secret = await asyncio.to_thread(
            self._core.read_namespaced_secret,
            "exomem-cell-credentials",
            metadata.resource_name,
        )
        _require_annotations(getattr(secret.metadata, "annotations", None), metadata)
        encoded = dict(getattr(secret, "data", None) or {}).get("credentials.json")
        if not isinstance(encoded, str):
            raise MetadataConflict("cell credential bundle is absent")
        try:
            decoded = base64.b64decode(encoded, validate=True)
            parsed = json.loads(decoded.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MetadataConflict("cell credential bundle is invalid") from error
        credentials = parsed.get("credentials") if isinstance(parsed, dict) else None
        if (
            not isinstance(parsed, dict)
            or parsed.get("schema_version") != 1
            or not isinstance(credentials, dict)
        ):
            raise MetadataConflict("cell credential bundle is invalid")
        values = {str(key): str(value) for key, value in credentials.items()}
        try:
            if not all(validate_machine_credential(value) for value in values.values()):
                raise ValueError
        except ValueError as error:
            raise MetadataConflict("cell credential bundle is invalid") from error
        return values, dict(getattr(secret.metadata, "annotations", None) or {})

    async def scale(self, metadata: OpaqueProviderMetadata, replicas: int) -> None:
        if replicas not in {0, 1}:
            raise MetadataConflict("hosted cell replicas must be zero or one")
        await asyncio.to_thread(
            self._apps.patch_namespaced_stateful_set_scale,
            metadata.resource_name,
            metadata.resource_name,
            {"spec": {"replicas": replicas}},
        )


class KubernetesMaintenanceLeaseAdapter:
    """Per-cell durable maintenance serialization using coordination.k8s.io Lease."""

    def __init__(
        self,
        *,
        coordination_v1: Any,
        duration_seconds: int = 120,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._coordination = coordination_v1
        self._duration = duration_seconds
        self._now = now

    @staticmethod
    def _name(metadata: OpaqueProviderMetadata) -> str:
        return metadata.resource_name + "-maintenance"

    async def acquire(self, metadata: OpaqueProviderMetadata, operation_id: str) -> bool:
        name = self._name(metadata)
        namespace = metadata.resource_name
        now = self._now()
        try:
            lease = await asyncio.to_thread(
                self._coordination.read_namespaced_lease, name, namespace
            )
        except Exception as error:
            if _api_status(error) != 404:
                raise
            body = {
                "apiVersion": "coordination.k8s.io/v1",
                "kind": "Lease",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "annotations": metadata.kubernetes_annotations,
                },
                "spec": {
                    "holderIdentity": operation_id,
                    "leaseDurationSeconds": self._duration,
                    "acquireTime": now,
                    "renewTime": now,
                },
            }
            try:
                await asyncio.to_thread(self._coordination.create_namespaced_lease, namespace, body)
                return True
            except Exception as conflict:
                if _api_status(conflict) == 409:
                    return False
                raise
        _require_cell_identity(getattr(lease.metadata, "annotations", None), metadata)
        holder = getattr(lease.spec, "holder_identity", None)
        renew = getattr(lease.spec, "renew_time", None)
        duration = getattr(lease.spec, "lease_duration_seconds", None)
        expired = (
            isinstance(renew, datetime)
            and isinstance(duration, int)
            and renew + timedelta(seconds=duration) <= now
        )
        if holder != operation_id and not expired:
            return False
        body = {
            "metadata": {
                "name": name,
                "namespace": namespace,
                "resourceVersion": lease.metadata.resource_version,
                "annotations": metadata.kubernetes_annotations,
            },
            "spec": {
                "holderIdentity": operation_id,
                "leaseDurationSeconds": self._duration,
                "renewTime": now,
            },
        }
        await asyncio.to_thread(
            self._coordination.replace_namespaced_lease,
            name,
            namespace,
            body,
        )
        return True

    async def release(self, metadata: OpaqueProviderMetadata, operation_id: str) -> None:
        name = self._name(metadata)
        namespace = metadata.resource_name
        try:
            lease = await asyncio.to_thread(
                self._coordination.read_namespaced_lease, name, namespace
            )
        except Exception as error:
            if _api_status(error) == 404:
                return
            raise
        _require_cell_identity(getattr(lease.metadata, "annotations", None), metadata)
        if getattr(lease.spec, "holder_identity", None) != operation_id:
            return
        await asyncio.to_thread(
            self._coordination.delete_namespaced_lease,
            name,
            namespace,
            {
                "preconditions": {
                    "uid": lease.metadata.uid,
                    "resourceVersion": lease.metadata.resource_version,
                }
            },
        )


CellRequester = Callable[..., Awaitable[Any]]


class PrivateCellApiAdapter:
    """Authenticated, content-free private runtime lifecycle client."""

    _PRINCIPAL_SCOPE = (
        base64.urlsafe_b64encode(
            hashlib.sha256(b"exomem-provisioner-private-cell-control").digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )

    def __init__(self, *, request: CellRequester, internal_origin: str) -> None:
        if not internal_origin.startswith("http://") and not internal_origin.startswith("https://"):
            raise ValueError("internal cell origin must use HTTP or HTTPS")
        self._request = request
        self._origin = internal_origin.rstrip("/")

    def __repr__(self) -> str:
        return f"PrivateCellApiAdapter(origin={self._origin!r})"

    def _headers(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        credential: str,
        protocol_version: str,
        operation_id: str | None = None,
        routing_stopped: bool = False,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {credential}",
            "X-Exomem-Cell-Id": metadata.subject_id,
            "X-Exomem-Protocol-Version": protocol_version,
            "X-Exomem-Request-Id": str(uuid.uuid4()),
            "X-Exomem-Principal-Scope": self._PRINCIPAL_SCOPE,
        }
        if operation_id is not None:
            headers["Idempotency-Key"] = operation_id
        if routing_stopped:
            headers["X-Exomem-Routing-Stopped"] = "true"
        return headers

    def _url(self, metadata: OpaqueProviderMetadata, path: str) -> str:
        origin = self._origin.format(
            resource=metadata.resource_name,
            namespace=metadata.resource_name,
            cell=metadata.subject_id,
        )
        return origin + "/private/exomem/v1/" + path.lstrip("/")

    async def _call(
        self,
        method: str,
        metadata: OpaqueProviderMetadata,
        path: str,
        *,
        credential: str,
        protocol_version: str,
        operation_id: str | None = None,
        routing_stopped: bool = False,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            method,
            self._url(metadata, path),
            headers=self._headers(
                metadata,
                credential=credential,
                protocol_version=protocol_version,
                operation_id=operation_id,
                routing_stopped=routing_stopped,
            ),
            json=body,
        )
        if response.status_code != 200:
            raise MetadataConflict("private cell lifecycle request failed")
        envelope = response.json()
        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            raise MetadataConflict("private cell lifecycle response is invalid")
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise MetadataConflict("private cell lifecycle response data is invalid")
        return data

    async def health(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        credential: str,
        protocol_version: str,
        config: LifecycleConfig,
        expected_release: str,
        expected_worker_policy: dict[str, Any],
    ) -> HealthObservation:
        live = await self._call(
            "GET",
            metadata,
            "live",
            credential=credential,
            protocol_version=protocol_version,
        )
        ready = await self._call(
            "GET",
            metadata,
            "ready",
            credential=credential,
            protocol_version=protocol_version,
        )
        contract_response = await self._request(
            "GET",
            self._url(metadata, "contract"),
            headers=self._headers(
                metadata,
                credential=credential,
                protocol_version=protocol_version,
            ),
            json=None,
        )
        if contract_response.status_code != 200:
            raise MetadataConflict("private cell contract request failed")
        contract = contract_response.json()
        if not isinstance(contract, dict):
            raise MetadataConflict("private cell contract response is invalid")
        digest = contract.get("digest")
        contract_digest = (
            digest.get("value")
            if isinstance(digest, dict) and digest.get("algorithm") == "sha256"
            else None
        )
        expected_policy_digest = hashlib.sha256(
            json.dumps(
                expected_worker_policy,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        try:
            credential_version = ready["authenticated_credential_version"]
            security_revision = ready["security_revision"]
            service_authenticated = (
                ready["service_authenticated"] is True
                and isinstance(credential_version, str)
                and bool(credential_version)
                and isinstance(security_revision, int)
                and not isinstance(security_revision, bool)
                and security_revision >= 1
            )
            policy_admitted = ready["worker_policy_digest"] == expected_policy_digest
            admission_admitted = ready["admission_phase"] == "active"
            observation = HealthObservation(
                live=(
                    live.get("live") is True
                    and live.get("cell_id") == metadata.subject_id
                    and live.get("protocol_version") == protocol_version
                ),
                ready=(
                    admission_admitted
                    and ready["vault_id"] == metadata.subject_id
                    and ready["exomem_release"] == expected_release
                    and ready["hosted_protocol"] == protocol_version
                    and service_authenticated
                    and ready["mutation_authority"] is True
                    and ready["read_admission"] is True
                    and ready["write_admission"] is True
                    and policy_admitted
                ),
                cell_id=str(ready["cell_id"]),
                protocol_version=str(ready["hosted_protocol"]),
                release_version=str(ready["exomem_release"]),
                service_authenticated=service_authenticated,
                mutation_authority=ready["mutation_authority"] is True,
                read_admission=ready["read_admission"] is True,
                write_admission=ready["write_admission"] is True,
                worker_policy=dict(expected_worker_policy),
                code="CELL_READY" if admission_admitted else str(ready["admission_phase"]),
                contract_digest=str(contract_digest),
                policy_admitted=policy_admitted,
                admission_admitted=admission_admitted,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MetadataConflict("private cell health response is incomplete") from error
        if observation.contract_digest != config.contract_digest:
            raise MetadataConflict("private cell contract digest differs")
        return observation

    async def quiesce(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        credential: str,
        protocol_version: str,
        operation_id: str,
    ) -> None:
        await self._call(
            "POST",
            metadata,
            "lifecycle/quiesce",
            credential=credential,
            protocol_version=protocol_version,
            operation_id=operation_id,
            routing_stopped=True,
            body={"timeout_seconds": 30},
        )

    async def operator(
        self,
        command: str,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        *,
        credential: str,
        protocol_version: str,
    ) -> dict[str, Any]:
        if command not in {"credential", "probe"}:
            raise MetadataConflict("unsupported private cell operator command")
        operation_id = request.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise MetadataConflict("private cell operator operation identity is absent")
        return await self._call(
            "POST",
            metadata,
            "operator/" + command,
            credential=credential,
            protocol_version=protocol_version,
            operation_id=operation_id,
            body=request,
        )

    async def resume(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        credential: str,
        protocol_version: str,
        operation_id: str,
    ) -> None:
        await self._call(
            "POST",
            metadata,
            "lifecycle/resume",
            credential=credential,
            protocol_version=protocol_version,
            operation_id=operation_id,
            body={},
        )

    async def seal(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        credential: str,
        protocol_version: str,
        operation_id: str,
        created_at: str,
    ) -> None:
        await self._call(
            "POST",
            metadata,
            "lifecycle/seal",
            credential=credential,
            protocol_version=protocol_version,
            operation_id=operation_id,
            routing_stopped=True,
            body={
                "operation_id": operation_id,
                "created_at": created_at,
                "reason_code": "tenant-deletion",
            },
        )

    async def credential_rejected(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        credential: str,
        protocol_version: str,
    ) -> bool:
        response = await self._request(
            "GET",
            self._url(metadata, "ready"),
            headers=self._headers(
                metadata,
                credential=credential,
                protocol_version=protocol_version,
            ),
            json=None,
        )
        return response.status_code in {401, 403}


class HCloudVolumeAdapter:
    """Official HCloud client boundary with immutable Exomem identity labels."""

    _IDENTITY_KEYS = {
        "exomem_tenant",
        "exomem_subject",
        "exomem_operation",
        "exomem_fence",
    }
    _IDENTITY_PREFIXES = (
        "exomem_tenant_id_",
        "exomem_cell_id_",
        "exomem_operation_id_",
        "exomem_identity_",
    )

    def __init__(
        self,
        *,
        client: Any,
        identity_verifier: ProviderRecoveryIdentityVerifier | None = None,
    ) -> None:
        self._client = client
        self._identity_verifier = identity_verifier

    def __repr__(self) -> str:
        return "HCloudVolumeAdapter()"

    @classmethod
    def from_token(cls, token: str) -> HCloudVolumeAdapter:
        from hcloud import Client

        return cls(client=Client(token=token))

    async def _get(self, handle: str) -> Any | None:
        try:
            volume_id = int(handle)
        except ValueError as error:
            raise MetadataConflict("HCloud CSI volumeHandle must be numeric") from error
        return await asyncio.to_thread(self._client.volumes.get_by_id, volume_id)

    async def label_volume(
        self,
        handle: str,
        metadata: OpaqueProviderMetadata,
        recovery_envelope: str | None = None,
    ) -> None:
        volume = await self._get(handle)
        if volume is None:
            raise MetadataConflict("HCloud volume is absent")
        labels = dict(getattr(volume, "labels", {}) or {})
        identity_present = bool(self._IDENTITY_KEYS.intersection(labels)) or any(
            key.startswith(self._IDENTITY_PREFIXES) for key in labels
        )
        if identity_present:
            OpaqueProviderMetadata.from_hcloud_labels(labels).require_same(metadata)
        reference = ProviderReference.hcloud(kind="volume", resource_id=handle)
        existing_recovery_identity = any(key.startswith("exomem_identity_") for key in labels)
        if existing_recovery_identity:
            try:
                existing_envelope = decode_hcloud_identity_envelope(labels)
                if self._identity_verifier is None:
                    raise ProviderIdentityConflict(
                        "provider recovery verifier is required for existing identity"
                    )
                self._identity_verifier.authenticate(
                    existing_envelope,
                    provider="hcloud",
                    provider_reference=reference,
                    tenant_id=metadata.tenant_id,
                    cell_id=metadata.subject_id,
                    operation_id=metadata.operation_id,
                    fence_generation=metadata.fence_generation,
                )
                if recovery_envelope is not None and recovery_envelope != existing_envelope:
                    raise ProviderIdentityConflict("provider recovery identity is immutable")
            except ProviderIdentityConflict as error:
                raise MetadataConflict(
                    "HCloud provider recovery identity did not authenticate"
                ) from error
        if recovery_envelope is not None and self._identity_verifier is not None:
            try:
                self._identity_verifier.authenticate(
                    recovery_envelope,
                    provider="hcloud",
                    provider_reference=reference,
                    tenant_id=metadata.tenant_id,
                    cell_id=metadata.subject_id,
                    operation_id=metadata.operation_id,
                    fence_generation=metadata.fence_generation,
                )
            except ProviderIdentityConflict as error:
                raise MetadataConflict(
                    "HCloud provider recovery identity did not authenticate"
                ) from error
        labels.update(metadata.hcloud_labels)
        if recovery_envelope is not None:
            labels.update(chunk_hcloud_identity_envelope(recovery_envelope))
        await asyncio.to_thread(self._client.volumes.update, volume, labels=labels)

    async def verify_volume(
        self, handle: str, metadata: OpaqueProviderMetadata, location: str
    ) -> bool:
        volume = await self._get(handle)
        if volume is None:
            return False
        labels = dict(getattr(volume, "labels", {}) or {})
        actual_location = getattr(getattr(volume, "location", None), "name", None)
        if actual_location != location:
            return False
        OpaqueProviderMetadata.from_hcloud_labels(labels).require_same(metadata)
        if self._identity_verifier is not None:
            try:
                self._identity_verifier.authenticate(
                    decode_hcloud_identity_envelope(labels),
                    provider="hcloud",
                    provider_reference=ProviderReference.hcloud(kind="volume", resource_id=handle),
                    tenant_id=metadata.tenant_id,
                    cell_id=metadata.subject_id,
                    operation_id=metadata.operation_id,
                    fence_generation=metadata.fence_generation,
                )
            except ProviderIdentityConflict as error:
                raise MetadataConflict(
                    "HCloud provider recovery identity did not authenticate"
                ) from error
        return True

    async def delete_volume(self, handle: str) -> None:
        volume = await self._get(handle)
        if volume is not None:
            await asyncio.to_thread(self._client.volumes.delete, volume)

    async def volume_absent(self, handle: str) -> bool:
        return await self._get(handle) is None

    async def discover_tenant_volumes(self, tenant_id: str) -> tuple[str, ...]:
        selector = f"exomem_tenant={_digest(tenant_id)}"
        volumes = await asyncio.to_thread(
            self._client.volumes.get_all,
            label_selector=selector,
        )
        return tuple(sorted(str(volume.id) for volume in volumes))

    async def observed_fence(self, tenant_id: str) -> int:
        selector = f"exomem_tenant={_digest(tenant_id)}"
        volumes = await asyncio.to_thread(
            self._client.volumes.get_all,
            label_selector=selector,
        )
        observed = 0
        for volume in volumes:
            labels = dict(getattr(volume, "labels", {}) or {})
            metadata = OpaqueProviderMetadata.from_hcloud_labels(labels)
            if metadata.tenant_id != tenant_id:
                raise MetadataConflict("HCloud tenant selector returned another identity")
            if self._identity_verifier is not None:
                try:
                    self._identity_verifier.authenticate(
                        decode_hcloud_identity_envelope(labels),
                        provider="hcloud",
                        provider_reference=ProviderReference.hcloud(
                            kind="volume", resource_id=volume.id
                        ),
                        tenant_id=metadata.tenant_id,
                        cell_id=metadata.subject_id,
                        operation_id=metadata.operation_id,
                        fence_generation=metadata.fence_generation,
                    )
                except ProviderIdentityConflict as error:
                    raise MetadataConflict(
                        "HCloud provider recovery identity did not authenticate"
                    ) from error
            observed = max(observed, metadata.fence_generation)
        return observed

    async def quarantine_volume(self, handle: str) -> None:
        volume = await self._get(handle)
        if volume is None:
            raise MetadataConflict("HCloud volume is absent")
        labels = dict(getattr(volume, "labels", {}) or {})
        labels["exomem_quarantine"] = "true"
        await asyncio.to_thread(self._client.volumes.update, volume, labels=labels)


Runner = Callable[[tuple[str, ...], dict[str, str]], Awaitable[Any]]


async def _subprocess_runner(argv: tuple[str, ...], environment: dict[str, str]) -> Any:
    process_environment = dict(os.environ)
    process_environment.update(environment)
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=process_environment,
    )
    stdout, stderr = await process.communicate()
    return type(
        "Completed",
        (),
        {
            "returncode": process.returncode,
            "stdout": stdout.decode("utf-8", "replace"),
            "stderr": stderr.decode("utf-8", "replace"),
        },
    )()


class HelmCliAdapter:
    """Pinned Helm CLI boundary; values never contain credential material."""

    _SECRET_KEYS = {
        "serviceCredential",
        "nextCredential",
        "credential",
        "token",
        "password",
        "secret",
    }

    def __init__(
        self,
        *,
        binary: str,
        expected_version: str,
        chart_path: str,
        chart_version: str,
        runner: Runner = _subprocess_runner,
        temporary_directory: Path | None = None,
    ) -> None:
        self._binary = binary
        self._expected_version = expected_version
        self._chart_path = chart_path
        self._chart_version = chart_version
        self._runner = runner
        self._temporary_directory = temporary_directory

    @classmethod
    def _has_secret_key(cls, value: Any) -> bool:
        if isinstance(value, dict):
            return any(
                str(key) in cls._SECRET_KEYS or cls._has_secret_key(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(cls._has_secret_key(item) for item in value)
        return False

    async def ensure_release(
        self, metadata: OpaqueProviderMetadata, values: dict[str, Any]
    ) -> None:
        if self._has_secret_key(values):
            raise MetadataConflict("Helm values must not carry plaintext credentials")
        environment = {"HELM_DRIVER": "configmap"}
        version = await self._runner(
            (self._binary, "version", "--template", "{{.Version}}"),
            environment,
        )
        if version.returncode != 0 or version.stdout.strip() != f"v{self._expected_version}":
            raise MetadataConflict("installed Helm CLI does not match the pinned version")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="exomem-helm-values-",
                suffix=".json",
                dir=self._temporary_directory,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                json.dump(values, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            result = await self._runner(
                (
                    self._binary,
                    "upgrade",
                    "--install",
                    metadata.resource_name,
                    self._chart_path,
                    "--version",
                    self._chart_version,
                    "--labels",
                    ",".join(
                        f"{key}={value}" for key, value in sorted(metadata.hcloud_labels.items())
                    ),
                    "--namespace",
                    metadata.resource_name,
                    "--create-namespace=false",
                    "--atomic",
                    "--wait",
                    "--wait-for-jobs",
                    "--timeout",
                    "5m",
                    "--values",
                    str(temporary),
                ),
                environment,
            )
            if result.returncode != 0:
                raise MetadataConflict("pinned Helm reconciliation failed")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


Probe = Callable[[str, str, dict[str, str]], Awaitable[int]]


def mint_maintenance_transfer_grant(
    *,
    credential: str,
    credential_version: str,
    cell_id: str,
    browser_origin: str,
    issued_at: int,
    jti: str,
) -> str:
    """Mint one valid, unused, one-byte download grant for route-closure proof."""

    validate_machine_credential(credential)
    claims = {
        "aud": "exomem-hosted-transfer",
        "cell": cell_id,
        "exp": issued_at + 300,
        "iat": issued_at,
        "jti": jti,
        "kid": credential_version,
        "limits": {"max_bytes": 1},
        "method": "GET",
        "nbf": issued_at,
        "op": "download",
        "origin": browser_origin,
        "principal": PrivateCellApiAdapter._PRINCIPAL_SCOPE,
        "target": {"kind": "download-v1", "path": "maintenance/route-proof"},
        "v": 2,
    }
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(credential.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return payload + "." + signature


class TraefikRoutingAdapter:
    """Exact per-cell Traefik route writer plus external Cloudflare-path proof."""

    def __init__(
        self,
        *,
        custom_objects: Any,
        control_hostname: str,
        transfer_hostname: str,
        probe: Probe,
    ) -> None:
        self._custom = custom_objects
        self._control_hostname = control_hostname
        self._transfer_hostname = transfer_hostname
        self._probe = probe

    async def disable(self, metadata: OpaqueProviderMetadata) -> None:
        for suffix in ("control", "transfer"):
            try:
                await asyncio.to_thread(
                    self._custom.delete_namespaced_custom_object,
                    group="traefik.io",
                    version="v1alpha1",
                    namespace=metadata.resource_name,
                    plural="ingressroutes",
                    name=f"{metadata.resource_name}-{suffix}",
                    body={"propagationPolicy": "Foreground"},
                )
            except Exception as error:
                if _api_status(error) != 404:
                    raise

    async def enable(self, metadata: OpaqueProviderMetadata) -> None:
        for plural, route in self._objects(metadata):
            await asyncio.to_thread(
                self._custom.patch_namespaced_custom_object,
                group="traefik.io",
                version="v1alpha1",
                namespace=metadata.resource_name,
                plural=plural,
                name=route["metadata"]["name"],
                body=route,
                field_manager="exomem-provisioner",
                force=True,
            )

    async def prove_rejected(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        unused_ticket: str,
        browser_origin: str,
        control_credential: str,
        protocol_version: str,
    ) -> bool:
        control_url = (
            f"https://{self._control_hostname}/cells/{metadata.subject_id}/private/exomem/v1/ready"
        )
        control_status = await self._probe(
            "GET",
            control_url,
            {
                "Authorization": f"Bearer {control_credential}",
                "X-Exomem-Hosted-Cell": metadata.subject_id,
                "X-Exomem-Hosted-Protocol": protocol_version,
            },
        )
        url = (
            f"https://{self._transfer_hostname}/cells/{metadata.subject_id}"
            "/public/exomem/v2/transfers/download"
        )
        preflight_status = await self._probe(
            "OPTIONS",
            url,
            {
                "Access-Control-Request-Headers": "X-Exomem-Transfer-Grant",
                "Access-Control-Request-Method": "GET",
                "Origin": browser_origin,
            },
        )
        transfer_status = await self._probe(
            "GET",
            url,
            {
                "Origin": browser_origin,
                "X-Exomem-Transfer-Grant": unused_ticket,
            },
        )
        rejected = {404, 503}
        return (
            control_status in {401, 403, 404, 503}
            and preflight_status in rejected
            and transfer_status in rejected
        )

    def _objects(self, metadata: OpaqueProviderMetadata) -> tuple[tuple[str, dict[str, Any]], ...]:
        annotations = metadata.kubernetes_annotations
        base = {
            "apiVersion": "traefik.io/v1alpha1",
            "kind": "IngressRoute",
        }
        service = [{"name": metadata.resource_name, "port": 8765}]
        control_prefix = f"/cells/{metadata.subject_id}/private/exomem/v1"
        transfer_prefix = f"/cells/{metadata.subject_id}/public/exomem/v2/transfers"
        middleware_name = metadata.resource_name + "-strip-cell"
        middleware = {
            **base,
            "kind": "Middleware",
            "metadata": {
                "name": middleware_name,
                "namespace": metadata.resource_name,
                "annotations": annotations,
            },
            "spec": {"stripPrefix": {"prefixes": [f"/cells/{metadata.subject_id}"]}},
        }
        return (
            ("middlewares", middleware),
            (
                "ingressroutes",
                {
                    **base,
                    "metadata": {
                        "name": metadata.resource_name + "-control",
                        "namespace": metadata.resource_name,
                        "annotations": annotations,
                    },
                    "spec": {
                        "entryPoints": ["web"],
                        "routes": [
                            {
                                "kind": "Rule",
                                "match": (
                                    f"Host(`{self._control_hostname}`) && "
                                    f"PathPrefix(`{control_prefix}`)"
                                ),
                                "middlewares": [{"name": middleware_name}],
                                "services": service,
                            }
                        ],
                    },
                },
            ),
            (
                "ingressroutes",
                {
                    **base,
                    "metadata": {
                        "name": metadata.resource_name + "-transfer",
                        "namespace": metadata.resource_name,
                        "annotations": annotations,
                    },
                    "spec": {
                        "entryPoints": ["web"],
                        "routes": [
                            {
                                "kind": "Rule",
                                "match": (
                                    f"Host(`{self._transfer_hostname}`) && "
                                    f"(Path(`{transfer_prefix}/upload`) || "
                                    f"Path(`{transfer_prefix}/download`))"
                                ),
                                "middlewares": [{"name": middleware_name}],
                                "services": service,
                            }
                        ],
                    },
                },
            ),
        )
