"""Least-privilege one-shot durability workload entrypoints."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .database_backup import (
    DatabaseBackupWorkflow,
    PostgresLogicalBackup,
    PostgresRecoveryConfig,
    ScheduledDatabaseBackupWorkflow,
)
from .durability import BackupTarget, CentralBackupScheduler, ExportGarbageCollector
from .durability_crypto import (
    AesGcmKeyWrapper,
    AesGcmRecoveryEnvelopeCodec,
    ChunkedArchiveCipher,
)
from .durability_repository import DurabilityRepository, RunKind
from .durability_store import B2DeletionObjectStore, B2UploadOnlyObjectStore
from .logging import configure_content_free_logging
from .models import Operation, OperationAction, OperationState
from .provider_recovery import ProviderRecoveryIdentityCodec

_ReportT = TypeVar("_ReportT")


class OperationBatchWorker(Protocol):
    async def run_once(self) -> bool: ...


class BackupSweep(Protocol[_ReportT]):
    async def run_once(self) -> _ReportT: ...


class DeletionDispatchSource(Protocol):
    async def next_dispatchable_deletion(self) -> str | None: ...


class DeletionJobLauncher(Protocol):
    async def has_active_job(self) -> bool: ...

    async def create_scoped_job(self, operation_id: str) -> None: ...


class RepositoryDeletionDispatchSource:
    """Read eligible destructive work without claiming or decrypting it."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def next_dispatchable_deletion(self, *, now: datetime | None = None) -> str | None:
        async with self._sessions() as session:
            if now is not None:
                checked_at = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            elif session.get_bind().dialect.name == "postgresql":
                checked_at = await session.scalar(select(func.clock_timestamp()))
                if not isinstance(checked_at, datetime):
                    raise RuntimeError("dispatcher database clock is unavailable")
            else:
                checked_at = datetime.now(UTC)
            operation_id = await session.scalar(
                select(Operation.id)
                .where(
                    Operation.action.in_((OperationAction.DISCARD, OperationAction.DESTROY)),
                    Operation.available_at <= checked_at,
                    or_(
                        Operation.state == OperationState.PENDING,
                        and_(
                            Operation.state == OperationState.CLAIMED,
                            Operation.claim_expires_at <= checked_at,
                        ),
                    ),
                )
                .order_by(Operation.created_at, Operation.id)
                .limit(1)
            )
            return str(operation_id) if operation_id is not None else None


class KubernetesDeletionJobLauncher:
    """Instantiate only the reviewed privileged Job template."""

    def __init__(
        self,
        *,
        batch_v1: object,
        namespace: str,
        job_template: dict[str, object],
    ) -> None:
        if not namespace:
            raise ValueError("deletion dispatcher namespace is required")
        self._validate_template(job_template)
        self._batch = batch_v1
        self._namespace = namespace
        self._template = copy.deepcopy(job_template)

    async def has_active_job(self) -> bool:
        result = await asyncio.to_thread(
            self._batch.list_namespaced_job,  # type: ignore[attr-defined]
            self._namespace,
            label_selector="exomem.io/deletion-job=true",
        )
        for item in getattr(result, "items", ()) or ():
            status = getattr(item, "status", None)
            if getattr(status, "completion_time", None) is None and not getattr(
                status, "failed", 0
            ):
                return True
        return False

    async def create_scoped_job(self, operation_id: str) -> None:
        if not operation_id:
            raise ValueError("deletion operation identity is required")
        body = copy.deepcopy(self._template)
        metadata = body.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("deletion Job metadata is invalid")
        annotations = metadata.setdefault("annotations", {})
        if not isinstance(annotations, dict):
            raise ValueError("deletion Job annotations are invalid")
        annotations["exomem.io/deletion-operation-sha256"] = hashlib.sha256(
            operation_id.encode("utf-8")
        ).hexdigest()
        await asyncio.to_thread(
            self._batch.create_namespaced_job,  # type: ignore[attr-defined]
            self._namespace,
            body,
        )

    @staticmethod
    def _validate_template(template: dict[str, object]) -> None:
        metadata = template.get("metadata")
        spec = template.get("spec")
        if (
            template.get("apiVersion") != "batch/v1"
            or template.get("kind") != "Job"
            or not isinstance(metadata, dict)
            or metadata.get("generateName") != "exomem-deletion-"
            or metadata.get("labels") != {"exomem.io/deletion-job": "true"}
            or not isinstance(spec, dict)
        ):
            raise ValueError("deletion Job template identity differs")
        pod_template = spec.get("template")
        pod_spec = pod_template.get("spec") if isinstance(pod_template, dict) else None
        containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
        if (
            spec.get("activeDeadlineSeconds") != 240
            or spec.get("backoffLimit") != 0
            or spec.get("ttlSecondsAfterFinished") != 300
            or not isinstance(pod_spec, dict)
            or pod_spec.get("serviceAccountName") != "exomem-deletion-worker"
            or pod_spec.get("restartPolicy") != "Never"
            or not isinstance(containers, list)
            or len(containers) != 1
            or not isinstance(containers[0], dict)
            or containers[0].get("name") != "deletion-worker"
            or containers[0].get("command") != ["exomem-deletion-worker"]
        ):
            raise ValueError("deletion Job template privilege boundary differs")


