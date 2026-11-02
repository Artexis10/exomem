"""The cell-unavailable, cell-disk-low and cell-recall-lagging alerts
(cloud-platform-operations, "Monitoring spans independent failure domains").

On 2026-10-16 a cell's volume filled and the cell answered 503 for hours with
nobody told. These tests run whole passes against the disposable Postgres and a
fake cluster, and read what the alert receiver would have been sent.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from cellctl import alerts, db, k8s_client, reconcile
from cellctl.reconcile import ClusterConfig, LoopMemory
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

from .conftest import CellDatabase
from .test_reconcile import FakeClusterGateway, _secrets_config, _seed_cell, _served

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
CELL = "aaaaaaaaaaaaaaaa"
GIB = 1024**3
# Not the contract's numbers: these tests pin the logic, and the chart tests pin where the numbers come from.
THRESHOLDS = alerts.AlertThresholds(
    unavailable_after=timedelta(minutes=15), disk_free_floor_bytes=GIB, recall_lag_after=timedelta(minutes=10)
)
CONFIG = ClusterConfig(
    object_storage_bucket="b",
    alert_delivery_secret=("exomem-platform", "exomem-hosted-alert-delivery", "url"),
    alert_thresholds=THRESHOLDS,
)
UNREADY = {"pod_ready": False}
POD_IP = "10.42.0.7"
OTHER_CELL, OTHER_POD_IP = "bbbbbbbbbbbbbbbb", "10.42.0.8"


class Cluster(FakeClusterGateway):
    api_down = False
    # Each ready pod's readiness answer, by pod IP; a pod missing here does not answer.
    readiness: dict[str, bytes]

    def __init__(self) -> None:
        super().__init__()
        self.readiness = {}

    def observe(self, cell_id: str, namespace: str):
        if self.api_down:
            raise OSError("the cluster API is down")
        return super().observe(cell_id, namespace)

    def read_cell_readiness(self, addresses: dict[str, str]) -> dict[str, bytes]:
        return {cell: self.readiness[ip] for cell, ip in addresses.items() if ip in self.readiness}

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
    await reconcile.reconcile_once(connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(),
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


def _answer(state: str, **lag: object) -> bytes:
    """A cell's readiness answer, with the other fields a real one carries."""
    return json.dumps({
        "status": "ready", "service": "exomem", "reasons": [],
        "retrieval": {"state": state, "admitted": state == "ready", **lag},
    }).encode()


def _lagging(seconds: float | None) -> bytes:
    return _answer("lagging", lagging_scopes=["notes"], catalogue_unchanged_seconds=seconds)


async def test_a_cell_whose_recall_lags_past_the_threshold_fires_and_catching_up_clears_it(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]]
) -> None:
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    cluster, memory = Cluster(), LoopMemory()
    cluster.observations[CELL] = _served(CELL, ready_pod_ip=POD_IP)
    try:
        cluster.readiness[POD_IP] = _lagging(599.999)
        await _pass(connection, cluster, memory, NOW)
        assert _states(delivered, alerts.RECALL_LAG_ALERT) == []

        # The age is the cell's own: one minute of cellctl time later it reports exactly the threshold.
        cluster.readiness[POD_IP] = _lagging(600)
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=1))
        assert _states(delivered, alerts.RECALL_LAG_ALERT) == [True]

        cluster.readiness[POD_IP] = _answer("ready")
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=2))
    finally:
        await connection.close()

    assert _states(delivered, alerts.RECALL_LAG_ALERT) == [True, False]


@pytest.mark.parametrize(
    "answer",
    [
        None,  # the pod does not answer, or answers 503
        _lagging(None),  # the cell lags but has no age to report
        b"[" * 50_000,  # an answer nested deeper than the JSON parser recurses
        _lagging(10**400),  # an age no float can hold, which a compromised cell can send
    ],
    ids=["no-answer", "lag-with-no-age", "garbled-answer", "hostile-age"],
)
async def test_a_missing_unreadable_or_hostile_answer_never_resolves_a_firing_alert(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]], answer: bytes | None
) -> None:
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    cluster, memory = Cluster(), LoopMemory()
    cluster.observations[CELL] = _served(CELL, ready_pod_ip=POD_IP)
    try:
        cluster.readiness[POD_IP] = _lagging(3600)
        await _pass(connection, cluster, memory, NOW)
        if answer is None:
            del cluster.readiness[POD_IP]
        else:
            cluster.readiness[POD_IP] = answer
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=1))
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=2))
    finally:
        await connection.close()

    assert _states(delivered, alerts.RECALL_LAG_ALERT) == [True]


@pytest.mark.parametrize(
    "later",
    [
        _lagging(5),  # a busy catalogue lags briefly and often
        None,  # the pod stops answering, or loses readiness
    ],
    ids=["brief-lag", "no-answer"],
)
async def test_a_cell_that_catches_up_resolves_while_another_says_nothing_new(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]], later: bytes | None
) -> None:
    # The other cell read strictly ready once; a later answer that cannot be
    # judged says nothing new about it, so it must not hold the resolution back.
    await _seed_cell(cell_db, CELL, "tenant-a")
    await _seed_cell(cell_db, OTHER_CELL, "tenant-b")
    connection = await _connect(cell_db)
    cluster, memory = Cluster(), LoopMemory()
    cluster.observations[CELL] = _served(CELL, ready_pod_ip=POD_IP)
    cluster.observations[OTHER_CELL] = _served(OTHER_CELL, ready_pod_ip=OTHER_POD_IP)
    try:
        cluster.readiness[POD_IP] = _lagging(3600)
        cluster.readiness[OTHER_POD_IP] = _answer("ready")
        await _pass(connection, cluster, memory, NOW)
        if later is None:
            del cluster.readiness[OTHER_POD_IP]
        else:
            cluster.readiness[OTHER_POD_IP] = later
        cluster.readiness[POD_IP] = _answer("ready")
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=1))
    finally:
        await connection.close()

    assert _states(delivered, alerts.RECALL_LAG_ALERT) == [True, False]


