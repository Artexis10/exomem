from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

RUN_POSTGRESQL17 = os.environ.get("RUN_POSTGRESQL17_TEST") == "1"
POSTGRESQL17_IMAGE = os.environ.get("POSTGRESQL17_IMAGE", "postgres:17-alpine")
PROVISIONER_ROOT = Path(__file__).resolve().parents[1]
PROVISIONER_TEST_IMAGE = os.environ.get("PROVISIONER_TEST_IMAGE")
if PROVISIONER_TEST_IMAGE is None:
    from sqlalchemy import func, select, text, update

    import exomem_provisioner.repository as repository_module
    from exomem_provisioner.capacity import (
        CapacityBlocked,
        CapacityObservation,
        CapacityReservationAuthority,
        VerifiedCapacityReceipt,
    )
    from exomem_provisioner.config import ProvisionerSettings
    from exomem_provisioner.crypto import AesGcmEnvelopeCodec
    from exomem_provisioner.database import DATABASE_REVISION, ProvisionerDatabase
    from exomem_provisioner.models import (
        CapacityLedger,
        CapacityReservation,
        Operation,
        TenantFence,
    )
    from exomem_provisioner.repository import OperationRepository, StaleFence
else:
    database_source = (
        PROVISIONER_ROOT / "src/exomem_provisioner/database.py"
    ).read_text(encoding="utf-8")
    revision_match = re.search(r'^DATABASE_REVISION = "([^"]+)"$', database_source, re.MULTILINE)
    assert revision_match is not None
    DATABASE_REVISION = revision_match.group(1)

LEGACY_POSTGRESQL17 = pytest.mark.skipif(
    PROVISIONER_TEST_IMAGE is not None,
    reason="checkout-backed repository tests run in the provisioner project environment",
)
ASYNCIO_POSTGRESQL17 = (
    LEGACY_POSTGRESQL17 if PROVISIONER_TEST_IMAGE is not None else pytest.mark.asyncio
)

pytestmark = pytest.mark.skipif(
    not RUN_POSTGRESQL17,
    reason="set RUN_POSTGRESQL17_TEST=1 to run the real PostgreSQL 17 gates",
)


@dataclass(frozen=True, slots=True)
class PostgreSQL17:
    container: str
    network: str
    port: int
    admin_password: str


@dataclass(frozen=True, slots=True)
class ProvisionerTestDatabase:
    name: str
    role: str
    schema: str
    runtime_password: str
    settings: object | None


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
    network = f"exomem-provisioner-pg17-{uuid.uuid4().hex[:12]}"
    admin_password = f"admin-{uuid.uuid4().hex}"
    _run(["docker", "network", "create", network])
    _run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            container,
            "--network",
            network,
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
            network=network,
            port=int(port_match.group(1)),
            admin_password=admin_password,
        )
        version = _psql(server, "SHOW server_version_num;")
        assert version.stdout.strip().splitlines()[-1].startswith("17")
        yield server
    finally:
        _run(["docker", "rm", "--force", container], check=False)
        _run(["docker", "network", "rm", network], check=False)


@pytest.fixture(scope="module")
def other_postgresql17(postgresql17: PostgreSQL17) -> Iterator[PostgreSQL17]:
    container = f"exomem-provisioner-pg17-other-{uuid.uuid4().hex[:12]}"
    admin_password = f"admin-{uuid.uuid4().hex}"
    _run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            container,
            "--network",
            postgresql17.network,
            "--env",
            f"POSTGRES_PASSWORD={admin_password}",
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
        server = PostgreSQL17(
            container=container,
            network=postgresql17.network,
            port=0,
            admin_password=admin_password,
        )
        assert _psql(server, "SHOW server_version_num;").stdout.strip().splitlines()[
            -1
        ].startswith("17")
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
    _psql(server, f'CREATE DATABASE "{name}" OWNER postgres;')
    settings = None
    if PROVISIONER_TEST_IMAGE is None:
        settings = ProvisionerSettings(
            bearer="b" * 32,
            envelope_key="k" * 32,
            database_url=(
                f"postgresql+asyncpg://{role}:{password}@127.0.0.1:{server.port}/{name}"
            ),
            database_schema=schema,
            database_role=role,
            trusted_proxy_ips="127.0.0.1",
        )
    return ProvisionerTestDatabase(
        name=name,
        role=role,
        schema=schema,
        runtime_password=password,
        settings=settings,
    )


