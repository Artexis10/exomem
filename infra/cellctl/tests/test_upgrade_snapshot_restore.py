"""automate-cloud-releases D6: a local cell's upgrade restore point is a CSI
VolumeSnapshot taken after the stop, with one clone backing the upgrade
backup and the forced-failure restore. Hetzner cells keep the stopped restic
path, without its prune.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from cellctl import reconcile
from cellctl.decide import UPGRADE_FORWARD_ONLY, decide
from cellctl.manifests import (
    BACKUP_JOB_NAME,
    RETENTION_ARGS,
    SNAPSHOT_NAME,
    snapshot_backup_name,
)
from cellctl.reconcile import ClusterConfig, _backup_source
from cellctl.state import CellRow, ClusterObservation, RolloutRow
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

from .conftest import CellDatabase
from .test_local_storage import LOCAL, MIGRATING, _bound, _row
from .test_reconcile import FakeClusterGateway, _job_manifest, _secrets_config, _seed_cell

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
STARTED = datetime(2026, 1, 1, 11, 58, tzinfo=UTC)
IMAGE_A = "registry.example/cell@sha256:" + "a" * 64
IMAGE_B = "registry.example/cell@sha256:" + "b" * 64
RESTIC = "e" * 64
CELL_ID = "aaaaaaaaaaaaaaaa"
VOL_NAME = snapshot_backup_name(SNAPSHOT_NAME, CELL_ID, STARTED.isoformat())
CLONE_NAME = "cell-clone" + VOL_NAME[len("cell-snapshot"):]


def _upgrade(**overrides) -> ClusterObservation:
    return _bound(
        LOCAL.class_name, statefulset_row_generation=1, pv_node="agent-1",
        **{"statefulset_hold_started_at": STARTED, "statefulset_hold_kind": "upgrade", "statefulset_upgrade_target": IMAGE_B,
           "statefulset_previous_image": IMAGE_A, **overrides},
    )


def _step(observation: ClusterObservation, row: CellRow | None = None, now: datetime = NOW,
          rollout: RolloutRow | None = None):
    return decide(row or _row(hold_kind="upgrade", hold_started_at=STARTED),
                  rollout or RolloutRow(), observation,
                  now=now, cell_image=IMAGE_B, start_upgrade=False, storage=MIGRATING)


def test_the_snapshot_is_taken_once_the_pod_object_is_gone() -> None:
    # Still draining: no VolumeSnapshot yet, the hold keeps replicas 0.
    scaling = _step(_upgrade(statefulset_replicas=0, pod_exists=True))
    assert (scaling.replicas, scaling.create_snapshot, scaling.pre_upgrade_volume_snapshot) == (0, False, None)

    stopped = _step(_upgrade(statefulset_replicas=0, pod_exists=False))
    assert (stopped.replicas, stopped.create_snapshot, stopped.pre_upgrade_volume_snapshot) == (
        0, True, VOL_NAME)


def test_the_annotated_snapshot_is_created_and_the_target_applies_once_it_is_ready() -> None:
    waiting = _step(_upgrade(statefulset_replicas=0, pod_exists=False,
                             statefulset_pre_upgrade_volume_snapshot=VOL_NAME, snapshot_exists=True))
    assert (waiting.replicas, waiting.create_snapshot) == (0, False)  # not ready yet

    ready = _step(_upgrade(statefulset_replicas=0, pod_exists=False,
                           statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                           snapshot_exists=True, snapshot_ready=True))
    # The target applies at once; the backup Job follows on the clone.
    assert (ready.image, ready.replicas, ready.target_applied_at) == (IMAGE_B, 1, NOW)
    assert ready.create_clone is True


def test_the_backup_job_runs_once_on_the_clone_and_the_hold_keeps_its_clone() -> None:
    unbound = _step(_upgrade(statefulset_replicas=1, statefulset_image=IMAGE_B,
                             statefulset_target_applied_at=NOW,
                             statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                             snapshot_exists=True, snapshot_ready=True, clone_exists=True))
    assert (unbound.create_clone, unbound.run_backup_job) == (False, False)

    bound = _step(_upgrade(statefulset_replicas=1, statefulset_image=IMAGE_B,
                           statefulset_target_applied_at=NOW,
                           statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                           snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True))
    assert (bound.create_clone, bound.run_backup_job, bound.backup_prune) == (False, True, False)

    # Ready before the Job ends: the hold keeps replicas 1 and its clone.
    done = _step(_upgrade(statefulset_replicas=1, statefulset_image=IMAGE_B,
                          statefulset_target_applied_at=NOW, statefulset_pre_upgrade_snapshot=RESTIC,
                          statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                          snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True,
                          backup_job_succeeded=True, backup_job_snapshot_id=RESTIC))
    assert (done.replicas, done.delete_snapshot_backup, done.hold_kind) == (1, True, "upgrade")

    # Gone: the hold exits and the annotations are cleared.
    gone = _step(_upgrade(statefulset_replicas=1, statefulset_image=IMAGE_B,
                          statefulset_target_applied_at=NOW, statefulset_pre_upgrade_snapshot=RESTIC,
                          statefulset_pre_upgrade_volume_snapshot=VOL_NAME),
                 _row(hold_kind="upgrade", hold_started_at=STARTED, last_backup_at=NOW,
                      last_backup_snapshot=RESTIC))
    assert (gone.hold_kind, gone.row_updates["hold_kind"], gone.replicas) == (None, None, 1)


def test_the_attempt_restic_snapshot_is_recorded_before_the_upgrade_finishes() -> None:
    decision = _step(_upgrade(statefulset_replicas=1, statefulset_image=IMAGE_B,
                              statefulset_target_applied_at=NOW,
                              statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                              snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True,
                              backup_job_succeeded=True, backup_job_snapshot_id=RESTIC))
    assert decision.pre_upgrade_snapshot == RESTIC
    assert decision.row_updates["last_backup_snapshot"] == RESTIC


def test_a_ready_miss_restores_from_the_clone_before_the_restic_snapshot() -> None:
    late = STARTED + timedelta(minutes=45)  # past the readiness deadline
    missed = _step(_upgrade(statefulset_replicas=1, statefulset_image=IMAGE_B, pod_ready=False,
                            statefulset_target_applied_at=STARTED + timedelta(minutes=1),
                            statefulset_pre_upgrade_snapshot=RESTIC,
                            statefulset_pre_upgrade_volume_snapshot=VOL_NAME, ready_pod_image=None),
                   _row(hold_kind="upgrade", hold_started_at=STARTED), now=late)
    assert (missed.hold_kind, missed.replicas) == ("restore", 0)
    assert missed.rollout_updates["error_code"] == "UPGRADE_READINESS_TIMEOUT"
    # Both restore points survive onto the restore hold's annotations.
    assert missed.pre_upgrade_volume_snapshot == VOL_NAME

    restoring = _step(_upgrade(statefulset_hold_kind="restore", statefulset_image=IMAGE_B,
                               statefulset_replicas=0, statefulset_pre_upgrade_snapshot=RESTIC,
                               statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                               snapshot_exists=True, clone_exists=True, clone_bound=True),
                      _row(hold_kind="restore", hold_started_at=STARTED))
    assert (restoring.run_clone_restore_job, restoring.run_restore_job_snapshot) == (True, None)

    # The clone gone mid-restore: the Job runs against the restic snapshot.
    restic = _step(_upgrade(statefulset_hold_kind="restore", statefulset_image=IMAGE_B,
                            statefulset_replicas=0, statefulset_pre_upgrade_snapshot=RESTIC,
                            statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                            snapshot_exists=True, clone_exists=False),
                   _row(hold_kind="restore", hold_started_at=STARTED))
    assert (restic.run_clone_restore_job, restic.run_restore_job_snapshot) == (False, RESTIC)


def test_a_ready_miss_with_neither_restore_point_fails_without_an_image_start() -> None:
    late = STARTED + timedelta(minutes=45)
    decision = _step(_upgrade(statefulset_replicas=1, statefulset_image=IMAGE_B, pod_ready=False,
                              statefulset_target_applied_at=STARTED + timedelta(minutes=1),
                              ready_pod_image=None),
                     _row(hold_kind="upgrade", hold_started_at=STARTED), now=late)
    assert decision.row_updates["last_error_code"] == "RESTORE_FAILED"
    assert (decision.replicas, decision.rollout_updates["paused"]) == (0, True)


def test_a_forward_only_hold_keeps_and_resume_releases_the_attempts_snapshot_and_clone() -> None:
    crossed = _step(_upgrade(statefulset_replicas=1, statefulset_image=IMAGE_B,
                             statefulset_target_applied_at=NOW - timedelta(minutes=20),
                             statefulset_upgrade_phase="forward-only-held",
                             statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                             snapshot_exists=True, clone_exists=True),
                    _row(hold_kind="upgrade", hold_started_at=STARTED,
                         last_error_code=UPGRADE_FORWARD_ONLY),
                    rollout=RolloutRow(paused=True))
    assert (crossed.hold_kind, crossed.replicas) == ("upgrade", 0)
    assert crossed.pre_upgrade_volume_snapshot == VOL_NAME  # kept, not deleted

    resuming = _step(_upgrade(statefulset_replicas=0,
                              statefulset_upgrade_phase="forward-only-held",
                              statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                              statefulset_upgrade_target="registry.example/cell@sha256:" + "c" * 64),
                     _row(hold_kind="upgrade", hold_started_at=STARTED,
                          last_error_code=UPGRADE_FORWARD_ONLY))
    assert resuming.delete_snapshot_backup is True  # the attempt's pair goes first


def test_a_pause_mid_attempt_releases_the_clone_and_snapshot() -> None:
    decision = _step(_upgrade(statefulset_replicas=0, pod_exists=False,
                              statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                              snapshot_exists=True, clone_exists=True),
                     _row(generation=2, hold_kind="upgrade", hold_started_at=STARTED),
                     rollout=RolloutRow(paused=True))
    assert decision.delete_snapshot_backup is True
    assert decision.hold_kind == "upgrade"  # held until the pair is gone
    # Once both are observed gone the attempt ends without a failure record.
    done = _step(_upgrade(statefulset_replicas=0, pod_exists=False,
                          statefulset_pre_upgrade_volume_snapshot=VOL_NAME),
                 _row(generation=2, hold_kind="upgrade", hold_started_at=STARTED),
                 rollout=RolloutRow(paused=True))
    assert done.row_updates["hold_kind"] is None
    assert done.row_updates["last_error_code"] is None


# --- 3.2: no forget --prune on the stopped Hetzner backup --------------------


def test_the_stopped_upgrade_backup_skips_prune_only_on_a_hetzner_volume() -> None:
    from cellctl.decide import Decision
    from cellctl.manifests import CellManifestSpec

    hetzner = CellManifestSpec(cell_id="aaaaaaaaaaaaaaaa", image=IMAGE_A, replicas=1, read_only=False,
                               storage_gib=10, storage_class="exomem-cloud-volume")
    upgrade = Decision(hold_kind="upgrade")

    assert _backup_source(upgrade, hetzner)["retention"] is None
    assert _backup_source(Decision(hold_kind=None), hetzner)["retention"] == RETENTION_ARGS

    local = CellManifestSpec(cell_id="aaaaaaaaaaaaaaaa", image=IMAGE_A, replicas=1, read_only=False,
                             storage_gib=10, storage_class=LOCAL.class_name, local_volume=True,
                             pre_upgrade_volume_snapshot=VOL_NAME)
    source = _backup_source(upgrade, local)
    assert (source["retention"], source["claim_name"]) == (None, CLONE_NAME)


# --- reconcile-level: the clone-backed Jobs reach the cluster ---------------

async def test_the_clone_and_the_backup_job_apply_while_the_upgrade_waits(cell_db: CellDatabase) -> None:
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    cluster = FakeClusterGateway()
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    config = ClusterConfig(object_storage_bucket="exomem-cloud-backups", storage=MIGRATING)
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute(
            "UPDATE exomem_cloud_cells SET hold_kind = 'upgrade', hold_started_at = $1 WHERE cell_id = $2",
            STARTED, cell_id)
    finally:
        await owner.close()
    try:
        # Mid-attempt: snapshot ready, the target already applied; the pass
        # renders the clone and the backup Job that reads it.
        cluster.observations[cell_id] = _upgrade(
            statefulset_replicas=1, statefulset_image=IMAGE_B, statefulset_target_applied_at=NOW,
            statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
            snapshot_exists=True, snapshot_ready=True)
        await reconcile.reconcile_once(
            connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(), config, now=NOW)
        clone = _job_manifest(cluster, CLONE_NAME)

        cluster.observations[cell_id] = _upgrade(
            statefulset_replicas=1, statefulset_image=IMAGE_B, statefulset_target_applied_at=NOW,
            statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
            snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True)
        await reconcile.reconcile_once(
            connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(), config, now=NOW + timedelta(seconds=5))
        assert clone["spec"]["dataSourceRef"]["name"] == VOL_NAME
        backup = next(m for m in cluster.jobs
                      if m["kind"] == "Job" and m["metadata"]["name"].startswith(BACKUP_JOB_NAME))
        volumes = {v["name"]: v for v in backup["spec"]["template"]["spec"]["volumes"]}
        assert volumes["data"]["persistentVolumeClaim"]["claimName"] == CLONE_NAME
        mount = next(m for m in backup["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
                     if m["name"] == "data")
        assert mount["readOnly"] is True
    finally:
        await connection.close()


async def test_the_clone_restore_job_mounts_the_clone_read_only_and_runs_as_the_cell_user(
    cell_db: CellDatabase,
) -> None:
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    cluster = FakeClusterGateway()
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    config = ClusterConfig(object_storage_bucket="exomem-cloud-backups", storage=MIGRATING)
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute(
            "UPDATE exomem_cloud_cells SET hold_kind = 'restore', hold_started_at = $1 WHERE cell_id = $2",
            STARTED, cell_id)
    finally:
        await owner.close()
    try:
        cluster.observations[cell_id] = _upgrade(
            statefulset_hold_kind="restore", statefulset_replicas=0,
            statefulset_pre_upgrade_snapshot=RESTIC,
            statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
            snapshot_exists=True, clone_exists=True, clone_bound=True)
        await reconcile.reconcile_once(
            connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(), config, now=NOW)

        job = next(m for m in cluster.jobs
                   if m["kind"] == "Job" and m["metadata"]["name"].startswith("cell-restore"))
        pod = job["spec"]["template"]["spec"]
        assert pod["containers"][0]["securityContext"]["runAsUser"] == 10001
        volumes = {v["name"]: v for v in pod["volumes"]}
        assert volumes["snapshot"]["persistentVolumeClaim"]["claimName"] == CLONE_NAME
        assert volumes["snapshot"]["persistentVolumeClaim"]["readOnly"] is True
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "snapshot")
        assert mount["readOnly"] is True
        command = pod["containers"][0]["command"][-1]
        for name in ("vault", "host"):
            assert command.index(f"test -d /snapshot/{name}") < command.index("-delete")
    finally:
        await connection.close()


def test_the_hourly_backup_never_starts_while_an_upgrade_hold_owns_the_clone() -> None:
    from cellctl.decide import ReconcileConfig
    from cellctl.reconcile import _select_backup_candidates

    cell_id = CELL_ID
    rows = [_row(cell_id=cell_id, hold_kind="upgrade", hold_started_at=STARTED,
                 last_backup_at=NOW - timedelta(hours=2))]
    observations = {cell_id: _upgrade(
        statefulset_replicas=1, statefulset_image=IMAGE_B, statefulset_target_applied_at=NOW,
        statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
        snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True)}

    assert _select_backup_candidates(rows, observations, NOW, ReconcileConfig(), storage=MIGRATING) == set()


def test_a_finished_clone_restore_restarts_the_previous_image_and_releases_the_pair() -> None:
    hold_row = _row(hold_kind="restore", hold_started_at=STARTED)
    base = dict(
        statefulset_hold_kind="restore", statefulset_image=IMAGE_B, statefulset_replicas=0,
        statefulset_pre_upgrade_snapshot=RESTIC, statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
        snapshot_exists=True, clone_exists=True, clone_bound=True)

    # The Job reported success: the pass records which restore point ran,
    # so a Job lost to its TTL is never run again.
    recorded = _step(_upgrade(**base, restore_job_succeeded=True), hold_row)
    assert recorded.restored_snapshot == VOL_NAME

    # Restart resume: the marker alone says the restore ran; the cell starts
    # on its previous image and the pair goes once the cell is provisioning.
    restarting = _step(_upgrade(**base, statefulset_restored_snapshot=VOL_NAME), hold_row)
    assert (restarting.replicas, restarting.image, restarting.restored_snapshot) == (1, IMAGE_A, VOL_NAME)

    running = _step(_upgrade(**{**base, "statefulset_image": IMAGE_A, "statefulset_replicas": 1,
                                "pod_ready": True, "ready_pod_image": IMAGE_A},
                             statefulset_restored_snapshot=VOL_NAME), hold_row)
    assert running.delete_snapshot_backup is True

    gone = _step(_upgrade(**{**base, "snapshot_exists": False, "clone_exists": False,
                             "statefulset_image": IMAGE_A, "statefulset_replicas": 1,
                             "pod_ready": True, "ready_pod_image": IMAGE_A},
                          statefulset_restored_snapshot=VOL_NAME), hold_row)
    assert gone.row_updates["hold_kind"] is None


def test_a_restart_resumes_each_step_from_the_annotations() -> None:
    """The 3.1 restart bullet: a restarted cellctl reads the StatefulSet
    annotations and the cluster's own objects only -- nothing is held in
    memory. Each local-path step below is one observation taken cold, and the
    decision is the same step the earlier tests reach."""

    # Stop draining: still no snapshot while the pod exists, then create it.
    assert _step(_upgrade(statefulset_replicas=0, pod_exists=True)).create_snapshot is False
    assert _step(_upgrade(statefulset_replicas=0, pod_exists=False)).create_snapshot is True

    # Annotated, snapshot exists but not ready: wait; ready: apply the target.
    waiting = _step(_upgrade(statefulset_replicas=0, pod_exists=False,
                             statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                             snapshot_exists=True))
    assert (waiting.replicas, waiting.image) == (0, IMAGE_A)
    applied = _step(_upgrade(statefulset_replicas=0, pod_exists=False,
                             statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                             snapshot_exists=True, snapshot_ready=True))
    assert (applied.image, applied.create_clone) == (IMAGE_B, True)

    # Clone unbound: hold. Bound: the backup Job runs on it.
    unbound = _step(_upgrade(statefulset_replicas=1, statefulset_image=IMAGE_B,
                             statefulset_target_applied_at=NOW,
                             statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                             snapshot_exists=True, snapshot_ready=True, clone_exists=True))
    assert (unbound.run_backup_job, unbound.create_clone) == (False, False)
    bound = _step(_upgrade(statefulset_replicas=1, statefulset_image=IMAGE_B,
                           statefulset_target_applied_at=NOW,
                           statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                           snapshot_exists=True, snapshot_ready=True,
                           clone_exists=True, clone_bound=True))
    assert bound.run_backup_job is True

    # The backup Job ended: the pair is deleted and the hold ends.
    done = _step(_upgrade(statefulset_replicas=1, statefulset_image=IMAGE_B,
                          statefulset_target_applied_at=NOW,
                          statefulset_pre_upgrade_snapshot=RESTIC,
                          statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                          backup_job_succeeded=True, backup_job_snapshot_id=RESTIC),
                 _row(hold_kind="upgrade", hold_started_at=STARTED, last_backup_at=NOW,
                      last_backup_snapshot=RESTIC))
    assert (done.hold_kind, done.replicas) == (None, 1)

    # A restart inside a restore hold resumes from the annotations the same
    # way: the clone restore runs, and a finished Job restarts the previous
    # image.
    restore = _step(_upgrade(statefulset_replicas=0, pod_exists=False,
                             statefulset_hold_kind="restore",
                             statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                             clone_exists=True, clone_bound=True,
                             statefulset_pre_upgrade_snapshot=RESTIC),
                    _row(hold_kind="restore", hold_started_at=STARTED))
    assert restore.run_clone_restore_job is True


async def test_a_restore_rebuilds_the_clone_when_the_snapshot_outlived_it(cell_db: CellDatabase) -> None:
    """S2: the attempt's VolumeSnapshot exists and is ready but its clone is
    gone -- recreate the clone and wait for it to bind rather than falling to
    the restic snapshot or RESTORE_FAILED."""
    restore_started = STARTED + timedelta(minutes=31)
    restore_now = restore_started + timedelta(minutes=1)
    observation = _upgrade(statefulset_replicas=0, pod_exists=False,
                           statefulset_hold_kind="restore", statefulset_hold_started_at=restore_started,
                           statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                           statefulset_pre_upgrade_snapshot=RESTIC,
                           snapshot_exists=True, snapshot_ready=True)
    row = _row(hold_kind="restore", hold_started_at=restore_started)

    create = _step(observation, row, now=restore_now)
    assert (create.create_clone, create.run_clone_restore_job, create.run_restore_job_snapshot) == (
        True, False, None)

    unbound = _step(_upgrade(statefulset_replicas=0, pod_exists=False,
                             statefulset_hold_kind="restore", statefulset_hold_started_at=restore_started,
                             statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                             statefulset_pre_upgrade_snapshot=RESTIC,
                             snapshot_exists=True, snapshot_ready=True, clone_exists=True), row, now=restore_now)
    assert (unbound.run_clone_restore_job, unbound.create_clone) == (False, False)

    bound = _step(_upgrade(statefulset_replicas=0, pod_exists=False,
                           statefulset_hold_kind="restore", statefulset_hold_started_at=restore_started,
                           statefulset_pre_upgrade_volume_snapshot=VOL_NAME,
                           statefulset_pre_upgrade_snapshot=RESTIC,
                           snapshot_exists=True, snapshot_ready=True,
                           clone_exists=True, clone_bound=True), row, now=restore_now)
    assert bound.run_clone_restore_job is True

    await _seed_cell(cell_db, CELL_ID, "tenant-a")
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute(
            "UPDATE exomem_cloud_cells SET hold_kind='restore', hold_started_at=$1 WHERE cell_id=$2",
            restore_started, CELL_ID)
    finally:
        await owner.close()
    cluster = FakeClusterGateway()
    cluster.observations[CELL_ID] = observation
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        await reconcile.reconcile_once(
            connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(),
            ClusterConfig(object_storage_bucket="exomem-cloud-backups", storage=MIGRATING), now=restore_now)
        clone = next(m for m in cluster.jobs if m["kind"] == "PersistentVolumeClaim")
        assert clone["metadata"]["name"] == CLONE_NAME
        assert clone["spec"]["dataSourceRef"] == {
            "apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": VOL_NAME}
    finally:
        await connection.close()


def test_probe_attempt_that_predates_the_snapshot_path() -> None:
    observation = _upgrade(
        statefulset_replicas=1, statefulset_image=IMAGE_B, pod_exists=True, pod_ready=False,
        statefulset_target_applied_at=NOW, statefulset_pre_upgrade_snapshot=RESTIC)
    waiting = _step(observation)
    assert (waiting.replicas, waiting.image, waiting.create_snapshot) == (1, IMAGE_B, False)
    assert waiting.pre_upgrade_volume_snapshot is None
    assert waiting.pre_upgrade_snapshot == RESTIC

    ready = _step(replace(observation, pod_ready=True))
    assert (ready.replicas, ready.hold_kind, ready.create_snapshot) == (1, None, False)
    missed = _step(observation, now=NOW + timedelta(minutes=31))
    assert (missed.hold_kind, missed.replicas, missed.create_snapshot) == ("restore", 0, False)
    restoring = _step(replace(observation, statefulset_hold_kind="restore",
                               statefulset_hold_started_at=NOW + timedelta(minutes=31),
                               statefulset_replicas=0, pod_exists=False, pod_uses_volume=False),
                      _row(hold_kind="restore", hold_started_at=NOW + timedelta(minutes=31)),
                      now=NOW + timedelta(minutes=32))
    assert restoring.run_restore_job_snapshot == RESTIC
    assert (restoring.create_snapshot, restoring.create_clone) == (False, False)


@pytest.mark.parametrize("job", [
    {"backup_job_running": True},
    {"backup_job_succeeded": True, "backup_job_snapshot_id": RESTIC},
])
def test_an_old_stopped_backup_job_finishes_on_the_restic_path(job) -> None:
    decision = _step(_upgrade(statefulset_replicas=0, pod_exists=False, **job))
    assert (decision.create_snapshot, decision.create_clone, decision.replicas) == (False, False, 0)
    if job.get("backup_job_succeeded"):
        assert decision.pre_upgrade_snapshot == RESTIC
    else:
        assert decision.run_backup_job is True


def test_reservation_backed_upgrades_use_restic_without_a_snapshot_family() -> None:
    decision = decide(
        _row(hold_kind="upgrade", hold_started_at=STARTED), RolloutRow(),
        _upgrade(statefulset_replicas=0, pod_exists=False), now=NOW, cell_image=IMAGE_B,
        start_upgrade=False, storage=replace(MIGRATING, local=replace(LOCAL, reservations=True)))
    assert (decision.run_backup_job, decision.create_snapshot, decision.create_clone) == (True, False, False)


def test_a_restic_upgrade_on_local_storage_backs_up_the_live_claim() -> None:
    from cellctl.decide import Decision
    from cellctl.manifests import CellManifestSpec, render_backup_job

    spec = CellManifestSpec(cell_id=CELL_ID, image=IMAGE_A, replicas=0, read_only=False,
                            local_volume=True, hold_kind="upgrade", hold_started_at=STARTED.isoformat())
    job = render_backup_job(spec, bucket_name="backups", endpoint="https://s3.example",
                            **_backup_source(Decision(hold_kind="upgrade"), spec))
    pod = job["spec"]["template"]["spec"]
    data = next(v for v in pod["volumes"] if v["name"] == "data")
    assert data["persistentVolumeClaim"]["claimName"] == spec.pvc_name
