"""Initial durable provisioner schema.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    return context.config.attributes.get("provisioner_schema")


def _role() -> str | None:
    return context.config.attributes.get("provisioner_role")


def _foreign_key(table: str, column: str) -> str:
    schema = _schema()
    prefix = f"{schema}." if schema else ""
    return f"{prefix}{table}.{column}"


def upgrade() -> None:
    schema = _schema()
    role = _role()
    if schema is not None and role is not None:
        op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{schema}" AUTHORIZATION "{role}"'))

    action = sa.Enum(
        "PROVISION",
        "HEALTH",
        "ROTATE_CREDENTIAL",
        "QUIESCE",
        "RESUME",
        "STOP",
        "EXPORT",
        "EXPORT_RELEASE",
        "EXPORT_DELETE",
        "RESTORE",
        "EXPORT_DOWNLOAD",
        "SEAL",
        "DISCARD",
        "DESTROY",
        name="operationaction",
        native_enum=False,
        length=32,
    )
    state = sa.Enum(
        "PENDING",
        "CLAIMED",
        "FINAL",
        "ERROR",
        name="operationstate",
        native_enum=False,
        length=16,
    )
    resource_kind = sa.Enum(
        "KUBERNETES_NAMESPACE",
        "HELM_RELEASE",
        "PVC",
        "VOLUME",
        "ROUTE",
        "PROVIDER_OBJECT",
        name="resourcekind",
        native_enum=False,
        length=32,
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("action", action, nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("canonical_request_sha256", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("cell_id", sa.String(256)),
        sa.Column("external_operation_id", sa.String(256), nullable=False),
        sa.Column("fence_generation", sa.BigInteger(), nullable=False),
        sa.Column("provider_operation_id", sa.String(256), nullable=False),
        sa.Column("provider_fence_generation", sa.BigInteger(), nullable=False),
        sa.Column("state", state, nullable=False),
        sa.Column("checkpoint", sa.String(256), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("request_ciphertext", sa.Text(), nullable=False),
        sa.Column("result_ciphertext", sa.Text()),
        sa.Column("result_redacted", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("retry_after_seconds", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_owner", sa.String(128)),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("fence_generation >= 1", name="ck_operation_positive_fence"),
        sa.CheckConstraint("length(canonical_request_sha256) = 64", name="ck_operation_hash"),
        sa.UniqueConstraint("action", "idempotency_key", name="uq_operation_action_key"),
        schema=schema,
    )
    op.create_index(
        "ix_operation_claim",
        "operations",
        ["state", "available_at", "claim_expires_at"],
        schema=schema,
    )
    op.create_index(
        "ix_operation_tenant_fence",
        "operations",
        ["tenant_id", "fence_generation"],
        schema=schema,
    )
    op.create_table(
        "tenant_fences",
        sa.Column("tenant_id", sa.String(256), primary_key=True),
        sa.Column("fence_generation", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("fence_generation >= 1", name="ck_tenant_fence"),
        schema=schema,
    )
    op.create_table(
        "resources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "operation_id",
            sa.String(36),
            sa.ForeignKey(_foreign_key("operations", "id"), ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("cell_id", sa.String(256)),
        sa.Column("kind", resource_kind, nullable=False),
        sa.Column("reference_digest", sa.String(64), nullable=False),
        sa.Column("reference_ciphertext", sa.Text(), nullable=False),
        sa.Column("provider_operation_id", sa.String(256), nullable=False),
        sa.Column("provider_fence_generation", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider_fence_generation >= 1", name="ck_resource_fence"),
        sa.UniqueConstraint("operation_id", "kind", name="uq_resource_operation_kind"),
        schema=schema,
    )
    op.create_index("ix_resource_tenant_cell", "resources", ["tenant_id", "cell_id"], schema=schema)
    op.create_table(
        "credential_metadata",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "operation_id",
            sa.String(36),
            sa.ForeignKey(_foreign_key("operations", "id"), ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("cell_id", sa.String(256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("credential_digest", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_credential_version"),
        sa.CheckConstraint("length(credential_digest) = 64", name="ck_credential_digest"),
        sa.UniqueConstraint("cell_id", "version", name="uq_credential_cell_version"),
        schema=schema,
    )
    for table, hash_column, size_column in (
        ("exports", "archive_sha256", True),
        ("backups", "object_sha256", False),
    ):
        columns = [
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "operation_id",
                sa.String(36),
                sa.ForeignKey(_foreign_key("operations", "id"), ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("tenant_id", sa.String(256), nullable=False),
            sa.Column("cell_id", sa.String(256), nullable=False),
            sa.Column("reference_digest", sa.String(64), nullable=False),
            sa.Column("reference_ciphertext", sa.Text(), nullable=False),
            sa.Column(hash_column, sa.String(64), nullable=False),
        ]
        if size_column:
            columns.extend(
                [
                    sa.Column("manifest_sha256", sa.String(64), nullable=False),
                    sa.Column("archive_size", sa.BigInteger(), nullable=False),
                ]
            )
        columns.extend(
            [
                sa.Column("provider_operation_id", sa.String(256), nullable=False),
                sa.Column("provider_fence_generation", sa.BigInteger(), nullable=False),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                sa.CheckConstraint("provider_fence_generation >= 1", name=f"ck_{table[:-1]}_fence"),
                sa.UniqueConstraint("operation_id", name=f"uq_{table[:-1]}_operation"),
            ]
        )
        if size_column:
            columns.append(sa.CheckConstraint("archive_size > 0", name="ck_export_size"))
        op.create_table(table, *columns, schema=schema)

    if schema is not None and role is not None:
        op.execute(sa.text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        op.execute(
            sa.text(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"'
            )
        )


def downgrade() -> None:
    schema = _schema()
    for table in (
        "backups",
        "exports",
        "credential_metadata",
        "resources",
        "tenant_fences",
        "operations",
    ):
        op.drop_table(table, schema=schema)
