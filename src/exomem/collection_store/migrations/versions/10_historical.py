"""Historical installation schema 10."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_10

revision = "10"
down_revision = '9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_10(op.get_bind().connection.driver_connection)
