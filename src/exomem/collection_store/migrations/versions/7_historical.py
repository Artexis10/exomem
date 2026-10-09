"""Historical installation schema 7."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_7

revision = "7"
down_revision = '6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_7(op.get_bind().connection.driver_connection)
