"""Bind canonical typed versions to immutable payload generations."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_11

revision = "11"
down_revision = "10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_11(op.get_bind().connection.driver_connection)
