"""Historical installation schema 6."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_6

revision = "6"
down_revision = '5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_6(op.get_bind().connection.driver_connection)