async def dispatch_one_deletion_job(
    source: DeletionDispatchSource,
    launcher: DeletionJobLauncher,
) -> bool:
    """Create one privileged Job only when durable deletion work is eligible."""

    if await launcher.has_active_job():
        return False
    operation_id = await source.next_dispatchable_deletion()
    if operation_id is None:
        return False
    await launcher.create_scoped_job(operation_id)
    return True


async def run_bounded_operation_batch(
    worker: OperationBatchWorker,
    *,
    max_operations: int,
) -> int:
    """Drain at most one configured batch and yield when the queue is empty."""

    if not 1 <= max_operations <= 1000:
        raise ValueError("operation batch size must be between 1 and 1000")
    completed = 0
    while completed < max_operations:
        if not await worker.run_once():
            break
        completed += 1
    return completed


async def run_verified_backup_sweep(scheduler: BackupSweep[_ReportT]) -> _ReportT:
    """Run one CronJob sweep and fail closed unless every target meets its RPO gate."""

    report = await scheduler.run_once()
    if (
        getattr(report, "failed", None) != 0
        or getattr(report, "capacity_rpo_met", None) is not True
    ):
        raise RuntimeError("backup sweep did not reach verified durability success")
    return report


async def _dispose_after(
    action: Callable[[], Awaitable[object]],
    dispose: Callable[[], Awaitable[None]],
) -> None:
    try:
        await action()
    finally:
        await dispose()


