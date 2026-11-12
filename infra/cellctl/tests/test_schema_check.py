"""automate-cloud-releases 4.1 (D7): cellctl refuses a schema it cannot fully
read or write, and the command names every gap."""

from __future__ import annotations

import asyncpg
import pytest

from cellctl.schema_check import SchemaGap, check_schema

from .conftest import CellDatabase


async def _as_owner(cell_db: CellDatabase, sql: str) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute(sql)
    finally:
        await owner.close()


async def test_a_schema_missing_one_column_names_it(cell_db: CellDatabase) -> None:
    await _as_owner(cell_db, "ALTER TABLE exomem_cloud_cells DROP COLUMN last_backup_at")

    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        gaps = await check_schema(connection)
    finally:
        await connection.close()

    assert "column exomem_cloud_cells.last_backup_at is missing" in gaps


async def test_a_schema_without_the_history_read_grant_names_it(cell_db: CellDatabase) -> None:
    await _as_owner(cell_db, "REVOKE SELECT ON exomem_cloud_image_history FROM exomem_cellctl")

    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        gaps = await check_schema(connection)
    finally:
        await connection.close()

    assert "SELECT privilege on exomem_cloud_image_history.image is missing" in gaps


async def test_the_full_fixture_schema_has_no_gap(cell_db: CellDatabase) -> None:
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        assert await check_schema(connection) == []
    finally:
        await connection.close()


async def test_run_loop_refuses_to_start_on_the_gap(cell_db: CellDatabase) -> None:
    from cellctl import reconcile
    from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

    from .test_local_storage import MIGRATING
    from .test_reconcile import FakeClusterGateway, _secrets_config

    await _as_owner(cell_db, "REVOKE SELECT ON exomem_cloud_image_history FROM exomem_cellctl")
    cluster = FakeClusterGateway()
    with pytest.raises(SchemaGap, match="exomem_cloud_image_history"):
        await reconcile.run_loop(
            cell_db.dsn(role="exomem_cellctl"),
            cluster,
            FakeHetznerVolumeProvider(),
            _secrets_config(),
            reconcile.ClusterConfig(object_storage_bucket="exomem-cloud-backups", storage=MIGRATING),
            heartbeat_path="/nonexistent-heartbeat-path",
        )


async def test_access_tables_are_no_gap_while_access_recording_is_off(cell_db: CellDatabase) -> None:
    for table in ("exomem_cloud_access_entries", "exomem_cloud_access_records",
                  "exomem_cloud_access_receipts", "exomem_cloud_placements"):
        await _as_owner(cell_db, f"DROP TABLE {table}")

    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        assert await check_schema(connection) == []
        gaps = await check_schema(connection, access_record=True)
    finally:
        await connection.close()

    assert "table exomem_cloud_access_receipts is missing" in gaps
    assert "table exomem_cloud_placements is missing" in gaps


async def test_a_schema_missing_a_column_read_by_import_row_names_it(cell_db: CellDatabase) -> None:
    await _as_owner(cell_db, "ALTER TABLE exomem_cloud_imports DROP COLUMN declared_bytes")

    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        gaps = await check_schema(connection)
    finally:
        await connection.close()

    assert "column exomem_cloud_imports.declared_bytes is missing" in gaps
