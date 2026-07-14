from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update

import exomem_provisioner.repository as repository_module
from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import DATABASE_REVISION, ProvisionerDatabase
from exomem_provisioner.models import Operation, TenantFence
from exomem_provisioner.repository import OperationRepository, StaleFence

RUN_POSTGRESQL17 = os.environ.get("RUN_POSTGRESQL17_TEST") == "1"
POSTGRESQL17_IMAGE = os.environ.get("POSTGRESQL17_IMAGE", "postgres:17-alpine")
PROVISIONER_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not RUN_POSTGRESQL17,
    reason="set RUN_POSTGRESQL17_TEST=1 to run the real PostgreSQL 17 gates",
)


@dataclass(frozen=True, slots=True)
class PostgreSQL17:
    container: str
    port: int
    admin_password: str


@dataclass(frozen=True, slots=True)
class ProvisionerTestDatabase:
    name: str
    role: str
    schema: str
    settings: ProvisionerSettings


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=PROVISIONER_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


@pytest.fixture(scope="module")
def postgresql17() -> Iterator[PostgreSQL17]:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for the PostgreSQL 17 integration gates")
    container = f"exomem-provisioner-pg17-{uuid.uuid4().hex[:12]}"
    admin_password = f"admin-{uuid.uuid4().hex}"
    _run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            container,
            "--env",
            f"POSTGRES_PASSWORD={admin_password}",
            "--publish",
            "127.0.0.1::5432",
            POSTGRESQL17_IMAGE,
        ]
    )
    try:
        for _ in range(60):
            ready = _run(
                ["docker", "exec", container, "pg_isready", "--username", "postgres"],
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise AssertionError(_run(["docker", "logs", container], check=False).stdout)
        port_output = _run(["docker", "port", container, "5432/tcp"]).stdout.strip()
        port_match = re.search(r":([0-9]+)$", port_output)
        assert port_match is not None, port_output
        server = PostgreSQL17(
            container=container,
            port=int(port_match.group(1)),
            admin_password=admin_password,
        )
        version = _psql(server, "SHOW server_version_num;")
        assert version.stdout.strip().splitlines()[-1].startswith("17")
        yield server
    finally:
        _run(["docker", "rm", "--force", container], check=False)


def _psql(
    server: PostgreSQL17,
    statement: str,
    *,
    database: str = "postgres",
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "docker",
            "exec",
            server.container,
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--set",
            "ON_ERROR_STOP=1",
            "--username",
            "postgres",
            "--dbname",
            database,
            "--command",
            statement,
        ]
    )


def _new_database(server: PostgreSQL17, label: str) -> ProvisionerTestDatabase:
    suffix = uuid.uuid4().hex[:10]
    name = f"provisioner_{label}_{suffix}"
    role = f"runtime_{label}_{suffix}"
    schema = f"schema_{label}_{suffix}"
    password = f"runtime-{suffix}"
    _psql(server, f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}';")
    _psql(server, f'CREATE DATABASE "{name}" OWNER postgres;')
    _psql(server, f'GRANT CONNECT, CREATE ON DATABASE "{name}" TO "{role}";')
    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=(f"postgresql+asyncpg://{role}:{password}@127.0.0.1:{server.port}/{name}"),
        database_schema=schema,
        database_role=role,
        trusted_proxy_ips="127.0.0.1",
    )
    return ProvisionerTestDatabase(name=name, role=role, schema=schema, settings=settings)


def _migrate(database: ProvisionerTestDatabase) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "EXOMEM_PROVISIONER_DATABASE_URL": (database.settings.database_url.get_secret_value()),
            "EXOMEM_PROVISIONER_DATABASE_SCHEMA": database.schema,
            "EXOMEM_PROVISIONER_DATABASE_ROLE": database.role,
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=PROVISIONER_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _request(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "operationId": "operation-postgresql-alpha",
        "checkpoint": "requested",
        "fenceGeneration": 7,
        "tenantId": "tenant-postgresql-alpha",
        "cellId": "cell-postgresql-alpha",
        "protocolVersion": "exomem-hosted.v1",
        "releaseVersion": "0.22.0",
        "serviceCredential": "service-credential-postgresql-sentinel",
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
    }
    request.update(overrides)
    return request


def _repository(
    database: ProvisionerDatabase, settings: ProvisionerSettings
) -> OperationRepository:
    return OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
    )


