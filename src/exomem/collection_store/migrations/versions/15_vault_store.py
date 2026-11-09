"""Add the store-mode tables; every vault keeps them empty until it enters store mode."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_15

revision = "15"
down_revision = "14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_15(op.get_bind().connection.driver_connection)
