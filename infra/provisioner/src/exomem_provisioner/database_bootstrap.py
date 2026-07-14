"""Fail-closed database bootstrap and packaged migration commands."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from alembic import command

from .database import DATABASE_REVISION

_MIGRATION_ROOT = Path("/opt/exomem/provisioner-migrations")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_DISALLOWED_ROLES = {"postgres", "public", "neondb_owner"}
_LOCK_POLL_SECONDS = 0.2


class DatabaseBootstrapError(RuntimeError):
    """Content-free database bootstrap failure."""


@dataclass(frozen=True, slots=True)
class RuntimeRoleState:
    """Security-relevant PostgreSQL runtime-role attributes."""

    name: str
    can_login: bool
    is_superuser: bool
    can_create_database: bool
    can_create_role: bool
    can_replicate: bool
    can_bypass_rls: bool
    member_of: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatabaseCommandConfiguration:
    """Validated database command configuration with redacted URL representations."""

    admin_url: URL | None
    runtime_url: URL
    database: str
    admin_role: str | None
    runtime_role: str
    schema: str
    lock_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class PackagedMigrations:
    configuration: Config
    head: str
    known: frozenset[str]


def database_lock_key(database: str, schema: str) -> int:
    """Return one stable signed advisory-lock key for a database/schema pair."""

    digest = hashlib.sha256(
        b"exomem-provisioner-database-v1\0"
        + database.encode("utf-8")
        + b"\0"
        + schema.encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def validate_runtime_role(
    state: RuntimeRoleState,
    *,
    expected_role: str,
    admin_role: str,
    database_owner: str,
    expected_schema: str,
    owned_schemas: tuple[str, ...],
) -> None:
    """Reject privilege or ownership drift instead of repairing it."""

    if (
        state.name != expected_role
        or not state.can_login
        or state.is_superuser
        or state.can_create_database
        or state.can_create_role
        or state.can_replicate
        or state.can_bypass_rls
        or state.member_of
    ):
        raise DatabaseBootstrapError("runtime role is unsafe")
    if admin_role == expected_role:
        raise DatabaseBootstrapError("admin and runtime identities must differ")
    if database_owner == expected_role:
        raise DatabaseBootstrapError("runtime role must not own the database")
    if set(owned_schemas) != {expected_schema}:
        raise DatabaseBootstrapError("runtime schema ownership is invalid")


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise DatabaseBootstrapError("database command configuration is invalid")
    return value


def _database_url(name: str) -> URL:
    try:
        value = make_url(_required_environment(name))
    except Exception as error:
        raise DatabaseBootstrapError("database command configuration is invalid") from error
    if (
        value.drivername != "postgresql+asyncpg"
        or not value.username
        or value.password is None
        or not value.host
        or not value.database
    ):
        raise DatabaseBootstrapError("database command configuration is invalid")
    return value


def load_configuration(*, require_admin: bool) -> DatabaseCommandConfiguration:
    """Load the complete environment-only command contract."""

    runtime_url = _database_url("EXOMEM_PROVISIONER_DATABASE_URL")
    runtime_role = _required_environment("EXOMEM_PROVISIONER_DATABASE_ROLE")
    schema = _required_environment("EXOMEM_PROVISIONER_DATABASE_SCHEMA")
    if (
        not _IDENTIFIER.fullmatch(runtime_role)
        or not _IDENTIFIER.fullmatch(schema)
        or runtime_role in _DISALLOWED_ROLES
        or schema == "public"
        or runtime_url.username != runtime_role
    ):
        raise DatabaseBootstrapError("database command configuration is invalid")
    timeout_raw = os.environ.get("EXOMEM_PROVISIONER_DATABASE_LOCK_TIMEOUT_SECONDS", "60")
    try:
        lock_timeout_seconds = int(timeout_raw)
    except ValueError as error:
        raise DatabaseBootstrapError("database command configuration is invalid") from error
    if not 1 <= lock_timeout_seconds <= 300:
        raise DatabaseBootstrapError("database command configuration is invalid")

    admin_url = _database_url("EXOMEM_PROVISIONER_DATABASE_ADMIN_URL") if require_admin else None
    admin_role = admin_url.username if admin_url is not None else None
    if admin_url is not None and (
        admin_url.database != runtime_url.database or admin_role == runtime_role
    ):
        raise DatabaseBootstrapError("database command configuration is invalid")
    return DatabaseCommandConfiguration(
        admin_url=admin_url,
        runtime_url=runtime_url,
        database=str(runtime_url.database),
        admin_role=admin_role,
        runtime_role=runtime_role,
        schema=schema,
        lock_timeout_seconds=lock_timeout_seconds,
    )


def load_packaged_migrations(root: Path | None = None) -> PackagedMigrations:
    """Load and bind the immutable, single-head migration package."""

    if root is None:
        root = _MIGRATION_ROOT
    try:
        configuration = Config(str(root / "alembic.ini"))
        scripts = ScriptDirectory.from_config(configuration)
        heads = scripts.get_heads()
        known = frozenset(revision.revision for revision in scripts.walk_revisions())
    except Exception as error:
        raise DatabaseBootstrapError("packaged migrations are invalid") from error
    if heads != [DATABASE_REVISION] or DATABASE_REVISION not in known:
        raise DatabaseBootstrapError("packaged migration head does not match runtime")
    return PackagedMigrations(configuration=configuration, head=heads[0], known=known)


def validate_revision_state(
    revisions: tuple[str, ...],
    *,
    known: frozenset[str],
    head: str,
    exact: bool,
) -> None:
    """Reject multiple, unknown, ahead, or non-exact database revisions."""

    if len(revisions) > 1 or any(revision not in known for revision in revisions):
        raise DatabaseBootstrapError("database revision is invalid")
    if exact and revisions != (head,):
        raise DatabaseBootstrapError("database is not at the packaged revision")


async def _acquire_lock(
    connection: AsyncConnection,
    *,
    key: int,
    timeout_seconds: int,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        )
        if acquired is True:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise DatabaseBootstrapError("database command lock timed out")
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def _release_lock(connection: AsyncConnection, *, key: int) -> None:
    released = await connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    if released is not True:
        raise DatabaseBootstrapError("database command lock ownership was lost")


async def _runtime_role_state(
    connection: AsyncConnection, role: str
) -> RuntimeRoleState | None:
    row = (
        await connection.execute(
            text(
                "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = :role"
            ),
            {"role": role},
        )
    ).one_or_none()
    if row is None:
        return None
    memberships = tuple(
        (
            await connection.execute(
                text(
                    "SELECT parent.rolname FROM pg_auth_members membership "
                    "JOIN pg_roles member ON member.oid = membership.member "
                    "JOIN pg_roles parent ON parent.oid = membership.roleid "
                    "WHERE member.rolname = :role ORDER BY parent.rolname"
                ),
                {"role": role},
            )
        ).scalars()
    )
    return RuntimeRoleState(
        name=str(row.rolname),
        can_login=bool(row.rolcanlogin),
        is_superuser=bool(row.rolsuper),
        can_create_database=bool(row.rolcreatedb),
        can_create_role=bool(row.rolcreaterole),
        can_replicate=bool(row.rolreplication),
        can_bypass_rls=bool(row.rolbypassrls),
        member_of=memberships,
    )


async def _database_owner(connection: AsyncConnection, database: str) -> str:
    owner = await connection.scalar(
        text(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :database"
        ),
        {"database": database},
    )
    if not isinstance(owner, str):
        raise DatabaseBootstrapError("database ownership is invalid")
    return owner


async def _owned_schemas(connection: AsyncConnection, role: str) -> tuple[str, ...]:
    return tuple(
        (
            await connection.execute(
                text(
                    "SELECT namespace.nspname FROM pg_namespace namespace "
                    "JOIN pg_roles role ON role.oid = namespace.nspowner "
                    "WHERE role.rolname = :role "
                    "AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                    "AND namespace.nspname != 'information_schema' "
                    "ORDER BY namespace.nspname"
                ),
                {"role": role},
            )
        ).scalars()
    )


async def _validate_runtime_identity(
    connection: AsyncConnection,
    configuration: DatabaseCommandConfiguration,
    *,
    admin_role: str,
) -> None:
    state = await _runtime_role_state(connection, configuration.runtime_role)
    if state is None:
        raise DatabaseBootstrapError("runtime role is absent")
    validate_runtime_role(
        state,
        expected_role=configuration.runtime_role,
        admin_role=admin_role,
        database_owner=await _database_owner(connection, configuration.database),
        expected_schema=configuration.schema,
        owned_schemas=await _owned_schemas(connection, configuration.runtime_role),
    )


async def _create_or_validate_runtime_identity(
    connection: AsyncConnection,
    configuration: DatabaseCommandConfiguration,
) -> str:
    admin_role = await connection.scalar(text("SELECT current_user"))
    if not isinstance(admin_role, str) or admin_role != configuration.admin_role:
        raise DatabaseBootstrapError("admin database identity is invalid")
    state = await _runtime_role_state(connection, configuration.runtime_role)
    if state is None:
        password = configuration.runtime_url.password
        if password is None:
            raise DatabaseBootstrapError("runtime database credential is invalid")
        create_role = await connection.scalar(
            text(
                "SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD %L', "
                "CAST(:role AS text), CAST(:password AS text))"
            ),
            {"role": configuration.runtime_role, "password": password},
        )
        create_schema = await connection.scalar(
            text(
                "SELECT format('CREATE SCHEMA %I AUTHORIZATION %I', "
                "CAST(:schema AS text), CAST(:role AS text))"
            ),
            {"schema": configuration.schema, "role": configuration.runtime_role},
        )
        if not isinstance(create_role, str) or not isinstance(create_schema, str):
            raise DatabaseBootstrapError("runtime database identity could not be created")
        await connection.exec_driver_sql(create_role)
        await connection.exec_driver_sql(create_schema)
    await _validate_runtime_identity(connection, configuration, admin_role=admin_role)
    return admin_role


async def _revisions(connection: AsyncConnection, schema: str) -> tuple[str, ...]:
    present = await connection.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_tables "
            "WHERE schemaname = :schema AND tablename = 'alembic_version')"
        ),
        {"schema": schema},
    )
    if not present:
        return ()
    return tuple(
        (
            await connection.execute(
                text(f'SELECT version_num FROM "{schema}".alembic_version ORDER BY version_num')
            )
        ).scalars()
    )


def _upgrade(connection: object, packaged: PackagedMigrations, configuration: DatabaseCommandConfiguration) -> None:
    alembic_configuration = Config(str(Path(packaged.configuration.config_file_name or "")))
    alembic_configuration.set_main_option(
        "sqlalchemy.url", configuration.runtime_url.render_as_string(hide_password=False).replace("%", "%%")
    )
    alembic_configuration.attributes.update(
        {
            "connection": connection,
            "provisioner_schema": configuration.schema,
            "provisioner_role": configuration.runtime_role,
        }
    )
    command.upgrade(alembic_configuration, packaged.head)


async def _migrate_or_validate_runtime(
    configuration: DatabaseCommandConfiguration,
    packaged: PackagedMigrations,
    *,
    exact: bool,
    lock_already_held: bool,
) -> None:
    engine = create_async_engine(
        configuration.runtime_url,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "search_path": f"{configuration.schema},pg_catalog",
                "application_name": "exomem-provisioner-database-command",
            }
        },
    )
    key = database_lock_key(configuration.database, configuration.schema)
    try:
        async with engine.connect() as connection:
            if not lock_already_held:
                await _acquire_lock(
                    connection, key=key, timeout_seconds=configuration.lock_timeout_seconds
                )
            try:
                current_user = await connection.scalar(text("SELECT current_user"))
                await _validate_runtime_identity(
                    connection,
                    configuration,
                    admin_role="__operator_identity_must_be_distinct__",
                )
                if current_user != configuration.runtime_role:
                    raise DatabaseBootstrapError("runtime database identity is invalid")
                revisions = await _revisions(connection, configuration.schema)
                validate_revision_state(
                    revisions,
                    known=packaged.known,
                    head=packaged.head,
                    exact=exact,
                )
                if not exact and revisions != (packaged.head,):
                    try:
                        await connection.run_sync(_upgrade, packaged, configuration)
                        await connection.commit()
                    except Exception:
                        await connection.rollback()
                        raise
                final_revisions = await _revisions(connection, configuration.schema)
                validate_revision_state(
                    final_revisions,
                    known=packaged.known,
                    head=packaged.head,
                    exact=True,
                )
                current_schema = await connection.scalar(text("SELECT current_schema"))
                if current_schema != configuration.schema:
                    raise DatabaseBootstrapError("runtime database schema is invalid")
            finally:
                if not lock_already_held:
                    if connection.in_transaction():
                        await connection.rollback()
                    await _release_lock(connection, key=key)
    finally:
        await engine.dispose()


async def bootstrap(
    *,
    after_identity: Callable[[], object | Awaitable[object]] | None = None,
) -> None:
    """Create/validate authority, migrate as runtime, and prove final runtime access."""

    configuration = load_configuration(require_admin=True)
    packaged = load_packaged_migrations()
    if configuration.admin_url is None:
        raise DatabaseBootstrapError("admin database credential is required")
    engine = create_async_engine(configuration.admin_url, pool_pre_ping=True)
    key = database_lock_key(configuration.database, configuration.schema)
    try:
        async with engine.connect() as connection:
            await _acquire_lock(
                connection, key=key, timeout_seconds=configuration.lock_timeout_seconds
            )
            await connection.commit()
            try:
                async with connection.begin():
                    await _create_or_validate_runtime_identity(connection, configuration)
                if after_identity is not None:
                    result = after_identity()
                    if inspect.isawaitable(result):
                        await result
                await _migrate_or_validate_runtime(
                    configuration,
                    packaged,
                    exact=False,
                    lock_already_held=True,
                )
            finally:
                await _release_lock(connection, key=key)
    finally:
        await engine.dispose()


async def migrate() -> None:
    configuration = load_configuration(require_admin=False)
    await _migrate_or_validate_runtime(
        configuration,
        load_packaged_migrations(),
        exact=False,
        lock_already_held=False,
    )


async def validate() -> None:
    configuration = load_configuration(require_admin=False)
    await _migrate_or_validate_runtime(
        configuration,
        load_packaged_migrations(),
        exact=True,
        lock_already_held=False,
    )


def _run(command_function: Callable[[], Awaitable[None]], failure: str) -> None:
    try:
        asyncio.run(command_function())
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise SystemExit(failure) from None


def run_bootstrap() -> None:
    _run(bootstrap, "database bootstrap failed")


def run_migrate() -> None:
    _run(migrate, "database migration failed")


def run_validate() -> None:
    _run(validate, "database validation failed")