async def test_a_stalled_cell_stopped_and_started_again_is_judged_afresh(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]]
) -> None:
    # The started cell runs a new process that reports a young lag: its stall
    # before the stop is not evidence about the catalogue it serves now.
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    cluster, memory = Cluster(), LoopMemory()
    cluster.observations[CELL] = _served(CELL, ready_pod_ip=POD_IP)
    try:
        cluster.readiness[POD_IP] = _lagging(3600)
        await _pass(connection, cluster, memory, NOW)
        await owner.execute("UPDATE exomem_cloud_cells SET desired_state = 'stopped' WHERE cell_id = $1", CELL)
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=1))
        await owner.execute("UPDATE exomem_cloud_cells SET desired_state = 'running' WHERE cell_id = $1", CELL)
        cluster.readiness[POD_IP] = _lagging(30)
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=2))
    finally:
        await owner.close()
        await connection.close()

    assert _states(delivered, alerts.RECALL_LAG_ALERT) == [True, False]


async def test_a_stalled_cell_with_no_ready_pod_keeps_its_last_verdict(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]]
) -> None:
    # A stuck cell that goes unready has no pod to read. The cell-unavailable
    # alert covers the outage; this one must not report its stall as over.
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    cluster, memory = Cluster(), LoopMemory()
    cluster.readiness[POD_IP] = _lagging(3600)
    try:
        cluster.observations[CELL] = _served(CELL, ready_pod_ip=POD_IP)
        await _pass(connection, cluster, memory, NOW)
        cluster.observations[CELL] = _served(CELL, **UNREADY)
        await _pass(connection, cluster, memory, NOW + timedelta(minutes=1))
    finally:
        await connection.close()

    assert _states(delivered, alerts.RECALL_LAG_ALERT) == [True]


async def test_a_restart_of_the_cell_or_of_cellctl_during_a_stall_never_sends_a_false_resolution(
    cell_db: CellDatabase, delivered: list[tuple[str, bool]]
) -> None:
    # The cell measures the age from when its own process first saw the
    # catalogue, so a restarted cell reports a young lag for an old stall. A
    # restarted cellctl has no verdicts until it reads each cell again.
    await _seed_cell(cell_db, CELL, "tenant-a")
    connection = await _connect(cell_db)
    cluster = Cluster()
    cluster.observations[CELL] = _served(CELL, ready_pod_ip=POD_IP)
    try:
        before_restart = LoopMemory()
        cluster.readiness[POD_IP] = _lagging(3600)
        await _pass(connection, cluster, before_restart, NOW)
        cluster.readiness[POD_IP] = _lagging(30)
        await _pass(connection, cluster, before_restart, NOW + timedelta(minutes=1))

        after_restart = LoopMemory()
        cluster.readiness[POD_IP] = _lagging(90)
        await _pass(connection, cluster, after_restart, NOW + timedelta(minutes=2))
        cluster.readiness[POD_IP] = _lagging(600)
        await _pass(connection, cluster, after_restart, NOW + timedelta(minutes=11))
    finally:
        await connection.close()

    assert _states(delivered, alerts.RECALL_LAG_ALERT) == [True, True]


def test_the_readiness_read_asks_the_health_port_for_readiness_and_drops_a_not_ready_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cell port carries MCP and every bearer route; cellctl derives the
    # bearers, so it must read readiness only on the health-only port.
    asked: list[str] = []

    class Cell(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
            asked.append(self.path)
            self.send_response(200 if self.server.ready else 503)
            self.end_headers()
            self.wfile.write(_lagging(30))

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Cell)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(k8s_client, "CELL_HEALTH_PORT", server.server_address[1])
    client = k8s_client.ClusterClient.__new__(k8s_client.ClusterClient)
    try:
        server.ready = True
        ready = client.read_cell_readiness({CELL: "127.0.0.1"})
        server.ready = False
        not_ready = client.read_cell_readiness({CELL: "127.0.0.1"})
    finally:
        server.shutdown()
        server.server_close()

    assert asked == ["/health/ready", "/health/ready"]
    assert ready == {CELL: _lagging(30)}
    assert not_ready == {}


def test_a_fleet_read_gives_every_cell_the_whole_budget_however_many_cells_there_are(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each answer takes most of the budget. Reads queued behind a connection
    # cap would run out of time, and the cells at the tail would go unjudged.
    cells = {f"{index:016x}": "127.0.0.1" for index in range(150)}

    class SlowCell(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
            time.sleep(k8s_client.READINESS_TIMEOUT * 2 / 3)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(_answer("ready"))

        def log_message(self, *args) -> None:
            pass

    class Server(http.server.ThreadingHTTPServer):
        request_queue_size = len(cells)  # every cell connects at once

    server = Server(("127.0.0.1", 0), SlowCell)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(k8s_client, "CELL_HEALTH_PORT", server.server_address[1])
    client = k8s_client.ClusterClient.__new__(k8s_client.ClusterClient)
    try:
        answers = client.read_cell_readiness(cells)
    finally:
        server.shutdown()
        server.server_close()

    assert sorted(answers) == sorted(cells)
