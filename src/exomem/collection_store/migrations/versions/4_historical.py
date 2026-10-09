"""Historical installation schema 4."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_4

revision = "4"
down_revision = '3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_4(op.get_bind().connection.driver_connection)
