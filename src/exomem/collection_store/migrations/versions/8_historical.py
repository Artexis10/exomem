"""Historical installation schema 8."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_8

revision = "8"
down_revision = '7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_8(op.get_bind().connection.driver_connection)
