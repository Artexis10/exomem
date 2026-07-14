"""SQLAlchemy 2 durable provisioner records."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class OperationAction(StrEnum):
    PROVISION = "provision"
    HEALTH = "health"
    ROTATE_CREDENTIAL = "rotate-credential"
    QUIESCE = "quiesce"
    RESUME = "resume"
    STOP = "stop"
    EXPORT = "export"
    EXPORT_RELEASE = "export-release"
    EXPORT_DELETE = "export-delete"
    RESTORE = "restore"
    EXPORT_DOWNLOAD = "export-download"
    SEAL = "seal"
    DISCARD = "discard"
    DESTROY = "destroy"


class OperationState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    FINAL = "final"
    ERROR = "error"


class ResourceKind(StrEnum):
    KUBERNETES_NAMESPACE = "kubernetes-namespace"
    HELM_RELEASE = "helm-release"
    PVC = "pvc"
    VOLUME = "volume"
    ROUTE = "route"
    PROVIDER_OBJECT = "provider-object"


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("action", "idempotency_key", name="uq_operation_action_key"),
        CheckConstraint("fence_generation >= 1", name="ck_operation_positive_fence"),
        CheckConstraint("length(canonical_request_sha256) = 64", name="ck_operation_hash"),
        Index("ix_operation_claim", "state", "available_at", "claim_expires_at"),
        Index("ix_operation_tenant_fence", "tenant_id", "fence_generation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    action: Mapped[OperationAction] = mapped_column(
        Enum(OperationAction, native_enum=False, length=32), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    canonical_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    cell_id: Mapped[str | None] = mapped_column(String(256))
    external_operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider_fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[OperationState] = mapped_column(
        Enum(OperationState, native_enum=False, length=16),
        default=OperationState.PENDING,
        nullable=False,
    )
    caller_checkpoint: Mapped[str] = mapped_column(String(256), nullable=False)
    checkpoint: Mapped[str] = mapped_column(String(256), nullable=False)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    request_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    result_ciphertext: Mapped[str | None] = mapped_column(Text)
    result_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    retry_after_seconds: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    claim_owner: Mapped[str | None] = mapped_column(String(128))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claim_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TenantFence(Base):
    __tablename__ = "tenant_fences"
    __table_args__ = (CheckConstraint("fence_generation >= 1", name="ck_tenant_fence"),)

    tenant_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("operation_id", "kind", name="uq_resource_operation_kind"),
        CheckConstraint("provider_fence_generation >= 1", name="ck_resource_fence"),
        Index("ix_resource_tenant_cell", "tenant_id", "cell_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    cell_id: Mapped[str | None] = mapped_column(String(256))
    kind: Mapped[ResourceKind] = mapped_column(
        Enum(ResourceKind, native_enum=False, length=32), nullable=False
    )
    reference_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    provider_operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider_fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CredentialMetadata(Base):
    __tablename__ = "credential_metadata"
    __table_args__ = (
        UniqueConstraint("cell_id", "version", name="uq_credential_cell_version"),
        CheckConstraint("version >= 1", name="ck_credential_version"),
        CheckConstraint("length(credential_digest) = 64", name="ck_credential_digest"),
        Index(
            "uq_credential_one_active_per_cell",
            "cell_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT"), nullable=False
    )
    cell_id: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    credential_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ExportRecord(Base):
    __tablename__ = "exports"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_export_operation"),
        CheckConstraint("archive_size > 0", name="ck_export_size"),
        CheckConstraint("provider_fence_generation >= 1", name="ck_export_fence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    cell_id: Mapped[str] = mapped_column(String(256), nullable=False)
    reference_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider_fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class BackupRecord(Base):
    __tablename__ = "backups"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_backup_operation"),
        CheckConstraint("provider_fence_generation >= 1", name="ck_backup_fence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    cell_id: Mapped[str] = mapped_column(String(256), nullable=False)
    reference_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    object_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider_fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
