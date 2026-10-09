"""Historical installation schema 5."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_5

revision = "5"
down_revision = '4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_5(op.get_bind().connection.driver_connection)
