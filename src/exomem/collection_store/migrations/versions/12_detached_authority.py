"""Restrict detached candidates whose canonical authority is unresolved."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_12

revision = "12"
down_revision = "11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_12(op.get_bind().connection.driver_connection)
