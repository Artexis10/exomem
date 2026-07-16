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
    Uuid,
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


class CellOperationLock(Base):
    """One shared lease for lifecycle and durability effects on a cell."""

    __tablename__ = "cell_operation_locks"
    __table_args__ = (
        CheckConstraint("fence_generation >= 1", name="ck_cell_operation_lock_fence"),
        Index("ix_cell_operation_lock_expiry", "lease_expires_at"),
    )

    cell_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class CapacityReservationClass(StrEnum):
    USER = "USER"
    RECOVERY = "RECOVERY"


class CapacityReleaseReason(StrEnum):
    DISCARD = "DISCARD"
    DESTROY = "DESTROY"


class CapacityLedger(Base):
    """The singleton serialization point for hosted capacity decisions."""

    __tablename__ = "capacity_ledger"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_capacity_ledger_singleton"),
        CheckConstraint("revision >= 0", name="ck_capacity_ledger_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class CapacityReservation(Base):
    """Immutable reservation history with nullable, atomic release metadata."""

    __tablename__ = "capacity_reservations"
    __table_args__ = (
        UniqueConstraint(
            "reserving_operation_id", name="uq_capacity_reservation_reserving_operation"
        ),
        CheckConstraint(
            "reserving_fence_generation >= 1",
            name="ck_capacity_reservation_positive_reserving_fence",
        ),
        CheckConstraint(
            "releasing_fence_generation IS NULL OR releasing_fence_generation >= 1",
            name="ck_capacity_reservation_positive_releasing_fence",
        ),
        CheckConstraint(
            "(released_at IS NULL AND releasing_operation_id IS NULL "
            "AND releasing_provider_operation_id IS NULL "
            "AND releasing_fence_generation IS NULL AND release_reason IS NULL) "
            "OR (released_at IS NOT NULL AND releasing_operation_id IS NOT NULL "
            "AND releasing_provider_operation_id IS NOT NULL "
            "AND releasing_fence_generation IS NOT NULL AND release_reason IS NOT NULL)",
            name="ck_capacity_reservation_release_all_or_none",
        ),
        Index(
            "uq_capacity_reservation_active_tenant_cell",
            "tenant_id",
            "cell_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
            sqlite_where=text("released_at IS NULL"),
        ),
        Index(
            "uq_capacity_reservation_active_resource",
            "resource_name",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
            sqlite_where=text("released_at IS NULL"),
        ),
        Index(
            "ix_capacity_reservation_active_class",
            "reservation_class",
            "released_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    cell_id: Mapped[str] = mapped_column(String(256), nullable=False)
    resource_name: Mapped[str] = mapped_column(String(253), nullable=False)
    reservation_class: Mapped[CapacityReservationClass] = mapped_column(
        Enum(CapacityReservationClass, native_enum=False, length=16), nullable=False
    )
    reserving_operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT"), nullable=False
    )
    reserving_provider_operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    reserving_fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    releasing_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT")
    )
    releasing_provider_operation_id: Mapped[str | None] = mapped_column(String(256))
    releasing_fence_generation: Mapped[int | None] = mapped_column(BigInteger)
    release_reason: Mapped[CapacityReleaseReason | None] = mapped_column(
        Enum(CapacityReleaseReason, native_enum=False, length=16)
    )


class CapacityDestructiveFence(Base):
    """Immutable proof-valid DISCARD/DESTROY completion history."""

    __tablename__ = "capacity_destructive_fences"
    __table_args__ = (
        UniqueConstraint(
            "destructive_operation_id",
            name="uq_capacity_destructive_fence_operation",
        ),
        CheckConstraint(
            "fence_generation >= 1",
            name="ck_capacity_destructive_fence_positive_fence",
        ),
        CheckConstraint(
            "(release_reason = 'DISCARD' AND cell_id IS NOT NULL) OR "
            "(release_reason = 'DESTROY' AND cell_id IS NULL)",
            name="ck_capacity_destructive_fence_scope",
        ),
        Index(
            "ix_capacity_destructive_fence_tenant_fence",
            "tenant_id",
            "fence_generation",
        ),
        Index(
            "ix_capacity_destructive_fence_tenant_cell_fence",
            "tenant_id",
            "cell_id",
            "fence_generation",
        ),
    )

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    cell_id: Mapped[str | None] = mapped_column(String(256))
    release_reason: Mapped[CapacityReleaseReason] = mapped_column(
        Enum(CapacityReleaseReason, native_enum=False, length=16), nullable=False
    )
    destructive_operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT"), nullable=False
    )
    provider_operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


