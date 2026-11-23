"""Queue the derivation of a store-mode page after the transaction that stores its bytes."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_20

revision = "20"
down_revision = "19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_20(op.get_bind().connection.driver_connection)
