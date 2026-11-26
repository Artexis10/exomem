"""A backup hold publishes the cell's two storage figures to its row (add-cloud-media-store D10, task 4.9).

Each case drives one cell through its nightly backup against a real database and reads the four media usage columns.
The Job's report comes from the cluster observation, as ClusterClient.observe() builds it from the termination message.
The time column is the cell's: the record's write time, or for the media figure the older census behind it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from cellctl import db, media_meter, reconcile
from cellctl.decide import DEFAULT_RECONCILE_CONFIG
from cellctl.state import MediaStorage
from cellctl.storage.fake_b2 import FakeB2

from .test_media_edge import CANARY, _fleet, _turn_off
from .test_reconcile import _observed_from_applied

WINDOW = datetime(2026, 1, 1, 2, tzinfo=UTC)
READ_AT = WINDOW + timedelta(minutes=1)  # When the cell wrote the record that the Job copied.
ALLOWANCE = 10 << 30
SNAPSHOT = "e" * 64
USAGE = ("vault_used_bytes", "media_used_bytes", "media_allowance_bytes", "media_usage_observed_at")
NO_FIGURES = dict.fromkeys(USAGE)


async def _served_fleet(cell_db, allowance: object = ALLOWANCE, **edge: object):
    fleet = await _fleet(cell_db, CANARY, store_policy={"allowance_bytes": allowance}, **edge)
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


def _reading(vault: int | None, media: int | None, *, at: datetime, census: datetime | None | str = "at") -> dict:
    """The Job's report of a record written `at`, whose media figure comes from the census read at `census`
    (by default the same time, and none for a media figure that is unknown)."""
    observed = (at if media is not None else None) if census == "at" else census
    return {"backup_job_media_storage": MediaStorage(
        vault_bytes=vault, media_used_bytes=media, captured_at=at, media_observed_at=observed)}


async def test_a_backup_publishes_the_cells_two_figures_and_its_allowance_beside_the_use_it_measured(cell_db) -> None:
    """The outcome: the record the Job copied is in the four columns Home reads, dated by the cell's reading and not
    by the backup, and used_bytes keeps its meaning."""
    fleet = await _served_fleet(cell_db)
    try:
        recorded = await _back_up(fleet, **_reading(9_000, 5_000, at=READ_AT))

        row = await _row(fleet)

        assert {column: row[column] for column in USAGE} == {
            "vault_used_bytes": 9_000, "media_used_bytes": 5_000, "media_allowance_bytes": ALLOWANCE,
            "media_usage_observed_at": READ_AT}
        assert (row["used_bytes"], row["last_backup_snapshot"], row["last_backup_at"]) == (700, SNAPSHOT, recorded)
    finally:
        await fleet.close()


async def test_a_media_figure_from_an_old_census_is_published_with_the_census_time(cell_db) -> None:
    """Catches a quiet cell whose owner census is a month old, which Home would show as read at last night's backup
    although deletions since then have lowered the real figure."""
    fleet = await _served_fleet(cell_db)
    try:
        census = WINDOW - timedelta(days=30)
        await _back_up(fleet, **_reading(9_000, 5_000, at=READ_AT, census=census))

        row = await _row(fleet)

        assert {column: row[column] for column in USAGE} == {
            "vault_used_bytes": 9_000, "media_used_bytes": 5_000, "media_allowance_bytes": ALLOWANCE,
            "media_usage_observed_at": census}  # Old, and still a figure: a quiet cell's census can be right.
    finally:
        await fleet.close()


async def test_a_media_figure_that_names_no_census_time_is_unknown_beside_the_vault_figure(cell_db) -> None:
    """Catches a media figure that nothing dates: Home would show it as read when the record was written."""
    fleet = await _served_fleet(cell_db)
    try:
        await _back_up(fleet, **_reading(9_000, 5_000, at=READ_AT, census=None))

        row = await _row(fleet)

        assert {column: row[column] for column in USAGE} == {
            "vault_used_bytes": 9_000, "media_used_bytes": None, "media_allowance_bytes": ALLOWANCE,
            "media_usage_observed_at": READ_AT}
    finally:
        await fleet.close()


async def test_a_figure_the_cell_could_not_read_is_unknown_in_the_row_beside_the_one_it_could(cell_db) -> None:
    """Catches an unknown figure that Home would show as 0 bytes, or that blanks the figure the cell did read."""
    fleet = await _served_fleet(cell_db)
    try:
        await _back_up(fleet, **_reading(None, 5_000, at=READ_AT))

        row = await _row(fleet)

        assert {column: row[column] for column in USAGE} == {
            "vault_used_bytes": None, "media_used_bytes": 5_000, "media_allowance_bytes": ALLOWANCE,
            "media_usage_observed_at": READ_AT}
    finally:
        await fleet.close()


async def test_a_record_older_than_the_bound_publishes_unknown_never_its_figures(cell_db) -> None:
    """Catches a cell that stopped writing its record while Home keeps showing the last figures as current. With no
    figure to date, the time is the backup that found none."""
    fleet = await _served_fleet(cell_db)
    try:
        stale = WINDOW - 2 * DEFAULT_RECONCILE_CONFIG.media_storage_max_age  # The record was read before the bound.
        recorded = await _back_up(fleet, **_reading(9_000, 5_000, at=stale, census=stale))

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
        await _back_up(fleet, **_reading(9_000, 5_000, at=READ_AT))
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

        await _back_up(fleet, **_reading(9_000, 5_000, at=READ_AT))

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


async def test_an_allowance_beyond_the_column_is_unknown_and_the_figures_are_still_published(cell_db) -> None:
    """Catches a policy whose allowance no bigint holds: its write would fail for the cell on every backup."""
    fleet = await _served_fleet(cell_db, allowance=2**63)
    try:
        await _back_up(fleet, **_reading(9_000, 5_000, at=READ_AT))

        row = await _row(fleet)

        assert {column: row[column] for column in USAGE} == {
            "vault_used_bytes": 9_000, "media_used_bytes": 5_000, "media_allowance_bytes": None,
            "media_usage_observed_at": READ_AT}
    finally:
        await fleet.close()


async def test_a_refused_figure_write_is_logged_and_the_rest_of_the_pass_still_runs(
        cell_db, caplog, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches a Substrate constraint that differs from the column contract: the refusal must not end the pass before
    the rollout write that follows it, and the backup stays recorded."""
    fleet = await _served_fleet(cell_db)
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute("ALTER TABLE exomem_cloud_cells ADD CONSTRAINT refuses_the_allowance "
                            "CHECK (media_allowance_bytes IS NULL OR media_allowance_bytes < 5)")
    finally:
        await owner.close()
    decide = reconcile.decide
    rollout_writes: list[dict] = []
    write_rollout = db.write_rollout

    def decide_with_rollout(*args, **kwargs):
        decision = decide(*args, **kwargs)
        if "last_backup_snapshot" in decision.row_updates:
            decision.rollout_updates = {"last_good_image": "registry.example/cell:1"}
        return decision

    async def spy_write_rollout(connection, updates, **kwargs):
        rollout_writes.append(updates)
        return await write_rollout(connection, updates, **kwargs)

    monkeypatch.setattr(reconcile, "decide", decide_with_rollout)
    monkeypatch.setattr(db, "write_rollout", spy_write_rollout)
    try:
        with caplog.at_level(logging.WARNING, logger="cellctl"):
            await _back_up(fleet, **_reading(9_000, 5_000, at=READ_AT))

        row = await _row(fleet)
        assert (row["used_bytes"], row["last_backup_snapshot"]) == (700, SNAPSHOT)
        assert {column: row[column] for column in USAGE} == NO_FIGURES
        assert [updates for updates in rollout_writes if "last_good_image" in updates] != []
        assert caplog.text.count("the media usage figures were not written") == 1
        assert "CheckViolationError" in caplog.text and "10737418240" not in caplog.text  # The class, never the row.
    finally:
        await fleet.close()


async def test_the_owner_figure_is_unchanged_when_only_the_physical_listing_fails(cell_db) -> None:
    """Task 4.9's third clause: the operator's bucket listing is a separate meter (D10), so a listing that fails
    leaves the figure that the cell reported, and its time, as they are."""
    fleet = await _served_fleet(cell_db, physical_headroom=1.5)
    try:
        listing = fleet.media_listing = FakeB2()
        listing.fail_listing_with = RuntimeError("the provider is away")

        await _back_up(fleet, **_reading(9_000, 5_000, at=READ_AT))

        row = await _row(fleet)
        assert fleet.memory.media_physical[CANARY] == media_meter.UNKNOWN  # The listing was tried, and failed.
        assert {column: row[column] for column in USAGE} == {
            "vault_used_bytes": 9_000, "media_used_bytes": 5_000, "media_allowance_bytes": ALLOWANCE,
            "media_usage_observed_at": READ_AT}
    finally:
        await fleet.close()
