"""automate-cloud-releases phase 1 in cellctl: pinned attempts and the pause abort
(task 1.7), upgrade windows (1.8), the forward-only rule (1.10) and the rollout
alerts (1.11).

Whole passes run against the disposable Postgres and the fake cluster, as in
test_reconcile.py, whose helpers these reuse.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from cellctl import alerts, db, reconcile
from cellctl.barriers import Barrier
from cellctl.decide import ReconcileConfig
from cellctl.manifests import namespace_name
from cellctl.reconcile import ClusterConfig, LoopMemory
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
from cellctl.upgrade_window import WindowConfig

from .conftest import CellDatabase
from .test_reconcile import (
    _ATTEMPT_SNAPSHOT,
    IMAGE_A,
    IMAGE_B,
    FakeClusterGateway,
    _cluster_config,
    _observed_from_applied,
    _secrets_config,
    _seed_cell,
    _set_cell_image,
)

IMAGE_C = "registry.example/cell@sha256:" + "c" * 64
IMAGE_D = "registry.example/cell@sha256:" + "d" * 64
OWNER, TENANT, TENANT_2, TENANT_3 = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc", "dddddddddddddddd"
# 2026-01-01 is a Thursday; 12:00 UTC is outside every window these tests give a tenant.
NOON = datetime(2026, 1, 1, 12, tzinfo=UTC)
WINDOW_HOUR = 16
WINDOWS = WindowConfig(enabled=True, lookback_days=14, min_coverage=0.5, default_hour_start=5,
                       default_hour_end=2, idle_minutes=5, force_minutes=10)
IDLE = json.dumps({"traffic": {"inflight": 0, "last_request_age_seconds": 900}}).encode()
BUSY = json.dumps({"traffic": {"inflight": 2, "last_request_age_seconds": 0}}).encode()
ONE_WAY_CELL = (Barrier("vault-format", (1, 5, 0), "cell", "one-way"),)


def _history(quiet_hour: int, now: datetime) -> bytes:
    """Fourteen observed days, quietest at `quiet_hour`."""

    hours = 14 * 24
    first = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    requests = [1 if (first + timedelta(hours=index)).hour == quiet_hour else 50 for index in range(hours)]
    return json.dumps({"retention_hours": 672, "first_hour": first.isoformat().replace("+00:00", "Z"),
                       "requests": requests, "observed_seconds": [3600] * hours}).encode()


async def _pass(connection, cluster, now, *, config=reconcile.DEFAULT_RECONCILE_CONFIG, memory=None,
                cluster_config=None) -> None:
    await reconcile.reconcile_once(connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(),
                                   cluster_config or _cluster_config(), config=config, now=now, memory=memory)


def _statefulset(cluster: FakeClusterGateway, cell_id: str) -> dict:
    return cluster.applied[(namespace_name(cell_id), "StatefulSet", "cell")]


def _image(cluster: FakeClusterGateway, cell_id: str) -> str:
    return _statefulset(cluster, cell_id)["spec"]["template"]["spec"]["containers"][0]["image"]


def _annotations(cluster: FakeClusterGateway, cell_id: str) -> dict:
    return _statefulset(cluster, cell_id)["metadata"]["annotations"]


def _observe(cluster: FakeClusterGateway, *cell_ids: str, **overrides: object) -> None:
    for cell_id in cell_ids:
        ip = {"ready_pod_ip": f"10.42.0.{ord(cell_id[0])}"}
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id, **{**ip, **overrides})


class Fleet:
    """An owner cell (the canary) and tenants, converged on IMAGE_A."""

    def __init__(self, cell_db: CellDatabase, connection, admin, cluster, config, memory) -> None:
        self.cell_db, self.connection, self.admin = cell_db, connection, admin
        self.cluster, self.config, self.memory = cluster, config, memory

    @classmethod
    async def start(cls, cell_db: CellDatabase, *tenants: str, config=reconcile.DEFAULT_RECONCILE_CONFIG, now=NOON,
                    histories: dict[str, bytes] | None = None) -> Fleet:
        for index, cell_id in enumerate((OWNER, *tenants)):
            await _seed_cell(cell_db, cell_id, f"tenant-{index}")
        admin = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
        connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
        await admin.execute("UPDATE exomem_cloud_cells SET rollout_priority = 0 WHERE cell_id = $1", OWNER)
        fleet = cls(cell_db, connection, admin, FakeClusterGateway(), config, LoopMemory())
        fleet.cluster.histories.update(histories or {})
        for _ in range(3):
            await fleet.run(now - timedelta(minutes=30))
            _observe(fleet.cluster, OWNER, *tenants)
        return fleet

    async def run(self, now: datetime, **kwargs) -> None:
        await _pass(self.connection, self.cluster, now, config=self.config, memory=self.memory, **kwargs)

    async def close(self) -> None:
        await self.connection.close()
        await self.admin.close()

    async def to_target_applied(self, cell_id: str, now: datetime) -> datetime:
        """From an attempt's start: stop, the attempt's own backup, then the target's apply."""

        await self.run(now)  # the hold starts, scaled to 0
        _observe(self.cluster, cell_id)
        await self.run(now + timedelta(seconds=5))  # the backup Job runs
        _observe(self.cluster, cell_id, backup_job_succeeded=True, backup_job_snapshot_id=_ATTEMPT_SNAPSHOT)
        await self.run(now + timedelta(seconds=35))  # the snapshot is recorded
        _observe(self.cluster, cell_id)
        await self.run(now + timedelta(seconds=40))  # the target is applied
        _observe(self.cluster, cell_id, pod_ready=False, ready_pod_image=None)
        return now + timedelta(seconds=45)

    async def finish(self, cell_id: str, now: datetime) -> datetime:
        """The applied target becomes Ready, and the hold ends."""

        _observe(self.cluster, cell_id)
        await self.run(now)
        _observe(self.cluster, cell_id)
        await self.run(now + timedelta(seconds=5))
        _observe(self.cluster, cell_id)
        return now + timedelta(seconds=10)

    async def rollout(self):
        return await db.read_rollout(self.connection)


