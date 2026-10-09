"""Historical installation schema 1."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_1

revision = "1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_1(op.get_bind().connection.driver_connection)
