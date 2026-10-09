"""Historical installation schema 2."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_2

revision = "2"
down_revision = '1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_2(op.get_bind().connection.driver_connection)