# --- Task 1.7: pinned attempts and the pause abort ---------------------------------


async def test_an_attempt_keeps_its_pinned_target_when_cell_image_changes(cell_db: CellDatabase) -> None:
    # Catches an un-canaried image applied to an in-flight cell.
    fleet = await Fleet.start(cell_db, TENANT)
    try:
        await _set_cell_image(cell_db, IMAGE_B)
        now = await fleet.to_target_applied(OWNER, NOON)
        assert _annotations(fleet.cluster, OWNER)["exomem.io/upgrade-target"] == IMAGE_B

        await _set_cell_image(cell_db, IMAGE_C)
        await fleet.run(now)

        assert _image(fleet.cluster, OWNER) == IMAGE_B
        assert _annotations(fleet.cluster, OWNER)["exomem.io/upgrade-target"] == IMAGE_B
        now = await fleet.finish(OWNER, now + timedelta(seconds=5))
        assert _image(fleet.cluster, OWNER) == IMAGE_B
        # The canary is behind its new target again: it starts toward IMAGE_C before any tenant.
        await fleet.run(now)
        assert _annotations(fleet.cluster, OWNER)["exomem.io/upgrade-target"] == IMAGE_C
        assert "exomem.io/hold" not in _annotations(fleet.cluster, TENANT)
    finally:
        await fleet.close()


async def test_a_restarted_cellctl_resumes_the_attempt_with_its_pinned_target(cell_db: CellDatabase) -> None:
    # Catches a pin kept only in memory.
    fleet = await Fleet.start(cell_db)
    try:
        await _set_cell_image(cell_db, IMAGE_B)
        now = await fleet.to_target_applied(OWNER, NOON)
        await _set_cell_image(cell_db, IMAGE_C)

        fleet.memory = LoopMemory()  # a new cellctl process
        await fleet.run(now)

        assert _image(fleet.cluster, OWNER) == IMAGE_B
        assert _annotations(fleet.cluster, OWNER)["exomem.io/hold"] == "upgrade"
    finally:
        await fleet.close()


async def test_an_attempt_without_a_pin_that_has_not_applied_ends_on_its_current_image(
        cell_db: CellDatabase) -> None:
    # Catches an attempt started by a cellctl before the pin taking a cell_image the
    # canary never ran.
    fleet = await Fleet.start(cell_db, TENANT)
    try:
        await _set_cell_image(cell_db, IMAGE_B)
        now = await fleet.finish(OWNER, await fleet.to_target_applied(OWNER, NOON))
        await fleet.run(now)  # the canary's hold end is recorded
        _observe(fleet.cluster, OWNER)
        await fleet.run(now + timedelta(seconds=5))  # the tenant's attempt toward IMAGE_B starts
        assert _annotations(fleet.cluster, TENANT)["exomem.io/hold"] == "upgrade"
        await _set_cell_image(cell_db, IMAGE_C)
        _observe(fleet.cluster, TENANT, statefulset_upgrade_target=None)

        await fleet.run(now + timedelta(seconds=10))

        statefulset = _statefulset(fleet.cluster, TENANT)
        assert "exomem.io/hold" not in statefulset["metadata"]["annotations"]
        assert (statefulset["spec"]["replicas"], _image(fleet.cluster, TENANT)) == (1, IMAGE_A)
    finally:
        await fleet.close()


async def test_an_attempt_without_a_pin_that_has_applied_is_pinned_to_the_applied_image(
        cell_db: CellDatabase) -> None:
    # Catches an attempt started by a cellctl before the pin switching to a newer
    # cell_image after its target ran.
    fleet = await Fleet.start(cell_db, TENANT)
    try:
        await _set_cell_image(cell_db, IMAGE_B)
        now = await fleet.finish(OWNER, await fleet.to_target_applied(OWNER, NOON))
        now = await fleet.to_target_applied(TENANT, now)
        await _set_cell_image(cell_db, IMAGE_C)
        _observe(fleet.cluster, TENANT, statefulset_upgrade_target=None, pod_ready=False, ready_pod_image=None)

        await fleet.run(now)

        assert _image(fleet.cluster, TENANT) == IMAGE_B
        assert _annotations(fleet.cluster, TENANT)["exomem.io/upgrade-target"] == IMAGE_B
    finally:
        await fleet.close()


