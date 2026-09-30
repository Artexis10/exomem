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