@pytest.mark.asyncio
async def test_fresh_postgresql17_bootstrap_creates_owned_schema_before_version_table(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "fresh")

    migrated = _migrate(target)

    assert migrated.returncode == 0, migrated.stdout + migrated.stderr
    database = ProvisionerDatabase(target.settings)
    try:
        async with database.engine.connect() as connection:
            version = await connection.scalar(text("SHOW server_version_num"))
            owner = await connection.scalar(
                text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=:schema"),
                {"schema": target.schema},
            )
            revisions = (
                (
                    await connection.execute(
                        text(f'SELECT version_num FROM "{target.schema}".alembic_version')
                    )
                )
                .scalars()
                .all()
            )
            tables = set(
                (
                    await connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname=:schema "
                            "ORDER BY tablename"
                        ),
                        {"schema": target.schema},
                    )
                ).scalars()
            )
        assert str(version).startswith("17")
        assert owner == target.role
        assert revisions == [DATABASE_REVISION]
        assert {"operations", "tenant_fences", "resources", "credential_metadata"} <= tables
        assert await database.ready() is True

        wrong_owner = f"wrong_owner_{uuid.uuid4().hex[:8]}"
        _psql(postgresql17, f'CREATE ROLE "{wrong_owner}";', database=target.name)
        _psql(
            postgresql17,
            f'ALTER SCHEMA "{target.schema}" OWNER TO "{wrong_owner}"; '
            f'GRANT ALL ON SCHEMA "{target.schema}" TO "{target.role}";',
            database=target.name,
        )
        assert await database.ready() is False
    finally:
        await database.dispose()


def test_postgresql17_migration_rejects_existing_schema_with_wrong_owner(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "owner")
    wrong_owner = f"wrong_owner_{uuid.uuid4().hex[:8]}"
    _psql(postgresql17, f'CREATE ROLE "{wrong_owner}";', database=target.name)
    _psql(
        postgresql17,
        f'CREATE SCHEMA "{target.schema}" AUTHORIZATION "{wrong_owner}"; '
        f'GRANT ALL ON SCHEMA "{target.schema}" TO "{target.role}";',
        database=target.name,
    )

    migrated = _migrate(target)

    assert migrated.returncode != 0
    assert "schema owner does not match dedicated runtime role" in (
        migrated.stdout + migrated.stderr
    )
    owner = _psql(
        postgresql17,
        f"SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='{target.schema}';",
        database=target.name,
    )
    assert owner.stdout.strip() == wrong_owner


@pytest.mark.asyncio
async def test_postgresql17_claim_waits_for_and_rechecks_concurrent_higher_fence(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "claim")
    migrated = _migrate(target)
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr
    database = ProvisionerDatabase(target.settings)
    repository = _repository(database, target.settings)
    try:
        await repository.submit("provision", "claim-race", _request())
        async with database.session_factory() as blocker:
            async with blocker.begin():
                fence = await blocker.scalar(
                    select(TenantFence)
                    .where(TenantFence.tenant_id == "tenant-postgresql-alpha")
                    .with_for_update()
                )
                assert fence is not None
                fence.fence_generation = 8
                await blocker.flush()
                claiming = asyncio.create_task(repository.claim_next("claim-race-worker"))
                await asyncio.sleep(0.2)
                assert not claiming.done(), "claim acquisition did not lock TenantFence first"
            claimed = await asyncio.wait_for(claiming, timeout=5)
        assert claimed is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_postgresql17_stale_post_claim_completion_is_rejected(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "completion")
    migrated = _migrate(target)
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr
    database = ProvisionerDatabase(target.settings)
    repository = _repository(database, target.settings)
    try:
        operation = await repository.submit("provision", "stale-completion", _request())
        claim = await repository.claim_next("completion-worker")
        assert claim is not None and claim.claim_token
        await repository.submit(
            "destroy",
            "newer-fence",
            _request(operationId="operation-postgresql-newer", fenceGeneration=8),
        )

        with pytest.raises(StaleFence, match="active claim fence"):
            await repository.complete(
                operation.id,
                {},
                worker_id="completion-worker",
                claim_token=claim.claim_token,
                claim_generation=claim.claim_generation,
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_postgresql17_claim_uses_database_clock_without_explicit_test_time(
    postgresql17: PostgreSQL17,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _new_database(postgresql17, "clock")
    migrated = _migrate(target)
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr
    database = ProvisionerDatabase(target.settings)
    repository = _repository(database, target.settings)
    try:
        operation = await repository.submit("provision", "clock-authority", _request())
        async with database.session_factory.begin() as session:
            await session.execute(
                update(Operation)
                .where(Operation.id == operation.id)
                .values(available_at=func.clock_timestamp() + text("INTERVAL '1 hour'"))
            )

        class SkewedDateTime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> SkewedDateTime:
                return cls(2100, 1, 1, tzinfo=UTC)

        monkeypatch.setattr(repository_module, "datetime", SkewedDateTime)

        assert await repository.claim_next("clock-skewed-worker") is None
    finally:
        await database.dispose()