async def test_a_pause_restarts_a_stopped_cell_whose_target_was_not_applied(cell_db: CellDatabase) -> None:
    # Catches a paused rollout that keeps applying targets. The canary's own failure
    # paused the rollout while the tenant's attempt had stopped it for its backup.
    fleet = await Fleet.start(cell_db, TENANT)
    try:
        await fleet.admin.execute("UPDATE exomem_cloud_rollout SET last_good_image = $1", IMAGE_B)
        await fleet.admin.execute("UPDATE exomem_cloud_cells SET desired_image = $1 WHERE cell_id = $2",
                                  IMAGE_B, TENANT)
        await fleet.run(NOON)  # the tenant's attempt starts (the canary runs its own target)
        assert _annotations(fleet.cluster, TENANT)["exomem.io/hold"] == "upgrade"
        _observe(fleet.cluster, TENANT)
        await fleet.run(NOON + timedelta(seconds=5))  # its backup Job runs
        _observe(fleet.cluster, TENANT, backup_job_running=True)
        await fleet.admin.execute(
            "UPDATE exomem_cloud_rollout SET paused = true, error_code = 'UPGRADE_READINESS_TIMEOUT', "
            "held_cell_id = $1", OWNER)

        await fleet.run(NOON + timedelta(seconds=10))

        statefulset = _statefulset(fleet.cluster, TENANT)
        assert "exomem.io/hold" not in statefulset["metadata"]["annotations"]
        assert (statefulset["spec"]["replicas"], _image(fleet.cluster, TENANT)) == (1, IMAGE_A)
        row = next(row for row in await db.select_all_rows(fleet.connection) if row.cell_id == TENANT)
        assert row.last_error_code is None
        rollout = await fleet.rollout()
        assert (rollout.held_cell_id, rollout.error_code) == (OWNER, "UPGRADE_READINESS_TIMEOUT")
    finally:
        await fleet.close()


async def test_an_applied_target_still_restores_on_a_miss_and_the_first_held_cell_stays(
    cell_db: CellDatabase,
) -> None:
    # Catches a cell left on an unready image, and a second failure that hides the first held cell.
    fleet = await Fleet.start(cell_db, TENANT)
    try:
        await _set_cell_image(cell_db, IMAGE_B)
        now = await fleet.to_target_applied(OWNER, NOON)
        await fleet.admin.execute(
            "UPDATE exomem_cloud_rollout SET paused = true, error_code = 'TARGET_REJECTED', held_cell_id = $1",
            TENANT)

        await fleet.run(now + ReconcileConfig().upgrade_ready_deadline + timedelta(minutes=1))

        assert _annotations(fleet.cluster, OWNER)["exomem.io/hold"] == "restore"
        rollout = await fleet.rollout()
        assert (rollout.paused, rollout.held_cell_id, rollout.error_code) == (True, TENANT, "TARGET_REJECTED")
    finally:
        await fleet.close()


# --- Task 1.8: upgrade windows ------------------------------------------------------


async def _windowed_fleet(cell_db: CellDatabase, *tenants: str) -> Fleet:
    """Windows on; the canary runs its target; every tenant's quiet hour is WINDOW_HOUR."""

    fleet = await Fleet.start(cell_db, *tenants, config=ReconcileConfig(upgrade_window=WINDOWS),
                              histories={cell_id: _history(WINDOW_HOUR, NOON) for cell_id in tenants})
    await fleet.admin.execute("UPDATE exomem_cloud_rollout SET last_good_image = $1", IMAGE_A)
    return fleet


async def test_a_tenant_starts_only_inside_its_window_after_its_idle_minutes(cell_db: CellDatabase) -> None:
    # Catches a window that the selector ignores.
    fleet = await _windowed_fleet(cell_db, TENANT)
    try:
        await fleet.admin.execute("UPDATE exomem_cloud_cells SET desired_image = $1 WHERE cell_id = $2",
                                  IMAGE_B, TENANT)
        await fleet.run(NOON)
        _observe(fleet.cluster, TENANT)
        assert _annotations(fleet.cluster, TENANT)["exomem.io/upgrade-window"] == f"{WINDOW_HOUR} history"
        assert "exomem.io/hold" not in _annotations(fleet.cluster, TENANT)

        fleet.cluster.readiness[TENANT] = BUSY
        await fleet.run(NOON.replace(hour=WINDOW_HOUR, minute=10))
        assert "exomem.io/hold" not in _annotations(fleet.cluster, TENANT)

        fleet.cluster.readiness[TENANT] = IDLE
        await fleet.run(NOON.replace(hour=WINDOW_HOUR, minute=11))
        assert _annotations(fleet.cluster, TENANT)["exomem.io/hold"] == "upgrade"
    finally:
        await fleet.close()