def _migrate(
    server: PostgreSQL17, database: ProvisionerTestDatabase
) -> subprocess.CompletedProcess[str]:
    assert database.settings is not None
    environment = os.environ.copy()
    environment.update(
        {
            "EXOMEM_PROVISIONER_DATABASE_ADMIN_URL": (
                "postgresql+asyncpg://postgres:"
                f"{server.admin_password}@127.0.0.1:"
                f"{server.port}/{database.name}"
            ),
            "EXOMEM_PROVISIONER_DATABASE_URL": (database.settings.database_url.get_secret_value()),
            "EXOMEM_PROVISIONER_DATABASE_SCHEMA": database.schema,
            "EXOMEM_PROVISIONER_DATABASE_ROLE": database.role,
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "import asyncio; "
                "import exomem_provisioner.database_bootstrap as database_bootstrap; "
                f"database_bootstrap._MIGRATION_ROOT = Path({str(PROVISIONER_ROOT)!r}); "
                "asyncio.run(database_bootstrap.bootstrap())"
            ),
        ],
        cwd=PROVISIONER_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _image_environment(
    server: PostgreSQL17,
    database: ProvisionerTestDatabase,
    *,
    include_admin: bool,
    admin_server: PostgreSQL17 | None = None,
) -> list[str]:
    environment = [
        "--env",
        (
            "EXOMEM_PROVISIONER_DATABASE_URL="
            f"postgresql+asyncpg://{database.role}:{database.runtime_password}@"
            f"{server.container}:5432/{database.name}"
        ),
        "--env",
        f"EXOMEM_PROVISIONER_DATABASE_SCHEMA={database.schema}",
        "--env",
        f"EXOMEM_PROVISIONER_DATABASE_ROLE={database.role}",
        "--env",
        "EXOMEM_PROVISIONER_DATABASE_LOCK_TIMEOUT_SECONDS=15",
    ]
    if include_admin:
        admin_server = admin_server or server
        environment.extend(
            [
                "--env",
                (
                    "EXOMEM_PROVISIONER_DATABASE_ADMIN_URL="
                    f"postgresql+asyncpg://postgres:{admin_server.admin_password}@"
                    f"{admin_server.container}:5432/{database.name}"
                ),
            ]
        )
    return environment


def _image_command(
    server: PostgreSQL17,
    database: ProvisionerTestDatabase,
    command_name: str,
    *,
    include_admin: bool = False,
    admin_server: PostgreSQL17 | None = None,
    python: str | None = None,
) -> subprocess.CompletedProcess[str]:
    assert PROVISIONER_TEST_IMAGE is not None
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        server.network,
        *_image_environment(
            server,
            database,
            include_admin=include_admin,
            admin_server=admin_server,
        ),
    ]
    if python is None:
        command.extend([PROVISIONER_TEST_IMAGE, command_name])
    else:
        command.extend(["--entrypoint", "python", PROVISIONER_TEST_IMAGE, "-c", python])
    return _run(command, check=False)


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


@ASYNCIO_POSTGRESQL17
async def test_fresh_postgresql17_bootstrap_creates_owned_schema_before_version_table(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "fresh")

    migrated = _migrate(postgresql17, target)

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
            ledger_rows = await connection.scalar(
                text(f'SELECT count(*) FROM "{target.schema}".capacity_ledger')
            )
        assert str(version).startswith("17")
        assert owner == target.role
        assert revisions == [DATABASE_REVISION]
        assert {"operations", "tenant_fences", "resources", "credential_metadata"} <= tables
        assert {"capacity_ledger", "capacity_reservations"} <= tables
        assert ledger_rows == 1
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


