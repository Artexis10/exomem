"""Record each collection's item key scheme; every existing collection keeps hashed-v1."""

from alembic import op

from exomem.collection_store.schema import _migrate_to_14

revision = "14"
down_revision = "13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _migrate_to_14(op.get_bind().connection.driver_connection)
