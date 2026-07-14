from __future__ import annotations

import asyncio
import os
import re
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from exomem_provisioner.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,62}$")


def _configuration() -> tuple[str, str | None, str | None]:
    url = os.environ.get("EXOMEM_PROVISIONER_DATABASE_URL") or config.get_main_option(
        "sqlalchemy.url"
    )
    schema = os.environ.get("EXOMEM_PROVISIONER_DATABASE_SCHEMA")
    role = os.environ.get("EXOMEM_PROVISIONER_DATABASE_ROLE")
    if url.startswith("sqlite"):
        return url, None, None
    if (
        not schema
        or not role
        or not _IDENTIFIER.fullmatch(schema)
        or not _IDENTIFIER.fullmatch(role)
    ):
        raise RuntimeError("dedicated provisioner schema and role are required")
    return url, schema, role


database_url, provisioner_schema, provisioner_role = _configuration()
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
config.attributes["provisioner_schema"] = provisioner_schema
config.attributes["provisioner_role"] = provisioner_role


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=provisioner_schema,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table_schema=provisioner_schema,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_sync_migrations() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run_migrations(connection)
    connectable.dispose()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif "+asyncpg" in database_url or "+aiosqlite" in database_url:
    asyncio.run(run_async_migrations())
else:
    run_sync_migrations()