@LEGACY_POSTGRESQL17
def test_postgresql17_migration_rejects_existing_schema_with_wrong_owner(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "owner")
    _psql(
        postgresql17,
        f'CREATE ROLE "{target.role}" LOGIN PASSWORD \'{target.runtime_password}\';',
        database=target.name,
    )
    wrong_owner = f"wrong_owner_{uuid.uuid4().hex[:8]}"
    _psql(postgresql17, f'CREATE ROLE "{wrong_owner}";', database=target.name)
    _psql(
        postgresql17,
        f'CREATE SCHEMA "{target.schema}" AUTHORIZATION "{wrong_owner}"; '
        f'GRANT ALL ON SCHEMA "{target.schema}" TO "{target.role}";',
        database=target.name,
    )

    migrated = _migrate(postgresql17, target)

    assert migrated.returncode != 0
    assert "runtime schema ownership is invalid" in migrated.stdout + migrated.stderr
    owner = _psql(
        postgresql17,
        f"SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='{target.schema}';",
        database=target.name,
    )
    assert owner.stdout.strip() == wrong_owner


@pytest.mark.skipif(
    PROVISIONER_TEST_IMAGE is None,
    reason="set PROVISIONER_TEST_IMAGE to verify the built image without checkout mounts",
)
def test_built_image_packages_exact_read_only_single_head_migrations() -> None:
    assert PROVISIONER_TEST_IMAGE is not None
    probe = """
import hashlib
import json
from pathlib import Path
import stat
from alembic.config import Config
from alembic.script import ScriptDirectory
from exomem_provisioner.database import DATABASE_REVISION
root = Path('/opt/exomem/provisioner-migrations')
payload = {}
directories = set()
for path in (root, *sorted(root.rglob('*'))):
    if path.is_symlink():
        raise SystemExit(2)
    metadata = path.stat()
    if metadata.st_uid != 0 or metadata.st_gid != 0:
        raise SystemExit(2)
    if metadata.st_mode & 0o222:
        raise SystemExit(3)
    relative = '.' if path == root else str(path.relative_to(root))
    if stat.S_ISDIR(metadata.st_mode):
        directories.add(relative)
    elif stat.S_ISREG(metadata.st_mode):
        payload[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        raise SystemExit(2)
config = Config(str(root / 'alembic.ini'))
heads = ScriptDirectory.from_config(config).get_heads()
print(json.dumps({
    'directories': sorted(directories),
    'files': payload,
    'heads': heads,
    'runtime': DATABASE_REVISION,
}, sort_keys=True))
"""
    inspected = _run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--entrypoint",
            "python",
            PROVISIONER_TEST_IMAGE,
            "-c",
            probe,
        ]
    )
    payload = json.loads(inspected.stdout)
    expected: dict[str, str] = {}
    for source in [PROVISIONER_ROOT / "alembic.ini", *sorted((PROVISIONER_ROOT / "alembic").rglob("*"))]:
        if source.is_file() and "__pycache__" not in source.parts:
            expected[str(source.relative_to(PROVISIONER_ROOT))] = hashlib.sha256(
                source.read_bytes()
            ).hexdigest()
    expected_directories = {
        ".",
        "alembic",
        *{
            str(source.relative_to(PROVISIONER_ROOT))
            for source in (PROVISIONER_ROOT / "alembic").rglob("*")
            if source.is_dir() and "__pycache__" not in source.parts
        },
    }

    assert set(payload["directories"]) == expected_directories
    assert payload["files"] == expected
    assert payload["heads"] == [DATABASE_REVISION]
    assert payload["runtime"] == DATABASE_REVISION


