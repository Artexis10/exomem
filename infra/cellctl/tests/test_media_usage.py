"""A backup hold publishes the cell's two storage figures to its row (add-cloud-media-store D10, task 4.9).

Each case drives one cell through its nightly backup against a real database and reads the four media usage columns.
The Job's report comes from the cluster observation, as ClusterClient.observe() builds it from the termination message.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import asyncpg

from cellctl import reconcile
from cellctl.decide import DEFAULT_RECONCILE_CONFIG
from cellctl.state import MediaStorage

from .test_media_edge import CANARY, _fleet, _turn_off
from .test_reconcile import _observed_from_applied

WINDOW = datetime(2026, 1, 1, 2, tzinfo=UTC)
ALLOWANCE = 10 << 30
SNAPSHOT = "e" * 64
USAGE = ("vault_used_bytes", "media_used_bytes", "media_allowance_bytes", "media_usage_observed_at")
NO_FIGURES = dict.fromkeys(USAGE)


async def _served_fleet(cell_db):
    fleet = await _fleet(cell_db, CANARY, store_policy={"allowance_bytes": ALLOWANCE})
    fleet.config = await reconcile._observe_media_usage_columns(fleet.connection, fleet.config)
    return fleet


async def _back_up(fleet, day: int = 0, **report: object) -> datetime:
    """Drive the canary through its nightly backup, give it the Job's report, and return the pass that recorded it."""
    cluster, start = fleet.cluster, WINDOW + timedelta(days=day)
    for _ in range(3):
        await fleet.run(start - timedelta(hours=3))
        cluster.observations[CANARY] = _observed_from_applied(cluster, CANARY)
    await fleet.run(start)
    cluster.observations[CANARY] = _observed_from_applied(
        cluster, CANARY, pod_exists=False, pod_uses_volume=False, pod_ready=False)
    await fleet.run(start + timedelta(minutes=1))
    cluster.observations[CANARY] = _observed_from_applied(
        cluster, CANARY, backup_job_succeeded=True, backup_job_snapshot_id=SNAPSHOT,
        backup_job_used_bytes=700, backup_job_total_bytes=1000, **report)
    await fleet.run(start + timedelta(minutes=2))
    return start + timedelta(minutes=2)


async def _row(fleet) -> dict:
    row = await fleet.connection.fetchrow(
        f"SELECT {', '.join(USAGE)}, used_bytes, last_backup_snapshot, last_backup_at, hold_kind "
        "FROM exomem_cloud_cells WHERE cell_id = $1", CANARY)
    return dict(row)


def _reading(vault: int | None, media: int | None, *, at: datetime) -> dict:
    return {"backup_job_media_storage": MediaStorage(vault_bytes=vault, media_used_bytes=media, captured_at=at)}


async def test_a_backup_publishes_the_cells_two_figures_and_its_allowance_beside_the_use_it_measured(cell_db) -> None:
    """The outcome: the record the Job copied is in the four columns Home reads, and used_bytes keeps its meaning."""
    fleet = await _served_fleet(cell_db)
    try:
        recorded = await _back_up(fleet, **_reading(9_000, 5_000, at=WINDOW + timedelta(minutes=1)))

        row = await _row(fleet)

        assert {column: row[column] for column in USAGE} == {
            "vault_used_bytes": 9_000, "media_used_bytes": 5_000, "media_allowance_bytes": ALLOWANCE,
            "media_usage_observed_at": recorded}
        assert (row["used_bytes"], row["last_backup_snapshot"], row["last_backup_at"]) == (700, SNAPSHOT, recorded)
    finally:
        await fleet.close()


async def test_a_figure_the_cell_could_not_read_is_unknown_in_the_row_beside_the_one_it_could(cell_db) -> None:
    """Catches an unknown figure that Home would show as 0 bytes, or that blanks the figure the cell did read."""
    fleet = await _served_fleet(cell_db)
    try:
        recorded = await _back_up(fleet, **_reading(None, 5_000, at=WINDOW + timedelta(minutes=1)))

        row = await _row(fleet)

        assert {column: row[column] for column in USAGE} == {
            "vault_used_bytes": None, "media_used_bytes": 5_000, "media_allowance_bytes": ALLOWANCE,
            "media_usage_observed_at": recorded}
    finally:
        await fleet.close()


