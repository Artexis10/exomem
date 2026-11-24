"""Keep each collection's shape history in its own append-only table."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_21

revision = "21"
down_revision = "20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_21(op.get_bind().connection.driver_connection)
