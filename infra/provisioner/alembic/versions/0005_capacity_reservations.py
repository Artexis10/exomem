"""Serialized hosted capacity reservations.

Revision ID: 0005_capacity_reservations
Revises: 0004_export_delivery_ledger
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import context, op

revision: str = "0005_capacity_reservations"
down_revision: str | None = "0004_export_delivery_ledger"
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
    ledger = op.create_table(
        "capacity_ledger",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_capacity_ledger_singleton"),
        sa.CheckConstraint("revision >= 0", name="ck_capacity_ledger_revision"),
        schema=schema,
    )
    op.bulk_insert(
        ledger,
        [{"id": 1, "revision": 0, "updated_at": datetime.now(UTC)}],
    )
    op.create_table(
        "capacity_reservations",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("cell_id", sa.String(256), nullable=False),
        sa.Column("resource_name", sa.String(253), nullable=False),
        sa.Column("reservation_class", sa.String(16), nullable=False),
        sa.Column(
            "reserving_operation_id",
            sa.String(36),
            sa.ForeignKey(_foreign_key("operations", "id"), ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reserving_provider_operation_id", sa.String(256), nullable=False),
        sa.Column("reserving_fence_generation", sa.BigInteger(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column(
            "releasing_operation_id",
            sa.String(36),
            sa.ForeignKey(_foreign_key("operations", "id"), ondelete="RESTRICT"),
        ),
        sa.Column("releasing_provider_operation_id", sa.String(256)),
        sa.Column("releasing_fence_generation", sa.BigInteger()),
        sa.Column("release_reason", sa.String(16)),
        sa.CheckConstraint(
            "reserving_fence_generation >= 1",
            name="ck_capacity_reservation_positive_reserving_fence",
        ),
        sa.CheckConstraint(
            "releasing_fence_generation IS NULL OR releasing_fence_generation >= 1",
            name="ck_capacity_reservation_positive_releasing_fence",
        ),
        sa.CheckConstraint(
            "(released_at IS NULL AND releasing_operation_id IS NULL "
            "AND releasing_provider_operation_id IS NULL "
            "AND releasing_fence_generation IS NULL AND release_reason IS NULL) "
            "OR (released_at IS NOT NULL AND releasing_operation_id IS NOT NULL "
            "AND releasing_provider_operation_id IS NOT NULL "
            "AND releasing_fence_generation IS NOT NULL AND release_reason IS NOT NULL)",
            name="ck_capacity_reservation_release_all_or_none",
        ),
        sa.UniqueConstraint(
            "reserving_operation_id", name="uq_capacity_reservation_reserving_operation"
        ),
        schema=schema,
    )
    op.create_index(
        "uq_capacity_reservation_active_tenant_cell",
        "capacity_reservations",
        ["tenant_id", "cell_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
        sqlite_where=sa.text("released_at IS NULL"),
        schema=schema,
    )
    op.create_index(
        "uq_capacity_reservation_active_resource",
        "capacity_reservations",
        ["resource_name"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
        sqlite_where=sa.text("released_at IS NULL"),
        schema=schema,
    )
    op.create_index(
        "ix_capacity_reservation_active_class",
        "capacity_reservations",
        ["reservation_class", "released_at"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_table("capacity_reservations", schema=schema)
    op.drop_table("capacity_ledger", schema=schema)
