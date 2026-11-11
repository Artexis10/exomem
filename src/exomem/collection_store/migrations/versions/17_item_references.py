"""Index declared references for inverse expansion."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_17

revision = "17"
down_revision = "16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_17(op.get_bind().connection.driver_connection)
