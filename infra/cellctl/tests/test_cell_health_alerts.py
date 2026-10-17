"""The cell-unavailable and cell-disk-low alerts (cloud-platform-operations,
"Monitoring spans independent failure domains").

On 2026-10-16 a cell's volume filled and the cell answered 503 for hours with
nobody told. These tests run whole passes against the disposable Postgres and a
fake cluster, and read what the alert receiver would have been sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from cellctl import alerts, db, reconcile
from cellctl.reconcile import ClusterConfig, LoopMemory
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

from .conftest import CellDatabase
from .test_reconcile import FakeClusterGateway, _secrets_config, _seed_cell, _served

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
CELL = "aaaaaaaaaaaaaaaa"
GIB = 1024**3
# Not the contract's numbers: these tests pin the logic, and the chart tests pin where the numbers come from.
THRESHOLDS = alerts.AlertThresholds(unavailable_after=timedelta(minutes=15), disk_free_floor_bytes=GIB)
CONFIG = ClusterConfig(
    object_storage_bucket="b",
    alert_delivery_secret=("exomem-platform", "exomem-hosted-alert-delivery", "url"),
    alert_thresholds=THRESHOLDS,
)
UNREADY = {"pod_ready": False}


class Cluster(FakeClusterGateway):
    api_down = False

    def observe(self, cell_id: str, namespace: str):
        if self.api_down:
            raise OSError("the cluster API is down")
        return super().observe(cell_id, namespace)

    def read_secret_value(self, namespace: str, name: str, key: str) -> str:
        return "https://receiver.example/alerts/token"


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    sent: list[tuple[str, bool]] = []

    def deliver(url: str, *, alert: str, active: bool, observed_at: datetime) -> None:
        sent.append((alert, active))

    monkeypatch.setattr(alerts, "deliver", deliver)
    return sent


def _states(delivered: list[tuple[str, bool]], alert: str) -> list[bool]:
    return [active for name, active in delivered if name == alert]


async def _connect(cell_db: CellDatabase) -> asyncpg.Connection:
    return await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))


async def _pass(connection: asyncpg.Connection, cluster: Cluster, memory: LoopMemory, at: datetime) -> None:
    await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                   CONFIG, now=at, memory=memory)


async def test_a_serving_cell_unready_past_the_threshold_fires_and_recovery_clears_it(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]]
) -> None:
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    cluster, memory = Cluster(), LoopMemory()
    try:
        cluster.observations[CELL] = _served(CELL, **UNREADY)
        await _pass(connection, cluster, memory, NOW)
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=14, seconds=59))
        assert _states(delivered, alerts.UNAVAILABLE_ALERT) == []

        await _pass(connection, cluster, memory, NOW + timedelta(minutes=15))
        assert _states(delivered, alerts.UNAVAILABLE_ALERT) == [True]

        cluster.observations[CELL] = _served(CELL)
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=16))
    finally:
        await connection.close()

    assert _states(delivered, alerts.UNAVAILABLE_ALERT) == [True, False]


@pytest.mark.parametrize(
    ("desired_state", "overrides", "row_hold"),
    [
        ("running", {"statefulset_hold_kind": "upgrade", "statefulset_hold_started_at": NOW}, None),
        # The StatefulSet cannot be read, so the row's own hold is what says it is a transition.
        ("running", {"statefulset_exists": False}, "restore"),
        ("stopped", {}, None),
        ("deleted", {}, None),
    ],
    ids=["annotated-hold", "row-hold", "stopped", "deleted"],
)
async def test_a_cell_under_a_hold_or_not_serving_never_fires(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]], desired_state: str, overrides: dict, row_hold: str | None
) -> None:
    await _seed_cell(cell_db, CELL, "tenant-a", desired_state=desired_state)
    connection = await _connect(cell_db)
    cluster, memory = Cluster(), LoopMemory()
    cluster.observations[CELL] = _served(CELL, **UNREADY, **overrides)
    try:
        if row_hold:
            await db.write_observed(connection, CELL, {"hold_kind": row_hold, "hold_started_at": NOW})
        await _pass(connection, cluster, memory, NOW)
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=16))
    finally:
        await connection.close()

    assert True not in _states(delivered, alerts.UNAVAILABLE_ALERT)


async def test_a_pass_that_cannot_observe_the_cell_keeps_its_clock(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]]
) -> None:
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    cluster, memory = Cluster(), LoopMemory()
    cluster.observations[CELL] = _served(CELL, **UNREADY)
    try:
        await _pass(connection, cluster, memory, NOW)
        cluster.api_down = True
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=10))
        cluster.api_down = False
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=16))
    finally:
        await connection.close()

    assert _states(delivered, alerts.UNAVAILABLE_ALERT) == [True]


async def test_a_cell_stopped_and_started_again_begins_a_new_clock(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]]
) -> None:
    # An operator stops a cell for maintenance (an import, a restore): the
    # unready time before the stop must not count against its restart.
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    cluster, memory = Cluster(), LoopMemory()
    cluster.observations[CELL] = _served(CELL, **UNREADY)
    try:
        await _pass(connection, cluster, memory, NOW)
        await owner.execute("UPDATE exomem_cloud_cells SET desired_state = 'stopped' WHERE cell_id = $1", CELL)
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=10))
        await owner.execute("UPDATE exomem_cloud_cells SET desired_state = 'running' WHERE cell_id = $1", CELL)
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=11))
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=16))
    finally:
        await owner.close()
        await connection.close()

    assert True not in _states(delivered, alerts.UNAVAILABLE_ALERT)


async def test_a_restart_during_an_outage_never_sends_a_false_resolution(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]]
) -> None:
    # A restart forgets every clock. The receiver emails RESOLVED on the first
    # False after a True, so the new process must say nothing until it knows.
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    cluster = Cluster()
    cluster.observations[CELL] = _served(CELL, **UNREADY)
    try:
        before_restart = LoopMemory()
        await _pass(connection, cluster, before_restart, NOW)
        await _pass(connection, cluster, before_restart, NOW + timedelta(minutes=15))
        after_restart = LoopMemory()
        await _pass(connection, cluster, after_restart, NOW + timedelta(minutes=16))
        await _pass(connection, cluster, after_restart, NOW + timedelta(minutes=31))
    finally:
        await connection.close()

    assert _states(delivered, alerts.UNAVAILABLE_ALERT) == [True, True]


async def test_a_cell_below_the_free_bytes_floor_fires_and_one_above_it_does_not(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]]
) -> None:
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    cluster, memory = Cluster(), LoopMemory()
    try:
        # 10 GiB volume, 9 GiB used: exactly the floor free, which is not below it.
        cluster.observations[CELL] = _served(CELL, backup_job_used_bytes=9 * GIB, backup_job_total_bytes=10 * GIB)
        await _pass(connection, cluster, memory, NOW)
        assert _states(delivered, alerts.DISK_LOW_ALERT) == [False]

        cluster.observations[CELL] = _served(CELL, backup_job_used_bytes=9 * GIB + 1, backup_job_total_bytes=10 * GIB)
        await _pass(connection, cluster, memory, NOW + timedelta(hours=1))
        assert _states(delivered, alerts.DISK_LOW_ALERT) == [False, True]

        # The backup Job is gone on the next pass: the last reading still stands.
        cluster.observations[CELL] = _served(CELL)
        await _pass(connection, cluster, memory, NOW + timedelta(hours=1, minutes=1))
        assert _states(delivered, alerts.DISK_LOW_ALERT) == [False, True]

        cluster.observations[CELL] = _served(CELL, backup_job_used_bytes=2 * GIB, backup_job_total_bytes=20 * GIB)
        await _pass(connection, cluster, memory, NOW + timedelta(hours=25))
    finally:
        await connection.close()

    assert _states(delivered, alerts.DISK_LOW_ALERT) == [False, True, False]


async def test_a_disk_alert_the_receiver_refused_is_retried_after_the_backup_report_has_gone(
    cell_db: CellDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The report is visible for a pass or two, while the backup Job lasts. A
    # receiver that is down then must still be told the reading later.
    attempts: list[bool] = []

    def deliver(url: str, *, alert: str, active: bool, observed_at: datetime) -> None:
        if alert == alerts.DISK_LOW_ALERT:
            attempts.append(active)
            if len(attempts) == 1:
                raise RuntimeError("alert delivery failed")

    monkeypatch.setattr(alerts, "deliver", deliver)
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    cluster, memory = Cluster(), LoopMemory()
    try:
        cluster.observations[CELL] = _served(CELL, backup_job_used_bytes=10 * GIB - 1, backup_job_total_bytes=10 * GIB)
        await _pass(connection, cluster, memory, NOW)
        cluster.observations[CELL] = _served(CELL)
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=2))
    finally:
        await connection.close()

    assert attempts == [True, True]


async def test_a_cell_never_measured_leaves_the_disk_alert_unknown_not_resolved(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]]
) -> None:
    # After a restart nothing is measured until each cell's next backup; a
    # RESOLVED then would hide a cell that is still full.
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    cluster = Cluster()
    cluster.observations[CELL] = _served(CELL)
    try:
        await _pass(connection, cluster, LoopMemory(), NOW)
    finally:
        await connection.close()

    assert _states(delivered, alerts.DISK_LOW_ALERT) == []
