"""Least-privilege production composition for durability worker commands."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .deletion import DeletionVerificationError
from .driver import (
    DriverFinal,
    DriverPending,
    DriverRetryable,
    DriverTerminal,
    EffectContext,
)
from .durability_repository import DurabilityRepository
from .models import (
    CellOperationLock,
    Operation,
    OperationAction,
    OperationState,
    TenantFence,
)
from .provider_identity import ProviderRecoveryIdentityVerifier
from .repository import OperationRepository
from .worker import ProvisionerWorker
from .worker_ownership import DELETION_OPERATION_ACTIONS

try:
    from .lifecycle import MetadataConflict
    from .provider_deletion import DeletionLeaseBusy
    from .provider_identity import ProviderIdentityConflict
except ModuleNotFoundError:
    # Provider-lane modules are merged alongside this lane in production. The
    # fallbacks keep this independently testable without weakening the merged
    # runtime, where the canonical exception classes are imported above.
    class MetadataConflict(RuntimeError):
        pass

    class DeletionLeaseBusy(RuntimeError):
        pass

    class ProviderIdentityConflict(RuntimeError):
        pass


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class DeletionRuntimeSettings(BaseSettings):
    """Verifier-only environment for the privileged provider cleanup worker."""

    model_config = SettingsConfigDict(
        env_prefix="EXOMEM_PROVISIONER_",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    database_url: SecretStr = Field(min_length=1, max_length=4096)
    database_schema: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    database_role: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    envelope_key: SecretStr = Field(min_length=32, max_length=4096)
    claim_seconds: int = Field(default=30, ge=5, le=300)
    max_failure_attempts: int = Field(default=6, ge=1, le=100)
    worker_id: str = Field(default="deletion-worker", pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    provider_recovery_public_key: SecretStr = Field(
        min_length=40,
        max_length=128,
        validation_alias="EXOMEM_PROVIDER_RECOVERY_PUBLIC_KEY",
    )
    hcloud_token: SecretStr = Field(min_length=1, max_length=4096)
    b2_endpoint_url: str = Field(min_length=8, max_length=2048)
    b2_region: str = Field(pattern=r"^[a-z0-9-]{2,64}$")
    recovery_bucket: str = Field(pattern=r"^[a-z0-9-]{6,63}$")
    user_export_bucket: str = Field(pattern=r"^[a-z0-9-]{6,63}$")
    recovery_delete_key_id: SecretStr = Field(min_length=1, max_length=4096)
    recovery_delete_key: SecretStr = Field(min_length=1, max_length=4096)
    user_export_delete_key_id: SecretStr = Field(min_length=1, max_length=4096)
    user_export_delete_key: SecretStr = Field(min_length=1, max_length=4096)

    @field_validator("database_url")
    @classmethod
    def require_postgres(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("deletion worker requires PostgreSQL")
        return value

    @field_validator("b2_endpoint_url")
    @classmethod
    def require_https_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("B2 endpoint must be an HTTPS origin")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_boundaries(self) -> DeletionRuntimeSettings:
        parsed = urlsplit(self.database_url.get_secret_value())
        if unquote(parsed.username or "") != self.database_role:
            raise ValueError("deletion database URL must use its declared role")
        if self.recovery_bucket == self.user_export_bucket:
            raise ValueError("deletion buckets must be distinct")
        ProviderRecoveryIdentityVerifier.from_public_key(
            self.provider_recovery_public_key.get_secret_value()
        )
        return self


class BucketScopedB2Client:
    """Dispatch the three deletion-only S3 calls by an exact bucket allowlist."""

    def __init__(self, clients: dict[str, Any]) -> None:
        if len(clients) < 1 or any(not bucket for bucket in clients):
            raise ValueError("bucket-scoped B2 clients require an exact allowlist")
        self._clients = dict(clients)

    def list_object_versions(self, **arguments: Any) -> dict[str, Any]:
        return self._client(arguments).list_object_versions(**arguments)

    def head_object(self, **arguments: Any) -> dict[str, Any]:
        return self._client(arguments).head_object(**arguments)

    def delete_object(self, **arguments: Any) -> dict[str, Any]:
        return self._client(arguments).delete_object(**arguments)

    def _client(self, arguments: dict[str, Any]) -> Any:
        bucket = arguments.get("Bucket")
        if not isinstance(bucket, str) or bucket not in self._clients:
            raise ValueError("B2 bucket is outside deletion scope")
        return self._clients[bucket]


class DeletionClaimAuthority:
    """Bind destructive provider work to the live authoritative DB claim."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def current_fence(self, tenant_id: str) -> int:
        async with self._sessions() as session:
            value = await session.scalar(
                select(TenantFence.fence_generation).where(TenantFence.tenant_id == tenant_id)
            )
        return int(value or 0)

    async def acquire(self, tenant_id: str, operation_id: str, fence: int) -> bool:
        async with self._sessions.begin() as session:
            operation, _ = await self._active_deletion_claim(
                session,
                tenant_id=tenant_id,
                operation_id=operation_id,
                fence=fence,
            )
            return operation is not None

    async def acquire_cell(
        self,
        tenant_id: str,
        cell_id: str,
        operation_id: str,
        fence: int,
    ) -> bool:
        if not cell_id:
            return False
        async with self._sessions.begin() as session:
            operation, checked_at = await self._active_deletion_claim(
                session,
                tenant_id=tenant_id,
                operation_id=operation_id,
                fence=fence,
            )
            if operation is None or operation.claim_expires_at is None:
                return False
            if operation.action is OperationAction.DISCARD and operation.cell_id != cell_id:
                return False
            lock = await session.get(CellOperationLock, cell_id, with_for_update=True)
            if lock is not None:
                owned = (
                    lock.tenant_id == tenant_id
                    and lock.operation_id == operation_id
                    and lock.fence_generation == fence
                )
                if not owned and _utc(lock.lease_expires_at) > checked_at:
                    return False
                lock.tenant_id = tenant_id
                lock.operation_id = operation_id
                lock.fence_generation = fence
                lock.lease_expires_at = operation.claim_expires_at
                lock.updated_at = checked_at
            else:
                session.add(
                    CellOperationLock(
                        cell_id=cell_id,
                        tenant_id=tenant_id,
                        operation_id=operation_id,
                        fence_generation=fence,
                        lease_expires_at=operation.claim_expires_at,
                        updated_at=checked_at,
                    )
                )
            await session.flush()
            return True

    @staticmethod
    async def _active_deletion_claim(
        session: AsyncSession,
        *,
        tenant_id: str,
        operation_id: str,
        fence: int,
    ) -> tuple[Operation | None, datetime]:
        checked_at = await session.scalar(select(func.now()))
        if not isinstance(checked_at, datetime):
            raise RuntimeError("database clock is unavailable")
        checked_at = _utc(checked_at)
        tenant_fence = await session.get(TenantFence, tenant_id, with_for_update=True)
        operation = await session.scalar(
            select(Operation)
            .where(
                Operation.external_operation_id == operation_id,
                Operation.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if (
            tenant_fence is None
            or tenant_fence.fence_generation != fence
            or operation is None
            or operation.action not in {OperationAction.DISCARD, OperationAction.DESTROY}
            or operation.state is not OperationState.CLAIMED
            or operation.fence_generation != fence
            or operation.claim_token is None
            or operation.claim_expires_at is None
            or _utc(operation.claim_expires_at) <= checked_at
        ):
            return None, checked_at
        return operation, checked_at


class RecoveryKeyRepository(Protocol):
    async def mark_recovery_object_deleted(
        self,
        opaque_reference: str,
        *,
        tenant_id: str,
    ) -> object: ...

    async def destroy_recovery_wrapped_key(
        self,
        opaque_reference: str,
        *,
        tenant_id: str,
    ) -> object: ...

    async def tenant_recovery_objects(self, tenant_id: str) -> list[Any]: ...

    async def tenant_export_deliveries(self, tenant_id: str) -> list[Any]: ...

    async def mark_export_delivery_deleted(
        self,
        reference: str,
        *,
        tenant_id: str,
    ) -> object: ...


class RepositoryWrappedKeyStore:
    """Destroy a tenant-scoped wrapped key only after provider absence proof."""

    def __init__(self, repository: RecoveryKeyRepository) -> None:
        self._repository = repository

    async def tenant_recovery_objects(self, tenant_id: str) -> list[Any]:
        return await self._repository.tenant_recovery_objects(tenant_id)

    async def tenant_export_deliveries(self, tenant_id: str) -> list[Any]:
        return await self._repository.tenant_export_deliveries(tenant_id)

    async def mark_recovery_object_deleted(
        self,
        reference: str,
        *,
        tenant_id: str,
    ) -> None:
        await self._repository.mark_recovery_object_deleted(reference, tenant_id=tenant_id)

    async def mark_export_delivery_deleted(
        self,
        reference: str,
        *,
        tenant_id: str,
    ) -> None:
        await self._repository.mark_export_delivery_deleted(reference, tenant_id=tenant_id)

    async def destroy(self, reference: str, *, tenant_id: str) -> None:
        # LiveDeletionProvider invokes this only after its independent object
        # HEAD/absence check. Persist that proof before erasing key material.
        await self._repository.mark_recovery_object_deleted(reference, tenant_id=tenant_id)
        await self._repository.destroy_recovery_wrapped_key(reference, tenant_id=tenant_id)

    async def absent(self, reference: str, *, tenant_id: str) -> bool:
        records = await self._repository.tenant_recovery_objects(tenant_id)
        return any(
            record.opaque_reference == reference
            and record.wrapped_data_key is None
            and record.key_destroyed_at is not None
            for record in records
        )

    async def deletion_complete(self, tenant_id: str) -> bool:
        records = await self._repository.tenant_recovery_objects(tenant_id)
        deliveries = await self._repository.tenant_export_deliveries(tenant_id)
        return all(
            record.deleted_at is not None
            and record.wrapped_data_key is None
            and record.key_destroyed_at is not None
            for record in records
        ) and all(record.deleted_at is not None for record in deliveries)


class CurrentFenceAuthority(Protocol):
    async def current_fence(self, tenant_id: str) -> int: ...


class OrderedDeletionPort(Protocol):
    async def discard_candidate(self, context: EffectContext) -> DriverPending | DriverFinal: ...

    async def destroy_tenant(self, context: EffectContext) -> DriverPending | DriverFinal: ...


class DeletionOnlyDriver:
    """Dispatch the privileged worker's two-action allowlist and nothing else."""

    def __init__(
        self,
        *,
        authority: CurrentFenceAuthority,
        workflow: OrderedDeletionPort,
    ) -> None:
        self._authority = authority
        self._workflow = workflow

    async def observed_fence(self, tenant_id: str) -> int:
        return await self._authority.current_fence(tenant_id)

    async def execute(
        self,
        action: str,
        request: dict[str, Any],
        context: EffectContext,
    ) -> DriverPending | DriverFinal:
        del request
        if action == OperationAction.DISCARD.value:
            operation = self._workflow.discard_candidate(context)
        elif action == OperationAction.DESTROY.value:
            operation = self._workflow.destroy_tenant(context)
        else:
            raise DriverTerminal("PROVISIONER_DELETION_ACTION_SCOPE")

        try:
            return await operation
        except DriverTerminal:
            raise
        except DeletionLeaseBusy:
            return DriverPending("deletion-lock-busy", 2)
        except (
            DeletionVerificationError,
            MetadataConflict,
            ProviderIdentityConflict,
        ):
            raise DriverTerminal("PROVISIONER_DELETION_VERIFICATION_FAILED") from None
        # SDKs used by this privileged worker expose heterogeneous transient
        # exceptions. Convert all ordinary provider failures into the worker's
        # bounded retry path; BaseException still propagates.
        except Exception:  # noqa: BLE001
            raise DriverRetryable("PROVISIONER_DELETION_PROVIDER_RETRY") from None


def build_deletion_operation_worker(
    *,
    repository: OperationRepository,
    workflow: OrderedDeletionPort,
    authority: CurrentFenceAuthority,
    worker_id: str,
) -> ProvisionerWorker:
    return ProvisionerWorker(
        repository,
        DeletionOnlyDriver(authority=authority, workflow=workflow),
        worker_id=worker_id,
        allowed_actions=DELETION_OPERATION_ACTIONS,
        resume_claim=True,
    )


def _b2_client(
    settings: DeletionRuntimeSettings,
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


def build_live_deletion_provider(
    *,
    core_v1: Any,
    apps_v1: Any,
    custom_objects: Any,
    hcloud_client: Any,
    b2_client: Any,
    recovery_bucket: str,
    export_bucket: str,
    provider_recovery_public_key: str,
    authority: Any,
    key_store: Any,
) -> Any:
    """Construct the live scanner with the provider lane's canonical verifier."""

    from .provider_deletion import LiveDeletionProvider

    return LiveDeletionProvider(
        core_v1=core_v1,
        apps_v1=apps_v1,
        custom_objects=custom_objects,
        hcloud_client=hcloud_client,
        b2_client=b2_client,
        recovery_bucket=recovery_bucket,
        export_bucket=export_bucket,
        identity_verifier=ProviderRecoveryIdentityVerifier.from_public_key(
            provider_recovery_public_key
        ),
        authority=authority,
        key_store=key_store,
    )


@asynccontextmanager
async def live_deletion_worker() -> AsyncIterator[ProvisionerWorker]:
    """Build the verifier-only provider deletion process from its exact env."""

    # These imports remain inside the privileged executable so the API,
    # scheduler, and routine provisioner cannot acquire deletion clients by
    # importing ordinary durability workflow code.
    import hcloud
    from kubernetes import client as kubernetes_client
    from kubernetes import config as kubernetes_config

    from .provider_deletion import FencedOrderedDeletionWorkflow

    settings = DeletionRuntimeSettings()  # type: ignore[call-arg]
    database = ProvisionerDatabase(settings)  # structurally supplies DB fields
    try:
        if not await database.ready():
            raise RuntimeError("deletion database is not ready")
        operation_repository = OperationRepository(
            database.session_factory,
            codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
            claim_seconds=settings.claim_seconds,
            max_failure_attempts=settings.max_failure_attempts,
        )
        recovery_repository = DurabilityRepository(
            database.session_factory,
            codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        )
        authority = DeletionClaimAuthority(database.session_factory)
        kubernetes_config.load_incluster_config()
        b2_client = BucketScopedB2Client(
            {
                settings.recovery_bucket: _b2_client(
                    settings,
                    key_id=settings.recovery_delete_key_id,
                    application_key=settings.recovery_delete_key,
                ),
                settings.user_export_bucket: _b2_client(
                    settings,
                    key_id=settings.user_export_delete_key_id,
                    application_key=settings.user_export_delete_key,
                ),
            }
        )
        provider = build_live_deletion_provider(
            core_v1=kubernetes_client.CoreV1Api(),
            apps_v1=kubernetes_client.AppsV1Api(),
            custom_objects=kubernetes_client.CustomObjectsApi(),
            hcloud_client=hcloud.Client(token=settings.hcloud_token.get_secret_value()),
            b2_client=b2_client,
            recovery_bucket=settings.recovery_bucket,
            export_bucket=settings.user_export_bucket,
            provider_recovery_public_key=(settings.provider_recovery_public_key.get_secret_value()),
            authority=authority,
            key_store=RepositoryWrappedKeyStore(recovery_repository),
        )
        yield build_deletion_operation_worker(
            repository=operation_repository,
            workflow=FencedOrderedDeletionWorkflow(provider),
            authority=authority,
            worker_id=settings.worker_id,
        )
    finally:
        await database.dispose()
