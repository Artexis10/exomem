"""Tests for the asyncpg persistence layer (task 3.3/3.5), against a
disposable Postgres with the C1-C1d fixture schema applied."""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pytest

from cellctl import db
from cellctl.secrets import generate_backup_data_key, unwrap_secret, wrap_secret

from .conftest import CellDatabase


async def _seed_cell(cell_db: CellDatabase, cell_id: str, tenant_id: str) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute("INSERT INTO tenants (tenant_id) VALUES ($1)", tenant_id)
        await owner.execute(
            "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state) "
            "VALUES ($1, $2, 'running')",
            cell_id,
            tenant_id,
        )
    finally:
        await owner.close()


async def test_select_all_rows_returns_the_seeded_cell(cell_db: CellDatabase) -> None:
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        rows = await db.select_all_rows(connection)
    finally:
        await connection.close()
    assert [r.cell_id for r in rows] == ["aaaaaaaaaaaaaaaa"]
    assert rows[0].desired_state == "running"
    assert rows[0].observed_state is None


async def test_write_observed_persists_through_the_cellctl_role(cell_db: CellDatabase) -> None:
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        await db.write_observed(
            connection,
            "aaaaaaaaaaaaaaaa",
            {"observed_state": "running", "ready": True, "observed_generation": 1},
        )
        rows = await db.select_all_rows(connection)
    finally:
        await connection.close()
    assert rows[0].observed_state == "running"
    assert rows[0].ready is True
    assert rows[0].observed_generation == 1


async def test_write_observed_rejects_a_desired_column() -> None:
    with pytest.raises(ValueError):
        await db.write_observed(None, "x", {"desired_state": "stopped"})  # type: ignore[arg-type]


async def test_try_write_once_sets_the_column_only_the_first_time(cell_db: CellDatabase) -> None:
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        first = await db.try_write_once(connection, "aaaaaaaaaaaaaaaa", "b2_key_id", "key-1")
        second = await db.try_write_once(connection, "aaaaaaaaaaaaaaaa", "b2_key_id", "key-2")
        rows = await db.select_all_rows(connection)
    finally:
        await connection.close()
    assert first is True
    assert second is False
    assert rows[0].b2_key_id == "key-1"  # the losing write never overwrote it


async def test_try_write_once_group_sets_every_column_together_the_first_time(
    cell_db: CellDatabase,
) -> None:
    # H5: the b2_key_id/b2_key_wrapped/b2_key_version (and
    # backup_key_wrapped/backup_key_version) groups must land in ONE
    # statement, never as two separate writes a crash could split.
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        won = await db.try_write_once_group(
            connection,
            "aaaaaaaaaaaaaaaa",
            {"b2_key_id": "key-1", "b2_key_wrapped": b"wrapped-1", "b2_key_version": 1},
            first_column="b2_key_id",
        )
        rows = await db.select_all_rows(connection)
    finally:
        await connection.close()
    assert won is True
    assert rows[0].b2_key_id == "key-1"
    assert rows[0].b2_key_wrapped == b"wrapped-1"
    assert rows[0].b2_key_version == 1


async def test_try_write_once_group_loses_atomically_never_partially(cell_db: CellDatabase) -> None:
    # A losing write must never partially land -- the row must show either
    # entirely the winner's group or (if raced before any write) nothing.
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        first = await db.try_write_once_group(
            connection,
            "aaaaaaaaaaaaaaaa",
            {"b2_key_id": "key-1", "b2_key_wrapped": b"wrapped-1", "b2_key_version": 1},
            first_column="b2_key_id",
        )
        second = await db.try_write_once_group(
            connection,
            "aaaaaaaaaaaaaaaa",
            {"b2_key_id": "key-2", "b2_key_wrapped": b"wrapped-2", "b2_key_version": 2},
            first_column="b2_key_id",
        )
        rows = await db.select_all_rows(connection)
    finally:
        await connection.close()
    assert first is True
    assert second is False
    # The loser's columns never appear anywhere on the row -- id, wrapped
    # and version all agree with the winner, never a mix of the two.
    assert (rows[0].b2_key_id, rows[0].b2_key_wrapped, rows[0].b2_key_version) == ("key-1", b"wrapped-1", 1)


