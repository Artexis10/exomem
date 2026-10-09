"""Historical installation schema 3."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_3

revision = "3"
down_revision = '2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_3(op.get_bind().connection.driver_connection)
