"""Authenticate exact Source associations beside immutable payload versions."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_18

revision = "18"
down_revision = "17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_18(op.get_bind().connection.driver_connection)