async def test_backup_key_reaches_the_database_only_wrapped(cell_db: CellDatabase) -> None:
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    master_key = b"m" * 32
    plaintext = generate_backup_data_key()
    aad = dict(cell_id="aaaaaaaaaaaaaaaa", column="backup_key_wrapped", key_version=1)
    wrapped = wrap_secret(master_key, plaintext, **aad)

    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        won = await db.try_write_once(connection, "aaaaaaaaaaaaaaaa", "backup_key_wrapped", wrapped)
        assert won is True
        stored = await connection.fetchval(
            "SELECT backup_key_wrapped FROM exomem_cloud_cells WHERE cell_id = 'aaaaaaaaaaaaaaaa'"
        )
    finally:
        await connection.close()

    assert bytes(stored) == wrapped
    assert bytes(stored) != plaintext
    assert unwrap_secret(master_key, bytes(stored), **aad) == plaintext


async def test_advisory_lock_is_single_writer(cell_db: CellDatabase) -> None:
    # L4: D4's single-writer guard for the direct LISTEN session. `Recreate`
    # alone does not stop two pods overlapping during an eviction.
    first = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    second = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        assert await db.try_advisory_lock(first) is True
        assert await db.try_advisory_lock(second) is False  # first still holds it
    finally:
        await first.close()  # releases the session-level lock
        second_after = await db.try_advisory_lock(second)
        await second.close()
    assert second_after is True  # free once the holder's session closed


async def test_read_rollout_and_write_rollout_as_cellctl(cell_db: CellDatabase) -> None:
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        before = await db.read_rollout(connection)
        assert before.paused is False
        await db.write_rollout(connection, {"paused": True, "last_good_image": "img@sha256:aa"})
        after = await db.read_rollout(connection)
    finally:
        await connection.close()
    assert after.paused is True
    assert after.last_good_image == "img@sha256:aa"


async def test_write_capacity_inserts_then_updates(cell_db: CellDatabase) -> None:
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        now = datetime.now(UTC)
        await db.write_capacity(connection, node="node-1", cell_slots=8, attachments_used=1, observed_at=now)
        await db.write_capacity(connection, node="node-1", cell_slots=8, attachments_used=2, observed_at=now)
        row = await connection.fetchrow(
            "SELECT cell_slots, attachments_used FROM exomem_cloud_capacity WHERE node = 'node-1'"
        )
    finally:
        await connection.close()
    assert row["cell_slots"] == 8
    assert row["attachments_used"] == 2


async def test_read_cell_image_reads_the_settings_row(cell_db: CellDatabase) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute(
            "UPDATE exomem_cloud_settings SET value = to_jsonb($1::text) WHERE key = 'cell_image'",
            "registry.example/cell@sha256:" + "a" * 64,
        )
    finally:
        await owner.close()

    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        image = await db.read_cell_image(connection)
    finally:
        await connection.close()
    assert image == "registry.example/cell@sha256:" + "a" * 64


async def test_connect_sets_tcp_keepalives_and_a_statement_timeout(cell_db: CellDatabase) -> None:
    # D4: after a partition the server reaps the old session, and with it the
    # single-writer advisory lock, within about a minute rather than after
    # the two-hour server default.
    connection = await db.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        settings = {
            name: await connection.fetchval(f"SHOW {name}")
            for name in ("tcp_keepalives_idle", "tcp_keepalives_interval", "tcp_keepalives_count", "statement_timeout")
        }
    finally:
        await connection.close()
    assert settings == {
        "tcp_keepalives_idle": "30",
        "tcp_keepalives_interval": "10",
        "tcp_keepalives_count": "3",
        "statement_timeout": "30s",
    }


async def test_a_cell_id_with_a_trailing_newline_is_skipped_when_the_row_is_read() -> None:
    # D4: a full match; Python's `$` also matches just before a trailing
    # newline, so `^...$` with `match` let "aaaaaaaaaaaaaaaa\n" through.
    def record(cell_id: str) -> dict:
        values = {column: None for column in db.CELL_COLUMNS}
        values.update(
            cell_id=cell_id, tenant_id="t", storage_gib=10, rollout_priority=1, desired_state="running", generation=1, ready=False
        )
        return values

    class _Connection:
        async def fetch(self, query: str):
            return [record("aaaaaaaaaaaaaaaa"), record("bbbbbbbbbbbbbbbb\n")]

    rows = await db.select_all_rows(_Connection())
    assert [row.cell_id for row in rows] == ["aaaaaaaaaaaaaaaa"]
