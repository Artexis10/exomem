"""One-shot production composition for user export and restore actions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select

from .adapters import HelmCliAdapter, KubernetesCellAdapter
from .config import load_hosted_release_manifest
from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .durability import ExportBackupWorkflow, ExportObjectService, RestoreWorkflow
from .durability_crypto import AesGcmKeyWrapper, ChunkedArchiveCipher
from .durability_driver import DurabilityActionDriver
from .durability_jobs import run_bounded_operation_batch
from .durability_repository import DurabilityRepository
from .durability_store import (
    B2DeletionObjectStore,
    B2PortableDeliveryStore,
    B2RestoreObjectStore,
    B2UploadOnlyObjectStore,
)
from .entrypoint import help_requested
from .kubernetes_restore import (
    CandidateRestoreBinding,
    KubernetesOfflineRestoreRuntime,
    RestoreJobFailed,
)
from .lifecycle import LifecycleConfig, OpaqueProviderMetadata, _fixed_helm_values
from .live import KubernetesProviderRegistry
from .logging import configure_content_free_logging
from .models import Operation, OperationAction, OperationState, TenantFence
from .production_durability import (
    B2PortableArchiveStager,
    CandidateBoundRestoreWorkflow,
    DynamicKubernetesRestoreRuntime,
    RefreshingExportWorkflow,
    build_durability_operation_worker,
)
from .provider_identity import (
    ProviderRecoveryIdentityCodec,
    ProviderRecoveryIdentityVerifier,
    ProviderReference,
)
from .repository import OperationRepository
from .vault_backup import (
    HttpPortableRuntimePort,
    KubernetesBackupTargetSource,
    LiveBackupTarget,
    LiveBackupTargetRegistry,
    VerifiedRouteMaintenancePort,
)

_IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_PRINCIPAL_SCOPE = (
    base64.urlsafe_b64encode(hashlib.sha256(b"exomem-provisioner-private-cell-control").digest())
    .rstrip(b"=")
    .decode("ascii")
)


class DurabilityActionSettings(BaseSettings):
    """Exact environment held only by the short-lived durability CronJob."""

    model_config = SettingsConfigDict(
        env_prefix="EXOMEM_DURABILITY_",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    database_url: SecretStr = Field(min_length=1, max_length=4096)
    database_schema: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    database_role: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    envelope_key: SecretStr = Field(min_length=32, max_length=4096)
    b2_endpoint_url: str = Field(min_length=8, max_length=2048)
    b2_region: str = Field(pattern=r"^[a-z0-9-]{2,64}$")
    recovery_bucket: str = Field(pattern=r"^[a-z0-9-]{6,63}$")
    user_export_bucket: str = Field(pattern=r"^[a-z0-9-]{6,63}$")
    recovery_restore_key_id: SecretStr = Field(min_length=1, max_length=4096)
    recovery_restore_key: SecretStr = Field(min_length=1, max_length=4096)
    user_export_upload_key_id: SecretStr = Field(min_length=1, max_length=4096)
    user_export_upload_key: SecretStr = Field(min_length=1, max_length=4096)
    user_export_restore_key_id: SecretStr = Field(min_length=1, max_length=4096)
    user_export_restore_key: SecretStr = Field(min_length=1, max_length=4096)
    user_export_delete_key_id: SecretStr = Field(min_length=1, max_length=4096)
    user_export_delete_key: SecretStr = Field(min_length=1, max_length=4096)
    user_export_delivery_key_id: SecretStr = Field(min_length=1, max_length=4096)
    user_export_delivery_key: SecretStr = Field(min_length=1, max_length=4096)
    provider_recovery_signing_key: SecretStr = Field(
        min_length=40,
        max_length=128,
        validation_alias="EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY",
    )
    release_manifest_path: Path = Field(validation_alias="EXOMEM_PROVISIONER_RELEASE_MANIFEST_PATH")
    cell_chart_path: str = Field(
        min_length=1,
        max_length=4096,
        validation_alias="EXOMEM_PROVISIONER_CELL_CHART_PATH",
    )
    cell_chart_version: str = Field(
        min_length=1,
        max_length=64,
        validation_alias="EXOMEM_PROVISIONER_CELL_CHART_VERSION",
    )
    helm_binary: str = Field(
        min_length=1,
        max_length=4096,
        validation_alias="EXOMEM_PROVISIONER_HELM_BINARY",
    )
    helm_version: str = Field(
        min_length=1,
        max_length=64,
        validation_alias="EXOMEM_PROVISIONER_HELM_VERSION",
    )
    control_hostname: str = Field(
        min_length=1,
        max_length=253,
        validation_alias="EXOMEM_PROVISIONER_CONTROL_HOSTNAME",
    )
    transfer_hostname: str = Field(
        min_length=1,
        max_length=253,
        validation_alias="EXOMEM_PROVISIONER_TRANSFER_HOSTNAME",
    )
    browser_origin: str = Field(
        min_length=1,
        max_length=255,
        validation_alias="EXOMEM_PROVISIONER_BROWSER_ORIGIN",
    )
    location: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{1,31}$",
        validation_alias="EXOMEM_PROVISIONER_LOCATION",
    )
    provisioner_image: str = Field(pattern=_IMAGE_DIGEST.pattern)
    scratch_root: Path
    worker_id: str = Field(
        default="durability-actions",
        pattern=r"^[A-Za-z0-9_.:-]{1,128}$",
    )
    max_operations: int = Field(default=1, ge=1, le=25)

    @field_validator("database_url")
    @classmethod
    def require_postgres(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("durability actions require PostgreSQL")
        return value

    @field_validator("b2_endpoint_url")
    @classmethod
    def require_https_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("B2 endpoint must be an HTTPS origin")
        return value.rstrip("/")

    @field_validator("release_manifest_path", "scratch_root")
    @classmethod
    def require_absolute_paths(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("durability paths must be absolute")
        return value

    @field_validator("control_hostname", "transfer_hostname")
    @classmethod
    def require_canonical_hostname(cls, value: str) -> str:
        parsed = urlsplit("https://" + value)
        if parsed.hostname != value or parsed.port is not None or value != value.lower():
            raise ValueError("hostnames must be canonical DNS names")
        return value

    @field_validator("browser_origin")
    @classmethod
    def require_https_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("browser origin must be a canonical HTTPS origin")
        return value

    @model_validator(mode="after")
    def validate_boundaries(self) -> DurabilityActionSettings:
        parsed = urlsplit(self.database_url.get_secret_value())
        if unquote(parsed.username or "") != self.database_role:
            raise ValueError("durability database URL must use its declared role")
        if self.recovery_bucket == self.user_export_bucket:
            raise ValueError("recovery and user-export buckets must be distinct")
        ProviderRecoveryIdentityCodec.from_encoded_seed(
            self.provider_recovery_signing_key.get_secret_value()
        )
        return self


class RestoreWorkflowRouter:
    """Select the exact bucket capability from the opaque reference kind."""

    def __init__(self, *, recovery: Any, user_export: Any) -> None:
        self._recovery = recovery
        self._user_export = user_export

    async def run(self, run: Any, **arguments: Any) -> dict[str, object]:
        reference = arguments.get("source_reference")
        if not isinstance(reference, str):
            raise ValueError("restore source reference is required")
        if reference.startswith("recovery_"):
            workflow = self._recovery
        elif reference.startswith("export_"):
            workflow = self._user_export
        else:
            raise ValueError("restore source reference kind is invalid")
        return await workflow.run(run, **arguments)


class CandidateController(Protocol):
    async def restart_and_verify(self) -> None: ...

    async def publish(self) -> None: ...


class CandidateExportCheck(Protocol):
    async def verify_export(self, cell_id: str, operation_id: str) -> None: ...


class HttpCandidateProductProbe:
    """Run genuine private product calls, restart, then publish routes."""

    PRODUCT_CHECKS = frozenset({"capture", "recall", "review", "export"})
    FINAL_CHECKS = frozenset({"restart", "candidateIdentity"})
    REQUIRED = PRODUCT_CHECKS | FINAL_CHECKS

    def __init__(
        self,
        *,
        requester: Any,
        metadata: OpaqueProviderMetadata,
        credential: str,
        credential_version: str,
        protocol_version: str,
        release_version: str,
        worker_policy_digest: str,
        operation_id: str,
        controller: CandidateController,
        export_check: CandidateExportCheck,
    ) -> None:
        self._request = requester
        self._metadata = metadata
        self._credential = credential
        self._credential_version = credential_version
        self._protocol = protocol_version
        self._release = release_version
        self._worker_policy_digest = worker_policy_digest
        self._operation_id = operation_id
        self._controller = controller
        self._export_check = export_check

    async def authenticated_readiness(self, cell_id: str) -> bool:
        if cell_id != self._metadata.subject_id:
            return False
        data = await self._call("GET", "ready")
        return isinstance(data, dict) and data == {
            "cell_id": self._metadata.subject_id,
            "vault_id": self._metadata.tenant_id,
            "exomem_release": self._release,
            "hosted_protocol": self._protocol,
            "authenticated_credential_version": self._credential_version,
            "security_revision": 1,
            "service_authenticated": True,
            "mutation_authority": True,
            "admission_phase": "ready",
            "read_admission": True,
            "write_admission": True,
            "worker_policy_digest": self._worker_policy_digest,
        }

    async def product_checks(self, cell_id: str) -> dict[str, bool]:
        if not await self.authenticated_readiness(cell_id):
            raise RestoreJobFailed("candidate identity readiness differs")
        marker = "restore-self-test-" + hashlib.sha256(self._operation_id.encode()).hexdigest()[:20]
        nonce = uuid.uuid4().hex
        remembered = await self._call(
            "POST",
            "command/remember",
            body={
                "content": f"## Claim\n\n{marker}\n\n## Why it holds\n\nRestore product gate.",
                "title": marker,
                "slug": marker,
                "note_type": "insight",
                "suggestions": False,
            },
            idempotency_key=f"{self._operation_id}:capture:{nonce}",
        )
        if not isinstance(remembered, dict) or not isinstance(remembered.get("path"), str):
            raise RestoreJobFailed("candidate capture proof is invalid")
        recalled = await self._call(
            "POST",
            "command/ask_memory",
            body={
                "query": marker,
                "scope": "vault",
                "mode": "keyword",
                "graph": False,
                "limit": 5,
            },
        )
        if marker not in json.dumps(recalled, sort_keys=True, default=str):
            raise RestoreJobFailed("candidate recall did not return the captured proof")
        reviewed = await self._call(
            "POST",
            "command/review_memory",
            body={"mode": "attention", "limit": 1},
        )
        if not isinstance(reviewed, dict):
            raise RestoreJobFailed("candidate review proof is invalid")
        await self._export_check.verify_export(cell_id, self._operation_id + ":product-export")
        return {name: True for name in self.PRODUCT_CHECKS}

    async def finalize_candidate(self, cell_id: str) -> dict[str, bool]:
        await self._controller.restart_and_verify()
        if not await self.authenticated_readiness(cell_id):
            raise RestoreJobFailed("candidate readiness failed after restart")
        await self._controller.publish()
        return {name: True for name in self.FINAL_CHECKS}

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        resource = self._metadata.resource_name
        url = (
            f"http://{resource}.{resource}.svc.cluster.local:8765/private/exomem/v1/"
            + path.lstrip("/")
        )
        headers = {
            "Authorization": f"Bearer {self._credential}",
            "X-Exomem-Cell-Id": self._metadata.subject_id,
            "X-Exomem-Protocol-Version": self._protocol,
            "X-Exomem-Request-Id": str(uuid.uuid4()),
            "X-Exomem-Principal-Scope": _PRINCIPAL_SCOPE,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        response = await self._request(method, url, headers=headers, json=body)
        if response.status_code != 200:
            raise RestoreJobFailed("candidate product request failed")
        try:
            envelope = response.json()
        except Exception as error:
            raise RestoreJobFailed("candidate product response is invalid") from error
        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            raise RestoreJobFailed("candidate product response is invalid")
        return envelope.get("data")


class HttpCandidateExportCheck:
    def __init__(self, runtime: HttpPortableRuntimePort) -> None:
        self._runtime = runtime

    async def verify_export(self, cell_id: str, operation_id: str) -> None:
        await self._runtime.quiesce(cell_id, operation_id, routing_stopped=True)
        failure: BaseException | None = None
        try:
            archive = await self._runtime.portable_export(cell_id, operation_id)
            if (
                not archive.archive_path.is_file()
                or archive.archive_size != archive.archive_path.stat().st_size
                or archive.source_cell_id != cell_id
                or archive.hosted_state_included
            ):
                raise RestoreJobFailed(
                    "candidate portable export proof differs or contains hosted state"
                )
        except BaseException as error:  # noqa: BLE001 - release must run for cancellation too
            failure = error
        try:
            await self._runtime.release(cell_id, operation_id)
        except BaseException:
            if failure is None:
                raise
        if failure is not None:
            raise failure


class HelmRestoreCandidateController:
    """Reconcile one authenticated restore-only release through private serving."""

    def __init__(
        self,
        *,
        metadata: OpaqueProviderMetadata,
        request: dict[str, Any],
        config: LifecycleConfig,
        identity_verifier: ProviderRecoveryIdentityVerifier,
        helm: HelmCliAdapter,
        core_api: Any,
        apps_api: Any,
        custom_objects: Any,
        poll_seconds: float = 1.0,
        timeout_polls: int = 300,
    ) -> None:
        if request.get("provisionMode") != "restore-candidate":
            raise ValueError("candidate controller requires restore-only provisioning")
        self._metadata = metadata
        self._request = request
        self._config = config
        self._verifier = identity_verifier
        self._helm = helm
        self._core = core_api
        self._apps = apps_api
        self._custom = custom_objects
        self._poll_seconds = poll_seconds
        self._timeout_polls = timeout_polls

    async def ensure_offline(self, binding: CandidateRestoreBinding) -> None:
        self._require_binding(binding)
        await self._helm.ensure_release(self._metadata, self._values("restore", routes=False))
        for api, method, name, provider, reference in (
            (
                self._apps,
                "read_namespaced_stateful_set",
                self._metadata.resource_name,
                "kubernetes",
                ProviderReference.kubernetes(
                    provider="kubernetes",
                    api_version="apps/v1",
                    kind="StatefulSet",
                    namespace=self._metadata.resource_name,
                    name=self._metadata.resource_name,
                ),
            ),
            (
                self._core,
                "read_namespaced_service",
                self._metadata.resource_name,
                "kubernetes",
                ProviderReference.kubernetes(
                    provider="kubernetes",
                    api_version="v1",
                    kind="Service",
                    namespace=self._metadata.resource_name,
                    name=self._metadata.resource_name,
                ),
            ),
        ):
            if await self._exists(api, method, name, provider=provider, reference=reference):
                raise RestoreJobFailed("restore candidate remained online")
        claim = await self._api_call(
            self._core,
            "read_namespaced_persistent_volume_claim",
            self._metadata.resource_name + "-data",
            self._metadata.resource_name,
        )
        if getattr(getattr(claim, "status", None), "phase", None) != "Bound":
            raise RestoreJobFailed("restore candidate PVC is not bound")
        page = await self._api_call(
            self._custom,
            "list_namespaced_custom_object",
            group="traefik.io",
            version="v1alpha1",
            namespace=self._metadata.resource_name,
            plural="ingressroutes",
        )
        if page.get("items"):
            raise RestoreJobFailed("restore candidate routes remained open")

    async def promote(self, binding: CandidateRestoreBinding) -> None:
        self._require_binding(binding)
        await self._helm.ensure_release(self._metadata, self._values("serve", routes=False))
        stateful_set = await self._api_call(
            self._apps,
            "read_namespaced_stateful_set",
            self._metadata.resource_name,
            self._metadata.resource_name,
        )
        self._require_owned(
            stateful_set,
            provider="kubernetes",
            reference=ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="apps/v1",
                kind="StatefulSet",
                namespace=self._metadata.resource_name,
                name=self._metadata.resource_name,
            ),
        )
        if int(getattr(getattr(stateful_set, "spec", None), "replicas", 0) or 0) != 1:
            raise RestoreJobFailed("promoted candidate replica contract differs")

    async def restart_and_verify(self) -> None:
        selector = (
            f"app.kubernetes.io/name=exomem-cell,exomem.io/cell={self._metadata.resource_name}"
        )
        before = await self._api_call(
            self._core,
            "list_namespaced_pod",
            self._metadata.resource_name,
            label_selector=selector,
        )
        ready = [pod for pod in before.items if self._pod_ready(pod)]
        if len(ready) != 1:
            raise RestoreJobFailed("candidate has no unique ready pod before restart")
        old_uid = str(ready[0].metadata.uid)
        await self._api_call(
            self._core,
            "delete_namespaced_pod",
            ready[0].metadata.name,
            self._metadata.resource_name,
            body={"gracePeriodSeconds": 30, "propagationPolicy": "Foreground"},
        )
        for _ in range(self._timeout_polls):
            page = await self._api_call(
                self._core,
                "list_namespaced_pod",
                self._metadata.resource_name,
                label_selector=selector,
            )
            replacements = [
                pod
                for pod in page.items
                if str(getattr(pod.metadata, "uid", "")) != old_uid and self._pod_ready(pod)
            ]
            if len(replacements) == 1:
                return
            await asyncio.sleep(self._poll_seconds)
        raise RestoreJobFailed("candidate did not become ready after a real restart")

    async def publish(self) -> None:
        await self._helm.ensure_release(self._metadata, self._values("serve", routes=True))
        for plural, kind, name in (
            ("middlewares", "Middleware", self._metadata.resource_name + "-strip-cell"),
            ("ingressroutes", "IngressRoute", self._metadata.resource_name + "-control"),
            ("ingressroutes", "IngressRoute", self._metadata.resource_name + "-transfer"),
        ):
            resource = await self._api_call(
                self._custom,
                "get_namespaced_custom_object",
                group="traefik.io",
                version="v1alpha1",
                namespace=self._metadata.resource_name,
                plural=plural,
                name=name,
            )
            self._require_owned(
                resource,
                provider="traefik",
                reference=ProviderReference.kubernetes(
                    provider="traefik",
                    api_version="traefik.io/v1alpha1",
                    kind=kind,
                    namespace=self._metadata.resource_name,
                    name=name,
                ),
            )

    def _values(self, mode: str, *, routes: bool) -> dict[str, Any]:
        values = _fixed_helm_values(self._metadata, self._request, self._config)
        values["workloadMode"] = mode
        values["routes"]["enabled"] = routes
        return values

    def _require_binding(self, binding: CandidateRestoreBinding) -> None:
        if (
            binding.namespace != self._metadata.resource_name
            or binding.cell_id != self._metadata.subject_id
            or binding.tenant_id != self._metadata.tenant_id
            or binding.target_pvc != self._metadata.resource_name + "-data"
        ):
            raise RestoreJobFailed("restore binding differs from authenticated candidate")

    def _require_owned(
        self,
        resource: Any,
        *,
        provider: str,
        reference: str,
    ) -> None:
        if isinstance(resource, dict):
            annotations = resource.get("metadata", {}).get("annotations", {})
        else:
            annotations = getattr(getattr(resource, "metadata", None), "annotations", {}) or {}
        normalized = {str(key): str(value) for key, value in annotations.items()}
        OpaqueProviderMetadata.from_kubernetes_annotations(normalized).require_same(self._metadata)
        self._verifier.authenticate(
            normalized.get("exomem.io/recovery-envelope", ""),
            provider=provider,
            provider_reference=reference,
            tenant_id=self._metadata.tenant_id,
            cell_id=self._metadata.subject_id,
            operation_id=self._metadata.operation_id,
            fence_generation=self._metadata.fence_generation,
        )

    async def _exists(
        self,
        api: Any,
        method: str,
        name: str,
        *,
        provider: str,
        reference: str,
    ) -> bool:
        try:
            resource = await self._api_call(
                api,
                method,
                name,
                self._metadata.resource_name,
            )
        except Exception as error:
            if getattr(error, "status", None) == 404:
                return False
            raise
        self._require_owned(resource, provider=provider, reference=reference)
        return True

    @staticmethod
    def _pod_ready(pod: Any) -> bool:
        phase = getattr(getattr(pod, "status", None), "phase", None)
        conditions = getattr(getattr(pod, "status", None), "conditions", ()) or ()
        return phase == "Running" and any(
            getattr(condition, "type", None) == "Ready"
            and getattr(condition, "status", None) == "True"
            for condition in conditions
        )

    @staticmethod
    async def _api_call(api: Any, method: str, *args: Any, **kwargs: Any) -> Any:
        import inspect

        call = getattr(api, method)
        if inspect.iscoroutinefunction(call):
            return await call(*args, **kwargs)
        return await asyncio.to_thread(call, *args, **kwargs)


@dataclass(frozen=True, slots=True)
class _CandidateContext:
    metadata: OpaqueProviderMetadata
    request: dict[str, Any]
    credential: str
    credential_version: str


class KubernetesRestoreCandidateResolver:
    """Authenticate one PVC-only candidate and construct its exact restore adapter."""

    def __init__(
        self,
        *,
        sessions: Any,
        operations: OperationRepository,
        core_api: Any,
        apps_api: Any,
        batch_api: Any,
        networking_api: Any,
        custom_objects: Any,
        identity_verifier: ProviderRecoveryIdentityVerifier,
        helm: HelmCliAdapter,
        lifecycle_config: LifecycleConfig,
        archive_stager: B2PortableArchiveStager,
        http: Any,
        scratch_root: Path,
        runtime_image: str,
        provisioner_image: str,
    ) -> None:
        self._sessions = sessions
        self._operations = operations
        self._core = core_api
        self._apps = apps_api
        self._batch = batch_api
        self._networking = networking_api
        self._custom = custom_objects
        self._verifier = identity_verifier
        self._helm = helm
        self._config = lifecycle_config
        self._stager = archive_stager
        self._http = http
        self._scratch = scratch_root
        self._runtime_image = runtime_image
        self._provisioner_image = provisioner_image

    async def resolve(self, candidate_cell_id: str, *, source_vault_id: str):
        context = await self._context(candidate_cell_id, source_vault_id=source_vault_id)
        metadata = context.metadata
        controller = HelmRestoreCandidateController(
            metadata=metadata,
            request=context.request,
            config=self._config,
            identity_verifier=self._verifier,
            helm=self._helm,
            core_api=self._core,
            apps_api=self._apps,
            custom_objects=self._custom,
        )
        registry = LiveBackupTargetRegistry()
        registry.replace(
            {
                metadata.subject_id: LiveBackupTarget(
                    metadata=metadata,
                    credential=context.credential,
                    credential_version=context.credential_version,
                    protocol_version=self._config.protocol_version,
                    release_version=self._config.release_version,
                    browser_origin=self._config.browser_origin,
                    control_hostname=self._config.control_hostname,
                    transfer_hostname=self._config.transfer_hostname,
                    routes=(),
                )
            }
        )
        worker_digest = hashlib.sha256(
            json.dumps(
                context.request["workerPolicy"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        probe = HttpCandidateProductProbe(
            requester=self._http.request,
            metadata=metadata,
            credential=context.credential,
            credential_version=context.credential_version,
            protocol_version=self._config.protocol_version,
            release_version=self._config.release_version,
            worker_policy_digest=worker_digest,
            operation_id=str(context.request["operationId"]),
            controller=controller,
            export_check=HttpCandidateExportCheck(
                HttpPortableRuntimePort(
                    http=self._http,
                    registry=registry,
                    scratch_root=self._scratch,
                )
            ),
        )
        binding = CandidateRestoreBinding(
            namespace=metadata.resource_name,
            service_account=metadata.resource_name,
            target_pvc=metadata.resource_name + "-data",
            credential_secret="exomem-cell-credentials",
            tenant_id=metadata.tenant_id,
            cell_id=metadata.subject_id,
            source_vault_id=source_vault_id,
            target_vault_id=metadata.tenant_id,
            target_vault_root="/var/lib/exomem/vault",
            target_state_root="/var/lib/exomem/state",
            target_log_root="/var/lib/exomem/logs",
            runtime_uid=10001,
            runtime_gid=10001,
            active_credential_version=context.credential_version,
            expected_protocol=self._config.protocol_version,
            workload_name=metadata.resource_name,
        )
        return KubernetesOfflineRestoreRuntime(
            batch_api=self._batch,
            core_api=self._core,
            networking_api=self._networking,
            candidate_controller=controller,
            candidate_probe=probe,
            archive_stager=self._stager,
            binding=binding,
            image=self._runtime_image,
            staging_image=self._provisioner_image,
            release_version=self._config.release_version,
        )

    async def _context(self, cell_id: str, *, source_vault_id: str) -> _CandidateContext:
        probe = OpaqueProviderMetadata(source_vault_id, cell_id, "discovery", 1)
        namespace = await HelmRestoreCandidateController._api_call(
            self._core,
            "read_namespace",
            probe.resource_name,
        )
        annotations = {
            str(key): str(value)
            for key, value in (getattr(namespace.metadata, "annotations", {}) or {}).items()
        }
        metadata = OpaqueProviderMetadata.from_kubernetes_annotations(annotations)
        if (
            metadata.tenant_id != source_vault_id
            or metadata.subject_id != cell_id
            or namespace.metadata.name != metadata.resource_name
            or annotations.get("exomem.io/provision-mode") != "restore-candidate"
        ):
            raise RestoreJobFailed("restore candidate namespace identity differs")
        self._verifier.authenticate(
            annotations.get("exomem.io/recovery-envelope", ""),
            provider="kubernetes",
            provider_reference=ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="v1",
                kind="Namespace",
                namespace="",
                name=metadata.resource_name,
            ),
            tenant_id=metadata.tenant_id,
            cell_id=metadata.subject_id,
            operation_id=metadata.operation_id,
            fence_generation=metadata.fence_generation,
        )
        async with self._sessions() as session:
            fence = await session.scalar(
                select(TenantFence.fence_generation).where(
                    TenantFence.tenant_id == metadata.tenant_id
                )
            )
            rows = (
                await session.scalars(
                    select(Operation).where(
                        Operation.tenant_id == metadata.tenant_id,
                        Operation.cell_id == metadata.subject_id,
                        Operation.external_operation_id == metadata.operation_id,
                        Operation.action == OperationAction.PROVISION,
                        Operation.state == OperationState.FINAL,
                    )
                )
            ).all()
        if fence != metadata.fence_generation or len(rows) != 1:
            raise RestoreJobFailed("restore candidate ledger identity differs")
        request = await self._operations.load_request(rows[0].id)
        if (
            request.get("provisionMode") != "restore-candidate"
            or request.get("tenantId") != metadata.tenant_id
            or request.get("cellId") != metadata.subject_id
            or request.get("fenceGeneration") != metadata.fence_generation
            or request.get("protocolVersion") != self._config.protocol_version
            or request.get("releaseVersion") != self._config.release_version
        ):
            raise RestoreJobFailed("restore candidate provision request differs")
        cell = KubernetesCellAdapter(
            core_v1=self._core,
            apps_v1=self._apps,
            identity_verifier=self._verifier,
        )
        credentials, secret_annotations = await cell.read_credential_bundle(metadata)
        version = secret_annotations.get("exomem.io/active-credential-version")
        credential = credentials.get(str(version))
        if version is None or not credential or credential != request.get("serviceCredential"):
            raise RestoreJobFailed("restore candidate credential proof differs")
        return _CandidateContext(metadata, request, credential, str(version))


class _ProviderMaximumFenceDriver:
    def __init__(self, sessions: Any, provider: Any) -> None:
        self._sessions = sessions
        self._provider = provider

    async def observed_fence(self, tenant_id: str) -> int:
        async with self._sessions() as session:
            value = await session.scalar(
                select(TenantFence.fence_generation).where(TenantFence.tenant_id == tenant_id)
            )
        return max(int(value or 0), int(await self._provider.observed_fence(tenant_id)))

    async def execute(self, action: str, request: dict[str, Any], context: Any):
        raise RuntimeError("durability worker received an action outside its allowlist")


def _b2_client(
    settings: DurabilityActionSettings,
    *,
    key_id: SecretStr,
    application_key: SecretStr,
) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.b2_endpoint_url,
        region_name=settings.b2_region,
        aws_access_key_id=key_id.get_secret_value(),
        aws_secret_access_key=application_key.get_secret_value(),
        config=Config(signature_version="s3v4", retries={"mode": "standard", "max_attempts": 3}),
    )


async def _run_durability_actions(settings: DurabilityActionSettings) -> None:
    from kubernetes import client, config

    release = load_hosted_release_manifest(settings.release_manifest_path)
    signer = ProviderRecoveryIdentityCodec.from_encoded_seed(
        settings.provider_recovery_signing_key.get_secret_value()
    )
    verifier = signer.verifier()
    database = ProvisionerDatabase(settings)
    api_client: Any | None = None
    try:
        if not await database.ready():
            raise RuntimeError("durability action database is not ready")
        codec = AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value())
        operations = OperationRepository(database.session_factory, codec=codec)
        repository = DurabilityRepository(
            database.session_factory,
            codec=codec,
            lease_seconds=300,
        )
        config.load_incluster_config()
        api_client = client.ApiClient()
        core = client.CoreV1Api(api_client)
        apps = client.AppsV1Api(api_client)
        batch = client.BatchV1Api(api_client)
        networking = client.NetworkingV1Api(api_client)
        coordination = client.CoordinationV1Api(api_client)
        custom = client.CustomObjectsApi(api_client)
        lifecycle_config = LifecycleConfig(
            image=release.runtimeImage,
            chart_path=settings.cell_chart_path,
            chart_version=settings.cell_chart_version,
            helm_version=settings.helm_version,
            control_hostname=settings.control_hostname,
            transfer_hostname=settings.transfer_hostname,
            browser_origin=settings.browser_origin,
            release_version=release.release,
            protocol_version=release.hostedProtocol,
            operator_contract_digest=release.operatorContractSha256,
            contract_digest=release.gatewayContractSha256,
            location=settings.location,
        )
        helm = HelmCliAdapter(
            binary=settings.helm_binary,
            expected_version=settings.helm_version,
            chart_path=settings.cell_chart_path,
            chart_version=settings.cell_chart_version,
        )
        recovery_restore = B2RestoreObjectStore(
            _b2_client(
                settings,
                key_id=settings.recovery_restore_key_id,
                application_key=settings.recovery_restore_key,
            ),
            bucket=settings.recovery_bucket,
        )
        export_restore = B2RestoreObjectStore(
            _b2_client(
                settings,
                key_id=settings.user_export_restore_key_id,
                application_key=settings.user_export_restore_key,
            ),
            bucket=settings.user_export_bucket,
        )
        export_delivery = B2PortableDeliveryStore(
            _b2_client(
                settings,
                key_id=settings.user_export_delivery_key_id,
                application_key=settings.user_export_delivery_key,
            ),
            bucket=settings.user_export_bucket,
        )
        export_delete = B2DeletionObjectStore(
            _b2_client(
                settings,
                key_id=settings.user_export_delete_key_id,
                application_key=settings.user_export_delete_key,
            ),
            bucket=settings.user_export_bucket,
        )
        registry = LiveBackupTargetRegistry()
        target_source = KubernetesBackupTargetSource(
            sessions=database.session_factory,
            operations=operations,
            core_v1=core,
            apps_v1=apps,
            custom_objects=custom,
            identity_verifier=verifier,
            registry=registry,
            protocol_version=release.hostedProtocol,
            release_version=release.release,
        )
        root_secret = settings.envelope_key.get_secret_value()
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(60.0, connect=5.0),
        ) as http:
            export_workflow = RefreshingExportWorkflow(
                target_source,
                ExportBackupWorkflow(
                    repository=repository,
                    routes=VerifiedRouteMaintenancePort(
                        custom_objects=custom,
                        coordination_v1=coordination,
                        http=http,
                        registry=registry,
                    ),
                    runtime=HttpPortableRuntimePort(
                        http=http,
                        registry=registry,
                        scratch_root=settings.scratch_root,
                    ),
                    upload_store=B2UploadOnlyObjectStore(
                        _b2_client(
                            settings,
                            key_id=settings.user_export_upload_key_id,
                            application_key=settings.user_export_upload_key,
                        ),
                        bucket=settings.user_export_bucket,
                    ),
                    cipher=ChunkedArchiveCipher(),
                    key_wrapper=AesGcmKeyWrapper.from_secret(root_secret),
                    provider_identity_signer=signer,
                    provider_bucket=settings.user_export_bucket,
                    scratch_root=settings.scratch_root,
                ),
            )
            resolver = KubernetesRestoreCandidateResolver(
                sessions=database.session_factory,
                operations=operations,
                core_api=core,
                apps_api=apps,
                batch_api=batch,
                networking_api=networking,
                custom_objects=custom,
                identity_verifier=verifier,
                helm=helm,
                lifecycle_config=lifecycle_config,
                archive_stager=B2PortableArchiveStager(export_delivery),
                http=http,
                scratch_root=settings.scratch_root,
                runtime_image=release.runtimeImage,
                provisioner_image=settings.provisioner_image,
            )
            dynamic_runtime = DynamicKubernetesRestoreRuntime(resolver)

            def restore(store: Any, bucket: str) -> RestoreWorkflow:
                return RestoreWorkflow(
                    repository=repository,
                    restore_store=store,
                    runtime=dynamic_runtime,
                    cipher=ChunkedArchiveCipher(),
                    key_wrapper=AesGcmKeyWrapper.from_secret(root_secret),
                    provider_identity_verifier=verifier,
                    provider_bucket=bucket,
                    scratch_root=settings.scratch_root,
                    release_version=release.release,
                )

            restore_workflow = CandidateBoundRestoreWorkflow(
                dynamic_runtime,
                RestoreWorkflowRouter(
                    recovery=restore(recovery_restore, settings.recovery_bucket),
                    user_export=restore(export_restore, settings.user_export_bucket),
                ),
            )
            object_service = ExportObjectService(
                repository=repository,
                restore_store=export_restore,
                delivery_store=export_delivery,
                deletion_store=export_delete,
                cipher=ChunkedArchiveCipher(),
                key_wrapper=AesGcmKeyWrapper.from_secret(root_secret),
                scratch_root=settings.scratch_root,
            )
            driver = DurabilityActionDriver(
                delegate=_ProviderMaximumFenceDriver(
                    database.session_factory,
                    KubernetesProviderRegistry(
                        core_v1=core,
                        apps_v1=apps,
                        batch_v1=batch,
                        custom_objects=custom,
                        identity_verifier=verifier,
                    ),
                ),
                repository=repository,
                export_workflow=export_workflow,
                restore_workflow=restore_workflow,
                object_service=object_service,
            )
            worker = build_durability_operation_worker(
                repository=operations,
                driver=driver,
                worker_id=settings.worker_id,
            )
            await run_bounded_operation_batch(worker, max_operations=settings.max_operations)
    finally:
        await database.dispose()
        if api_client is not None:
            await asyncio.to_thread(api_client.close)


def run_durability_actions() -> None:
    if help_requested("exomem-durability-actions", "one-shot user durability action worker"):
        return
    configure_content_free_logging()
    try:
        settings = DurabilityActionSettings()  # type: ignore[call-arg]
        asyncio.run(_run_durability_actions(settings))
    except Exception:  # noqa: BLE001 - privileged one-shot output stays content-free
        raise SystemExit(1) from None
