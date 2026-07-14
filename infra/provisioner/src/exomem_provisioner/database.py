"""Async SQLAlchemy database ownership for the provisioner."""

from __future__ import annotations

from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from .config import ProvisionerSettings
from .models import Base


class ProvisionerDatabase:
    def __init__(self, settings: ProvisionerSettings) -> None:
        database_url = settings.database_url.get_secret_value()
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

    async def create_for_tests(self) -> None:
        """Create test tables; production startup relies exclusively on Alembic."""

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def ready(self) -> bool:
        try:
            async with self.session_factory() as session:
                await session.execute(select(literal(1)))
            return True
        except Exception:  # noqa: BLE001 - readiness is deliberately content-free
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
