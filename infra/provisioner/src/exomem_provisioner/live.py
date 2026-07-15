"""Production provider composition for the hosted-cell lifecycle worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .adapters import (
    HelmCliAdapter,
    KubernetesCellAdapter,
    KubernetesMaintenanceLeaseAdapter,
    PrivateCellApiAdapter,
    TraefikRoutingAdapter,
    _api_status,
    _require_annotations,
    mint_maintenance_transfer_grant,
)
from .driver import EffectContext
from .lifecycle import (
    HealthObservation,
    LifecycleConfig,
    MetadataConflict,
    OpaqueProviderMetadata,
    _deterministic_uuid4,
    _digest,
    _fixed_helm_values,
)
from .models import ResourceKind
from .provider_identity import (
    ProviderIdentityConflict,
    ProviderRecoveryIdentityVerifier,
    ProviderReference,
    authenticate_cell_provider_recovery_envelopes,
    provider_operation_resource_name,
)
from .repository import OperationRepository


@dataclass(frozen=True, slots=True)
class KubernetesProviderSnapshot:
    namespace: bool
    release: bool
    init_complete: bool
    init_failed: bool
    serving: bool
    runtime_admitted: bool
    routes: tuple[bool, bool]


class KubernetesCapacityGate:
    """Live, fail-closed alpha capacity gate with no caller-authored receipt input."""

    ACTIVE_USER_CELL_LIMIT = 6
    RESERVED_VOLUME_ATTACHMENTS = 2
    PROVIDER_VOLUME_ATTACHMENT_LIMIT = 16
    MINIMUM_UNUSED_PROVIDER_HEADROOM = 8

    def __init__(self, *, core_v1: Any, storage_v1: Any) -> None:
        self._core = core_v1
        self._storage = storage_v1

    async def block_reason(self, metadata: OpaqueProviderMetadata) -> str | None:
        namespaces, attachments = await asyncio.gather(
            asyncio.to_thread(
                self._core.list_namespace,
                label_selector="exomem.io/tenant-cell=true",
            ),
            asyncio.to_thread(self._storage.list_volume_attachment),
        )
        active_names = {
            str(item.metadata.name)
            for item in (getattr(namespaces, "items", ()) or ())
            if isinstance(getattr(item.metadata, "name", None), str)
        }
        if metadata.resource_name in active_names:
            return None
        if len(active_names) >= self.ACTIVE_USER_CELL_LIMIT:
            return "active-user-cell-capacity-exhausted"
        attached_volumes = sum(
            bool(getattr(getattr(item, "status", None), "attached", False))
            for item in (getattr(attachments, "items", ()) or ())
        )
        safe_limit = self.PROVIDER_VOLUME_ATTACHMENT_LIMIT - self.MINIMUM_UNUSED_PROVIDER_HEADROOM
        projected = attached_volumes + 1 + self.RESERVED_VOLUME_ATTACHMENTS
        if projected > safe_limit:
            return "safe-volume-attachment-headroom-exhausted"
        return None


class KubernetesProviderRegistry:
    """Read/adopt provider state and retain opaque operation fences outside PostgreSQL."""

    _PSS_LABELS = {
        "exomem.io/tenant-cell": "true",
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "v1.35",
        "pod-security.kubernetes.io/audit": "restricted",
        "pod-security.kubernetes.io/audit-version": "v1.35",
        "pod-security.kubernetes.io/warn": "restricted",
        "pod-security.kubernetes.io/warn-version": "v1.35",
    }

    def __init__(
        self,
        *,
        core_v1: Any,
        apps_v1: Any,
        batch_v1: Any,
        custom_objects: Any,
        identity_verifier: ProviderRecoveryIdentityVerifier,
    ) -> None:
        self._core = core_v1
        self._apps = apps_v1
        self._batch = batch_v1
        self._custom = custom_objects
        self._identity_verifier = identity_verifier

    def _authenticate_annotations(
        self,
        annotations: dict[str, str] | None,
        metadata: OpaqueProviderMetadata,
        *,
        provider: str,
        provider_reference: str,
    ) -> None:
        values = annotations or {}
        try:
            self._identity_verifier.authenticate(
                values.get("exomem.io/recovery-envelope", ""),
                provider=provider,
                provider_reference=provider_reference,
                tenant_id=metadata.tenant_id,
                cell_id=metadata.subject_id,
                operation_id=metadata.operation_id,
                fence_generation=metadata.fence_generation,
            )
        except ProviderIdentityConflict as error:
            raise MetadataConflict(
                "Kubernetes provider recovery identity did not authenticate"
            ) from error

    @staticmethod
    def _cell_identity(
        annotations: dict[str, str] | None,
        metadata: OpaqueProviderMetadata,
    ) -> None:
        values = annotations or {}
        expected = metadata.kubernetes_annotations
        for key in (
            "exomem.io/tenant-id",
            "exomem.io/cell-id",
            "exomem.io/tenant-digest",
            "exomem.io/subject-digest",
        ):
            if values.get(key) != expected[key]:
                raise MetadataConflict("Kubernetes cell identity annotations differ")

    async def inspect(
        self,
        current: OpaqueProviderMetadata,
        owned: OpaqueProviderMetadata,
    ) -> KubernetesProviderSnapshot:
        namespace = None
        try:
            namespace = await asyncio.to_thread(self._core.read_namespace, current.resource_name)
        except Exception as error:
            if _api_status(error) != 404:
                raise
        if namespace is None:
            return KubernetesProviderSnapshot(
                False, False, False, False, False, False, (False, False)
            )
        self._cell_identity(getattr(namespace.metadata, "annotations", None), current)
        _require_annotations(getattr(namespace.metadata, "annotations", None), owned)
        self._authenticate_annotations(
            getattr(namespace.metadata, "annotations", None),
            owned,
            provider="kubernetes",
            provider_reference=ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="v1",
                kind="Namespace",
                namespace="",
                name=current.resource_name,
            ),
        )

        async def exists(call: Any, *arguments: str) -> Any | None:
            try:
                return await asyncio.to_thread(call, *arguments)
            except Exception as error:
                if _api_status(error) == 404:
                    return None
                raise

        pvc = await exists(
            self._core.read_namespaced_persistent_volume_claim,
            current.resource_name + "-data",
            current.resource_name,
        )
        if pvc is not None:
            _require_annotations(getattr(pvc.metadata, "annotations", None), owned)
            self._authenticate_annotations(
                getattr(pvc.metadata, "annotations", None),
                owned,
                provider="kubernetes",
                provider_reference=ProviderReference.kubernetes(
                    provider="kubernetes",
                    api_version="v1",
                    kind="PersistentVolumeClaim",
                    namespace=current.resource_name,
                    name=current.resource_name + "-data",
                ),
            )
        helm_releases = await asyncio.to_thread(
            self._core.list_namespaced_config_map,
            current.resource_name,
            label_selector=(f"owner=helm,name={current.resource_name},status=deployed"),
        )
        release_deployed = bool(getattr(helm_releases, "items", ()) or ())
        init_job = await exists(
            self._batch.read_namespaced_job,
            current.resource_name + "-init",
            current.resource_name,
        )
        conditions = getattr(getattr(init_job, "status", None), "conditions", ()) or ()
        init_complete = any(
            getattr(item, "type", None) == "Complete" and getattr(item, "status", None) == "True"
            for item in conditions
        )
        init_failed = any(
            getattr(item, "type", None) == "Failed" and getattr(item, "status", None) == "True"
            for item in conditions
        ) or bool(getattr(getattr(init_job, "status", None), "failed", 0))
        stateful_set = await exists(
            self._apps.read_namespaced_stateful_set,
            current.resource_name,
            current.resource_name,
        )
        routes: list[bool] = []
        for suffix in ("control", "transfer"):
            route = await exists(
                lambda name, namespace: self._custom.get_namespaced_custom_object(
                    group="traefik.io",
                    version="v1alpha1",
                    namespace=namespace,
                    plural="ingressroutes",
                    name=name,
                ),
                current.resource_name + "-" + suffix,
                current.resource_name,
            )
            if route is not None:
                _require_annotations(route.get("metadata", {}).get("annotations"), owned)
                self._authenticate_annotations(
                    route.get("metadata", {}).get("annotations"),
                    owned,
                    provider="traefik",
                    provider_reference=ProviderReference.kubernetes(
                        provider="traefik",
                        api_version="traefik.io/v1alpha1",
                        kind="IngressRoute",
                        namespace=current.resource_name,
                        name=current.resource_name + "-" + suffix,
                    ),
                )
            routes.append(route is not None)
        annotations = dict(getattr(namespace.metadata, "annotations", None) or {})
        return KubernetesProviderSnapshot(
            True,
            pvc is not None and release_deployed,
            init_complete,
            init_failed,
            stateful_set is not None,
            annotations.get("exomem.io/runtime-admitted") == "true",
            (routes[0], routes[1]),
        )

    async def ensure_namespace(
        self, metadata: OpaqueProviderMetadata, recovery_envelope: str
    ) -> None:
        labels = dict(self._PSS_LABELS)
        labels.update(
            {
                "app.kubernetes.io/managed-by": "Helm",
                "exomem.io/cell-resource": metadata.resource_name,
            }
        )
        annotations = dict(metadata.kubernetes_annotations)
        annotations["exomem.io/recovery-envelope"] = recovery_envelope
        annotations.update(
            {
                "helm.sh/resource-policy": "keep",
                "meta.helm.sh/release-name": metadata.resource_name,
                "meta.helm.sh/release-namespace": metadata.resource_name,
                "exomem.io/resource-name": metadata.resource_name,
                "exomem.io/pvc-name": metadata.resource_name + "-data",
                "exomem.io/credentials-secret-name": "exomem-cell-credentials",
                "exomem.io/init-request-configmap-name": metadata.resource_name + "-init-request",
            }
        )
        body = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": metadata.resource_name,
                "labels": labels,
                "annotations": annotations,
            },
        }
        try:
            await asyncio.to_thread(self._core.create_namespace, body)
        except Exception as error:
            if _api_status(error) != 409:
                raise
            existing = await asyncio.to_thread(self._core.read_namespace, metadata.resource_name)
            _require_annotations(getattr(existing.metadata, "annotations", None), metadata)

    async def record_operation(
        self, metadata: OpaqueProviderMetadata, recovery_envelope: str
    ) -> None:
        name = provider_operation_resource_name(metadata.operation_id)
        annotations = dict(metadata.kubernetes_annotations)
        annotations["exomem.io/recovery-envelope"] = recovery_envelope
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": metadata.resource_name,
                "annotations": annotations,
                "labels": {"exomem.io/provider-operation": "true"},
            },
            "immutable": True,
            "data": {},
        }
        try:
            await asyncio.to_thread(
                self._core.create_namespaced_config_map,
                metadata.resource_name,
                body,
            )
        except Exception as error:
            if _api_status(error) != 409:
                raise
            existing = await asyncio.to_thread(
                self._core.read_namespaced_config_map,
                name,
                metadata.resource_name,
            )
            _require_annotations(getattr(existing.metadata, "annotations", None), metadata)

    async def mark_runtime_admitted(self, metadata: OpaqueProviderMetadata) -> None:
        await asyncio.to_thread(
            self._core.patch_namespace,
            metadata.resource_name,
            {"metadata": {"annotations": {"exomem.io/runtime-admitted": "true"}}},
        )

    async def observed_fence(self, tenant_id: str) -> int:
        observed = 0
        namespaces = await asyncio.to_thread(
            self._core.list_namespace,
            label_selector="exomem.io/tenant-cell=true",
        )
        selected: list[str] = []
        for item in getattr(namespaces, "items", ()):
            annotations = dict(getattr(item.metadata, "annotations", None) or {})
            recovered = OpaqueProviderMetadata.from_kubernetes_annotations(annotations)
            if recovered.tenant_id != tenant_id:
                continue
            self._authenticate_annotations(
                annotations,
                recovered,
                provider="kubernetes",
                provider_reference=ProviderReference.kubernetes(
                    provider="kubernetes",
                    api_version="v1",
                    kind="Namespace",
                    namespace="",
                    name=str(item.metadata.name),
                ),
            )
            selected.append(str(item.metadata.name))
            observed = max(observed, recovered.fence_generation)
        if not selected:
            return observed
        config_maps = await asyncio.to_thread(
            self._core.list_config_map_for_all_namespaces,
            label_selector="exomem.io/provider-operation=true",
        )
        for item in getattr(config_maps, "items", ()):
            if getattr(item.metadata, "namespace", None) not in selected:
                continue
            annotations = dict(getattr(item.metadata, "annotations", None) or {})
            recovered = OpaqueProviderMetadata.from_kubernetes_annotations(annotations)
            if recovered.tenant_id == tenant_id:
                self._authenticate_annotations(
                    annotations,
                    recovered,
                    provider="kubernetes",
                    provider_reference=ProviderReference.kubernetes(
                        provider="kubernetes",
                        api_version="v1",
                        kind="ConfigMap",
                        namespace=str(item.metadata.namespace),
                        name=str(item.metadata.name),
                    ),
                )
                observed = max(observed, recovered.fence_generation)
        return observed


class LiveLifecyclePlane:
    """Routine Kubernetes/Helm/runtime composition with no HCloud or PV authority."""

    def __init__(
        self,
        *,
        repository: OperationRepository,
        registry: KubernetesProviderRegistry,
        cell: KubernetesCellAdapter,
        helm: HelmCliAdapter,
        runtime: PrivateCellApiAdapter,
        routes: TraefikRoutingAdapter,
        maintenance: KubernetesMaintenanceLeaseAdapter,
        capacity: KubernetesCapacityGate,
        identity_verifier: ProviderRecoveryIdentityVerifier,
        config: LifecycleConfig,
        now: Any = time.time,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._cell = cell
        self._helm = helm
        self._runtime = runtime
        self._routes = routes
        self._maintenance = maintenance
        self._capacity = capacity
        self._identity_verifier = identity_verifier
        self._config = config
        self._now = now
        self._owned: dict[str, OpaqueProviderMetadata] = {}
        self._snapshots: dict[str, KubernetesProviderSnapshot] = {}
        self._recovery_envelopes: dict[str, dict[str, str]] = {}
        self._helm_requests: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _key(metadata: OpaqueProviderMetadata) -> str:
        return _digest(metadata.tenant_id + ":" + metadata.subject_id, length=64)

    def _owner(self, metadata: OpaqueProviderMetadata) -> OpaqueProviderMetadata:
        return self._owned.get(self._key(metadata), metadata)

    def _snapshot(self, metadata: OpaqueProviderMetadata) -> KubernetesProviderSnapshot:
        try:
            return self._snapshots[self._key(metadata)]
        except KeyError as error:
            raise MetadataConflict(
                "provider state was not observed before reconciliation"
            ) from error

    async def _refresh(self, metadata: OpaqueProviderMetadata) -> KubernetesProviderSnapshot:
        snapshot = await self._registry.inspect(metadata, self._owner(metadata))
        self._snapshots[self._key(metadata)] = snapshot
        return snapshot

    async def observed_fence(self, tenant_id: str) -> int:
        return await self._registry.observed_fence(tenant_id)

    async def observe_operation(self, context: EffectContext, request: dict[str, Any]) -> None:
        if context.cell_id is None:
            return
        current = OpaqueProviderMetadata(
            tenant_id=context.tenant_id,
            subject_id=context.cell_id,
            operation_id=context.provider_operation_id,
            fence_generation=context.fence_generation,
        )
        try:
            recovery_envelopes = authenticate_cell_provider_recovery_envelopes(
                self._identity_verifier,
                request.get("_providerRecoveryEnvelopes"),
                tenant_id=current.tenant_id,
                cell_id=current.subject_id,
                operation_id=current.operation_id,
                fence_generation=current.fence_generation,
                resource_name=current.resource_name,
                operation_resource_name=provider_operation_resource_name(current.operation_id),
            )
        except ProviderIdentityConflict as error:
            raise MetadataConflict("provider recovery envelope set did not authenticate") from error
        self._recovery_envelopes[self._key(current)] = recovery_envelopes
        resources = await self._repository.list_resources(
            tenant_id=context.tenant_id,
            cell_id=context.cell_id,
        )
        owned = current
        helm_request = request
        for resource in resources:
            if resource.kind is ResourceKind.KUBERNETES_NAMESPACE:
                owned = OpaqueProviderMetadata(
                    tenant_id=context.tenant_id,
                    subject_id=context.cell_id,
                    operation_id=resource.provider_operation_id,
                    fence_generation=resource.provider_fence_generation,
                )
                helm_request = await self._repository.load_request(resource.operation_id)
                break
        self._owned[self._key(current)] = owned
        self._helm_requests[self._key(current)] = dict(helm_request)
        snapshot = await self._refresh(current)
        if snapshot.namespace:
            await self._registry.record_operation(
                current, recovery_envelopes["providerOperationConfigMap"]
            )

    def has_namespace(self, metadata: OpaqueProviderMetadata) -> bool:
        return self._snapshot(metadata).namespace

    async def capacity_block_reason(self, metadata: OpaqueProviderMetadata) -> str | None:
        return await self._capacity.block_reason(metadata)

    async def ensure_namespace(self, metadata: OpaqueProviderMetadata) -> None:
        envelopes = self._recovery_envelopes[self._key(metadata)]
        await self._registry.ensure_namespace(metadata, envelopes["namespace"])
        self._owned[self._key(metadata)] = metadata
        await self._registry.record_operation(metadata, envelopes["providerOperationConfigMap"])
        await self._refresh(metadata)

    def has_release(self, metadata: OpaqueProviderMetadata) -> bool:
        return self._snapshot(metadata).release

    async def install_release(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        owned = self._owner(metadata)
        await self._cell.write_credential_bundle(
            owned,
            {"1": str(request["serviceCredential"])},
            lifecycle_annotations={
                "exomem.io/active-credential-version": "1",
                "exomem.io/security-revision": "1",
                "exomem.io/credential-phase": "stable",
                "exomem.io/recovery-envelope": self._recovery_envelopes[self._key(metadata)][
                    "credentialSecret"
                ],
            },
        )
        await self._helm.ensure_release(owned, values)
        await self._refresh(metadata)

    async def volume_claim_bound(self, metadata: OpaqueProviderMetadata) -> bool:
        return await self._cell.volume_claim_bound(self._owner(metadata))

    def is_initialized(self, metadata: OpaqueProviderMetadata) -> bool:
        return self._snapshot(metadata).serving

    async def initialize(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        config: LifecycleConfig,
    ) -> bool:
        snapshot = await self._refresh(metadata)
        if snapshot.init_failed:
            raise MetadataConflict("cell storage initialization failed")
        if not snapshot.init_complete:
            return False
        values = _fixed_helm_values(self._owner(metadata), request, config)
        values["workloadMode"] = "serve"
        await self._helm.ensure_release(self._owner(metadata), values)
        return (await self._refresh(metadata)).serving

    async def health(
        self, metadata: OpaqueProviderMetadata, request: dict[str, Any]
    ) -> HealthObservation:
        return await self._runtime.health(
            self._owner(metadata),
            credential=str(request["serviceCredential"]),
            protocol_version=str(request["protocolVersion"]),
            config=self._config,
            expected_release=str(request["releaseVersion"]),
            expected_worker_policy=dict(request["workerPolicy"]),
        )

    async def admit_runtime(self, metadata: OpaqueProviderMetadata) -> None:
        await self._registry.mark_runtime_admitted(self._owner(metadata))
        await self._refresh(metadata)

    def runtime_admitted(self, metadata: OpaqueProviderMetadata) -> bool:
        return self._snapshot(metadata).runtime_admitted

    async def enable_routes(self, metadata: OpaqueProviderMetadata) -> None:
        owner = self._owner(metadata)
        try:
            request = self._helm_requests[self._key(metadata)]
        except KeyError as error:
            raise MetadataConflict("original Helm request was not authenticated") from error
        values = _fixed_helm_values(owner, request, self._config)
        values["workloadMode"] = "serve"
        values["routes"]["enabled"] = True
        await self._helm.ensure_release(owner, values)
        await self._refresh(metadata)

    async def disable_routes(self, metadata: OpaqueProviderMetadata) -> None:
        await self._routes.disable(self._owner(metadata))
        await self._refresh(metadata)

    def routes_enabled(self, metadata: OpaqueProviderMetadata) -> tuple[bool, bool]:
        return self._snapshot(metadata).routes

    async def prove_external_rejection(
        self, metadata: OpaqueProviderMetadata, request: dict[str, Any]
    ) -> bool:
        credential = str(request["serviceCredential"])
        version = await self._active_version(metadata)
        ticket = mint_maintenance_transfer_grant(
            credential=credential,
            credential_version=version,
            cell_id=metadata.subject_id,
            browser_origin=self._config.browser_origin,
            issued_at=int(self._now()),
            jti=str(uuid.uuid4()),
        )
        return await self._routes.prove_rejected(
            self._owner(metadata),
            unused_ticket=ticket,
            browser_origin=self._config.browser_origin,
            control_credential=credential,
            protocol_version=self._config.protocol_version,
        )

    async def acquire_maintenance(
        self, metadata: OpaqueProviderMetadata, operation_id: str
    ) -> bool:
        return await self._maintenance.acquire(metadata, operation_id)

    async def release_maintenance(
        self, metadata: OpaqueProviderMetadata, operation_id: str
    ) -> None:
        await self._maintenance.release(metadata, operation_id)

    async def quiesce(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        operation_id: str,
    ) -> None:
        await self._runtime.quiesce(
            self._owner(metadata),
            credential=str(request["serviceCredential"]),
            protocol_version=str(request["protocolVersion"]),
            operation_id=operation_id,
        )

    async def scale(self, metadata: OpaqueProviderMetadata, replicas: int) -> None:
        await self._cell.scale(self._owner(metadata), replicas)

    async def resume(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        operation_id: str,
    ) -> None:
        await self._runtime.resume(
            self._owner(metadata),
            credential=str(request["serviceCredential"]),
            protocol_version=str(request["protocolVersion"]),
            operation_id=operation_id,
        )

    async def _active_version(self, metadata: OpaqueProviderMetadata) -> str:
        _, annotations = await self._cell.read_credential_bundle(self._owner(metadata))
        version = annotations.get("exomem.io/active-credential-version")
        if not version:
            raise MetadataConflict("active credential version metadata is absent")
        return version

    @staticmethod
    def _worker_policy_digest(request: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(request["workerPolicy"], sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    async def _credential_transition(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        credentials: dict[str, str],
        annotations: dict[str, str],
        action: str,
        operation_id: str,
        version: str,
    ) -> dict[str, Any]:
        expected = int(annotations.get("exomem.io/security-revision", "0"))
        transition = operation_id + ":credential-" + action
        request: dict[str, Any] = {
            "request_id": _deterministic_uuid4(transition),
            "operation_id": transition,
            "cell_id": metadata.subject_id,
            "vault_id": metadata.tenant_id,
            "state_root": "/var/lib/exomem/state",
            "action": action,
            "expected_revision": expected,
        }
        if action == "stage":
            request["pending_version"] = version
        prepared = {
            "exomem.io/credential-transition": action,
            "exomem.io/credential-transition-operation": _digest(transition, length=64),
            "exomem.io/credential-transition-expected-revision": str(expected),
        }
        await self._cell.write_credential_bundle(
            metadata,
            credentials,
            lifecycle_annotations={**annotations, **prepared},
        )
        active = annotations.get("exomem.io/active-credential-version")
        active_credential = credentials.get(str(active))
        if not active_credential:
            raise MetadataConflict("active provider credential is absent")
        result = await self._runtime.operator(
            "credential",
            metadata,
            request,
            credential=active_credential,
            protocol_version=self._config.protocol_version,
        )
        revision = result.get("revision")
        if revision != expected + 1:
            raise MetadataConflict("hosted credential revision did not advance exactly once")
        return result

    async def stage_credential(
        self,
        metadata: OpaqueProviderMetadata,
        version: int,
        credential: str,
        request: dict[str, Any],
        operation_id: str,
    ) -> None:
        owned = self._owner(metadata)
        credentials, annotations = await self._cell.read_credential_bundle(owned)
        pending = str(version)
        active = annotations.get("exomem.io/active-credential-version")
        if active is None or credentials.get(active) != str(request["serviceCredential"]):
            raise MetadataConflict("active credential does not match provider state")
        if annotations.get("exomem.io/credential-phase") in {"staged", "proved", "promoted"}:
            if credentials.get(pending) != credential:
                raise MetadataConflict("pending credential version is immutable")
            return
        credentials[pending] = credential
        result = await self._credential_transition(
            owned,
            credentials=credentials,
            annotations=annotations,
            action="stage",
            operation_id=operation_id,
            version=pending,
        )
        if result.get("phase") != "staged" or result.get("pending_version") != pending:
            raise MetadataConflict("hosted credential did not enter staged overlap")
        await self._cell.write_credential_bundle(
            owned,
            credentials,
            lifecycle_annotations={
                **annotations,
                "exomem.io/security-revision": str(result["revision"]),
                "exomem.io/credential-phase": "staged",
                "exomem.io/pending-credential-version": pending,
            },
        )

    async def credential_accepted(
        self,
        metadata: OpaqueProviderMetadata,
        version: int,
        credential: str,
        request: dict[str, Any],
        operation_id: str,
    ) -> bool:
        owned = self._owner(metadata)
        credentials, annotations = await self._cell.read_credential_bundle(owned)
        pending = str(version)
        if credentials.get(pending) != credential:
            return False
        if annotations.get("exomem.io/credential-phase") == "proved":
            return True
        revision = int(annotations.get("exomem.io/security-revision", "0"))
        probe_operation = operation_id + ":credential-probe"
        operator_request = {
            "request_id": _deterministic_uuid4(probe_operation),
            "operation_id": probe_operation,
            "cell_id": owned.subject_id,
            "vault_id": owned.tenant_id,
            "state_root": "/var/lib/exomem/state",
            "selected_credential_version": pending,
            "expected_release": str(request["releaseVersion"]),
            "expected_protocol": str(request["protocolVersion"]),
            "expected_worker_policy_digest": self._worker_policy_digest(request),
            "expected_revision": revision,
            "port": 8765,
        }
        result = await self._runtime.operator(
            "probe",
            owned,
            operator_request,
            credential=credential,
            protocol_version=self._config.protocol_version,
        )
        proved = (
            result.get("authenticated_credential_version") == pending
            and result.get("security_revision") == revision
            and result.get("proof_recorded") is True
        )
        if proved:
            await self._cell.write_credential_bundle(
                owned,
                credentials,
                lifecycle_annotations={
                    **annotations,
                    "exomem.io/credential-phase": "proved",
                },
            )
        return proved

    async def promote_credential(
        self,
        metadata: OpaqueProviderMetadata,
        version: int,
        request: dict[str, Any],
        operation_id: str,
    ) -> bool:
        owned = self._owner(metadata)
        credentials, annotations = await self._cell.read_credential_bundle(owned)
        pending = str(version)
        old_version = annotations.get("exomem.io/active-credential-version")
        if old_version == pending and set(credentials) == {pending}:
            return await self._runtime.credential_rejected(
                owned,
                credential=str(request["serviceCredential"]),
                protocol_version=str(request["protocolVersion"]),
            )
        if old_version is None or pending not in credentials:
            raise MetadataConflict("pending credential is absent")
        phase = annotations.get("exomem.io/credential-phase")
        if phase == "proved":
            result = await self._credential_transition(
                owned,
                credentials=credentials,
                annotations=annotations,
                action="promote",
                operation_id=operation_id,
                version=pending,
            )
            if result.get("phase") != "promoted":
                raise MetadataConflict("hosted credential did not promote")
            annotations = {
                **annotations,
                "exomem.io/security-revision": str(result["revision"]),
                "exomem.io/credential-phase": "promoted",
            }
            await self._cell.write_credential_bundle(
                owned, credentials, lifecycle_annotations=annotations
            )
            phase = "promoted"
        if phase == "promoted":
            result = await self._credential_transition(
                owned,
                credentials=credentials,
                annotations=annotations,
                action="finalize",
                operation_id=operation_id,
                version=pending,
            )
            if result.get("phase") != "stable" or result.get("active_version") != pending:
                raise MetadataConflict("hosted credential did not finalize")
            annotations = {
                **annotations,
                "exomem.io/security-revision": str(result["revision"]),
                "exomem.io/credential-phase": "stable",
                "exomem.io/active-credential-version": pending,
            }
            await self._cell.write_credential_bundle(
                owned,
                {pending: credentials[pending]},
                lifecycle_annotations=annotations,
            )
        new_health = await self._runtime.health(
            owned,
            credential=credentials[pending],
            protocol_version=str(request["protocolVersion"]),
            config=self._config,
            expected_release=str(request["releaseVersion"]),
            expected_worker_policy=dict(request["workerPolicy"]),
        )
        old_rejected = await self._runtime.credential_rejected(
            owned,
            credential=str(request["serviceCredential"]),
            protocol_version=str(request["protocolVersion"]),
        )
        return new_health.ready and old_rejected

    async def seal(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        request: dict[str, Any],
        operation_id: str,
        created_at: str,
    ) -> None:
        await self._runtime.seal(
            self._owner(metadata),
            credential=str(request["serviceCredential"]),
            protocol_version=str(request["protocolVersion"]),
            operation_id=operation_id,
            created_at=created_at,
        )

    async def discard_candidate(self, metadata: OpaqueProviderMetadata) -> dict[str, bool]:
        raise MetadataConflict("candidate deletion requires the durability deletion worker")

    async def destroy_tenant_online(self, tenant_id: str) -> None:
        raise MetadataConflict("tenant deletion requires the durability deletion worker")

    def retention_wait_seconds(self, tenant_id: str) -> int | None:
        raise MetadataConflict("tenant deletion requires the durability deletion worker")

    async def destroy_expired_retention(self, tenant_id: str) -> None:
        raise MetadataConflict("tenant deletion requires the durability deletion worker")

    def destruction_proof(self, tenant_id: str) -> dict[str, bool]:
        raise MetadataConflict("tenant deletion requires the durability deletion worker")

    def provider_reference(self, metadata: OpaqueProviderMetadata) -> str:
        return "cell-" + _digest(metadata.tenant_id + ":" + metadata.subject_id, length=32)