class DurabilityRunKind(StrEnum):
    USER_EXPORT = "user-export"
    VAULT_BACKUP = "vault-backup"
    RESTORE = "restore"
    DATABASE_BACKUP = "database-backup"
    DATABASE_REDISCOVERY = "database-rediscovery"


class DurabilityRunState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETE = "complete"
    ERROR = "error"


class DurabilityRun(Base):
    __tablename__ = "durability_runs"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_durability_run_operation"),
        UniqueConstraint("kind", "cell_id", "scheduled_for", name="uq_durability_run_schedule"),
        CheckConstraint("fence_generation >= 1", name="ck_durability_run_fence"),
        Index(
            "uq_durability_active_cell",
            "cell_id",
            unique=True,
            postgresql_where=text("state IN ('PENDING', 'CLAIMED')"),
            sqlite_where=text("state IN ('PENDING', 'CLAIMED')"),
        ),
        Index("ix_durability_run_claim", "state", "claim_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[DurabilityRunKind] = mapped_column(
        Enum(DurabilityRunKind, native_enum=False, length=32), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    cell_id: Mapped[str] = mapped_column(String(256), nullable=False)
    fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[DurabilityRunState] = mapped_column(
        Enum(DurabilityRunState, native_enum=False, length=16), nullable=False
    )
    checkpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    state_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    result_ciphertext: Mapped[str | None] = mapped_column(Text)
    claim_owner: Mapped[str | None] = mapped_column(String(128))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claim_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecoveryObject(Base):
    __tablename__ = "recovery_objects"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_recovery_object_run"),
        UniqueConstraint("opaque_reference_digest", name="uq_recovery_object_opaque_ref"),
        CheckConstraint("archive_size > 0", name="ck_recovery_archive_size"),
        CheckConstraint("ciphertext_size > 0", name="ck_recovery_ciphertext_size"),
        CheckConstraint("fence_generation >= 1", name="ck_recovery_fence"),
        Index("ix_recovery_cell_verified", "cell_id", "verified_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("durability_runs.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[DurabilityRunKind] = mapped_column(
        Enum(DurabilityRunKind, native_enum=False, length=32), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    cell_id: Mapped[str] = mapped_column(String(256), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    opaque_reference_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ciphertext_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_lock_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    key_destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExportDelivery(Base):
    __tablename__ = "export_deliveries"
    __table_args__ = (
        UniqueConstraint("provider_reference_digest", name="uq_export_delivery_provider_ref"),
        CheckConstraint("fence_generation >= 1", name="ck_export_delivery_fence"),
        Index("ix_export_delivery_tenant_deleted", "tenant_id", "deleted_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_object_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_objects.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    cell_id: Mapped[str] = mapped_column(String(256), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_reference_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_reference_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderDisposition(StrEnum):
    ADOPTED = "adopted"
    QUARANTINED = "quarantined"


class ProviderObservation(Base):
    __tablename__ = "provider_observations"
    __table_args__ = (
        UniqueConstraint("provider", "reference_digest", name="uq_provider_observation"),
        CheckConstraint("fence_generation >= 1", name="ck_provider_observation_fence"),
        Index("ix_provider_observation_tenant_fence", "tenant_id", "fence_generation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    reference_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    cell_id: Mapped[str | None] = mapped_column(String(256))
    operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    fence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    disposition: Mapped[ProviderDisposition] = mapped_column(
        Enum(ProviderDisposition, native_enum=False, length=16), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
