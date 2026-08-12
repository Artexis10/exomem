"""Add immutable transactional receipts for init-retry operation recovery.

Revision ID: 0007_operation_recovery_receipt
Revises: 0006_operation_wire_protocol
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "0007_operation_recovery_receipt"
down_revision: str | Sequence[str] | None = "0006_operation_wire_protocol"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HASHES = (
    "helper_source_sha256",
    "operation_sha256",
    "preserved_sha256",
    "request_sha256",
    "request_ciphertext_sha256",
    "resources_sha256",
    "reservation_sha256",
    "tenant_fence_sha256",
    "first_observation_sha256",
    "second_observation_sha256",
    "committed_operation_sha256",
)


def _schema() -> str | None:
    return context.config.attributes.get("provisioner_schema")


def _qualified(name: str) -> str:
    schema = _schema()
    return f'"{schema}"."{name}"' if schema else f'"{name}"'


def _foreign_key(table: str, column: str) -> str:
    schema = _schema()
    return f"{schema}.{table}.{column}" if schema else f"{table}.{column}"


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "operation_recovery_receipts",
        sa.Column(
            "operation_id",
            sa.String(36),
            sa.ForeignKey(_foreign_key("operations", "id"), ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("helper_source_sha256", sa.String(64), nullable=False),
        sa.Column("old_state", sa.String(16), nullable=False),
        sa.Column("old_checkpoint", sa.String(256), nullable=False),
        sa.Column("new_state", sa.String(16), nullable=False),
        sa.Column("new_checkpoint", sa.String(256), nullable=False),
        sa.Column("resource_count", sa.Integer(), nullable=False),
        sa.Column("route_count", sa.Integer(), nullable=False),
        sa.Column("init_job_present", sa.Boolean(), nullable=False),
        sa.Column("init_job_complete", sa.Boolean(), nullable=False),
        *(sa.Column(name, sa.String(64), nullable=False) for name in _HASHES[1:]),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="ck_recovery_receipt_schema_version"),
        sa.CheckConstraint("old_state = 'error'", name="ck_recovery_receipt_old_state"),
        sa.CheckConstraint("old_checkpoint = 'failed'", name="ck_recovery_receipt_old_checkpoint"),
        sa.CheckConstraint("new_state = 'pending'", name="ck_recovery_receipt_new_state"),
        sa.CheckConstraint(
            "new_checkpoint = 'volume-owned'", name="ck_recovery_receipt_new_checkpoint"
        ),
        sa.CheckConstraint("resource_count = 4", name="ck_recovery_receipt_resource_count"),
        sa.CheckConstraint("route_count = 0", name="ck_recovery_receipt_route_count"),
        *(sa.CheckConstraint(f"length({name}) = 64", name=f"ck_recovery_receipt_{name}_hash") for name in _HASHES),
        schema=schema,
    )
    if op.get_bind().dialect.name == "postgresql":
        table = _qualified("operation_recovery_receipts")
        function = _qualified("prevent_operation_recovery_receipt_mutation")
        op.execute(
            sa.text(
                f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'operation recovery receipts are immutable'; END; $$"
            )
        )
        for event in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER operation_recovery_receipts_no_{event.lower()} "
                    f"BEFORE {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {function}()"
                )
            )


def downgrade() -> None:
    schema = _schema()
    if op.get_bind().dialect.name == "postgresql":
        table = _qualified("operation_recovery_receipts")
        function = _qualified("prevent_operation_recovery_receipt_mutation")
        for event in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"DROP TRIGGER IF EXISTS operation_recovery_receipts_no_{event.lower()} ON {table}"
                )
            )
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}()"))
    op.drop_table("operation_recovery_receipts", schema=schema)
