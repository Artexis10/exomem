"""Shared lifecycle and durability cell-operation lock.

Revision ID: 0003_cell_operation_lock
Revises: 0002_durability
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "0003_cell_operation_lock"
down_revision: str | None = "0002_durability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    return context.config.attributes.get("provisioner_schema")


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "recovery_objects",
        sa.Column("key_destroyed_at", sa.DateTime(timezone=True)),
        schema=schema,
    )
    op.create_table(
        "cell_operation_locks",
        sa.Column("cell_id", sa.String(256), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("operation_id", sa.String(256), nullable=False),
        sa.Column("fence_generation", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "fence_generation >= 1", name="ck_cell_operation_lock_fence"
        ),
        schema=schema,
    )
    op.create_index(
        "ix_cell_operation_lock_expiry",
        "cell_operation_locks",
        ["lease_expires_at"],
        schema=schema,
    )


def downgrade() -> None:
    op.drop_column("recovery_objects", "key_destroyed_at", schema=_schema())
    op.drop_table("cell_operation_locks", schema=_schema())
