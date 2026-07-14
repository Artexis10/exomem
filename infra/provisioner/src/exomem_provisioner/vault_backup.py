"""Live Kubernetes/runtime composition for centralized portable vault backups."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .durability import (
    BackupTarget,
    CentralBackupScheduler,
    ExportBackupWorkflow,
    PortableArchive,
)
from .durability_crypto import AesGcmKeyWrapper, ChunkedArchiveCipher
from .durability_repository import DurabilityRepository
from .durability_store import B2UploadOnlyObjectStore
from .models import CredentialMetadata, Operation, OperationAction, OperationState, TenantFence
from .provider_recovery import (
    ProviderRecoveryIdentityCodec,
    ProviderRecoveryIdentityVerifier,
    ProviderReference,
)

_HOST = re.compile(r"Host\(`([^`]+)`\)")
_PRINCIPAL_SCOPE = (
    base64.urlsafe_b64encode(hashlib.sha256(b"exomem-provisioner-private-cell-control").digest())
    .rstrip(b"=")
    .decode("ascii")
)

try:
    from .lifecycle import OpaqueProviderMetadata
except ModuleNotFoundError:
    # The durability lane can be tested before the provider lane is merged.
    # Integrated/production builds always take the canonical shared class.
    @dataclass(frozen=True, slots=True)
    class OpaqueProviderMetadata:  # type: ignore[no-redef]
        tenant_id: str = field(repr=False)
        subject_id: str = field(repr=False)
        operation_id: str = field(repr=False)
        fence_generation: int

        @property
        def resource_name(self) -> str:
            digest = hashlib.sha256(self.subject_id.encode()).hexdigest()[:20]
            return f"exo-{digest}"

        @property
        def kubernetes_annotations(self) -> dict[str, str]:
            def digest(value: str) -> str:
                return hashlib.sha256(value.encode()).hexdigest()

            return {
                "exomem.io/tenant-id": self.tenant_id,
                "exomem.io/cell-id": self.subject_id,
                "exomem.io/operation-id": self.operation_id,
                "exomem.io/tenant-digest": digest(self.tenant_id),
                "exomem.io/subject-digest": digest(self.subject_id),
                "exomem.io/operation-digest": digest(self.operation_id),
                "exomem.io/fence": str(self.fence_generation),
            }

        @classmethod
        def from_kubernetes_annotations(cls, annotations: dict[str, str]) -> OpaqueProviderMetadata:
            fence = annotations.get("exomem.io/fence", "")
            if not fence.isdigit():
                raise RuntimeError("Kubernetes provider fence annotation is invalid")
            value = cls(
                annotations.get("exomem.io/tenant-id", ""),
                annotations.get("exomem.io/cell-id", ""),
                annotations.get("exomem.io/operation-id", ""),
                int(fence),
            )
            if any(
                annotations.get(key) != expected
                for key, expected in value.kubernetes_annotations.items()
            ):
                raise RuntimeError("Kubernetes provider identity digest differs")
            return value

        def require_same(self, other: OpaqueProviderMetadata) -> None:
            if self != other:
                raise RuntimeError("provider identity metadata is immutable")


class VaultBackupSettings(BaseSettings):
    """Exact environment consumed by the isolated vault-backup CronJob."""

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
    provider_recovery_signing_key: SecretStr = Field(
        min_length=40,
        max_length=128,
        validation_alias="EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY",
    )
    b2_endpoint_url: str = Field(min_length=8, max_length=2048)
    b2_region: str = Field(pattern=r"^[a-z0-9-]{2,64}$")
    recovery_bucket: str = Field(pattern=r"^[a-z0-9-]{6,63}$")
    recovery_upload_key_id: SecretStr = Field(min_length=1, max_length=4096)
    recovery_upload_key: SecretStr = Field(min_length=1, max_length=4096)
    release_manifest_path: Path = Field(validation_alias="EXOMEM_PROVISIONER_RELEASE_MANIFEST_PATH")
    max_concurrency: int = Field(default=4, ge=1, le=32)
    scratch_root: Path
    worker_id: str = Field(
        default="vault-backup-worker",
        pattern=r"^[A-Za-z0-9_.:-]{1,128}$",
    )

    @field_validator("database_url")
    @classmethod
    def require_postgres(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("vault backup requires PostgreSQL")
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
            raise ValueError("vault backup paths must be absolute")
        return value

    @model_validator(mode="after")
    def require_exact_trust_root(self) -> VaultBackupSettings:
        parsed = urlsplit(self.database_url.get_secret_value())
        if unquote(parsed.username or "") != self.database_role:
            raise ValueError("vault backup database URL must use its declared role")
        ProviderRecoveryIdentityCodec.from_encoded_seed(
            self.provider_recovery_signing_key.get_secret_value()
        )
        return self


@dataclass(frozen=True, slots=True)
class LiveBackupTarget:
    metadata: OpaqueProviderMetadata
    credential: str
    credential_version: str
    protocol_version: str
    release_version: str
    browser_origin: str
    control_hostname: str
    transfer_hostname: str
    routes: tuple[dict[str, Any], ...]


class LiveBackupTargetRegistry:
    def __init__(self) -> None:
        self._values: dict[str, LiveBackupTarget] = {}

    def replace(self, values: dict[str, LiveBackupTarget]) -> None:
        self._values = dict(values)

    def get(self, cell_id: str) -> LiveBackupTarget:
        try:
            return self._values[cell_id]
        except KeyError as error:
            raise RuntimeError("backup target is outside the authenticated sweep") from error


class KubernetesBackupTargetSource:
    """Enumerate authenticated live namespaces and resolve credentials from the ledger."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        operations: Any,
        core_v1: Any,
        apps_v1: Any,
        custom_objects: Any,
        identity_verifier: ProviderRecoveryIdentityVerifier,
        registry: LiveBackupTargetRegistry,
        protocol_version: str,
        release_version: str,
    ) -> None:
        self._sessions = sessions
        self._operations = operations
        self._core = core_v1
        self._apps = apps_v1
        self._custom = custom_objects
        self._verifier = identity_verifier
        self._registry = registry
        self._protocol = protocol_version
        self._release = release_version

    async def list_backup_targets(self) -> list[BackupTarget]:
        namespaces = await asyncio.to_thread(
            self._core.list_namespace,
            label_selector="exomem.io/tenant-cell=true",
        )
        resolved: dict[str, LiveBackupTarget] = {}
        targets: list[BackupTarget] = []
        for item in getattr(namespaces, "items", ()) or ():
            annotations = dict(getattr(item.metadata, "annotations", None) or {})
            metadata = OpaqueProviderMetadata.from_kubernetes_annotations(annotations)
            namespace = str(item.metadata.name)
            if namespace != metadata.resource_name:
                raise RuntimeError("backup namespace identity differs")
            reference = ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="v1",
                kind="Namespace",
                namespace="",
                name=namespace,
            )
            self._verifier.authenticate(
                str(annotations.get("exomem.io/recovery-envelope", "")),
                provider="kubernetes",
                provider_reference=reference,
                tenant_id=metadata.tenant_id,
                cell_id=metadata.subject_id,
                operation_id=metadata.operation_id,
                fence_generation=metadata.fence_generation,
            )
            fence, credential, credential_version = await self._ledger_context(metadata)
            stateful_set = await asyncio.to_thread(
                self._apps.read_namespaced_stateful_set,
                metadata.resource_name,
                namespace,
            )
            environment = _container_environment(stateful_set)
            if environment.get("EXOMEM_HOSTED_PROTOCOL_VERSION") != self._protocol:
                raise RuntimeError("backup target protocol differs from the release pin")
            if environment.get("EXOMEM_HOSTED_EXPECTED_RELEASE") != self._release:
                raise RuntimeError("backup target release differs from the release pin")
            browser_origin = environment.get("EXOMEM_HOSTED_TRANSFER_BROWSER_ORIGIN", "")
            if not _https_origin(browser_origin):
                raise RuntimeError("backup target browser origin is invalid")
            routes = await self._authenticated_routes(metadata)
            hosts = _route_hostnames(routes)
            resolved[metadata.subject_id] = LiveBackupTarget(
                metadata=metadata,
                credential=credential,
                credential_version=credential_version,
                protocol_version=self._protocol,
                release_version=self._release,
                browser_origin=browser_origin,
                control_hostname=hosts["control"],
                transfer_hostname=hosts["transfer"],
                routes=routes,
            )
            targets.append(BackupTarget(metadata.tenant_id, metadata.subject_id, fence))
        self._registry.replace(resolved)
        return targets

    async def _ledger_context(self, metadata: OpaqueProviderMetadata) -> tuple[int, str, str]:
        async with self._sessions() as session:
            fence = await session.scalar(
                select(TenantFence.fence_generation).where(
                    TenantFence.tenant_id == metadata.tenant_id
                )
            )
            active_version = await session.scalar(
                select(CredentialMetadata.version).where(
                    CredentialMetadata.cell_id == metadata.subject_id,
                    CredentialMetadata.active.is_(True),
                )
            )
            operations = (
                await session.scalars(
                    select(Operation)
                    .where(
                        Operation.tenant_id == metadata.tenant_id,
                        Operation.cell_id == metadata.subject_id,
                        Operation.state == OperationState.FINAL,
                        Operation.action.not_in((OperationAction.DISCARD, OperationAction.DESTROY)),
                    )
                    .order_by(Operation.finalized_at.desc(), Operation.created_at.desc())
                )
            ).all()
        if fence is None or active_version is None:
            raise RuntimeError("backup target has no current fence or active credential")
        credential = await _recover_active_credential(
            operations,
            self._operations,
            int(active_version),
        )
        return int(fence), credential, str(active_version)

    async def _authenticated_routes(
        self, metadata: OpaqueProviderMetadata
    ) -> tuple[dict[str, Any], ...]:
        page = await asyncio.to_thread(
            self._custom.list_namespaced_custom_object,
            group="traefik.io",
            version="v1alpha1",
            namespace=metadata.resource_name,
            plural="ingressroutes",
            label_selector="exomem.io/tenant-route=true",
        )
        values: list[dict[str, Any]] = []
        for route in page.get("items", ()):  # type: ignore[union-attr]
            route = dict(route)
            route_metadata = dict(route.get("metadata", {}))
            name = str(route_metadata.get("name", ""))
            annotations = {
                str(key): str(value)
                for key, value in dict(route_metadata.get("annotations", {})).items()
            }
            recovered = OpaqueProviderMetadata.from_kubernetes_annotations(annotations)
            recovered.require_same(metadata)
            reference = ProviderReference.kubernetes(
                provider="traefik",
                api_version="traefik.io/v1alpha1",
                kind="IngressRoute",
                namespace=metadata.resource_name,
                name=name,
            )
            self._verifier.authenticate(
                annotations.get("exomem.io/recovery-envelope", ""),
                provider="traefik",
                provider_reference=reference,
                tenant_id=metadata.tenant_id,
                cell_id=metadata.subject_id,
                operation_id=metadata.operation_id,
                fence_generation=metadata.fence_generation,
            )
            values.append(route)
        if len(values) != 2:
            raise RuntimeError("backup target does not have exactly two authenticated routes")
        return tuple(sorted(values, key=lambda value: value["metadata"]["name"]))


