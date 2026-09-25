"""Disposable Postgres fixture for cellctl tests.

Starts a throwaway `postgres` server process directly from the pinned
PostgreSQL 16 binaries (no Docker), listening on 127.0.0.1 on a free port
with unix sockets disabled, applies the C1-C1d fixture schema and grants,
and yields per-role asyncpg DSNs. The server and its data directory are torn
down at the end of the test session.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA_SQL = (FIXTURES / "exomem_cloud_schema.sql").read_text(encoding="utf-8")
GRANTS_SQL = (FIXTURES / "exomem_cloud_grants.sql").read_text(encoding="utf-8")

# Migration 0056 references exomem_tenants(id), which an earlier Substrate
# migration owns. The fixtures carry only the Cloud files, so the suite
# creates the columns C1's foreign key and the grants script touch.
TENANTS_STANDIN_SQL = """
CREATE TABLE exomem_tenants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  status text NOT NULL DEFAULT 'active'
);
"""

_TENANT_NAMESPACE = uuid.UUID("5f0c1b1e-7d4a-4c2e-9a51-3c6b2f8e0d17")

def tenant_uuid(label: str) -> uuid.UUID:
    """A stable tenant id for a readable test label."""

    return uuid.uuid5(_TENANT_NAMESPACE, label)


async def insert_tenant(connection: asyncpg.Connection, label: str) -> uuid.UUID:
    tenant_id = tenant_uuid(label)
    await connection.execute("INSERT INTO exomem_tenants (id) VALUES ($1)", tenant_id)
    return tenant_id


ROLES = ("substrate_owner", "substrate_app", "exomem_cellctl", "exomem_gateway")


def _default_pg_bin() -> Path:
    override = os.environ.get("CELLCTL_PG_BIN")
    if override:
        return Path(override)
    raise RuntimeError(
        "Set CELLCTL_PG_BIN to the directory containing the postgres/initdb binaries"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass(frozen=True)
class PostgresServer:
    host: str
    port: int
    admin_dsn: str

    def dsn(self, *, role: str | None = None, database: str = "postgres") -> str:
        user = role or "postgres"
        return f"postgresql://{user}@{self.host}:{self.port}/{database}"


async def _wait_ready(dsn: str, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            connection = await asyncpg.connect(dsn, timeout=2)
        except (OSError, asyncpg.PostgresError) as error:
            last_error = error
            await asyncio.sleep(0.1)
            continue
        await connection.close()
        return
    raise RuntimeError(f"postgres did not become ready in time: {last_error}")


async def _bootstrap_roles(admin_dsn: str) -> None:
    connection = await asyncpg.connect(admin_dsn)
    try:
        for role in ROLES:
            await connection.execute(f'CREATE ROLE "{role}" LOGIN')
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def postgres_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresServer]:
    pg_bin = _default_pg_bin()
    data_dir = tmp_path_factory.mktemp("cellctl-pgdata")
    port = _free_port()
    log_path = data_dir.parent / "postgres.log"

    subprocess.run(
        [
            str(pg_bin / "initdb"),
            "-D",
            str(data_dir),
            "-U",
            "postgres",
            "-A",
            "trust",
            "--no-sync",
        ],
        check=True,
        capture_output=True,
    )

    log_file = open(log_path, "wb")
    process = subprocess.Popen(
        [
            str(pg_bin / "postgres"),
            "-D",
            str(data_dir),
            "-p",
            str(port),
            "-h",
            "127.0.0.1",
            "-k",
            "",
            "-c",
            "unix_socket_directories=",
            "-c",
            "listen_addresses=127.0.0.1",
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    admin_dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    try:
        asyncio.run(_wait_ready(admin_dsn))
        asyncio.run(_bootstrap_roles(admin_dsn))
        yield PostgresServer(host="127.0.0.1", port=port, admin_dsn=admin_dsn)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log_file.close()


_DB_COUNTER = 0


async def _create_cell_db(server: PostgresServer) -> str:
    global _DB_COUNTER
    _DB_COUNTER += 1
    name = f"cellctl_test_{os.getpid()}_{_DB_COUNTER}"
    admin = await asyncpg.connect(server.admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}" OWNER "substrate_owner"')
    finally:
        await admin.close()

    owner_dsn = server.dsn(role="substrate_owner", database=name)
    owner_connection = await asyncpg.connect(owner_dsn)
    try:
        await owner_connection.execute(TENANTS_STANDIN_SQL)
        await owner_connection.execute(SCHEMA_SQL)
        await owner_connection.execute(GRANTS_SQL)
    finally:
        await owner_connection.close()
    return name


async def _drop_cell_db(server: PostgresServer, name: str) -> None:
    admin = await asyncpg.connect(server.admin_dsn)
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await admin.close()


@dataclass(frozen=True)
class CellDatabase:
    server: PostgresServer
    name: str

    def dsn(self, *, role: str | None = None) -> str:
        return self.server.dsn(role=role, database=self.name)


@pytest.fixture
def cell_db(postgres_server: PostgresServer) -> Iterator[CellDatabase]:
    name = asyncio.run(_create_cell_db(postgres_server))
    try:
        yield CellDatabase(server=postgres_server, name=name)
    finally:
        asyncio.run(_drop_cell_db(postgres_server, name))
