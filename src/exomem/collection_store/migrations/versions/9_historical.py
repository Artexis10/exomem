"""Historical installation schema 9."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_9

revision = "9"
down_revision = '8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_9(op.get_bind().connection.driver_connection)