@pytest.mark.skipif(
    PROVISIONER_TEST_IMAGE is None,
    reason="set PROVISIONER_TEST_IMAGE to verify the built image without checkout mounts",
)
def test_built_image_bootstrap_is_concurrent_retryable_and_runtime_exact(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "image_bootstrap")
    assert PROVISIONER_TEST_IMAGE is not None
    base = [
        "docker",
        "run",
        "--rm",
        "--network",
        postgresql17.network,
        *_image_environment(postgresql17, target, include_admin=True),
        PROVISIONER_TEST_IMAGE,
        "exomem-provisioner-database-bootstrap",
    ]
    first = subprocess.Popen(base, cwd=PROVISIONER_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen(base, cwd=PROVISIONER_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first_stdout, first_stderr = first.communicate(timeout=30)
    second_stdout, second_stderr = second.communicate(timeout=30)
    assert first.returncode == 0, first_stdout + first_stderr
    assert second.returncode == 0, second_stdout + second_stderr

    validated = _image_command(
        postgresql17,
        target,
        "exomem-provisioner-database-validate",
    )
    assert validated.returncode == 0, validated.stdout + validated.stderr
    state = _psql(
        postgresql17,
        (
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
            "rolreplication, rolbypassrls FROM pg_roles "
            f"WHERE rolname = '{target.role}'; "
            f"SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = '{target.schema}'; "
            f'SELECT version_num FROM "{target.schema}".alembic_version;'
        ),
        database=target.name,
    )
    assert "t|f|f|f|f|f" in state.stdout.replace(" ", "")
    assert target.role in state.stdout
    assert DATABASE_REVISION in state.stdout

    _psql(
        postgresql17,
        f'UPDATE "{target.schema}".alembic_version SET version_num = \'0003_cell_operation_lock\';',
        database=target.name,
    )
    refused = _image_command(
        postgresql17,
        target,
        "exomem-provisioner-database-validate",
    )
    assert refused.returncode != 0
    assert "0003_cell_operation_lock" in _psql(
        postgresql17,
        f'SELECT version_num FROM "{target.schema}".alembic_version;',
        database=target.name,
    ).stdout


@pytest.mark.skipif(
    PROVISIONER_TEST_IMAGE is None,
    reason="set PROVISIONER_TEST_IMAGE to verify the built image without checkout mounts",
)
def test_built_image_bootstrap_retry_and_lock_owner_do_not_deadlock(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "image_retry")
    sentinel = "runtime-sentinel-do-not-print"
    target = replace(target, runtime_password=sentinel)
    injected = _image_command(
        postgresql17,
        target,
        "",
        include_admin=True,
        python=(
            "import asyncio; "
            "from exomem_provisioner.database_bootstrap import bootstrap; "
            "asyncio.run(bootstrap(after_identity=lambda: "
            "(_ for _ in ()).throw(RuntimeError('injected failure'))))"
        ),
    )
    assert injected.returncode != 0
    assert sentinel not in injected.stdout + injected.stderr
    retried = _image_command(
        postgresql17,
        target,
        "exomem-provisioner-database-bootstrap",
        include_admin=True,
    )
    assert retried.returncode == 0, retried.stdout + retried.stderr
    bad_password = "wrong-runtime-password-sentinel"
    rejected = _image_command(
        postgresql17,
        replace(target, runtime_password=bad_password),
        "exomem-provisioner-database-validate",
    )
    assert rejected.returncode != 0
    assert bad_password not in rejected.stdout + rejected.stderr

    serialized = _new_database(postgresql17, "image_serial")
    delayed_bootstrap = [
        "docker",
        "run",
        "--rm",
        "--network",
        postgresql17.network,
        *_image_environment(postgresql17, serialized, include_admin=True),
        "--entrypoint",
        "python",
        PROVISIONER_TEST_IMAGE,
        "-c",
        (
            "import asyncio; "
            "from exomem_provisioner.database_bootstrap import bootstrap; "
            "asyncio.run(bootstrap(after_identity=lambda: asyncio.sleep(2)))"
        ),
    ]
    bootstrap_process = subprocess.Popen(
        delayed_bootstrap,
        cwd=PROVISIONER_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.75)
    migration_process = subprocess.Popen(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            postgresql17.network,
            *_image_environment(postgresql17, serialized, include_admin=False),
            PROVISIONER_TEST_IMAGE,
            "exomem-provisioner-database-migrate",
        ],
        cwd=PROVISIONER_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    bootstrap_stdout, bootstrap_stderr = bootstrap_process.communicate(timeout=30)
    migration_stdout, migration_stderr = migration_process.communicate(timeout=30)
    assert bootstrap_process.returncode == 0, bootstrap_stdout + bootstrap_stderr
    assert migration_process.returncode == 0, migration_stdout + migration_stderr


@pytest.mark.skipif(
    PROVISIONER_TEST_IMAGE is None,
    reason="set PROVISIONER_TEST_IMAGE to verify the built image without checkout mounts",
)
def test_built_image_bootstrap_rejects_same_database_name_on_different_clusters(
    postgresql17: PostgreSQL17,
    other_postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(other_postgresql17, "cross_domain")
    initialized = _image_command(
        other_postgresql17,
        target,
        "exomem-provisioner-database-bootstrap",
        include_admin=True,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    _psql(
        postgresql17,
        f'CREATE DATABASE "{target.name}" OWNER postgres;',
    )

    crossed = _image_command(
        other_postgresql17,
        target,
        "exomem-provisioner-database-bootstrap",
        include_admin=True,
        admin_server=postgresql17,
    )

    assert crossed.returncode != 0
    validated = _image_command(
        other_postgresql17,
        target,
        "exomem-provisioner-database-validate",
    )
    assert validated.returncode == 0, validated.stdout + validated.stderr


@pytest.mark.skipif(
    PROVISIONER_TEST_IMAGE is None,
    reason="set PROVISIONER_TEST_IMAGE to verify the built image without checkout mounts",
)
def test_built_image_rejects_incoming_runtime_role_membership(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "incoming_membership")
    initialized = _image_command(
        postgresql17,
        target,
        "exomem-provisioner-database-bootstrap",
        include_admin=True,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    inherited_reader = f"inherited_reader_{uuid.uuid4().hex[:8]}"
    _psql(
        postgresql17,
        f'CREATE ROLE "{inherited_reader}" LOGIN; '
        f'GRANT "{target.role}" TO "{inherited_reader}";',
        database=target.name,
    )

    bootstrap = _image_command(
        postgresql17,
        target,
        "exomem-provisioner-database-bootstrap",
        include_admin=True,
    )
    validation = _image_command(
        postgresql17,
        target,
        "exomem-provisioner-database-validate",
    )

    assert bootstrap.returncode != 0
    assert validation.returncode != 0


@pytest.mark.skipif(
    PROVISIONER_TEST_IMAGE is None,
    reason="set PROVISIONER_TEST_IMAGE to verify the built image without checkout mounts",
)
@pytest.mark.parametrize("drift", ["owner", "attributes"])
def test_built_image_bootstrap_rejects_runtime_authority_drift(
    postgresql17: PostgreSQL17,
    drift: str,
) -> None:
    target = _new_database(postgresql17, f"image_{drift}")
    attributes = "SUPERUSER" if drift == "attributes" else "NOSUPERUSER"
    _psql(
        postgresql17,
        f'CREATE ROLE "{target.role}" LOGIN {attributes} PASSWORD \'{target.runtime_password}\';',
        database=target.name,
    )
    schema_owner = target.role
    if drift == "owner":
        schema_owner = f"wrong_owner_{uuid.uuid4().hex[:8]}"
        _psql(postgresql17, f'CREATE ROLE "{schema_owner}";', database=target.name)
    _psql(
        postgresql17,
        f'CREATE SCHEMA "{target.schema}" AUTHORIZATION "{schema_owner}";',
        database=target.name,
    )

    completed = _image_command(
        postgresql17,
        target,
        "exomem-provisioner-database-bootstrap",
        include_admin=True,
    )

    assert completed.returncode != 0
    if drift == "owner":
        assert schema_owner in _psql(
            postgresql17,
            f"SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='{target.schema}';",
            database=target.name,
        ).stdout
    else:
        assert _psql(
            postgresql17,
            f"SELECT rolsuper FROM pg_roles WHERE rolname='{target.role}';",
            database=target.name,
        ).stdout.strip() == "t"


@ASYNCIO_POSTGRESQL17
async def test_postgresql17_claim_waits_for_and_rechecks_concurrent_higher_fence(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "claim")
    migrated = _migrate(postgresql17, target)
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


@ASYNCIO_POSTGRESQL17
async def test_postgresql17_stale_post_claim_completion_is_rejected(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "completion")
    migrated = _migrate(postgresql17, target)
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


@ASYNCIO_POSTGRESQL17
async def test_postgresql17_claim_uses_database_clock_without_explicit_test_time(
    postgresql17: PostgreSQL17,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _new_database(postgresql17, "clock")
    migrated = _migrate(postgresql17, target)
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


@ASYNCIO_POSTGRESQL17
async def test_postgresql17_capacity_ledger_serializes_two_sixth_slot_attempts(
    postgresql17: PostgreSQL17,
) -> None:
    target = _new_database(postgresql17, "capacity")
    migrated = _migrate(postgresql17, target)
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr
    database = ProvisionerDatabase(target.settings)
    repository = _repository(database, target.settings)
    authority = CapacityReservationAuthority(database.session_factory)
    observed_at = datetime.now(UTC).replace(microsecond=0)
    observation = CapacityObservation(
        observed_at=observed_at,
        cluster_uid="cluster-postgresql-capacity",
        hcloud_server_id=101,
        hcloud_location="fsn1",
        user_resource_names=frozenset(),
        recovery_resource_names=frozenset(),
        orphan_attachment_ids=frozenset(),
        attached_hcloud_volumes=0,
    )
    receipt = VerifiedCapacityReceipt(
        receipt_id=str(uuid.uuid4()),
        sequence=1,
        observed_at=observed_at,
        expires_at=observed_at.replace(microsecond=0) + timedelta(seconds=300),
        cluster_uid=observation.cluster_uid,
        hcloud_server_id=observation.hcloud_server_id,
        hcloud_location=observation.hcloud_location,
        active_user_cells=0,
        active_recovery_cells=0,
        attached_volumes=0,
    )

    async def claim(index: int):
        request = _request(
            operationId=f"operation-postgresql-capacity-{index}",
            tenantId=f"tenant-postgresql-capacity-{index}",
            cellId=f"cell-postgresql-capacity-{index}",
            fenceGeneration=1,
            provisionMode="serve",
        )
        await repository.submit("provision", f"capacity-{index}", request)
        worker_id = f"capacity-worker-{index}"
        operation = await repository.claim_next(worker_id)
        assert operation is not None and operation.claim_token is not None
        return operation, request, worker_id

    async def reserve(claimed: tuple[object, dict[str, object], str]):
        operation, request, worker_id = claimed
        return await authority.reserve(
            operation,
            request,
            receipt=receipt,
            observation=observation,
            worker_id=worker_id,
            claim_token=operation.claim_token,
            claim_generation=operation.claim_generation,
        )

    try:
        for index in range(5):
            await reserve(await claim(index))
        contenders = [await claim(5), await claim(6)]
        results = await asyncio.gather(
            *(reserve(item) for item in contenders),
            return_exceptions=True,
        )

        assert sum(not isinstance(result, BaseException) for result in results) == 1
        blocked = [result for result in results if isinstance(result, CapacityBlocked)]
        assert len(blocked) == 1
        assert blocked[0].reason == "capacity-user-exhausted"

        expiring_operation, expiring_request, expiring_worker = await claim(7)
        expiring_receipt = replace(
            receipt,
            expires_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        async with database.session_factory() as blocker_session:
            async with blocker_session.begin():
                locked_ledger = await blocker_session.get(
                    CapacityLedger, 1, with_for_update=True
                )
                assert locked_ledger is not None
                waiting = asyncio.create_task(
                    authority.reserve(
                        expiring_operation,
                        expiring_request,
                        receipt=expiring_receipt,
                        observation=observation,
                        worker_id=expiring_worker,
                        claim_token=expiring_operation.claim_token,
                        claim_generation=expiring_operation.claim_generation,
                    )
                )
                await asyncio.sleep(0.2)
                assert not waiting.done(), "capacity admission did not wait on the ledger"
                await asyncio.sleep(1.1)
            with pytest.raises(CapacityBlocked) as expired:
                await asyncio.wait_for(waiting, timeout=5)
        assert expired.value.reason == "capacity-live-receipt-unavailable"

        async with database.session_factory() as session:
            active = await session.scalar(
                select(func.count()).select_from(CapacityReservation).where(
                    CapacityReservation.released_at.is_(None)
                )
            )
            revision = await session.scalar(
                select(CapacityLedger.revision).where(CapacityLedger.id == 1)
            )
        assert active == 6
        assert revision == 6
    finally:
        await database.dispose()
