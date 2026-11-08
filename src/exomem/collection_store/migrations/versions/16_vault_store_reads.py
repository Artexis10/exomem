"""Add the page mtime and the two covering indexes that the store-mode read path uses."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_16

revision = "16"
down_revision = "15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_16(op.get_bind().connection.driver_connection)
