"""Keep the authored timeline facts of Records writes inside the hash chain, and the derived span index."""

from alembic import op

from exomem.collection_store.schema import TIMELINE_SCHEMA_VERSION, _add_timeline_facts

# The number lives in the schema module, so a migration that merges before this one moves it there alone.
revision = str(TIMELINE_SCHEMA_VERSION)
down_revision = str(TIMELINE_SCHEMA_VERSION - 1)
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_timeline_facts(op.get_bind().connection.driver_connection)