class VerifiedRouteMaintenancePort:
    """Patch authenticated routes closed, prove rejection, then restore exact specs."""

    def __init__(
        self,
        *,
        custom_objects: Any,
        coordination_v1: Any,
        http: httpx.AsyncClient,
        registry: LiveBackupTargetRegistry,
        maintenance: Any | None = None,
    ) -> None:
        self._custom = custom_objects
        if maintenance is None:
            from .adapters import KubernetesMaintenanceLeaseAdapter

            maintenance = KubernetesMaintenanceLeaseAdapter(coordination_v1=coordination_v1)
        self._maintenance = maintenance
        self._http = http
        self._registry = registry

    async def close_and_verify(self, cell_id: str, operation_id: str) -> None:
        target = self._registry.get(cell_id)
        if not await self._maintenance.acquire(target.metadata, operation_id):
            raise RuntimeError("cell maintenance lease is held by another operation")
        try:
            for route in target.routes:
                await asyncio.to_thread(
                    self._custom.patch_namespaced_custom_object,
                    group="traefik.io",
                    version="v1alpha1",
                    namespace=target.metadata.resource_name,
                    plural="ingressroutes",
                    name=str(route["metadata"]["name"]),
                    body={"spec": {"routes": []}},
                )
            unused_ticket = _maintenance_grant(target)
            if not await self._prove_rejected(target, unused_ticket):
                raise RuntimeError("external routes remained reachable during backup")
        except Exception:
            try:
                await self._restore(target)
            finally:
                await self._maintenance.release(target.metadata, operation_id)
            raise

    async def open(self, cell_id: str, operation_id: str) -> None:
        target = self._registry.get(cell_id)
        try:
            await self._restore(target)
        finally:
            await self._maintenance.release(target.metadata, operation_id)

    async def _restore(self, target: LiveBackupTarget) -> None:
        for route in target.routes:
            await asyncio.to_thread(
                self._custom.patch_namespaced_custom_object,
                group="traefik.io",
                version="v1alpha1",
                namespace=target.metadata.resource_name,
                plural="ingressroutes",
                name=str(route["metadata"]["name"]),
                body={"spec": route["spec"]},
            )

    async def _prove_rejected(self, target: LiveBackupTarget, ticket: str) -> bool:
        control = await self._http.get(
            f"https://{target.control_hostname}/cells/{target.metadata.subject_id}"
            "/private/exomem/v1/ready",
            headers={
                "Authorization": f"Bearer {target.credential}",
                "X-Exomem-Hosted-Cell": target.metadata.subject_id,
                "X-Exomem-Hosted-Protocol": target.protocol_version,
            },
        )
        transfer_url = (
            f"https://{target.transfer_hostname}/cells/{target.metadata.subject_id}"
            "/public/exomem/v2/transfers/download"
        )
        preflight = await self._http.options(
            transfer_url,
            headers={
                "Access-Control-Request-Headers": "X-Exomem-Transfer-Grant",
                "Access-Control-Request-Method": "GET",
                "Origin": target.browser_origin,
            },
        )
        transfer = await self._http.get(
            transfer_url,
            headers={
                "Origin": target.browser_origin,
                "X-Exomem-Transfer-Grant": ticket,
            },
        )
        return (
            control.status_code in {401, 403, 404, 503}
            and preflight.status_code in {404, 503}
            and transfer.status_code in {404, 503}
        )


