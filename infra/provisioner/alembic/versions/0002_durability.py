"""Durable recovery runs, protected objects, and provider rediscovery.

Revision ID: 0002_durability
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "0002_durability"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    return context.config.attributes.get("provisioner_schema")


def _foreign_key(table: str, column: str) -> str:
    schema = _schema()
    prefix = f"{schema}." if schema else ""
    return f"{prefix}{table}.{column}"


def upgrade() -> None:
    schema = _schema()
    run_kind = sa.Enum(
        "USER_EXPORT",
        "VAULT_BACKUP",
        "RESTORE",
        "DATABASE_BACKUP",
        "DATABASE_REDISCOVERY",
        name="durabilityrunkind",
        native_enum=False,
        length=32,
    )
    run_state = sa.Enum(
        "PENDING",
        "CLAIMED",
        "COMPLETE",
        "ERROR",
        name="durabilityrunstate",
        native_enum=False,
        length=16,
    )
    disposition = sa.Enum(
        "ADOPTED",
        "QUARANTINED",
        name="providerdisposition",
        native_enum=False,
        length=16,
    )
    op.create_table(
        "durability_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", run_kind, nullable=False),
        sa.Column("operation_id", sa.String(256), nullable=False),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("cell_id", sa.String(256), nullable=False),
        sa.Column("fence_generation", sa.BigInteger(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", run_state, nullable=False),
        sa.Column("checkpoint", sa.String(64), nullable=False),
        sa.Column("state_ciphertext", sa.Text(), nullable=False),
        sa.Column("result_ciphertext", sa.Text()),
        sa.Column("claim_owner", sa.String(128)),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("fence_generation >= 1", name="ck_durability_run_fence"),
        sa.UniqueConstraint("operation_id", name="uq_durability_run_operation"),
        sa.UniqueConstraint("kind", "cell_id", "scheduled_for", name="uq_durability_run_schedule"),
        schema=schema,
    )
    op.create_index(
        "uq_durability_active_cell",
        "durability_runs",
        ["cell_id"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("state IN ('PENDING', 'CLAIMED')"),
        sqlite_where=sa.text("state IN ('PENDING', 'CLAIMED')"),
    )
    op.create_index(
        "ix_durability_run_claim",
        "durability_runs",
        ["state", "claim_expires_at"],
        schema=schema,
    )
    op.create_table(
        "recovery_objects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey(_foreign_key("durability_runs", "id"), ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", run_kind, nullable=False),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("cell_id", sa.String(256), nullable=False),
        sa.Column("operation_id", sa.String(256), nullable=False),
        sa.Column("fence_generation", sa.BigInteger(), nullable=False),
        sa.Column("opaque_reference_digest", sa.String(64), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("archive_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("archive_size", sa.BigInteger(), nullable=False),
        sa.Column("ciphertext_sha256", sa.String(64), nullable=False),
        sa.Column("ciphertext_size", sa.BigInteger(), nullable=False),
        sa.Column("metadata_sha256", sa.String(64), nullable=False),
        sa.Column("object_lock_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("archive_size > 0", name="ck_recovery_archive_size"),
        sa.CheckConstraint("ciphertext_size > 0", name="ck_recovery_ciphertext_size"),
        sa.CheckConstraint("fence_generation >= 1", name="ck_recovery_fence"),
        sa.UniqueConstraint("run_id", name="uq_recovery_object_run"),
        sa.UniqueConstraint("opaque_reference_digest", name="uq_recovery_object_opaque_ref"),
        schema=schema,
    )
    op.create_index(
        "ix_recovery_cell_verified",
        "recovery_objects",
        ["cell_id", "verified_at"],
        schema=schema,
    )
    op.create_table(
        "provider_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("reference_digest", sa.String(64), nullable=False),
        sa.Column("reference_ciphertext", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("cell_id", sa.String(256)),
        sa.Column("operation_id", sa.String(256), nullable=False),
        sa.Column("fence_generation", sa.BigInteger(), nullable=False),
        sa.Column("disposition", disposition, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("fence_generation >= 1", name="ck_provider_observation_fence"),
        sa.UniqueConstraint("provider", "reference_digest", name="uq_provider_observation"),
        schema=schema,
    )
    op.create_index(
        "ix_provider_observation_tenant_fence",
        "provider_observations",
        ["tenant_id", "fence_generation"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_table("provider_observations", schema=schema)
    op.drop_table("recovery_objects", schema=schema)
    op.drop_table("durability_runs", schema=schema)