class ExportGcSettings(BaseSettings):
    """Exact environment consumed by the isolated user-export GC CronJob."""

    model_config = SettingsConfigDict(
        env_prefix="EXOMEM_DURABILITY_",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: SecretStr = Field(min_length=1, max_length=4096)
    database_schema: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    database_role: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    envelope_key: SecretStr = Field(min_length=32, max_length=4096)
    b2_endpoint_url: str = Field(min_length=8, max_length=2048)
    b2_region: str = Field(pattern=r"^[a-z0-9-]{2,64}$")
    user_export_bucket: str = Field(pattern=r"^[a-z0-9-]{6,63}$")
    user_export_delete_key_id: SecretStr = Field(min_length=1, max_length=4096)
    user_export_delete_key: SecretStr = Field(min_length=1, max_length=4096)
    export_limit: int = Field(default=100, ge=1, le=1000)
    delivery_limit: int = Field(default=1000, ge=1, le=10_000)

    @field_validator("database_url")
    @classmethod
    def require_postgres(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("durability jobs require PostgreSQL")
        return value

    @field_validator("b2_endpoint_url")
    @classmethod
    def require_https_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("B2 endpoint must be an HTTPS origin")
        return value.rstrip("/")

    @model_validator(mode="after")
    def require_dedicated_database_role(self) -> ExportGcSettings:
        parsed = urlsplit(self.database_url.get_secret_value())
        if unquote(parsed.username or "") != self.database_role:
            raise ValueError("durability database URL must use its declared role")
        return self


class DatabaseBackupSettings(BaseSettings):
    """Exact environment consumed by the isolated complete-database CronJob."""

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
    database_backup_bucket: str = Field(pattern=r"^[a-z0-9-]{6,63}$")
    database_backup_upload_key_id: SecretStr = Field(min_length=1, max_length=4096)
    database_backup_upload_key: SecretStr = Field(min_length=1, max_length=4096)
    scratch_root: Path
    pg_dump: Path = Field(validation_alias="EXOMEM_DATABASE_BACKUP_PG_DUMP")
    pg_restore: Path = Field(validation_alias="EXOMEM_DATABASE_BACKUP_PG_RESTORE")
    psql: Path = Field(validation_alias="EXOMEM_DATABASE_BACKUP_PSQL")
    dropdb: Path = Field(validation_alias="EXOMEM_DATABASE_BACKUP_DROPDB")
    createdb: Path = Field(validation_alias="EXOMEM_DATABASE_BACKUP_CREATEDB")
    pg_service_file: Path = Field(validation_alias="EXOMEM_DATABASE_BACKUP_PG_SERVICE_FILE")
    pgpass_file: Path = Field(validation_alias="EXOMEM_DATABASE_BACKUP_PGPASS_FILE")
    source_service: str = Field(
        pattern=r"^[A-Za-z0-9_.-]{1,128}$",
        validation_alias="EXOMEM_DATABASE_BACKUP_SOURCE_SERVICE",
    )
    maintenance_service: str = Field(
        pattern=r"^[A-Za-z0-9_.-]{1,128}$",
        validation_alias="EXOMEM_DATABASE_BACKUP_MAINTENANCE_SERVICE",
    )
    scratch_service: str = Field(
        pattern=r"^[A-Za-z0-9_.-]{1,128}$",
        validation_alias="EXOMEM_DATABASE_BACKUP_SCRATCH_SERVICE",
    )
    scratch_database: str = Field(
        pattern=r"^[A-Za-z0-9_.:-]{1,128}$",
        validation_alias="EXOMEM_DATABASE_BACKUP_SCRATCH_DATABASE",
    )
    expected_restore_owner: str = Field(
        pattern=r"^[A-Za-z0-9_.:-]{1,128}$",
        validation_alias="EXOMEM_DATABASE_BACKUP_EXPECTED_RESTORE_OWNER",
    )
    verification_sql: str = Field(
        min_length=1,
        max_length=8192,
        validation_alias="EXOMEM_DATABASE_BACKUP_VERIFICATION_SQL",
    )
    proof_tenant_id: str = Field(
        pattern=r"^[A-Za-z0-9_.:-]{1,256}$",
        validation_alias="EXOMEM_DATABASE_BACKUP_PROOF_TENANT_ID",
    )
    proof_cell_id: str = Field(
        pattern=r"^[A-Za-z0-9_.:-]{1,256}$",
        validation_alias="EXOMEM_DATABASE_BACKUP_PROOF_CELL_ID",
    )
    system_tenant_id: str = Field(
        default="system-hosted-alpha",
        pattern=r"^[A-Za-z0-9_.:-]{1,256}$",
    )
    system_cell_id: str = Field(
        default="control-plane-databases",
        pattern=r"^[A-Za-z0-9_.:-]{1,256}$",
    )
    system_fence_generation: int = Field(default=1, ge=1, le=2**31 - 1)
    worker_id: str = Field(
        default="database-backup-worker",
        pattern=r"^[A-Za-z0-9_.:-]{1,128}$",
    )

    @field_validator("database_url")
    @classmethod
    def require_postgres(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("database backup requires PostgreSQL")
        return value

    @field_validator("b2_endpoint_url")
    @classmethod
    def require_https_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("B2 endpoint must be an HTTPS origin")
        return value.rstrip("/")

    @field_validator(
        "scratch_root",
        "pg_dump",
        "pg_restore",
        "psql",
        "dropdb",
        "createdb",
        "pg_service_file",
        "pgpass_file",
    )
    @classmethod
    def require_absolute_paths(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("database backup paths must be absolute")
        return value

    @model_validator(mode="after")
    def require_dedicated_database_role(self) -> DatabaseBackupSettings:
        parsed = urlsplit(self.database_url.get_secret_value())
        if unquote(parsed.username or "") != self.database_role:
            raise ValueError("durability database URL must use its declared role")
        ProviderRecoveryIdentityCodec.from_encoded_seed(
            self.provider_recovery_signing_key.get_secret_value()
        )
        return self


class DeletionJobSettings(BaseSettings):
    """Bounded batch policy for one isolated discard/destroy Job."""

    model_config = SettingsConfigDict(
        env_prefix="EXOMEM_DELETION_",
        extra="ignore",
        case_sensitive=False,
    )

    batch_size: int = Field(default=25, ge=1, le=1000)


class DeletionDispatcherSettings(BaseSettings):
    """Credential-free dispatcher configuration for one reviewed Job template."""

    model_config = SettingsConfigDict(
        env_prefix="EXOMEM_DELETION_DISPATCHER_",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: SecretStr = Field(min_length=1, max_length=4096)
    database_schema: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    database_role: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    namespace: str = Field(pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
    job_template_path: Path

    @field_validator("database_url")
    @classmethod
    def require_postgres(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("deletion dispatcher requires PostgreSQL")
        return value

    @field_validator("job_template_path")
    @classmethod
    def require_absolute_template(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("deletion Job template path must be absolute")
        return value

    @model_validator(mode="after")
    def require_dedicated_database_role(self) -> DeletionDispatcherSettings:
        parsed = urlsplit(self.database_url.get_secret_value())
        if unquote(parsed.username or "") != self.database_role:
            raise ValueError("dispatcher database URL must use its declared role")
        return self


def _b2_client(settings, *, key_id: SecretStr, application_key: SecretStr):
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


async def _run_export_gc(settings: ExportGcSettings) -> None:
    database = ProvisionerDatabase(settings)  # structurally supplies the three DB fields
    try:
        repository = DurabilityRepository(
            database.session_factory,
            codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        )
        collector = ExportGarbageCollector(
            repository=repository,
            deletion_store=B2DeletionObjectStore(
                _b2_client(
                    settings,
                    key_id=settings.user_export_delete_key_id,
                    application_key=settings.user_export_delete_key,
                ),
                bucket=settings.user_export_bucket,
            ),
        )
        await collector.run_once(
            export_limit=settings.export_limit,
            delivery_limit=settings.delivery_limit,
        )
    finally:
        await database.dispose()


def run_export_gc() -> None:
    configure_content_free_logging()
    try:
        settings = ExportGcSettings()  # type: ignore[call-arg]
        asyncio.run(_run_export_gc(settings))
    except Exception:  # noqa: BLE001 - one-shot job must fail closed without leaking details
        raise SystemExit(1) from None


class _DatabaseTargetSource:
    def __init__(self, settings: DatabaseBackupSettings) -> None:
        self._settings = settings

    async def list_backup_targets(self) -> list[BackupTarget]:
        return [
            BackupTarget(
                self._settings.system_tenant_id,
                self._settings.system_cell_id,
                self._settings.system_fence_generation,
            )
        ]


async def _run_database_backup(settings: DatabaseBackupSettings) -> None:
    database = ProvisionerDatabase(settings)  # structurally supplies the three DB fields
    try:
        repository = DurabilityRepository(
            database.session_factory,
            codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
            lease_seconds=300,
        )
        root_secret = settings.envelope_key.get_secret_value()
        workflow = DatabaseBackupWorkflow(
            repository=repository,
            logical_backup=PostgresLogicalBackup(
                PostgresRecoveryConfig(
                    pg_dump=str(settings.pg_dump),
                    pg_restore=str(settings.pg_restore),
                    psql=str(settings.psql),
                    dropdb=str(settings.dropdb),
                    createdb=str(settings.createdb),
                    service_file=settings.pg_service_file,
                    password_file=settings.pgpass_file,
                    source_service=settings.source_service,
                    maintenance_service=settings.maintenance_service,
                    scratch_service=settings.scratch_service,
                    scratch_database=settings.scratch_database,
                    expected_restore_owner=settings.expected_restore_owner,
                    verification_sql=settings.verification_sql,
                )
            ),
            upload_store=B2UploadOnlyObjectStore(
                _b2_client(
                    settings,
                    key_id=settings.database_backup_upload_key_id,
                    application_key=settings.database_backup_upload_key,
                ),
                bucket=settings.database_backup_bucket,
            ),
            cipher=ChunkedArchiveCipher(),
            key_wrapper=AesGcmKeyWrapper.from_secret(root_secret),
            recovery_envelope_codec=AesGcmRecoveryEnvelopeCodec.from_secret(root_secret),
            provider_identity_signer=ProviderRecoveryIdentityCodec.from_encoded_seed(
                settings.provider_recovery_signing_key.get_secret_value()
            ),
            provider_bucket=settings.database_backup_bucket,
            scratch_root=settings.scratch_root,
        )
        report = await CentralBackupScheduler(
            repository=repository,
            target_source=_DatabaseTargetSource(settings),
            workflow=ScheduledDatabaseBackupWorkflow(
                workflow,
                proof_tenant_id=settings.proof_tenant_id,
                proof_cell_id=settings.proof_cell_id,
            ),
            worker_id=settings.worker_id,
            run_kind=RunKind.DATABASE_BACKUP,
            max_concurrency=1,
        ).run_once()
        if report.failed or report.deferred_busy or report.completed != 1:
            raise RuntimeError("complete database backup did not reach verified success")
    finally:
        await database.dispose()


def run_database_backup() -> None:
    configure_content_free_logging()
    try:
        settings = DatabaseBackupSettings()  # type: ignore[call-arg]
        asyncio.run(_run_database_backup(settings))
    except Exception:  # noqa: BLE001 - one-shot job must fail closed without leaking details
        raise SystemExit(1) from None


async def _run_live_durability_backup() -> None:
    # Provider/platform composition is deliberately imported only by this
    # privileged command. API and routine worker processes cannot load signer
    # credentials or durability provider clients by importing this module.
    from .vault_backup import VaultBackupSettings, run_live_vault_backup

    settings = VaultBackupSettings()  # type: ignore[call-arg]
    await run_live_vault_backup(settings)


def run_durability_backup() -> None:
    configure_content_free_logging()
    try:
        asyncio.run(_run_live_durability_backup())
    except Exception:  # noqa: BLE001 - privileged one-shot output remains content-free
        raise SystemExit(1) from None


async def _run_live_deletion_worker(settings: DeletionJobSettings) -> None:
    from .durability_runtime import live_deletion_worker

    async with live_deletion_worker() as worker:
        await run_bounded_operation_batch(
            worker,
            max_operations=settings.batch_size,
        )


def run_deletion_worker() -> None:
    configure_content_free_logging()
    try:
        settings = DeletionJobSettings()
        asyncio.run(_run_live_deletion_worker(settings))
    except Exception:  # noqa: BLE001 - one-shot deletion Job output remains content-free
        raise SystemExit(1) from None


async def _run_deletion_dispatcher(settings: DeletionDispatcherSettings) -> None:
    from kubernetes import client as kubernetes_client
    from kubernetes import config as kubernetes_config

    template = json.loads(settings.job_template_path.read_text(encoding="utf-8"))
    if not isinstance(template, dict):
        raise ValueError("deletion Job template must be a JSON object")
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "search_path": f"{settings.database_schema},pg_catalog",
                "application_name": "exomem-deletion-dispatcher",
            }
        },
    )
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        kubernetes_config.load_incluster_config()
        await dispatch_one_deletion_job(
            RepositoryDeletionDispatchSource(sessions),
            KubernetesDeletionJobLauncher(
                batch_v1=kubernetes_client.BatchV1Api(),
                namespace=settings.namespace,
                job_template=template,
            ),
        )
    finally:
        await engine.dispose()


def run_deletion_dispatcher() -> None:
    configure_content_free_logging()
    try:
        settings = DeletionDispatcherSettings()  # type: ignore[call-arg]
        asyncio.run(_run_deletion_dispatcher(settings))
    except Exception:  # noqa: BLE001 - dispatcher output remains content-free
        raise SystemExit(1) from None
