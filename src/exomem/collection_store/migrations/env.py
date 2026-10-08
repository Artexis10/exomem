"""Installation revisions share the caller's single transaction."""

from alembic import context

from exomem.collection_store.schema import TABLES

connection = context.config.attributes["connection"]
context.configure(
    connection=connection,
    transactional_ddl=True,
    # User declarations belong to runtime mapping authorities, never autogeneration.
    include_name=lambda name, type_, parent_names: type_ != "table" or name in TABLES,
)
with context.begin_transaction():
    context.run_migrations()
