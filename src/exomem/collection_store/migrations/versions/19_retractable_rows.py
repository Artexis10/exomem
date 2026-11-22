"""Let a Records row leave current state while its row and history stay."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_19

revision = "19"
down_revision = "18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_19(op.get_bind().connection.driver_connection)