async def test_a_busy_tenant_starts_in_the_force_minutes(cell_db: CellDatabase) -> None:
    # Catches a busy cell that never rolls.
    fleet = await _windowed_fleet(cell_db, TENANT)
    try:
        await fleet.admin.execute("UPDATE exomem_cloud_cells SET desired_image = $1 WHERE cell_id = $2",
                                  IMAGE_B, TENANT)
        fleet.cluster.readiness[TENANT] = BUSY
        for seconds in (0, 5):
            await fleet.run(NOON + timedelta(seconds=seconds))
            _observe(fleet.cluster, TENANT)
        await fleet.run(NOON.replace(hour=WINDOW_HOUR, minute=40))
        assert "exomem.io/hold" not in _annotations(fleet.cluster, TENANT)

        await fleet.run(NOON.replace(hour=WINDOW_HOUR, minute=52))

        assert _annotations(fleet.cluster, TENANT)["exomem.io/hold"] == "upgrade"
    finally:
        await fleet.close()


async def test_the_canary_starts_at_once_outside_its_window(cell_db: CellDatabase) -> None:
    # Catches a windowed canary that holds the fleet for up to a day.
    fleet = await Fleet.start(cell_db, TENANT, config=ReconcileConfig(upgrade_window=WINDOWS))
    try:
        fleet.cluster.histories[OWNER] = _history(WINDOW_HOUR, NOON)
        await fleet.run(NOON)
        _observe(fleet.cluster, OWNER, TENANT)
        await _set_cell_image(cell_db, IMAGE_B)

        await fleet.run(NOON + timedelta(seconds=5))

        assert _annotations(fleet.cluster, OWNER)["exomem.io/hold"] == "upgrade"
    finally:
        await fleet.close()


async def test_windows_off_selects_candidates_as_before(cell_db: CellDatabase) -> None:
    # Catches a rollback switch that does nothing.
    off = ReconcileConfig(upgrade_window=WindowConfig(**{**WINDOWS.__dict__, "enabled": False}))
    fleet = await Fleet.start(cell_db, TENANT, config=off)
    try:
        await fleet.admin.execute("UPDATE exomem_cloud_rollout SET last_good_image = $1", IMAGE_B)
        await fleet.admin.execute("UPDATE exomem_cloud_cells SET desired_image = $1 WHERE cell_id = $2",
                                  IMAGE_B, TENANT)

        await fleet.run(NOON)

        assert _annotations(fleet.cluster, TENANT)["exomem.io/hold"] == "upgrade"
        assert fleet.cluster.history_reads == []
    finally:
        await fleet.close()


async def test_a_render_digest_change_waits_for_the_window(cell_db: CellDatabase) -> None:
    # Catches restarts that bypass windows: a new cell-token key version changes every digest.
    fleet = await _windowed_fleet(cell_db, TENANT)
    try:
        await fleet.run(NOON)
        _observe(fleet.cluster, OWNER, TENANT)
        before = {cell: _annotations(fleet.cluster, cell)["exomem.io/render-digest"] for cell in (OWNER, TENANT)}
        rotated = _secrets_config()
        rotated = type(rotated)(**{**rotated.__dict__, "cell_token_key_version": 2,
                                   "cell_token_key_previous": rotated.cell_token_key_current,
                                   "cell_token_key_previous_version": 1})

        async def run(now):
            await reconcile.reconcile_once(fleet.connection, fleet.cluster, FakeHetznerVolumeProvider(), rotated,
                                           _cluster_config(), config=fleet.config, now=now, memory=fleet.memory)

        for seconds in (5, 10, 15):
            await run(NOON + timedelta(seconds=seconds))
            _observe(fleet.cluster, OWNER, TENANT)
        assert {cell: _annotations(fleet.cluster, cell)["exomem.io/render-digest"] for cell in before} == before

        fleet.cluster.readiness[TENANT] = IDLE
        await run(NOON.replace(hour=WINDOW_HOUR, minute=20))
        assert _annotations(fleet.cluster, TENANT)["exomem.io/render-digest"] != before[TENANT]
    finally:
        await fleet.close()


