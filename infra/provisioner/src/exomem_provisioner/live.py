"""Production provider composition for the hosted-cell lifecycle worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from .adapters import (
    HelmCliAdapter,
    KubernetesCellAdapter,
    KubernetesMaintenanceLeaseAdapter,
    KubernetesVaultFingerprintAdapter,
    PrivateCellApiAdapter,
    TraefikRoutingAdapter,
    _api_status,
    _require_annotations,
    mint_maintenance_transfer_grant,
)
from .authorization_membership import (
    AUTHORIZATION_SESSION_SCHEMA_VERSION,
    DEFAULT_ATTESTATION_TTL_SECONDS,
    build_initial_hosted_authorization_bundle,
    inspect_hosted_authorization_bundle,
    transition_hosted_authorization_bundle,
)
from .capacity import CapacityError
from .conflict_reason import ConflictReason
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
from .models import CapacityReservationClass, ResourceKind
from .provider_identity import (
    ProviderIdentityConflict,
    ProviderRecoveryIdentityVerifier,
    ProviderReference,
    authenticate_cell_provider_recovery_envelopes,
    provider_operation_resource_name,
)
from .repository import OperationRepository, canonical_request_sha256
from .wire_protocol import runtime_identity


@dataclass(frozen=True, slots=True)
class KubernetesProviderSnapshot:
    namespace: bool
    release: bool
    init_job_present: bool
    init_complete: bool
    init_failed: bool
    serving: bool
    runtime_admitted: bool
    routes: tuple[bool, bool]
    runtime_desired_replicas: int = 0
    runtime_pods: int = 0


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
                "Kubernetes provider recovery identity did not authenticate",
                reason=ConflictReason.KUBERNETES_RECOVERY_IDENTITY_UNAUTHENTICATED,
            ) from error

    @staticmethod
    def _require_not_terminating(resource: Any) -> None:
        if getattr(getattr(resource, "metadata", None), "deletion_timestamp", None) is not None:
            raise MetadataConflict(
                "Kubernetes provider object is terminating",
                reason=ConflictReason.PROVIDER_OBJECT_TERMINATING,
            )

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
                raise MetadataConflict(
                    "Kubernetes cell identity annotations differ",
                    reason=ConflictReason.KUBERNETES_CELL_IDENTITY_ANNOTATIONS_DIFFER,
                )

    @staticmethod
    def _recovery_digest(*resources: Any) -> str:
        """Expose only a stability digest for exact Kubernetes object versions."""
        values: list[dict[str, str]] = []
        for resource in resources:
            metadata = getattr(resource, "metadata", None)
            values.append(
                {
                    "name": str(getattr(metadata, "name", "")),
                    "namespace": str(getattr(metadata, "namespace", "")),
                    "uid": str(getattr(metadata, "uid", "")),
                    "resourceVersion": str(getattr(metadata, "resource_version", "")),
                }
            )
        return hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    async def authenticate_recovery_record(self, metadata: OpaqueProviderMetadata) -> str:
        """Read and authenticate the durable Kubernetes recovery identities once."""
        namespace = await asyncio.to_thread(self._core.read_namespace, metadata.resource_name)
        pvc = await asyncio.to_thread(
            self._core.read_namespaced_persistent_volume_claim,
            metadata.resource_name + "-data",
            metadata.resource_name,
        )
        operation_record = await asyncio.to_thread(
            self._core.read_namespaced_config_map,
            provider_operation_resource_name(metadata.operation_id),
            metadata.resource_name,
        )
        helm_records = tuple(
            getattr(
                await asyncio.to_thread(
                    self._core.list_namespaced_config_map,
                    metadata.resource_name,
                    label_selector=(f"owner=helm,name={metadata.resource_name},status=deployed"),
                ),
                "items",
                (),
            )
            or ()
        )
        if len(helm_records) != 1:
            raise MetadataConflict(
                "deployed Helm release record is not exact",
                reason=ConflictReason.HELM_RELEASE_RECORD_NOT_EXACT,
            )
        helm_record = helm_records[0]
        self._require_not_terminating(helm_record)
        helm_labels = dict(getattr(helm_record.metadata, "labels", None) or {})
        if (
            getattr(helm_record.metadata, "namespace", None) != metadata.resource_name
            or helm_labels.get("owner") != "helm"
            or helm_labels.get("name") != metadata.resource_name
            or helm_labels.get("status") != "deployed"
        ):
            raise MetadataConflict(
                "deployed Helm release record identity differs",
                reason=ConflictReason.HELM_RELEASE_RECORD_IDENTITY_DIFFERS,
            )
        for resource in (namespace, pvc, operation_record):
            self._require_not_terminating(resource)
            _require_annotations(getattr(resource.metadata, "annotations", None), metadata)
        self._authenticate_annotations(
            getattr(namespace.metadata, "annotations", None),
            metadata,
            provider="kubernetes",
            provider_reference=ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="v1",
                kind="Namespace",
                namespace="",
                name=metadata.resource_name,
            ),
        )
        self._authenticate_annotations(
            getattr(pvc.metadata, "annotations", None),
            metadata,
            provider="kubernetes",
            provider_reference=ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="v1",
                kind="PersistentVolumeClaim",
                namespace=metadata.resource_name,
                name=metadata.resource_name + "-data",
            ),
        )
        self._authenticate_annotations(
            getattr(operation_record.metadata, "annotations", None),
            metadata,
            provider="kubernetes",
            provider_reference=ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="v1",
                kind="ConfigMap",
                namespace=metadata.resource_name,
                name=provider_operation_resource_name(metadata.operation_id),
            ),
        )
        try:
            init_job = await asyncio.to_thread(
                self._batch.read_namespaced_job,
                metadata.resource_name + "-init",
                metadata.resource_name,
            )
        except Exception as error:
            if _api_status(error) != 404:
                raise
            init_job = None
        if init_job is not None:
            self._require_not_terminating(init_job)
            self._authenticate_annotations(
                getattr(init_job.metadata, "annotations", None),
                metadata,
                provider="kubernetes",
                provider_reference=ProviderReference.kubernetes(
                    provider="kubernetes",
                    api_version="batch/v1",
                    kind="Job",
                    namespace=metadata.resource_name,
                    name=metadata.resource_name + "-init",
                ),
            )
        return self._recovery_digest(namespace, pvc, operation_record, helm_record, init_job)

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
                False, False, False, False, False, False, False, (False, False)
            )
        self._require_not_terminating(namespace)
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
            self._require_not_terminating(pvc)
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
        for release in getattr(helm_releases, "items", ()) or ():
            self._require_not_terminating(release)
        release_deployed = bool(getattr(helm_releases, "items", ()) or ())
        init_job = await exists(
            self._batch.read_namespaced_job,
            current.resource_name + "-init",
            current.resource_name,
        )
        if init_job is not None:
            self._require_not_terminating(init_job)
        conditions = getattr(getattr(init_job, "status", None), "conditions", ()) or ()
        init_complete = any(
            getattr(item, "type", None) == "Complete" and getattr(item, "status", None) == "True"
            for item in conditions
        )
        init_failed = not init_complete and (
            any(
                getattr(item, "type", None) == "Failed" and getattr(item, "status", None) == "True"
                for item in conditions
            )
            or bool(getattr(getattr(init_job, "status", None), "failed", 0))
        )
        stateful_set = await exists(
            self._apps.read_namespaced_stateful_set,
            current.resource_name,
            current.resource_name,
        )
        if stateful_set is not None:
            self._require_not_terminating(stateful_set)
        desired_replicas = int(getattr(getattr(stateful_set, "spec", None), "replicas", 0) or 0)
        runtime_pod_list = await asyncio.to_thread(
            self._core.list_namespaced_pod,
            current.resource_name,
            label_selector=(
                "app.kubernetes.io/name=exomem-cell,"
                f"exomem.io/cell={current.resource_name},"
                "!exomem.io/storage-init,!exomem.io/vault-fingerprint"
            ),
        )
        runtime_pods = len(tuple(getattr(runtime_pod_list, "items", ()) or ()))
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
                metadata = route.get("metadata", {})
                if metadata.get("deletionTimestamp") is not None:
                    raise MetadataConflict(
                        "Kubernetes provider object is terminating",
                        reason=ConflictReason.ROUTE_OBJECT_TERMINATING,
                    )
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
            init_job is not None,
            init_complete,
            init_failed,
            stateful_set is not None,
            annotations.get("exomem.io/runtime-admitted") == "true",
            (routes[0], routes[1]),
            desired_replicas,
            runtime_pods,
        )

    async def ensure_namespace(
        self,
        metadata: OpaqueProviderMetadata,
        recovery_envelope: str,
        provision_mode: str,
        helm_values: dict[str, Any],
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
                "exomem.io/vault-id": helm_values["vaultId"],
                "exomem.io/expected-release": helm_values["expectedRelease"],
                "exomem.io/worker-policy-digest": helm_values["workerPolicyDigest"],
                "exomem.io/records-reader-version": str(helm_values["recordsReaderVersion"]),
                "exomem.io/lifecycle-actions-enabled": str(
                    helm_values["lifecycleActionsEnabled"]
                ).lower(),
                "exomem.io/browser-origin": helm_values["browserOrigin"],
                "exomem.io/transfer-hostname": helm_values["transferHostname"],
                "helm.sh/resource-policy": "keep",
                "meta.helm.sh/release-name": metadata.resource_name,
                "meta.helm.sh/release-namespace": metadata.resource_name,
                "exomem.io/resource-name": metadata.resource_name,
                "exomem.io/pvc-name": metadata.resource_name + "-data",
                "exomem.io/credentials-secret-name": "exomem-cell-credentials",
                "exomem.io/authorization-session-secret-name": ("exomem-authorization-session"),
                "exomem.io/init-request-configmap-name": metadata.resource_name + "-init-request",
                "exomem.io/provision-mode": provision_mode,
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
            if (
                dict(getattr(existing.metadata, "annotations", None) or {}).get(
                    "exomem.io/provision-mode"
                )
                != provision_mode
            ):
                raise MetadataConflict(
                    "Kubernetes namespace provision mode differs",
                    reason=ConflictReason.NAMESPACE_PROVISION_MODE_DIFFERS,
                ) from error

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
        capacity: Any,
        identity_verifier: ProviderRecoveryIdentityVerifier,
        config: LifecycleConfig,
        fingerprint: KubernetesVaultFingerprintAdapter | None = None,
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
        self._fingerprint = fingerprint
        self._now = now
        self._owned: dict[str, OpaqueProviderMetadata] = {}
        self._snapshots: dict[str, KubernetesProviderSnapshot] = {}
        self._recovery_envelopes: dict[str, dict[str, str]] = {}
        self._helm_requests: dict[str, dict[str, Any]] = {}
        self._operation_ids: dict[str, str] = {}

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
                "provider state was not observed before reconciliation",
                reason=ConflictReason.PROVIDER_STATE_NOT_OBSERVED,
            ) from error

    async def _refresh(self, metadata: OpaqueProviderMetadata) -> KubernetesProviderSnapshot:
        snapshot = await self._registry.inspect(metadata, self._owner(metadata))
        self._snapshots[self._key(metadata)] = snapshot
        return snapshot

    async def _authorization_session_revision(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        *,
        require_fresh: bool = True,
    ) -> str:
        """Ensure one authenticated bootstrap generation before Helm can start a pod."""

        key = self._key(metadata)
        owned = self._owner(metadata)
        original = self._helm_requests.get(key, request)
        envelopes = original.get("_providerRecoveryEnvelopes")
        if not isinstance(envelopes, dict):
            raise MetadataConflict(
                "authorization Secret provider authority is absent",
                reason=ConflictReason.AUTHORIZATION_ENVELOPE_SET_ABSENT,
            )
        recovery_envelope = envelopes.get("authorizationSessionSecret")
        if not isinstance(recovery_envelope, str) or not recovery_envelope:
            raise MetadataConflict(
                "authorization Secret provider authority is absent",
                reason=ConflictReason.AUTHORIZATION_SECRET_ENVELOPE_ABSENT,
            )
        target = self._config.runtime_target_for(
            original,
            v2="runtimeTarget" in original,
        )
        replica_id = owned.resource_name + "-0"
        files = await self._cell.read_authorization_session_bundle(owned)
        current = int(self._now())

        async def _mint() -> Any:
            minted = build_initial_hosted_authorization_bundle(
                cell_id=owned.subject_id,
                logical_vault_id=owned.tenant_id,
                replica_id=replica_id,
                software_version=str(target["releaseVersion"]),
                schema_version=AUTHORIZATION_SESSION_SCHEMA_VERSION,
                recovery_envelope=recovery_envelope,
                now=current,
            )
            await self._cell.write_authorization_session_bundle(
                owned,
                minted.files,
                recovery_envelope=recovery_envelope,
                membership_epoch=minted.epoch,
                membership_digest=minted.membership_digest,
                revision=minted.revision,
            )
            return minted

        if files is None:
            bundle = await _mint()
        else:
            # Validate everything except the elapsed-time window first. The only
            # clauses `_require_fresh` gates are the control and serving-membership
            # freshness windows, so a bundle that passes here and fails below has
            # nothing wrong with it but age.
            bundle = inspect_hosted_authorization_bundle(
                files,
                expected_cell_id=owned.subject_id,
                expected_logical_vault_id=owned.tenant_id,
                expected_replica_id=replica_id,
                expected_software_version=None,
                expected_schema_version=AUTHORIZATION_SESSION_SCHEMA_VERSION,
                expected_recovery_envelope=recovery_envelope,
                now=current,
                _require_fresh=False,
            )
            if require_fresh:
                try:
                    bundle = inspect_hosted_authorization_bundle(
                        files,
                        expected_cell_id=owned.subject_id,
                        expected_logical_vault_id=owned.tenant_id,
                        expected_replica_id=replica_id,
                        expected_software_version=None,
                        expected_schema_version=AUTHORIZATION_SESSION_SCHEMA_VERSION,
                        expected_recovery_envelope=recovery_envelope,
                        now=current,
                        _require_fresh=True,
                    )
                except MetadataConflict:
                    # The bundle is sound and merely expired. A cell that has never
                    # been admitted or routed has never served a request under it,
                    # so nothing is depending on the old session and re-minting is
                    # the same act as minting one for a cell that had none.
                    #
                    # Provisioning a cell is not instantaneous: the bundle is minted
                    # at `install_release`, and any pause before initialize -- a
                    # retry, an operator recovery, a slow CSI attach -- can outlast
                    # the one-hour attestation TTL. Failing terminally there makes
                    # elapsed time indistinguishable from a forged attestation, and
                    # leaves a cell that can never finish provisioning.
                    snapshot = self._snapshot(metadata)
                    if snapshot.runtime_admitted or snapshot.routes != (False, False):
                        raise MetadataConflict(
                            "authorization session expired on a serving cell",
                            reason=(ConflictReason.AUTHORIZATION_SESSION_EXPIRED_ON_SERVING_CELL),
                        ) from None
                    bundle = await _mint()
        return bundle.revision

    async def _authorization_helm_values(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        values: dict[str, Any],
        *,
        require_fresh: bool = True,
    ) -> dict[str, Any]:
        desired = dict(values)
        desired["authorizationSessionRevision"] = await self._authorization_session_revision(
            metadata, request, require_fresh=require_fresh
        )
        return desired

    async def _transition_authorization_session_membership(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        target_state: str,
        target_no_in_flight: bool,
        target_software_version: str | None = None,
        require_runtime_attestation: bool = False,
        runtime_credential: str | None = None,
        runtime_protocol_version: str | None = None,
    ) -> str:
        """Commit one authenticated successor under the lifecycle maintenance lease."""

        key = self._key(metadata)
        owned = self._owner(metadata)
        try:
            original = self._helm_requests[key]
        except KeyError as error:
            raise MetadataConflict(
                "original authorization session identity is unavailable",
                reason=ConflictReason.ORIGINAL_AUTHORIZATION_SESSION_IDENTITY_UNAVAILABLE,
            ) from error
        envelopes = original.get("_providerRecoveryEnvelopes")
        if not isinstance(envelopes, dict):
            raise MetadataConflict(
                "authorization Secret provider authority is absent",
                reason=ConflictReason.AUTHORIZATION_ENVELOPE_SET_ABSENT,
            )
        recovery_envelope = envelopes.get("authorizationSessionSecret")
        if not isinstance(recovery_envelope, str) or not recovery_envelope:
            raise MetadataConflict(
                "authorization Secret provider authority is absent",
                reason=ConflictReason.AUTHORIZATION_SECRET_ENVELOPE_ABSENT,
            )
        files = await self._cell.read_authorization_session_bundle(owned)
        if files is None:
            raise MetadataConflict(
                "authorization session bundle is absent",
                reason=ConflictReason.AUTHORIZATION_SESSION_BUNDLE_ABSENT,
            )
        current = int(self._now())
        source = inspect_hosted_authorization_bundle(
            files,
            expected_cell_id=owned.subject_id,
            expected_logical_vault_id=owned.tenant_id,
            expected_replica_id=owned.resource_name + "-0",
            expected_software_version=None,
            expected_schema_version=AUTHORIZATION_SESSION_SCHEMA_VERSION,
            expected_recovery_envelope=recovery_envelope,
            now=current,
            _require_fresh=False,
        )
        runtime_attestation = None
        if require_runtime_attestation:
            if not runtime_credential or not runtime_protocol_version:
                raise MetadataConflict(
                    "runtime attestation authority is unavailable",
                    reason=ConflictReason.RUNTIME_ATTESTATION_AUTHORITY_UNAVAILABLE,
                )
            runtime_attestation = await self._runtime.attest_authorization_session_membership(
                owned,
                credential=runtime_credential,
                protocol_version=runtime_protocol_version,
                target_epoch=source.epoch + 1,
                previous_epoch_digest=source.membership_digest,
                ttl_seconds=DEFAULT_ATTESTATION_TTL_SECONDS,
            )
        successor = transition_hosted_authorization_bundle(
            files,
            expected_cell_id=owned.subject_id,
            expected_logical_vault_id=owned.tenant_id,
            expected_replica_id=owned.resource_name + "-0",
            expected_software_version=None,
            expected_schema_version=AUTHORIZATION_SESSION_SCHEMA_VERSION,
            expected_recovery_envelope=recovery_envelope,
            target_state=target_state,
            target_no_in_flight=target_no_in_flight,
            target_software_version=target_software_version,
            now=current,
            runtime_attestation=runtime_attestation,
        )
        if successor.revision != source.revision:
            await self._cell.write_authorization_session_bundle(
                owned,
                successor.files,
                recovery_envelope=recovery_envelope,
                membership_epoch=successor.epoch,
                membership_digest=successor.membership_digest,
                revision=successor.revision,
                expected_revision=source.revision,
            )
        return successor.revision

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
            raise MetadataConflict(
                "provider recovery envelope set did not authenticate",
                reason=ConflictReason.PROVIDER_RECOVERY_ENVELOPE_UNAUTHENTICATED,
            ) from error
        self._recovery_envelopes[self._key(current)] = recovery_envelopes
        self._operation_ids[self._key(current)] = context.operation_id
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

    async def _require_capacity_reservation(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
    ) -> CapacityReservationClass:
        mode = request.get("provisionMode")
        if mode == "serve":
            reservation_class = CapacityReservationClass.USER
        elif mode == "restore-candidate":
            reservation_class = CapacityReservationClass.RECOVERY
        else:
            raise MetadataConflict(
                "provision mode is invalid",
                reason=ConflictReason.PROVISION_MODE_INVALID,
            )
        internal_operation_id = self._operation_ids.get(self._key(metadata))
        if internal_operation_id is None:
            raise MetadataConflict(
                "capacity reservation operation is unavailable",
                reason=ConflictReason.CAPACITY_RESERVATION_OPERATION_UNAVAILABLE,
            )
        try:
            await self._capacity.require_active(
                internal_operation_id=internal_operation_id,
                tenant_id=metadata.tenant_id,
                cell_id=metadata.subject_id,
                provider_operation_id=metadata.operation_id,
                fence_generation=metadata.fence_generation,
                reservation_class=reservation_class,
            )
        except CapacityError as error:
            raise MetadataConflict(
                "exact active capacity reservation is absent",
                reason=ConflictReason.ACTIVE_CAPACITY_RESERVATION_ABSENT,
            ) from error
        return reservation_class

    async def ensure_namespace(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
    ) -> None:
        reservation_class = await self._require_capacity_reservation(metadata, request)
        envelopes = self._recovery_envelopes[self._key(metadata)]
        helm_values = _fixed_helm_values(self._owner(metadata), request, self._config)
        await self._registry.ensure_namespace(
            metadata,
            envelopes["namespace"],
            (
                "serve"
                if reservation_class is CapacityReservationClass.USER
                else "restore-candidate"
            ),
            helm_values,
        )
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
        await self._require_capacity_reservation(metadata, request)
        owned = self._owner(metadata)
        revision = await self._authorization_session_revision(metadata, request)
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
        desired = dict(values)
        desired["authorizationSessionRevision"] = revision
        await self._helm.ensure_release(owned, desired)
        await self._refresh(metadata)

    async def provision_retarget_required(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        config: LifecycleConfig,
    ) -> bool:
        if request.get("provisionMode") != "serve":
            return False
        desired = _fixed_helm_values(self._owner(metadata), request, config)
        current = await self._helm.current_release_values(self._owner(metadata))
        for key in ("image", "expectedRelease", "expectedProtocol"):
            if not isinstance(current.get(key), str):
                raise MetadataConflict(
                    "current Helm runtime selection is invalid",
                    reason=ConflictReason.CURRENT_HELM_RUNTIME_SELECTION_INVALID,
                )
        changed = any(
            current[key] != desired[key] for key in ("image", "expectedRelease", "expectedProtocol")
        )
        operation_digest = hashlib.sha256(metadata.operation_id.encode("utf-8")).hexdigest()
        upgrade = current.get("runtimeUpgrade")
        transition_owned = (
            isinstance(upgrade, dict)
            and upgrade.get("schemaVersion") == 1
            and upgrade.get("operationDigest") == operation_digest
            and isinstance(upgrade.get("priorRevision"), int)
            and not isinstance(upgrade.get("priorRevision"), bool)
            and upgrade["priorRevision"] >= 1
        )
        if not changed and not transition_owned:
            return False

        # A retarget is genuinely indicated, so an operator must have committed
        # one. The recovery receipt is written by exactly one caller -- the
        # operator-driven retarget recovery command in `operation_recovery` --
        # so requiring it here is what proves this move was authorised.
        #
        # It is deliberately checked AFTER the question above is answered. A
        # cell being provisioned for the first time has never been retargeted
        # and so can never hold a receipt; demanding one before asking whether
        # a retarget is needed made "no" unreachable and killed every first
        # provision at `volume-owned`.
        internal_operation_id = self._operation_ids.get(self._key(metadata))
        if internal_operation_id is None:
            raise MetadataConflict(
                "retargeted provision operation is unavailable",
                reason=ConflictReason.RETARGET_OPERATION_UNAVAILABLE,
            )
        operation = await self._repository.get_by_id(internal_operation_id)
        receipt = operation.progress.get("_runtime_retarget_recovery_v1") if operation else None
        receipt_keys = {
            "schema",
            "preflight_sha256",
            "source_request_sha256",
            "target_request_sha256",
            "target_runtime_sha256",
            "helper_source_sha256",
            "claim_generation",
            "committed_at",
        }
        target_runtime = request.get("runtimeTarget")
        target_runtime_sha256 = (
            hashlib.sha256(
                json.dumps(
                    target_runtime, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode("utf-8")
            ).hexdigest()
            if isinstance(target_runtime, dict)
            else ""
        )
        if (
            operation is None
            or operation.action.value != "provision"
            or operation.external_operation_id != metadata.operation_id
            or operation.tenant_id != metadata.tenant_id
            or operation.cell_id != metadata.subject_id
            or operation.fence_generation != metadata.fence_generation
            or not isinstance(receipt, dict)
            or set(receipt) != receipt_keys
            or receipt.get("schema") != 1
            or receipt.get("target_request_sha256") != operation.canonical_request_sha256
            or receipt.get("target_runtime_sha256") != target_runtime_sha256
            or receipt.get("source_request_sha256") == receipt.get("target_request_sha256")
            or not isinstance(receipt.get("claim_generation"), int)
            or receipt["claim_generation"] < 0
            or not isinstance(receipt.get("committed_at"), str)
            or any(
                not isinstance(receipt.get(key), str) or len(receipt[key]) != 64
                for key in receipt_keys
                if key.endswith("sha256")
            )
        ):
            raise MetadataConflict(
                "retargeted provision recovery receipt is invalid",
                reason=ConflictReason.RETARGET_RECOVERY_RECEIPT_INVALID,
            )
        snapshot = self._snapshot(metadata)
        if snapshot.runtime_admitted or snapshot.routes != (False, False):
            raise MetadataConflict(
                "retargeted provision is already admitted or routed",
                reason=ConflictReason.RETARGET_BLOCKED_BY_ADMITTED_OR_ROUTED_CELL,
            )
        if config.migration_mode not in {"binding-v1-to-v2", "state-root-v1"}:
            raise MetadataConflict(
                "retargeted provision requires a declared migration",
                reason=ConflictReason.RETARGET_REQUIRES_DECLARED_MIGRATION,
            )
        return True

    async def stop_stranded_provision(self, metadata: OpaqueProviderMetadata) -> None:
        snapshot = self._snapshot(metadata)
        if snapshot.runtime_admitted or snapshot.routes != (False, False):
            raise MetadataConflict(
                "retargeted provision is already admitted or routed",
                reason=ConflictReason.STRANDED_PROVISION_ADMITTED_OR_ROUTED,
            )
        if snapshot.runtime_desired_replicas != 0:
            await self._cell.scale(self._owner(metadata), 0)
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
            raise MetadataConflict(
                "cell storage initialization failed",
                reason=ConflictReason.CELL_STORAGE_INITIALIZATION_ALREADY_FAILED,
            )
        helm_request = request
        if not snapshot.init_complete:
            if snapshot.init_job_present:
                return False
            try:
                helm_request = self._helm_requests[self._key(metadata)]
            except KeyError as error:
                raise MetadataConflict(
                    "original Helm request was not authenticated",
                    reason=ConflictReason.ORIGINAL_HELM_REQUEST_UNAUTHENTICATED,
                ) from error
            values = _fixed_helm_values(self._owner(metadata), helm_request, config)
            values = await self._authorization_helm_values(metadata, helm_request, values)
            values["workloadMode"] = "initialize"
            await self._helm.ensure_release(self._owner(metadata), values)
            snapshot = await self._refresh(metadata)
            if snapshot.init_failed:
                raise MetadataConflict(
                    "cell storage initialization failed",
                    reason=ConflictReason.CELL_STORAGE_INITIALIZATION_FAILED,
                )
            if not snapshot.init_complete:
                return False
        values = _fixed_helm_values(self._owner(metadata), helm_request, config)
        values = await self._authorization_helm_values(metadata, helm_request, values)
        values["workloadMode"] = "serve"
        await self._helm.ensure_release(self._owner(metadata), values)
        return (await self._refresh(metadata)).serving

    async def health(
        self, metadata: OpaqueProviderMetadata, request: dict[str, Any], *, v2: bool
    ) -> HealthObservation:
        target = self._config.runtime_target_for(request, v2=v2)
        return await self._runtime.health(
            self._owner(metadata),
            credential=str(request["serviceCredential"]),
            protocol_version=target["protocolVersion"],
            config=self._config,
            expected_release=target["releaseVersion"],
            expected_worker_policy=dict(request["workerPolicy"]),
            require_runtime_identity=v2,
            expected_contract_digest=target["gatewayContractDigest"],
        )

    async def admit_runtime(self, metadata: OpaqueProviderMetadata) -> None:
        await self._registry.mark_runtime_admitted(self._owner(metadata))
        await self._refresh(metadata)

    def runtime_admitted(self, metadata: OpaqueProviderMetadata) -> bool:
        return self._snapshot(metadata).runtime_admitted

    async def enable_routes(
        self, metadata: OpaqueProviderMetadata, request: dict[str, Any]
    ) -> None:
        owner = self._owner(metadata)
        if self._key(metadata) not in self._helm_requests:
            raise MetadataConflict(
                "original Helm request was not authenticated",
                reason=ConflictReason.ORIGINAL_HELM_REQUEST_UNAUTHENTICATED,
            )
        values = (
            self._rollforward_helm_values(metadata, request, self._config)
            if "compatibilityDigest" in request
            else _fixed_helm_values(owner, request, self._config)
        )
        values = await self._authorization_helm_values(metadata, request, values)
        values["workloadMode"] = "serve"
        values["routes"]["enabled"] = True
        if "compatibilityDigest" in request:
            await self._helm.transition_release(
                owner,
                values,
                operation_id=metadata.operation_id,
            )
        else:
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
        runtime_request = request
        if "compatibilityDigest" in request:
            try:
                runtime_request = self._helm_requests[self._key(metadata)]
            except KeyError as error:
                raise MetadataConflict(
                    "original runtime identity is unavailable",
                    reason=ConflictReason.ORIGINAL_RUNTIME_IDENTITY_UNAVAILABLE,
                ) from error
        target = runtime_identity(runtime_request)
        snapshot = await self._runtime.quiesce(
            self._owner(metadata),
            credential=str(request["serviceCredential"]),
            protocol_version=target["protocolVersion"],
            operation_id=operation_id,
        )
        if (
            not isinstance(snapshot, dict)
            or set(snapshot)
            != {
                "phase",
                "active_reads",
                "active_mutations",
                "active_transfers",
                "reason_code",
            }
            or snapshot.get("phase") != "quiesced"
            or any(
                type(snapshot.get(name)) is not int or snapshot[name] != 0
                for name in ("active_reads", "active_mutations", "active_transfers")
            )
            or not isinstance(snapshot.get("reason_code"), str)
            or not snapshot["reason_code"]
        ):
            raise MetadataConflict(
                "runtime did not acknowledge a complete authorization drain",
                reason=ConflictReason.RUNTIME_DRAIN_NOT_ACKNOWLEDGED,
            )
        await self._transition_authorization_session_membership(
            metadata,
            target_state="DRAINING",
            target_no_in_flight=True,
            require_runtime_attestation=True,
            runtime_credential=str(request["serviceCredential"]),
            runtime_protocol_version=str(target["protocolVersion"]),
        )

    async def scale(self, metadata: OpaqueProviderMetadata, replicas: int) -> None:
        if replicas == 1:
            revision = await self._transition_authorization_session_membership(
                metadata,
                target_state="SERVING",
                target_no_in_flight=False,
            )
            await self._cell.stage_authorization_session_revision(
                self._owner(metadata),
                revision,
            )
        await self._cell.scale(self._owner(metadata), replicas)

    async def runtime_stopped(self, metadata: OpaqueProviderMetadata) -> bool:
        snapshot = await self._refresh(metadata)
        return snapshot.runtime_desired_replicas == 0 and snapshot.runtime_pods == 0

    async def resume(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        operation_id: str,
    ) -> None:
        target = runtime_identity(request)
        await self._runtime.resume(
            self._owner(metadata),
            credential=str(request["serviceCredential"]),
            protocol_version=target["protocolVersion"],
            operation_id=operation_id,
        )

    def _rollforward_helm_values(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        config: LifecycleConfig,
    ) -> dict[str, Any]:
        try:
            original = self._helm_requests[self._key(metadata)]
        except KeyError as error:
            raise MetadataConflict(
                "original Helm request was not authenticated",
                reason=ConflictReason.ORIGINAL_HELM_REQUEST_UNAUTHENTICATED,
            ) from error
        merged = dict(original)
        merged["workerPolicy"] = dict(request["workerPolicy"])
        merged["runtimeTarget"] = dict(request["runtimeTarget"])
        return _fixed_helm_values(self._owner(metadata), merged, config)

    async def canonical_vault_fingerprint(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        operation_id: str,
        *,
        phase: Literal["before", "after"],
    ) -> str:
        del request
        if self._fingerprint is None:
            raise MetadataConflict(
                "vault fingerprint adapter is unavailable",
                reason=ConflictReason.VAULT_FINGERPRINT_ADAPTER_UNAVAILABLE,
            )
        try:
            envelope = self._recovery_envelopes[self._key(metadata)]["initJob"]
        except KeyError as error:
            raise MetadataConflict(
                "vault fingerprint provider authority is absent",
                reason=ConflictReason.VAULT_FINGERPRINT_AUTHORITY_ABSENT,
            ) from error
        return await self._fingerprint.fingerprint(
            metadata,
            operation_id=operation_id,
            phase=phase,
            recovery_envelope=envelope,
        )

    async def run_runtime_migration(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        config: LifecycleConfig,
        operation_id: str,
    ) -> None:
        if config.migration_mode not in {"binding-v1-to-v2", "state-root-v1"}:
            raise MetadataConflict(
                "runtime migration was not declared by the deployment lock",
                reason=ConflictReason.RUNTIME_MIGRATION_NOT_DECLARED,
            )
        values = self._rollforward_helm_values(metadata, request, config)
        values = await self._authorization_helm_values(
            metadata, request, values, require_fresh=False
        )
        values["workloadMode"] = "migrate"
        values["routes"]["enabled"] = False
        migration_operation_id = (
            f"{operation_id}:runtime-migration:{canonical_request_sha256(request)}"
        )
        values["initOperationId"] = migration_operation_id
        values["initRequestId"] = _deterministic_uuid4(migration_operation_id)
        await self._helm.transition_release(
            self._owner(metadata),
            values,
            operation_id=operation_id,
        )
        await self._refresh(metadata)

    async def upgrade_runtime(
        self,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        config: LifecycleConfig,
        operation_id: str,
    ) -> None:
        target_release = runtime_identity(request)["releaseVersion"]
        revision = await self._transition_authorization_session_membership(
            metadata,
            target_state="SERVING",
            target_no_in_flight=False,
            target_software_version=target_release,
        )
        values = self._rollforward_helm_values(metadata, request, config)
        values["authorizationSessionRevision"] = revision
        values["workloadMode"] = "serve"
        values["routes"]["enabled"] = False
        await self._helm.transition_release(
            self._owner(metadata),
            values,
            operation_id=operation_id,
        )
        await self._refresh(metadata)

    async def rollback_runtime(
        self,
        metadata: OpaqueProviderMetadata,
        operation_id: str,
    ) -> None:
        await self._helm.rollback_release(
            self._owner(metadata),
            operation_id=operation_id,
        )
        try:
            original = self._helm_requests[self._key(metadata)]
        except KeyError as error:
            raise MetadataConflict(
                "original runtime identity is unavailable",
                reason=ConflictReason.ORIGINAL_RUNTIME_IDENTITY_UNAVAILABLE,
            ) from error
        revision = await self._transition_authorization_session_membership(
            metadata,
            target_state="SERVING",
            target_no_in_flight=False,
            target_software_version=runtime_identity(original)["releaseVersion"],
        )
        await self._cell.stage_authorization_session_revision(
            self._owner(metadata),
            revision,
        )
        await self._refresh(metadata)

    async def commit_runtime_upgrade(
        self,
        metadata: OpaqueProviderMetadata,
        operation_id: str,
    ) -> None:
        # The content-free marker is retained in Helm values as durable replay
        # evidence. A later operation has a distinct digest and captures the
        # then-current deployed revision as its own rollback point.
        del metadata, operation_id

    async def rollback_committed_runtime(
        self,
        metadata: OpaqueProviderMetadata,
        operation_id: str,
    ) -> None:
        await self._helm.rollback_release(
            self._owner(metadata),
            operation_id=operation_id,
            require_marker=True,
        )
        await self._refresh(metadata)

    async def _active_version(self, metadata: OpaqueProviderMetadata) -> str:
        _, annotations = await self._cell.read_credential_bundle(self._owner(metadata))
        version = annotations.get("exomem.io/active-credential-version")
        if not version:
            raise MetadataConflict(
                "active credential version metadata is absent",
                reason=ConflictReason.ACTIVE_CREDENTIAL_VERSION_ABSENT,
            )
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
        protocol_version: str,
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
            raise MetadataConflict(
                "active provider credential is absent",
                reason=ConflictReason.ACTIVE_PROVIDER_CREDENTIAL_ABSENT,
            )
        result = await self._runtime.operator(
            "credential",
            metadata,
            request,
            credential=active_credential,
            protocol_version=protocol_version,
        )
        revision = result.get("revision")
        if revision != expected + 1:
            raise MetadataConflict(
                "hosted credential revision did not advance exactly once",
                reason=ConflictReason.CREDENTIAL_REVISION_DID_NOT_ADVANCE,
            )
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
            raise MetadataConflict(
                "active credential does not match provider state",
                reason=ConflictReason.ACTIVE_CREDENTIAL_DOES_NOT_MATCH_PROVIDER,
            )
        if annotations.get("exomem.io/credential-phase") in {"staged", "proved", "promoted"}:
            if credentials.get(pending) != credential:
                raise MetadataConflict(
                    "pending credential version is immutable",
                    reason=ConflictReason.PENDING_CREDENTIAL_VERSION_IMMUTABLE,
                )
            return
        credentials[pending] = credential
        target = self._config.runtime_target_for(request, v2="runtimeTarget" in request)
        result = await self._credential_transition(
            owned,
            credentials=credentials,
            annotations=annotations,
            action="stage",
            operation_id=operation_id,
            version=pending,
            protocol_version=target["protocolVersion"],
        )
        if result.get("phase") != "staged" or result.get("pending_version") != pending:
            raise MetadataConflict(
                "hosted credential did not enter staged overlap",
                reason=ConflictReason.CREDENTIAL_DID_NOT_STAGE,
            )
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
        target = self._config.runtime_target_for(request, v2="runtimeTarget" in request)
        operator_request = {
            "request_id": _deterministic_uuid4(probe_operation),
            "operation_id": probe_operation,
            "cell_id": owned.subject_id,
            "vault_id": owned.tenant_id,
            "state_root": "/var/lib/exomem/state",
            "selected_credential_version": pending,
            "expected_release": target["releaseVersion"],
            "expected_protocol": target["protocolVersion"],
            "expected_worker_policy_digest": self._worker_policy_digest(request),
            "expected_revision": revision,
            "port": 8765,
        }
        result = await self._runtime.operator(
            "probe",
            owned,
            operator_request,
            credential=credential,
            protocol_version=target["protocolVersion"],
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
        target = self._config.runtime_target_for(request, v2="runtimeTarget" in request)
        if old_version == pending and set(credentials) == {pending}:
            return await self._runtime.credential_rejected(
                owned,
                credential=str(request["serviceCredential"]),
                protocol_version=target["protocolVersion"],
            )
        if old_version is None or pending not in credentials:
            raise MetadataConflict(
                "pending credential is absent",
                reason=ConflictReason.PENDING_CREDENTIAL_ABSENT,
            )
        phase = annotations.get("exomem.io/credential-phase")
        if phase == "proved":
            result = await self._credential_transition(
                owned,
                credentials=credentials,
                annotations=annotations,
                action="promote",
                operation_id=operation_id,
                version=pending,
                protocol_version=target["protocolVersion"],
            )
            if result.get("phase") != "promoted":
                raise MetadataConflict(
                    "hosted credential did not promote",
                    reason=ConflictReason.CREDENTIAL_DID_NOT_PROMOTE,
                )
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
                protocol_version=target["protocolVersion"],
            )
            if result.get("phase") != "stable" or result.get("active_version") != pending:
                raise MetadataConflict(
                    "hosted credential did not finalize",
                    reason=ConflictReason.CREDENTIAL_DID_NOT_FINALIZE,
                )
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
            protocol_version=target["protocolVersion"],
            config=self._config,
            expected_release=target["releaseVersion"],
            expected_worker_policy=dict(request["workerPolicy"]),
            expected_contract_digest=target["gatewayContractDigest"],
        )
        old_rejected = await self._runtime.credential_rejected(
            owned,
            credential=str(request["serviceCredential"]),
            protocol_version=target["protocolVersion"],
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
        target = runtime_identity(request)
        await self._runtime.seal(
            self._owner(metadata),
            credential=str(request["serviceCredential"]),
            protocol_version=target["protocolVersion"],
            operation_id=operation_id,
            created_at=created_at,
        )

    async def discard_candidate(self, metadata: OpaqueProviderMetadata) -> dict[str, bool]:
        raise MetadataConflict(
            "candidate deletion requires the durability deletion worker",
            reason=ConflictReason.CANDIDATE_DELETION_REQUIRES_DELETION_WORKER,
        )

    async def destroy_tenant_online(self, tenant_id: str) -> None:
        raise MetadataConflict(
            "tenant deletion requires the durability deletion worker",
            reason=ConflictReason.TENANT_DELETION_REQUIRES_DELETION_WORKER,
        )

    def retention_wait_seconds(self, tenant_id: str) -> int | None:
        raise MetadataConflict(
            "tenant deletion requires the durability deletion worker",
            reason=ConflictReason.TENANT_DELETION_REQUIRES_DELETION_WORKER,
        )

    async def destroy_expired_retention(self, tenant_id: str) -> None:
        raise MetadataConflict(
            "tenant deletion requires the durability deletion worker",
            reason=ConflictReason.TENANT_DELETION_REQUIRES_DELETION_WORKER,
        )

    def destruction_proof(self, tenant_id: str) -> dict[str, bool]:
        raise MetadataConflict(
            "tenant deletion requires the durability deletion worker",
            reason=ConflictReason.TENANT_DELETION_REQUIRES_DELETION_WORKER,
        )

    def provider_reference(self, metadata: OpaqueProviderMetadata) -> str:
        return "cell-" + _digest(metadata.tenant_id + ":" + metadata.subject_id, length=32)
