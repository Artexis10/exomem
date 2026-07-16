"""Durable exact-version ledger for plaintext export deliveries.

Revision ID: 0004_export_delivery_ledger
Revises: 0003_cell_operation_lock
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "0004_export_delivery_ledger"
down_revision: str | None = "0003_cell_operation_lock"
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
    op.create_table(
        "export_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "source_object_id",
            sa.String(36),
            sa.ForeignKey(_foreign_key("recovery_objects", "id"), ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("cell_id", sa.String(256), nullable=False),
        sa.Column("operation_id", sa.String(256), nullable=False),
        sa.Column("fence_generation", sa.BigInteger(), nullable=False),
        sa.Column("provider_reference_digest", sa.String(64), nullable=False),
        sa.Column("provider_reference_ciphertext", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("fence_generation >= 1", name="ck_export_delivery_fence"),
        sa.UniqueConstraint(
            "provider_reference_digest", name="uq_export_delivery_provider_ref"
        ),
        schema=schema,
    )
    op.create_index(
        "ix_export_delivery_tenant_deleted",
        "export_deliveries",
        ["tenant_id", "deleted_at"],
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("export_deliveries", schema=_schema())