async def test_a_record_older_than_the_bound_publishes_unknown_never_its_figures(cell_db) -> None:
    """Catches a cell that stopped writing its record while Home keeps showing the last figures as current."""
    fleet = await _served_fleet(cell_db)
    try:
        stale = WINDOW - 2 * DEFAULT_RECONCILE_CONFIG.media_storage_max_age  # The record was read before the bound.
        recorded = await _back_up(fleet, **_reading(9_000, 5_000, at=stale))

        row = await _row(fleet)

        assert {column: row[column] for column in USAGE} == {
            "vault_used_bytes": None, "media_used_bytes": None, "media_allowance_bytes": ALLOWANCE,
            "media_usage_observed_at": recorded}
    finally:
        await fleet.close()


async def test_a_job_that_reports_no_record_publishes_unknown_and_changes_nothing_else_in_the_pass(cell_db) -> None:
    """An in-flight Job rendered before this release, or one that failed to read: unknown, and the backup still records."""
    fleet = await _served_fleet(cell_db)
    try:
        recorded = await _back_up(fleet)  # no backup_job_media_storage

        row = await _row(fleet)

        assert {column: row[column] for column in USAGE} == {
            "vault_used_bytes": None, "media_used_bytes": None, "media_allowance_bytes": ALLOWANCE,
            "media_usage_observed_at": recorded}
        assert (row["used_bytes"], row["last_backup_snapshot"], row["hold_kind"]) == (700, SNAPSHOT, None)
    finally:
        await fleet.close()


async def test_a_cell_whose_store_is_turned_off_has_all_four_columns_back_at_null(cell_db) -> None:
    """Catches figures that outlive the store: Home would show a media meter for a cell that has none."""
    fleet = await _served_fleet(cell_db)
    try:
        await _back_up(fleet, **_reading(9_000, 5_000, at=WINDOW + timedelta(minutes=1)))
        assert (await _row(fleet))["media_used_bytes"] == 5_000
        _turn_off(fleet)

        await _back_up(fleet, day=1, **_reading(9_000, 5_000, at=WINDOW + timedelta(days=1, minutes=1)))

        row = await _row(fleet)
        assert {column: row[column] for column in USAGE} == NO_FIGURES
        assert row["last_backup_at"] > WINDOW + timedelta(days=1)  # The second backup did record.
    finally:
        await fleet.close()


async def test_a_schema_without_the_columns_records_the_backup_and_writes_no_figure(cell_db, caplog) -> None:
    """Catches a cellctl that stops, or fails the pass, on a Substrate that predates the migration."""
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute("ALTER TABLE exomem_cloud_cells DROP COLUMN vault_used_bytes, "
                            "DROP COLUMN media_used_bytes, DROP COLUMN media_allowance_bytes, "
                            "DROP COLUMN media_usage_observed_at")
    finally:
        await owner.close()
    fleet = await _fleet(cell_db, CANARY, store_policy={"allowance_bytes": ALLOWANCE})
    try:
        with caplog.at_level(logging.WARNING, logger="cellctl"):
            fleet.config = await reconcile._observe_media_usage_columns(fleet.connection, fleet.config)
        assert fleet.config.media_usage_columns is False
        assert caplog.text.count("no media usage columns") == 1

        await _back_up(fleet, **_reading(9_000, 5_000, at=WINDOW + timedelta(minutes=1)))

        row = await fleet.connection.fetchrow(
            "SELECT used_bytes, last_backup_snapshot FROM exomem_cloud_cells WHERE cell_id = $1", CANARY)
        assert (row["used_bytes"], row["last_backup_snapshot"]) == (700, SNAPSHOT)
    finally:
        await fleet.close()


async def test_cellctl_reads_the_columns_only_where_its_role_may_update_all_four(cell_db) -> None:
    """Catches a detection that counts a column the role cannot write: every backup pass would then fail."""
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute("REVOKE UPDATE (media_allowance_bytes) ON exomem_cloud_cells FROM exomem_cellctl")
    finally:
        await owner.close()
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        config = await reconcile._observe_media_usage_columns(connection, reconcile.ClusterConfig(object_storage_bucket="b"))
    finally:
        await connection.close()

    assert config.media_usage_columns is False