async def test_writing_the_window_restarts_no_pod_and_a_routine_apply_keeps_it(cell_db: CellDatabase) -> None:
    # Catches a recompute that restarts every cell each day, and server-side apply
    # stripping the copied annotations each pass, which would recompute every pass.
    fleet = await _windowed_fleet(cell_db, TENANT)
    try:
        template = json.dumps(_statefulset(fleet.cluster, TENANT)["spec"]["template"], sort_keys=True)
        digest = _annotations(fleet.cluster, TENANT)["exomem.io/render-digest"]
        await fleet.run(NOON)
        patches = [event for event in fleet.cluster.events if event[0] == "patch_annotations"
                   and event[1] == namespace_name(TENANT)]
        assert patches and set(patches[-1][2]) >= {"exomem.io/upgrade-window", "exomem.io/upgrade-window-at"}
        assert json.dumps(_statefulset(fleet.cluster, TENANT)["spec"]["template"], sort_keys=True) == template
        assert _annotations(fleet.cluster, TENANT)["exomem.io/render-digest"] == digest
        _observe(fleet.cluster, TENANT)
        reads = len(fleet.cluster.history_reads)

        # A routine apply: the row's generation changes.
        await fleet.admin.execute("UPDATE exomem_cloud_cells SET generation = generation + 1 WHERE cell_id = $1",
                                  TENANT)
        await fleet.run(NOON + timedelta(seconds=5))
        _observe(fleet.cluster, TENANT)
        await fleet.run(NOON + timedelta(seconds=10))

        assert _annotations(fleet.cluster, TENANT)["exomem.io/upgrade-window"] == f"{WINDOW_HOUR} history"
        assert len(fleet.cluster.history_reads) == reads
    finally:
        await fleet.close()


async def test_an_unreadable_history_keeps_the_cached_window(cell_db: CellDatabase) -> None:
    # Catches a window that jumps to the default on one failed read.
    fleet = await _windowed_fleet(cell_db, TENANT)
    try:
        await fleet.run(NOON)
        _observe(fleet.cluster, TENANT)
        del fleet.cluster.histories[TENANT]

        await fleet.run(NOON + timedelta(days=1))

        annotations = _annotations(fleet.cluster, TENANT)
        assert annotations["exomem.io/upgrade-window"] == f"{WINDOW_HOUR} history"
        assert annotations["exomem.io/upgrade-window-at"] == NOON.date().isoformat()
    finally:
        await fleet.close()


async def test_one_cells_unreadable_history_fails_neither_the_pass_nor_another_cells_window(
        cell_db: CellDatabase) -> None:
    # Catches one cell's answer stopping every reconcile pass fleet-wide.
    far_future = json.dumps({"retention_hours": 672, "first_hour": "9999-12-31T23:00:00-01:00",
                             "requests": [1], "observed_seconds": [3600]}).encode()
    fleet = await Fleet.start(cell_db, TENANT, TENANT_2, config=ReconcileConfig(upgrade_window=WINDOWS),
                              histories={TENANT: _history(WINDOW_HOUR, NOON), TENANT_2: far_future})
    try:
        await fleet.run(NOON)

        assert _annotations(fleet.cluster, TENANT)["exomem.io/upgrade-window"] == f"{WINDOW_HOUR} history"
        assert _annotations(fleet.cluster, TENANT_2)["exomem.io/upgrade-window"].endswith(" default")
    finally:
        await fleet.close()


async def test_releases_on_three_days_upgrade_every_cell_that_shares_a_window(cell_db: CellDatabase) -> None:
    # Catches a due order that starves the same cells each day. Each window has room
    # for one attempt; three tenants share it, and a new release lands every day.
    fleet = await _windowed_fleet(cell_db, TENANT, TENANT_2, TENANT_3)
    upgraded = []
    try:
        for day, image in enumerate((IMAGE_B, IMAGE_C, IMAGE_D)):
            await fleet.admin.execute("UPDATE exomem_cloud_cells SET desired_image = $1 WHERE cell_id = $2",
                                      image, OWNER)
            await fleet.admin.execute("UPDATE exomem_cloud_rollout SET last_good_image = $1", image)
            await _set_cell_image(cell_db, image)
            await fleet.admin.execute("UPDATE exomem_cloud_cells SET desired_image = NULL WHERE cell_id <> $1",
                                      OWNER)
            # The canary takes the image at once, outside the window.
            fleet.cluster.observations[OWNER] = _observed_from_applied(fleet.cluster, OWNER, statefulset_image=image,
                                                                       ready_pod_image=image)
            morning = NOON + timedelta(days=day)
            await fleet.run(morning)
            _observe(fleet.cluster, TENANT, TENANT_2, TENANT_3)
            fleet.cluster.observations[OWNER] = _observed_from_applied(fleet.cluster, OWNER, statefulset_image=image,
                                                                       ready_pod_image=image)
            for cell_id in (TENANT, TENANT_2, TENANT_3):
                fleet.cluster.readiness[cell_id] = IDLE
            window = morning.replace(hour=WINDOW_HOUR, minute=10)
            await fleet.run(window)
            started = [cell for cell in (TENANT, TENANT_2, TENANT_3)
                       if _annotations(fleet.cluster, cell).get("exomem.io/hold") == "upgrade"]
            assert len(started) == 1
            upgraded.append(started[0])
            now = await fleet.to_target_applied(started[0], window + timedelta(seconds=5))
            await fleet.finish(started[0], now)
            # The window closes before another attempt can start.

        assert sorted(upgraded) == [TENANT, TENANT_2, TENANT_3]
    finally:
        await fleet.close()


