"""Persist the exact provisioner wire protocol for every operation.

Revision ID: 0006_operation_wire_protocol
Revises: 0005_capacity_reservations
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "0006_operation_wire_protocol"
down_revision: str | Sequence[str] | None = "0005_capacity_reservations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1 = "exomem-cell-provisioner.v1"
_V2 = "exomem-cell-provisioner.v2"


def _schema() -> str | None:
    return context.config.attributes.get("provisioner_schema")


def _qualified(name: str) -> str:
    schema = _schema()
    return f'"{schema}"."{name}"' if schema else f'"{name}"'


def upgrade() -> None:
    schema = _schema()
    with op.batch_alter_table("operations", schema=schema) as batch:
        batch.add_column(
            sa.Column(
                "wire_protocol",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text(f"'{_V1}'"),
            )
        )
        batch.create_check_constraint(
            "ck_operation_wire_protocol",
            f"wire_protocol IN ('{_V1}', '{_V2}')",
        )
    if op.get_bind().dialect.name == "postgresql":
        table = _qualified("operations")
        function = _qualified("prevent_operation_wire_protocol_change")
        op.execute(
            sa.text(
                f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF NEW.wire_protocol <> OLD.wire_protocol THEN "
                "RAISE EXCEPTION 'operation wire protocol is immutable'; END IF; RETURN NEW; END; $$"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER operations_wire_protocol_immutable BEFORE UPDATE ON "
                f"{table} FOR EACH ROW "
                f"EXECUTE FUNCTION {function}()"
            )
        )


def downgrade() -> None:
    schema = _schema()
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS operations_wire_protocol_immutable ON "
                f"{_qualified('operations')}"
            )
        )
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_qualified('prevent_operation_wire_protocol_change')}()"))
    with op.batch_alter_table("operations", schema=schema) as batch:
        batch.drop_constraint("ck_operation_wire_protocol", type_="check")
        batch.drop_column("wire_protocol")
