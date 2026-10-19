"""Smoke tests for the C1-C1d fixture schema and grants (task 3.1)."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from .conftest import CellDatabase, insert_tenant


async def test_generation_bumps_only_on_desired_column_change(cell_db: CellDatabase) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        tenant = await insert_tenant(owner, "tenant-a")
        await owner.execute(
            "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state) "
            "VALUES ('aaaaaaaaaaaaaaaa', $1, 'running')",
            tenant,
        )
        generation = await owner.fetchval(
            "SELECT generation FROM exomem_cloud_cells WHERE cell_id = 'aaaaaaaaaaaaaaaa'"
        )
        assert generation == 1

        # An observed-column-only update must not bump generation.
        await owner.execute(
            "UPDATE exomem_cloud_cells SET observed_state = 'running' "
            "WHERE cell_id = 'aaaaaaaaaaaaaaaa'"
        )
        generation = await owner.fetchval(
            "SELECT generation FROM exomem_cloud_cells WHERE cell_id = 'aaaaaaaaaaaaaaaa'"
        )
        assert generation == 1

        # A desired-column change must bump generation and notify.
        await owner.execute("LISTEN exomem_cloud_cells")
        notifications: list[str] = []
        await owner.add_listener(
            "exomem_cloud_cells", lambda *args: notifications.append(args[-1])
        )
        await owner.execute(
            "UPDATE exomem_cloud_cells SET desired_state = 'stopped' "
            "WHERE cell_id = 'aaaaaaaaaaaaaaaa'"
        )
        await owner.execute("SELECT 1")  # pump the connection so listeners fire
        generation = await owner.fetchval(
            "SELECT generation FROM exomem_cloud_cells WHERE cell_id = 'aaaaaaaaaaaaaaaa'"
        )
        assert generation == 2
        assert notifications == ["aaaaaaaaaaaaaaaa"]
    finally:
        await owner.close()


async def test_tenant_can_have_only_one_non_deleted_cell(cell_db: CellDatabase) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        tenant = await insert_tenant(owner, "tenant-b")
        await owner.execute(
            "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state) "
            "VALUES ('bbbbbbbbbbbbbbbb', $1, 'running')",
            tenant,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await owner.execute(
                "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state) "
                "VALUES ('cccccccccccccccc', $1, 'running')",
                tenant,
            )
    finally:
        await owner.close()


async def test_paid_resize_notifies_and_only_controller_can_write_capacity_proof(cell_db: CellDatabase) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    tenant = await insert_tenant(owner, "paid-volume")
    await owner.execute("INSERT INTO exomem_cloud_cells(cell_id,tenant_id,desired_state) VALUES($1,$2,'running')", "abcdefghijklmnop", tenant)
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        notifications: list[str] = []
        await owner.add_listener("exomem_cloud_cells", lambda *args: notifications.append(args[-1]))
        before = await owner.fetchval("SELECT generation FROM exomem_cloud_cells WHERE cell_id='abcdefghijklmnop'")
        await app.execute("UPDATE exomem_cloud_cells SET storage_gib=20 WHERE cell_id='abcdefghijklmnop'")
        assert await owner.fetchval("SELECT generation FROM exomem_cloud_cells WHERE cell_id='abcdefghijklmnop'") == before + 1
        assert notifications == ["abcdefghijklmnop"]
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute("UPDATE exomem_cloud_cells SET observed_storage_capacity_bytes=21474836480 WHERE cell_id='abcdefghijklmnop'")
        await controller.execute("UPDATE exomem_cloud_cells SET observed_storage_pvc_uid='claim-a',observed_storage_pvc_version='42',observed_storage_capacity_bytes=21474836480,observed_storage_generation=$1,observed_storage_at=now() WHERE cell_id='abcdefghijklmnop'", before + 1)
        row = await owner.fetchrow("SELECT * FROM exomem_cloud_cells WHERE cell_id='abcdefghijklmnop'")
        assert row["observed_storage_capacity_bytes"] == 20 * 1024**3
        assert row["generation"] == before + 1
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await controller.execute("UPDATE exomem_cloud_cells SET storage_gib=30 WHERE cell_id='abcdefghijklmnop'")
    finally:
        await owner.close()
        await app.close()
        await controller.close()


async def test_gateway_role_cannot_write_desired_state(cell_db: CellDatabase) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        tenant = await insert_tenant(owner, "tenant-c")
        await owner.execute(
            "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state) "
            "VALUES ('dddddddddddddddd', $1, 'running')",
            tenant,
        )
    finally:
        await owner.close()

    gateway = await asyncpg.connect(cell_db.dsn(role="exomem_gateway"))
    try:
        row = await gateway.fetchrow(
            "SELECT cell_id, tenant_id, desired_state FROM exomem_cloud_cells "
            "WHERE cell_id = 'dddddddddddddddd'"
        )
        assert row["desired_state"] == "running"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await gateway.execute(
                "UPDATE exomem_cloud_cells SET desired_state = 'stopped' "
                "WHERE cell_id = 'dddddddddddddddd'"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await gateway.execute(
                "SELECT observed_state FROM exomem_cloud_cells WHERE cell_id = 'dddddddddddddddd'"
            )
    finally:
        await gateway.close()


async def test_cellctl_role_cannot_write_desired_columns(cell_db: CellDatabase) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        tenant = await insert_tenant(owner, "tenant-d")
        await owner.execute(
            "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state) "
            "VALUES ('eeeeeeeeeeeeeeee', $1, 'running')",
            tenant,
        )
    finally:
        await owner.close()

    cellctl = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await cellctl.execute(
                "UPDATE exomem_cloud_cells SET desired_state = 'stopped' "
                "WHERE cell_id = 'eeeeeeeeeeeeeeee'"
            )
        await cellctl.execute(
            "UPDATE exomem_cloud_cells SET observed_state = 'running', ready = true "
            "WHERE cell_id = 'eeeeeeeeeeeeeeee'"
        )
    finally:
        await cellctl.close()


async def test_a_cell_row_needs_an_existing_tenant(cell_db: CellDatabase) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await owner.execute(
                "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state) "
                "VALUES ('ffffffffffffffff', $1, 'running')",
                uuid.uuid4(),
            )
    finally:
        await owner.close()


async def test_c5_split_grants_and_prepared_tuple_are_enforced(cell_db):
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    tenant = await insert_tenant(owner, "import-tenant")
    await owner.execute("INSERT INTO exomem_cloud_cells(cell_id,tenant_id,desired_state) VALUES($1,$2,'running')", "abcdefghijklmnop", tenant)
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        import_id = await app.fetchval("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) VALUES('abcdefghijklmnop','archive_members',123,2) RETURNING import_id")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute("UPDATE exomem_cloud_imports SET recipient='forged' WHERE import_id=$1", import_id)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await controller.execute("UPDATE exomem_cloud_imports SET requested_state='uploaded' WHERE import_id=$1", import_id)
        await controller.execute("UPDATE exomem_cloud_imports SET recipient='public-recipient',observed_state='key_ready' WHERE import_id=$1", import_id)
        await app.execute("UPDATE exomem_cloud_imports SET ciphertext_sha256=$2,ciphertext_bytes=200 WHERE import_id=$1", import_id, "a" * 64)
        with pytest.raises(asyncpg.CheckViolationError):
            await app.execute("UPDATE exomem_cloud_imports SET ciphertext_bytes=201 WHERE import_id=$1", import_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await app.execute("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) VALUES('abcdefghijklmnop','adopt',0,0)")
    finally:
        await app.close()
        await controller.close()
        await owner.close()


async def test_c5_cleanup_and_terminal_discard_both_keep_the_open_import_slot(cell_db):
    from cellctl import db

    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        tenant = await insert_tenant(owner, "cleanup-slot")
        await owner.execute("INSERT INTO exomem_cloud_cells(cell_id,tenant_id,desired_state) VALUES($1,$2,'running')", "abcdefghijklmnop", tenant)
        insert = "INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) VALUES('abcdefghijklmnop','adopt',0,0) RETURNING import_id"
        import_id = await app.fetchval(insert)
        for state in ("cleanup_running", "cleanup_stopping", "cleanup_discard_stopping", "cleanup_pending"):
            await controller.execute("UPDATE exomem_cloud_imports SET observed_state=$2 WHERE import_id=$1", import_id, state)
            assert (await db.read_import(controller, "abcdefghijklmnop")).observed_state == state
            with pytest.raises(asyncpg.UniqueViolationError):
                await app.fetchval(insert)
        await controller.execute("UPDATE exomem_cloud_imports SET observed_state='done' WHERE import_id=$1", import_id)
        # Newer terminal history must not hide this older open discard behind C1's hold.
        newer = await app.fetchval(insert)
        await controller.execute("UPDATE exomem_cloud_imports SET observed_state='done' WHERE import_id=$1", newer)
        await controller.execute("UPDATE exomem_cloud_cells SET hold_kind='import' WHERE cell_id='abcdefghijklmnop'")
        await app.execute("UPDATE exomem_cloud_imports SET requested_state='discard_requested' WHERE import_id=$1", import_id)
        assert (await db.read_import(controller, "abcdefghijklmnop")).import_id == str(import_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await app.fetchval(insert)
        await controller.execute("UPDATE exomem_cloud_imports SET observed_state='discarded' WHERE import_id=$1", import_id)
        assert (await db.read_import(controller, "abcdefghijklmnop")).import_id == str(newer)
        await controller.execute("UPDATE exomem_cloud_cells SET hold_kind=NULL WHERE cell_id='abcdefghijklmnop'")
        assert await db.read_import(controller, "abcdefghijklmnop") is None
        assert await app.fetchval(insert) != import_id
    finally:
        await owner.close()
        await app.close()
        await controller.close()