class HttpPortableRuntimePort:
    """Stream the runtime's portable-export contract into bounded scratch."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        registry: LiveBackupTargetRegistry,
        scratch_root: Path,
        max_archive_bytes: int = 6 * 1024 * 1024 * 1024,
    ) -> None:
        self._http = http
        self._registry = registry
        self._scratch = scratch_root.resolve()
        self._max_archive_bytes = max_archive_bytes
        self._artifacts: dict[tuple[str, str], tuple[Path, Path]] = {}

    async def quiesce(
        self,
        cell_id: str,
        operation_id: str,
        *,
        routing_stopped: bool,
    ) -> None:
        if not routing_stopped:
            raise RuntimeError("portable backup requires externally stopped routing")
        target = self._registry.get(cell_id)
        await self._lifecycle_call(
            target,
            operation_id,
            "lifecycle/quiesce",
            {"timeout_seconds": 30},
            routing_stopped=True,
        )

    async def portable_export(self, cell_id: str, operation_id: str) -> PortableArchive:
        target = self._registry.get(cell_id)
        response = await self._http.post(
            self._private_url(target, "lifecycle/export"),
            headers=self._headers(target, operation_id, routing_stopped=True),
            json={"format": "exomem-portable-v1"},
        )
        descriptor = _response_data(response)
        archive_path = self._scratch / f".{_safe_operation(operation_id)}.runtime-export"
        manifest_path = self._scratch / f".{_safe_operation(operation_id)}.runtime-manifest.json"
        self._scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._scratch.chmod(0o700)
        archive_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        try:
            download_path = _required_text(descriptor, "downloadPath")
            download_url = urljoin(self._private_url(target, ""), download_path)
            if not download_url.startswith(self._private_url(target, "")):
                raise RuntimeError("portable export download escaped the private cell origin")
            size, digest = await self._stream(download_url, target, operation_id, archive_path)
            manifest_bytes = _manifest_bytes(descriptor)
            await asyncio.to_thread(_write_private, manifest_path, manifest_bytes)
            expected_size = _required_positive_int(descriptor, "archiveSize")
            archive_sha = _required_sha256(descriptor, "archiveSha256")
            manifest_sha = _required_sha256(descriptor, "manifestSha256")
            if size != expected_size or digest != archive_sha:
                raise RuntimeError("portable export archive proof differs")
            if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha:
                raise RuntimeError("portable export manifest proof differs")
            result = PortableArchive(
                archive_path=archive_path,
                manifest_path=manifest_path,
                archive_sha256=archive_sha,
                manifest_sha256=manifest_sha,
                archive_size=size,
                source_cell_id=_required_text(descriptor, "sourceCellId"),
                release_version=_required_text(descriptor, "releaseVersion"),
                hosted_state_included=descriptor.get("hostedStateIncluded") is True,
            )
            if result.source_cell_id != cell_id or result.release_version != target.release_version:
                raise RuntimeError("portable export source identity differs")
            self._artifacts[(cell_id, operation_id)] = (archive_path, manifest_path)
            return result
        except Exception:
            archive_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            raise

    async def release(self, cell_id: str, operation_id: str) -> None:
        target = self._registry.get(cell_id)
        try:
            await self._lifecycle_call(target, operation_id, "lifecycle/resume", {})
        finally:
            for path in self._artifacts.pop((cell_id, operation_id), ()):
                path.unlink(missing_ok=True)

    async def _lifecycle_call(
        self,
        target: LiveBackupTarget,
        operation_id: str,
        path: str,
        body: dict[str, Any],
        *,
        routing_stopped: bool = False,
    ) -> None:
        response = await self._http.post(
            self._private_url(target, path),
            headers=self._headers(
                target,
                operation_id,
                routing_stopped=routing_stopped,
            ),
            json=body,
        )
        _response_data(response)

    def _private_url(self, target: LiveBackupTarget, path: str) -> str:
        return self._internal_origin(target) + "/private/exomem/v1/" + path.lstrip("/")

    @staticmethod
    def _internal_origin(target: LiveBackupTarget) -> str:
        resource = target.metadata.resource_name
        return f"http://{resource}.{resource}.svc.cluster.local:8765"

    @staticmethod
    def _headers(
        target: LiveBackupTarget,
        operation_id: str,
        *,
        routing_stopped: bool,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {target.credential}",
            "X-Exomem-Cell-Id": target.metadata.subject_id,
            "X-Exomem-Protocol-Version": target.protocol_version,
            "X-Exomem-Request-Id": str(uuid.uuid4()),
            "X-Exomem-Principal-Scope": _PRINCIPAL_SCOPE,
            "Idempotency-Key": operation_id,
        }
        if routing_stopped:
            headers["X-Exomem-Routing-Stopped"] = "true"
        return headers

    async def _stream(
        self,
        url: str,
        target: LiveBackupTarget,
        operation_id: str,
        destination: Path,
    ) -> tuple[int, str]:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        size = 0
        digest = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as handle:
                async with self._http.stream(
                    "GET",
                    url,
                    headers=self._headers(target, operation_id, routing_stopped=True),
                ) as response:
                    if response.status_code != 200:
                        raise RuntimeError("portable export download failed")
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > self._max_archive_bytes:
                            raise RuntimeError("portable export exceeds bounded scratch size")
                        digest.update(chunk)
                        await asyncio.to_thread(handle.write, chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return size, digest.hexdigest()


async def run_live_vault_backup(settings: VaultBackupSettings) -> None:
    from kubernetes import client, config

    from .config import load_hosted_release_manifest

    release = load_hosted_release_manifest(settings.release_manifest_path)
    database = ProvisionerDatabase(settings)
    api_client: Any | None = None
    try:
        if not await database.ready():
            raise RuntimeError("vault backup database is not ready")
        config.load_incluster_config()
        api_client = client.ApiClient()
        core_v1 = client.CoreV1Api(api_client)
        apps_v1 = client.AppsV1Api(api_client)
        coordination_v1 = client.CoordinationV1Api(api_client)
        custom_objects = client.CustomObjectsApi(api_client)
        codec = AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value())
        repository = DurabilityRepository(
            database.session_factory,
            codec=codec,
            lease_seconds=300,
        )
        from .repository import OperationRepository

        operations = OperationRepository(database.session_factory, codec=codec)
        registry = LiveBackupTargetRegistry()
        signer = ProviderRecoveryIdentityCodec.from_encoded_seed(
            settings.provider_recovery_signing_key.get_secret_value()
        )
        verifier = signer.verifier()
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(60.0, connect=5.0),
        ) as http:
            routes = VerifiedRouteMaintenancePort(
                custom_objects=custom_objects,
                coordination_v1=coordination_v1,
                http=http,
                registry=registry,
            )
            runtime = HttpPortableRuntimePort(
                http=http,
                registry=registry,
                scratch_root=settings.scratch_root,
            )
            root_secret = settings.envelope_key.get_secret_value()
            workflow = ExportBackupWorkflow(
                repository=repository,
                routes=routes,
                runtime=runtime,
                upload_store=B2UploadOnlyObjectStore(
                    _b2_client(settings),
                    bucket=settings.recovery_bucket,
                ),
                cipher=ChunkedArchiveCipher(),
                key_wrapper=AesGcmKeyWrapper.from_secret(root_secret),
                provider_identity_signer=signer,
                provider_bucket=settings.recovery_bucket,
                scratch_root=settings.scratch_root,
            )
            report = await CentralBackupScheduler(
                repository=repository,
                target_source=KubernetesBackupTargetSource(
                    sessions=database.session_factory,
                    operations=operations,
                    core_v1=core_v1,
                    apps_v1=apps_v1,
                    custom_objects=custom_objects,
                    identity_verifier=verifier,
                    registry=registry,
                    protocol_version=release.hostedProtocol,
                    release_version=release.release,
                ),
                workflow=workflow,
                worker_id=settings.worker_id,
                max_concurrency=settings.max_concurrency,
            ).run_once()
            if report.failed or report.deferred_busy or not report.capacity_rpo_met:
                raise RuntimeError("central vault backup sweep did not reach verified success")
    finally:
        await database.dispose()
        if api_client is not None:
            await asyncio.to_thread(api_client.close)


def _b2_client(settings: VaultBackupSettings) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.b2_endpoint_url,
        region_name=settings.b2_region,
        aws_access_key_id=settings.recovery_upload_key_id.get_secret_value(),
        aws_secret_access_key=settings.recovery_upload_key.get_secret_value(),
        config=Config(signature_version="s3v4", retries={"mode": "standard", "max_attempts": 3}),
    )


def _container_environment(stateful_set: Any) -> dict[str, str]:
    containers = getattr(
        getattr(getattr(stateful_set, "spec", None), "template", None), "spec", None
    )
    values = getattr(containers, "containers", ()) or ()
    if len(values) != 1:
        raise RuntimeError("backup target runtime container is ambiguous")
    result: dict[str, str] = {}
    for item in getattr(values[0], "env", ()) or ():
        name = str(getattr(item, "name", ""))
        value = getattr(item, "value", None)
        if name and isinstance(value, str):
            result[name] = value
    return result


async def _recover_active_credential(
    operations: list[Any],
    repository: Any,
    active_version: int,
) -> str:
    for operation in operations:
        request = await repository.load_request(operation.id)
        if operation.action is OperationAction.ROTATE_CREDENTIAL:
            if request.get("credentialVersion") != active_version:
                continue
            value = request.get("nextCredential")
        elif operation.action is OperationAction.PROVISION:
            if active_version != 1:
                continue
            value = request.get("serviceCredential")
        else:
            continue
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if isinstance(value, str) and value:
            return value
    raise RuntimeError("backup target has no recoverable active credential")


def _route_hostnames(routes: tuple[dict[str, Any], ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for route in routes:
        name = str(route["metadata"]["name"])
        kind = (
            "control"
            if name.endswith("-control")
            else "transfer"
            if name.endswith("-transfer")
            else ""
        )
        specifications = route.get("spec", {}).get("routes", ())
        matches = [_HOST.search(str(value.get("match", ""))) for value in specifications]
        hosts = {match.group(1) for match in matches if match is not None}
        if not kind or len(hosts) != 1:
            raise RuntimeError("backup target route hostname is ambiguous")
        result[kind] = hosts.pop()
    if set(result) != {"control", "transfer"}:
        raise RuntimeError("backup target routes are incomplete")
    return result


def _maintenance_grant(target: LiveBackupTarget) -> str:
    issued_at = int(time.time())
    claims = {
        "aud": "exomem-hosted-transfer",
        "cell": target.metadata.subject_id,
        "exp": issued_at + 300,
        "iat": issued_at,
        "jti": secrets.token_urlsafe(24),
        "kid": target.credential_version,
        "limits": {"max_bytes": 1},
        "method": "GET",
        "nbf": issued_at,
        "op": "download",
        "origin": target.browser_origin,
        "principal": _PRINCIPAL_SCOPE,
        "target": {"kind": "download-v1", "path": "maintenance/route-proof"},
        "v": 2,
    }
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode("ascii")
    )
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(
                target.credential.encode(),
                payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return payload + "." + signature


def _https_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.port is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def _response_data(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise RuntimeError("portable export request failed")
    value = response.json()
    if not isinstance(value, dict) or value.get("success") is not True:
        raise RuntimeError("portable export response is invalid")
    data = value.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("portable export descriptor is invalid")
    return data


def _manifest_bytes(descriptor: dict[str, Any]) -> bytes:
    raw = descriptor.get("manifestJson")
    if isinstance(raw, str):
        return raw.encode("utf-8")
    value = descriptor.get("manifest")
    if not isinstance(value, dict):
        raise RuntimeError("portable export manifest is absent")
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_private(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)


def _required_text(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RuntimeError("portable export descriptor is incomplete")
    return item


def _required_sha256(value: dict[str, Any], key: str) -> str:
    item = _required_text(value, key)
    if not re.fullmatch(r"[0-9a-f]{64}", item):
        raise RuntimeError("portable export digest is invalid")
    return item


def _required_positive_int(value: dict[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or item < 1:
        raise RuntimeError("portable export size is invalid")
    return item


def _safe_operation(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
