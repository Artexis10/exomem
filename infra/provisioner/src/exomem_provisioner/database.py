"""Async SQLAlchemy database ownership for the provisioner."""

from __future__ import annotations

from sqlalchemy import Column, MetaData, String, Table, delete, func, insert, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from .config import ProvisionerSettings
from .models import Base

DATABASE_REVISION = "0004_export_delivery_ledger"


class ProvisionerDatabase:
    def __init__(self, settings: ProvisionerSettings) -> None:
        database_url = settings.database_url.get_secret_value()
        self._settings = settings
        self._is_sqlite = database_url.startswith("sqlite+aiosqlite://")
        options: dict[str, object] = {"pool_pre_ping": True}
        if database_url == "sqlite+aiosqlite:///:memory:":
            options["poolclass"] = StaticPool
        if database_url.startswith("postgresql+asyncpg://"):
            options["connect_args"] = {
                "server_settings": {
                    "search_path": f"{settings.database_schema},pg_catalog",
                    "application_name": "exomem-provisioner",
                }
            }
        self.engine: AsyncEngine = create_async_engine(database_url, **options)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        revision_metadata = MetaData()
        self._revision_table = Table(
            "alembic_version",
            revision_metadata,
            Column("version_num", String(32), nullable=False),
            schema=None if self._is_sqlite else settings.database_schema,
        )

    async def create_for_tests(self) -> None:
        """Create test tables; production startup relies exclusively on Alembic."""

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(self._revision_table.metadata.create_all)
            await connection.execute(delete(self._revision_table))
            await connection.execute(
                insert(self._revision_table).values(version_num=DATABASE_REVISION)
            )

    async def ready(self) -> bool:
        try:
            async with self.session_factory() as session:
                revisions = list(await session.scalars(select(self._revision_table.c.version_num)))
                if self._is_sqlite:
                    role = self._settings.database_role
                    schema = self._settings.database_schema
                    schema_owner = self._settings.database_role
                else:
                    role = await session.scalar(select(func.current_user()))
                    schema = await session.scalar(select(func.current_schema()))
                    schema_owner = await session.scalar(
                        text(
                            "SELECT pg_get_userbyid(nspowner) "
                            "FROM pg_namespace WHERE nspname = :schema"
                        ),
                        {"schema": self._settings.database_schema},
                    )
            return (
                revisions == [DATABASE_REVISION]
                and role == self._settings.database_role
                and schema == self._settings.database_schema
                and schema_owner == self._settings.database_role
            )
        except Exception:  # noqa: BLE001 - readiness is deliberately content-free
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