# --- Task 1.10: the forward-only rule -----------------------------------------------


async def _history_table(admin, rows: dict[str, str]) -> None:
    """A stand-in for Substrate's image history (automate-cloud-releases D1, Substrate
    migration 0085), with cellctl's read grant. It moves into the pinned contract
    fixture once that migration merges."""

    await admin.execute("CREATE TABLE IF NOT EXISTS exomem_cloud_image_history "
                        "(image text PRIMARY KEY, release_tag text, recorded_at timestamptz NOT NULL DEFAULT now())")
    await admin.execute("GRANT SELECT ON exomem_cloud_image_history TO exomem_cellctl")
    for image, release in rows.items():
        await admin.execute("INSERT INTO exomem_cloud_image_history (image, release_tag) VALUES ($1, $2)",
                            image, release)


async def _barrier_fleet(cell_db: CellDatabase, history: dict[str, str]) -> Fleet:
    fleet = await Fleet.start(cell_db, config=ReconcileConfig(upgrade_barriers=ONE_WAY_CELL))
    await _history_table(fleet.admin, history)
    return fleet


async def test_a_crossing_attempt_that_misses_ready_holds_for_a_person(cell_db: CellDatabase) -> None:
    # A cell on a pre-barrier image whose target is two releases past a one-way `cell`
    # entry misses Ready. Catches a restore onto external state the previous image
    # cannot read, a hold that leaves a person no state to restore, a hold that restarts
    # the failed target every deadline, and a hold that no action can end.
    fleet = await _barrier_fleet(cell_db, {IMAGE_A: "v1.4.0", IMAGE_B: "v1.6.0", IMAGE_C: "v1.7.0"})
    try:
        await _set_cell_image(cell_db, IMAGE_B)
        now = await fleet.to_target_applied(OWNER, NOON)
        miss = now + ReconcileConfig().upgrade_ready_deadline + timedelta(minutes=1)

        await fleet.run(miss)

        annotations = _annotations(fleet.cluster, OWNER)
        assert (annotations["exomem.io/hold"], annotations["exomem.io/upgrade-phase"]) == ("upgrade",
                                                                                          "forward-only-held")
        assert annotations["exomem.io/pre-upgrade-snapshot"] == _ATTEMPT_SNAPSHOT
        assert _statefulset(fleet.cluster, OWNER)["spec"]["replicas"] == 0
        assert not [job for job in fleet.cluster.jobs if "restore" in job["metadata"]["name"]]
        rollout = await fleet.rollout()
        assert (rollout.paused, rollout.error_code, rollout.held_cell_id) == (True, "UPGRADE_FORWARD_ONLY", OWNER)

        for minutes in (5, 60, 120):
            _observe(fleet.cluster, OWNER)
            await fleet.run(miss + timedelta(minutes=minutes))
            assert _statefulset(fleet.cluster, OWNER)["spec"]["replicas"] == 0
            assert _annotations(fleet.cluster, OWNER)["exomem.io/pre-upgrade-snapshot"] == _ATTEMPT_SNAPSHOT
        assert not [job for job in fleet.cluster.jobs if "restore" in job["metadata"]["name"]]

        # A person fixes forward and clears the pause: one new attempt starts toward the current target.
        await _set_cell_image(cell_db, IMAGE_C)
        await fleet.admin.execute("UPDATE exomem_cloud_rollout SET paused = false, error_code = NULL")
        _observe(fleet.cluster, OWNER)
        await fleet.run(miss + timedelta(hours=3))
        annotations = _annotations(fleet.cluster, OWNER)
        assert annotations["exomem.io/upgrade-target"] == IMAGE_C
        assert annotations["exomem.io/previous-image"] == IMAGE_A
        assert "exomem.io/upgrade-phase" not in annotations
    finally:
        await fleet.close()


async def test_a_held_tenant_resumes_toward_its_canaried_target_after_a_person_clears_the_pause(
        cell_db: CellDatabase) -> None:
    # Catches a tenant that runs a cell_image before the canary does.
    fleet = await Fleet.start(cell_db, TENANT, config=ReconcileConfig(upgrade_barriers=ONE_WAY_CELL))
    try:
        await _history_table(fleet.admin, {IMAGE_A: "v1.4.0", IMAGE_B: "v1.6.0", IMAGE_C: "v1.7.0"})
        await _set_cell_image(cell_db, IMAGE_B)
        now = await fleet.finish(OWNER, await fleet.to_target_applied(OWNER, NOON))
        now = await fleet.to_target_applied(TENANT, now)
        miss = now + ReconcileConfig().upgrade_ready_deadline + timedelta(minutes=1)
        await fleet.run(miss)
        assert _annotations(fleet.cluster, TENANT)["exomem.io/upgrade-phase"] == "forward-only-held"

        await _set_cell_image(cell_db, IMAGE_C)
        await fleet.admin.execute("UPDATE exomem_cloud_rollout SET paused = false, error_code = NULL")
        _observe(fleet.cluster, OWNER, TENANT)
        await fleet.run(miss + timedelta(hours=1))

        annotations = _annotations(fleet.cluster, TENANT)
        assert annotations["exomem.io/upgrade-target"] == IMAGE_B
        assert "exomem.io/upgrade-phase" not in annotations
    finally:
        await fleet.close()


async def test_a_previous_image_with_no_history_counts_as_before_the_barrier(cell_db: CellDatabase) -> None:
    # Catches an unknown image that restores across a barrier.
    fleet = await _barrier_fleet(cell_db, {IMAGE_B: "v1.6.0"})
    try:
        await _set_cell_image(cell_db, IMAGE_B)
        now = await fleet.to_target_applied(OWNER, NOON)

        await fleet.run(now + ReconcileConfig().upgrade_ready_deadline + timedelta(minutes=1))

        assert _annotations(fleet.cluster, OWNER)["exomem.io/upgrade-phase"] == "forward-only-held"
    finally:
        await fleet.close()


async def test_an_unmapped_desired_image_past_the_barrier_is_a_crossing_attempt(cell_db: CellDatabase) -> None:
    # Catches an unmapped target that restores across a barrier.
    fleet = await _barrier_fleet(cell_db, {IMAGE_A: "v1.4.0"})
    try:
        await fleet.admin.execute("UPDATE exomem_cloud_cells SET desired_image = $1 WHERE cell_id = $2",
                                  IMAGE_D, OWNER)
        now = await fleet.to_target_applied(OWNER, NOON)

        await fleet.run(now + ReconcileConfig().upgrade_ready_deadline + timedelta(minutes=1))

        assert _annotations(fleet.cluster, OWNER)["exomem.io/upgrade-phase"] == "forward-only-held"
    finally:
        await fleet.close()


async def test_an_attempt_past_every_barrier_restores_as_before(cell_db: CellDatabase) -> None:
    fleet = await _barrier_fleet(cell_db, {IMAGE_A: "v1.5.0", IMAGE_B: "v1.6.0"})
    try:
        await _set_cell_image(cell_db, IMAGE_B)
        now = await fleet.to_target_applied(OWNER, NOON)

        await fleet.run(now + ReconcileConfig().upgrade_ready_deadline + timedelta(minutes=1))

        assert _annotations(fleet.cluster, OWNER)["exomem.io/hold"] == "restore"
    finally:
        await fleet.close()


async def test_a_withdrawal_across_a_one_way_barrier_is_refused(cell_db: CellDatabase) -> None:
    # Catches a withdrawal that moves cells back across a one-way change.
    fleet = await _barrier_fleet(cell_db, {IMAGE_A: "v1.5.0", IMAGE_B: "v1.4.0"})
    try:
        await _set_cell_image(cell_db, IMAGE_B)

        await fleet.run(NOON)

        assert "exomem.io/hold" not in _annotations(fleet.cluster, OWNER)
        assert _statefulset(fleet.cluster, OWNER)["spec"]["replicas"] == 1
        assert _image(fleet.cluster, OWNER) == IMAGE_A
        rollout = await fleet.rollout()
        assert (rollout.paused, rollout.error_code, rollout.held_cell_id) == (True, "UPGRADE_BACKWARD_CROSSING",
                                                                              OWNER)
    finally:
        await fleet.close()


# --- Task 1.11: rollout alerts ------------------------------------------------------

THRESHOLDS = alerts.AlertThresholds(
    unavailable_after=timedelta(days=30), disk_free_floor_bytes=1, recall_lag_after=timedelta(days=30),
    rollout_held_after=timedelta(minutes=60), rollout_stalled_after=timedelta(days=3),
    canary_wait_after=timedelta(minutes=60),
)
ALERTING = ClusterConfig(
    object_storage_bucket="exomem-cloud-backups", cell_data_backup_bucket="cell-backups-locked",
    cell_data_staging_bucket="cell-staging", cell_data_endpoint="https://s3.cell-data.example",
    alert_delivery_secret=("exomem-platform", "exomem-hosted-alert-delivery", "url"), alert_thresholds=THRESHOLDS,
)


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    sent: list[tuple[str, bool]] = []
    monkeypatch.setattr(alerts, "deliver", lambda url, *, alert, active, observed_at: sent.append((alert, active)))
    return sent


def _states(delivered, alert: str) -> list[bool]:
    return [active for name, active in delivered if name == alert]


async def test_the_paused_alert_fires_on_a_forward_only_pause_and_resolves_when_a_person_clears_it(
    cell_db: CellDatabase, delivered
) -> None:
    # Catches a forward-only hold that nobody hears about, and an alert that never resolves.
    fleet = await Fleet.start(cell_db)
    try:
        await fleet.run(NOON, cluster_config=ALERTING)
        await fleet.admin.execute(
            "UPDATE exomem_cloud_rollout SET paused = true, error_code = 'UPGRADE_FORWARD_ONLY', held_cell_id = $1",
            OWNER)
        await fleet.run(NOON + timedelta(minutes=1), cluster_config=ALERTING)
        await fleet.admin.execute("UPDATE exomem_cloud_rollout SET paused = false, error_code = NULL")
        await fleet.run(NOON + timedelta(minutes=2), cluster_config=ALERTING)

        assert _states(delivered, alerts.ROLLOUT_PAUSED_ALERT) == [False, True, False]
    finally:
        await fleet.close()


async def test_the_held_and_canary_alerts_fire_on_a_long_attempt_and_resolve_when_it_ends(
    cell_db: CellDatabase, delivered
) -> None:
    fleet = await Fleet.start(cell_db)
    try:
        await _set_cell_image(cell_db, IMAGE_B)
        now = await fleet.to_target_applied(OWNER, NOON)
        await fleet.run(now + timedelta(minutes=61), cluster_config=ALERTING)
        assert _states(delivered, alerts.ROLLOUT_HELD_ALERT) == [True]
        assert _states(delivered, alerts.ROLLOUT_CANARY_WAITING_ALERT) == [True]

        _observe(fleet.cluster, OWNER)
        await fleet.run(now + timedelta(minutes=62), cluster_config=ALERTING)
        _observe(fleet.cluster, OWNER)
        await fleet.run(now + timedelta(minutes=63), cluster_config=ALERTING)

        assert _states(delivered, alerts.ROLLOUT_HELD_ALERT) == [True, False]
        assert _states(delivered, alerts.ROLLOUT_CANARY_WAITING_ALERT) == [True, False]
    finally:
        await fleet.close()


async def test_a_newer_target_keeps_the_stall_clock_and_the_stalled_alert_fires(
    cell_db: CellDatabase, delivered
) -> None:
    # Catches a stall that hides because releases arrive every 8 hours.
    fleet = await _windowed_fleet(cell_db, TENANT)
    try:
        await fleet.admin.execute("UPDATE exomem_cloud_cells SET desired_image = NULL WHERE cell_id = $1", OWNER)
        fleet.cluster.readiness[TENANT] = BUSY
        for step, image in enumerate((IMAGE_B, IMAGE_C, IMAGE_D) * 4):
            await _set_cell_image(cell_db, image)
            fleet.cluster.observations[OWNER] = _observed_from_applied(fleet.cluster, OWNER, statefulset_image=image,
                                                                       ready_pod_image=image)
            # Never in the tenant's window: it stays behind.
            await fleet.run(NOON + timedelta(hours=8 * step), cluster_config=ALERTING)
            _observe(fleet.cluster, TENANT)

        since = _annotations(fleet.cluster, TENANT)["exomem.io/candidate-since"]
        assert datetime.fromisoformat(since) == NOON
        assert _states(delivered, alerts.ROLLOUT_STALLED_ALERT)[-1] is True

        # The tenant runs its target: the clock ends and the alert resolves.
        fleet.cluster.observations[TENANT] = _observed_from_applied(fleet.cluster, TENANT, statefulset_image=image,
                                                                    ready_pod_image=image)
        await fleet.run(NOON + timedelta(hours=100), cluster_config=ALERTING)
        assert "exomem.io/candidate-since" not in _annotations(fleet.cluster, TENANT)
        assert _states(delivered, alerts.ROLLOUT_STALLED_ALERT)[-1] is False
    finally:
        await fleet.close()


async def test_a_pass_that_cannot_observe_a_cell_sends_no_rollout_transition(cell_db: CellDatabase, delivered) -> None:
    # Catches a blind pass that resolves a firing alert.
    fleet = await Fleet.start(cell_db, TENANT)
    try:
        await fleet.admin.execute("UPDATE exomem_cloud_rollout SET paused = true, error_code = 'X'")
        await fleet.run(NOON, cluster_config=ALERTING)
        assert _states(delivered, alerts.ROLLOUT_PAUSED_ALERT) == [True]

        await fleet.admin.execute("UPDATE exomem_cloud_rollout SET paused = false, error_code = NULL")

        class Blind(type(fleet.cluster)):
            def observe(self, cell_id, namespace):
                if cell_id == TENANT:
                    raise OSError("the cluster API timed out")
                return super().observe(cell_id, namespace)

        fleet.cluster.__class__ = Blind
        await fleet.run(NOON + timedelta(minutes=1), cluster_config=ALERTING)

        assert _states(delivered, alerts.ROLLOUT_PAUSED_ALERT) == [True]
    finally:
        await fleet.close()


async def test_a_backward_move_needs_known_releases(cell_db: CellDatabase) -> None:
    # The image history read failing (the table missing) starts no attempt at all.
    fleet = await Fleet.start(cell_db, config=ReconcileConfig(upgrade_barriers=ONE_WAY_CELL))
    try:
        await _set_cell_image(cell_db, IMAGE_B)

        await fleet.run(NOON)

        assert "exomem.io/hold" not in _annotations(fleet.cluster, OWNER)
        assert not (await fleet.rollout()).paused
    finally:
        await fleet.close()
