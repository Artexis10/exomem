"""move-cloud-cells-to-local-storage 6.4, 7.3 and 7.4: a planned move of a
healthy cell to a new claim.

The operator marks the cell's namespace with the volume it is on. cellctl
stops the cell, takes a fresh stopped backup of that volume, and only then
retains the old volume and restores the backup into a new claim. Each test
names the write it would otherwise lose, or the volume it would otherwise
touch.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import asyncpg
import pytest
import yaml

from cellctl import reconcile
from cellctl.decide import decide
from cellctl.manifests import (
    BACKUP_OUTCOME_ANNOTATION,
    HOLD_ANNOTATION,
    JOB_KIND_LABEL,
    PRE_UPGRADE_SNAPSHOT_ANNOTATION,
    RELOCATION_ANNOTATION,
    STORAGE_CLASS,
    CellManifestSpec,
    namespace_name,
    render_backup_job,
    render_restore_job,
)
from cellctl.reconcile import ClusterConfig
from cellctl.state import ClusterObservation, RolloutRow
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
from cellctl.storage_config import StorageConfig

from .conftest import CellDatabase
from .test_local_storage import (
    CUT_OVER,
    IMAGE_A,
    LAST_BACKUP,
    LOCAL,
    MIGRATING,
    NOW,
    STARTED,
    _bound,
    _local,
    _row,
)
from .test_reconcile import FakeClusterGateway, _observed_from_applied, _secrets_config, _seed_cell

# The stopped backup the move takes; LAST_BACKUP is the cell's older hourly one.
FRESH_BACKUP = "e" * 64
# The cell on its old volume, with the operator's mark naming that volume.
MARKED = dict(pv_name="pv-old", pvc_volume_id="vol-old", pvc_exists=True, namespace_move_volume="vol-old")


def _marked_row(**overrides):
    defaults = dict(volume_id="vol-old", node="agent-1", last_backup_at=STARTED, last_backup_snapshot=LAST_BACKUP)
    defaults.update(overrides)
    return _row(**defaults)


def _moving(**overrides) -> ClusterObservation:
    """The move's first step: the cell stopped on its old volume for its backup."""

    defaults = dict(statefulset_hold_kind="move", statefulset_hold_started_at=STARTED,
                    statefulset_relocation_volume="vol-old", statefulset_replicas=0, pod_exists=False,
                    pod_uses_volume=False, pod_ready=False, **MARKED)
    defaults.update(overrides)
    return _local(**defaults)


def _decide(observation: ClusterObservation, row=None, *, storage: StorageConfig = MIGRATING, now=NOW):
    return decide(row or _marked_row(hold_kind="backup", hold_started_at=STARTED), RolloutRow(), observation,
                  now=now, cell_image=IMAGE_A, start_upgrade=False, storage=storage)


def test_a_planned_move_takes_its_stopped_backup_before_it_touches_the_claim() -> None:
    # Retiring the claim first would restore an older backup and lose every
    # write since it, from a node that is healthy.
    started = _decide(_local(**MARKED), _marked_row())
    assert (started.hold_kind, started.replicas, started.relocation_volume) == ("move", 0, "vol-old")
    assert started.row_updates["hold_kind"] == "backup"

    stopping = _decide(_moving(pod_exists=True, pod_uses_volume=True))
    copying = _decide(_moving())

    assert not stopping.run_backup_job  # the cell's pod still holds the volume
    assert (copying.run_backup_job, copying.replicas) == (True, 0)
    for decision in (started, stopping, copying):
        assert (decision.retain_volume, decision.delete_claim) == (None, False)
        assert "volume_id" not in decision.row_updates


def test_a_planned_move_restores_its_fresh_backup_not_the_last_hourly_one() -> None:
    # The cell's last hourly backup misses the writes made since it.
    decision = _decide(_moving(backup_job_succeeded=True, backup_job_snapshot_id=FRESH_BACKUP))

    assert (decision.hold_kind, decision.relocation_volume) == ("restore", "vol-old")
    assert decision.pre_upgrade_snapshot == FRESH_BACKUP
    assert decision.row_updates["last_backup_snapshot"] == FRESH_BACKUP
    # Its finished pod would hold the old claim until the Job expires.
    assert decision.delete_backup_job is True


@pytest.mark.parametrize("change", [dict(pv_node_lost=True), dict(namespace_move_volume=None)],
                         ids=["node-lost", "mark-removed"])
def test_a_move_whose_backup_succeeded_restores_it_whatever_happens_next(change: dict) -> None:
    # A node lost, or the mark removed, after the stopped backup succeeded:
    # ending the move there would leave a lost node's cell to the older
    # hourly backup, and lose the writes the fresh one holds.
    decision = _decide(_moving(backup_job_succeeded=True, backup_job_snapshot_id=FRESH_BACKUP, **change))

    assert (decision.hold_kind, decision.relocation_volume) == ("restore", "vol-old")
    assert decision.pre_upgrade_snapshot == FRESH_BACKUP
    assert decision.row_updates["last_backup_snapshot"] == FRESH_BACKUP


@pytest.mark.parametrize(("failure", "now"), [
    (dict(backup_job_failed=True), NOW),
    (dict(backup_job_succeeded=True, backup_job_snapshot_id=None), NOW),  # exit 0 without a real snapshot (M1/M2)
    ({}, STARTED + timedelta(minutes=16)),  # past the backup deadline with no result
], ids=["job-failed", "no-snapshot-id", "deadline"])
def test_a_failed_move_backup_leaves_the_cell_stopped_on_its_untouched_claim(failure: dict, now) -> None:
    # Without a fresh backup there is nothing to restore that keeps every
    # write, so the claim, and the volume behind it, stay as they are.
    failed = _decide(_moving(**failure), now=now)

    assert (failed.hold_kind, failed.replicas, failed.backup_outcome) == ("move", 0, "failed")
    assert (failed.retain_volume, failed.delete_claim) == (None, False)
    assert failed.row_updates["last_error_code"] == "MOVE_BACKUP_FAILED"
    assert "volume_id" not in failed.row_updates

    # The failed Job expires; the recorded outcome keeps the cell stopped
    # without backing it up again.
    later = _decide(_moving(statefulset_backup_outcome="failed"), now=now + timedelta(minutes=10))
    assert (later.hold_kind, later.replicas, later.run_backup_job) == ("move", 0, False)
    assert (later.retain_volume, later.delete_claim) == (None, False)


def test_removing_the_mark_before_the_backup_succeeds_starts_the_cell_on_its_own_volume() -> None:
    # The operator's way out of a failed move; the cell must not stay down.
    unmarked = _decide(_moving(statefulset_backup_outcome="failed", namespace_move_volume=None))

    assert (unmarked.hold_kind, unmarked.replicas, unmarked.relocation_volume) == (None, 1, None)
    assert (unmarked.retain_volume, unmarked.delete_claim, unmarked.delete_backup_job) == (None, False, True)
    assert unmarked.row_updates["hold_kind"] is None


@pytest.mark.parametrize("observed", [True, False], ids=["observed", "unobserved"])
def test_a_hetzner_cells_move_takes_a_nightly_backup_slot(observed: bool) -> None:
    # The move's stopped backup of a Hetzner volume is a nightly backup's
    # work. Not counting it lets one more backup run than the window allows,
    # and only while the moving cell can be observed.
    from cellctl.decide import ReconcileConfig
    from cellctl.reconcile import _select_backup_candidates

    moving, due = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    in_window = datetime(2026, 1, 1, 3, tzinfo=UTC)
    rows = [_row(cell_id=moving, hold_kind="backup", hold_started_at=STARTED), _row(cell_id=due)]
    observations = {moving: _bound(STORAGE_CLASS, statefulset_hold_kind="move"), due: _bound(STORAGE_CLASS)}
    config = dataclasses.replace(ReconcileConfig(), backup_concurrency=1)

    if observed:
        chosen = _select_backup_candidates(rows, observations, in_window, config, storage=MIGRATING)
    else:
        chosen = _select_backup_candidates(rows[1:], observations, in_window, config, unobserved=rows[:1],
                                           storage=MIGRATING)

    assert chosen == set()


@pytest.mark.parametrize(("row", "observation", "storage"), [
    # The mark names a volume the cell is not on.
    (_marked_row(), _local(**{**MARKED, "namespace_move_volume": "vol-other"}), MIGRATING),
    # Left in place after a finished move: the cell is on its new volume.
    (_marked_row(volume_id="vol-new"), _local(**{**MARKED, "pvc_volume_id": "vol-new"}), MIGRATING),
    # Production today: no local class is configured, so nothing moves.
    (_marked_row(), _bound(STORAGE_CLASS, statefulset_row_generation=1, **MARKED), StorageConfig()),
], ids=["other-volume", "after-the-move", "no-local-storage"])
def test_a_stale_move_mark_is_ignored(row, observation, storage) -> None:
    # A stale mark that moved anything would stop a healthy cell, and a mark
    # left after its move would move the cell again on every pass.
    decision = _decide(observation, row, storage=storage)

    assert decision.hold_kind is None
    assert (decision.replicas, decision.run_backup_job, decision.retain_volume, decision.delete_claim) == (
        1, False, None, False)


@pytest.mark.parametrize("hold", ["upgrade", "backup", "restore", "snapshot-backup", "import"])
def test_another_hold_blocks_a_planned_move(hold: str) -> None:
    # A move started inside another hold would stop a cell mid-upgrade, or
    # retire a claim that a backup or restore Job is still using.
    observation = _local(**MARKED, statefulset_hold_kind=hold, statefulset_hold_started_at=STARTED,
                         statefulset_pre_upgrade_snapshot=LAST_BACKUP, statefulset_previous_image=IMAGE_A,
                         pod_ready=False)

    decision = _decide(observation, _marked_row(hold_kind="backup", hold_started_at=STARTED))

    assert decision.hold_kind == hold
    assert (decision.relocation_volume, decision.retain_volume, decision.delete_claim) == (None, None, False)


def test_deletion_supersedes_a_planned_move() -> None:
    # A move that went on would restore a deleted cell's data into a new claim.
    decision = _decide(_moving(), _marked_row(desired_state="deleted", generation=2, hold_kind="backup",
                                              hold_started_at=STARTED))

    assert decision.delete_namespace is True
    assert (decision.hold_kind, decision.run_backup_job, decision.run_restore_job_snapshot) == (None, False, None)
    assert (decision.retain_volume, decision.delete_claim) == (None, False)
    assert decision.row_updates["hold_kind"] is None


def test_a_local_backup_job_tolerates_its_cordoned_node_and_a_restore_job_never_does() -> None:
    # The removal playbook cordons the agent before it moves the cells off it.
    # The stopped backup must still run there, where the volume is; the
    # restore must not, or the new claim binds back onto the node being removed.
    spec = CellManifestSpec(cell_id="aaaaaaaaaaaaaaaa", image=IMAGE_A, replicas=0, read_only=False,
                            storage_class=LOCAL.class_name, local_volume=True, hold_kind="move",
                            hold_started_at=STARTED.isoformat())
    hetzner = dataclasses.replace(spec, storage_class=STORAGE_CLASS, local_volume=False)

    def pod(job: dict) -> dict:
        return job["spec"]["template"]["spec"]

    backup = render_backup_job(spec, bucket_name="b", endpoint="https://s3.example", retention=None)
    restore = render_restore_job(spec, bucket_name="b", endpoint="https://s3.example", snapshot_id=FRESH_BACKUP)
    hetzner_backup = render_backup_job(hetzner, bucket_name="b", endpoint="https://s3.example", retention=None)

    assert pod(backup)["tolerations"] == [
        {"key": "node.kubernetes.io/unschedulable", "operator": "Exists", "effect": "NoSchedule"}]
    assert "tolerations" not in pod(restore)
    # A Hetzner volume attaches anywhere, so its Job never lands on a node being drained.
    assert "tolerations" not in pod(hetzner_backup)


def test_the_removal_playbook_sets_the_mark_cellctl_reads() -> None:
    # The playbook cannot import cellctl, so its copy of the mark is checked
    # here. Any other key would leave every removal waiting on a move that
    # never starts.
    from pathlib import Path

    from cellctl.k8s_client import ClusterClient

    from .test_k8s_client import CELL_ID, NAMESPACE, _client, _Core

    defaults = yaml.safe_load((Path(__file__).parents[3] / "infra/ansible/roles/k3s/defaults/main.yml").read_text())
    key = defaults["k3s_remove_move_annotation"]
    namespace = NS(metadata=NS(name=NAMESPACE, labels={"exomem.io/cloud-cell": CELL_ID}, annotations={key: "vol-1"}))
    client: ClusterClient = _client(hold_started_at=None, jobs={}, pods=[])
    client._core = _Core([], namespaces=[namespace])

    assert client.observe(CELL_ID, NAMESPACE).namespace_move_volume == "vol-1"


def test_the_removal_playbook_names_the_hourly_backup_hold_as_cellctl_does() -> None:
    # The removal ignores only this hold on a cell that left nothing but a
    # retained volume on the agent. A copy that drifts makes it ignore a
    # restore that may still need that volume, or refuse over hourly backups.
    from pathlib import Path

    from cellctl.state import SNAPSHOT_BACKUP

    defaults = yaml.safe_load((Path(__file__).parents[3] / "infra/ansible/roles/k3s/defaults/main.yml").read_text())

    assert defaults["k3s_snapshot_backup_hold"] == SNAPSHOT_BACKUP


def test_the_removal_playbook_reserves_what_cellctl_reserves() -> None:
    # The playbook applies capacity.pool_takes from its own copy of the
    # reserve's inputs. A copy that drifts below the chart's lets a removal
    # move cells onto a node whose hourly backups then have no room.
    from pathlib import Path

    from cellctl.storage_config import LocalStorage

    root = Path(__file__).parents[3]
    defaults = yaml.safe_load((root / "infra/ansible/roles/k3s/defaults/main.yml").read_text())
    values = yaml.safe_load((root / "infra/helm/platform/values.yaml").read_text())
    chart_device_class = next(entry["name"] for entry in values["topolvm"]["lvmd"]["deviceClasses"] if entry.get("default"))
    local = LocalStorage()

    assert (defaults["k3s_local_backup_concurrency_per_node"]
            == values["cellStorage"]["local"]["backupConcurrencyPerNode"] == local.backup_concurrency_per_node)
    assert defaults["k3s_local_default_cell_gib"] == local.default_cell_gib
    assert defaults["k3s_topolvm_device_class"] == chart_device_class == local.device_class
    assert defaults["k3s_local_storage_driver"] == local.driver


def test_cellctl_agrees_with_the_removal_playbook_on_where_a_moved_cell_fits() -> None:
    # The playbook copies this rule; tests/test_hosted_node_pool_contract.py
    # runs the playbook on the same cases. A side that drifts places a moved
    # cell on a node whose backups then have no room, or refuses a removal
    # that fits.
    import json
    from pathlib import Path

    from cellctl.capacity import GIB, pool_takes, snapshot_reserve_bytes
    from cellctl.k8s_client import _general_node
    from cellctl.storage_config import LocalStorage

    def node(case: dict) -> NS:
        taints = [NS(key=taint["key"], effect=taint["effect"], value=None) for taint in case.get("taints", [])]
        ready = NS(type="Ready", status="True" if case.get("ready", True) else "False")
        return NS(metadata=NS(name=case["name"], labels=case.get("labels", {})),
                  spec=NS(taints=taints, unschedulable=case.get("unschedulable", False)),
                  status=NS(conditions=[ready]))

    local = LocalStorage()
    cases = json.loads((Path(__file__).parent / "fixtures/removal-room-cases.json").read_text())["cases"]
    for case in cases:
        largest = max([case["moving_gib"], *case["other_claims_gib"]])
        takes = sorted(entry["name"] for entry in case["nodes"] if _general_node(node(entry)) and pool_takes(
            local, free_bytes=entry["free_gib"] * GIB, added_bytes=case["moving_gib"] * GIB, largest_cell_gib=largest))

        assert snapshot_reserve_bytes(local, largest) == case["reserve_bytes"], case["name"]
        assert takes == case["takes_the_moving_cell"], case["name"]


@pytest.mark.parametrize("per_cell_destination", [False, True])
async def test_a_planned_move_through_the_controller_backs_up_first_then_restores_onto_a_fresh_claim(
    cell_db: CellDatabase, per_cell_destination: bool,
) -> None:
    # Task 7.3 after the cutover: a cell on its Hetzner volume moves to local
    # storage. The old claim keeps its class while it exists (the field is
    # immutable, so any other class would be refused on every pass), the
    # backup runs before any volume write, and the restore reads that backup.
    from cellctl import db

    class Gateway(FakeClusterGateway):
        def run_job(self, manifest: dict) -> None:
            super().run_job(manifest)
            self.events.append(("job", manifest["metadata"]["labels"].get(JOB_KIND_LABEL)))

        def retain_volume(self, name: str) -> None:
            self.events.append(("retain", name))

        def delete_claim(self, namespace: str) -> None:
            self.events.append(("delete claim", namespace))

        def apply_all(self, manifests: list[dict]) -> None:
            super().apply_all(manifests)
            self.events.extend(("apply claim", manifest["spec"]["storageClassName"])
                               for manifest in manifests if manifest["kind"] == "PersistentVolumeClaim")

    cell_id, namespace = "aaaaaaaaaaaaaaaa", namespace_name("aaaaaaaaaaaaaaaa")
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    await db.write_observed(connection, cell_id, {
        "observed_state": "running", "observed_generation": 1, "observed_image": IMAGE_A, "ready": True,
        "volume_id": "vol-old", "node": "server", "last_backup_at": STARTED, "last_backup_snapshot": LAST_BACKUP})
    cluster = Gateway()
    destination = "cell-key-storage" if per_cell_destination else LOCAL.class_name
    storage = StorageConfig(per_cell_class=destination) if per_cell_destination else CUT_OVER
    config = ClusterConfig(object_storage_bucket="b", storage=storage)
    on_hetzner = dict(pvc_storage_class=STORAGE_CLASS, pv_name="pv-old", pvc_volume_id="vol-old", pvc_exists=True)
    stopped = dict(pod_exists=False, pod_uses_volume=False, pod_ready=False)
    cluster.observations[cell_id] = _bound(STORAGE_CLASS, statefulset_row_generation=1, **on_hetzner)

    def observe(**overrides) -> None:
        annotations = cluster.applied[(namespace, "StatefulSet", "cell")]["metadata"]["annotations"]
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id, **{
            **on_hetzner, "namespace_move_storage_class": destination if per_cell_destination else None,
            "statefulset_relocation_volume": annotations.get(RELOCATION_ANNOTATION),
            "statefulset_backup_outcome": annotations.get(BACKUP_OUTCOME_ANNOTATION), **overrides})

    def claim_class() -> str:
        return cluster.applied[(namespace, "PersistentVolumeClaim", "cell-data")]["spec"]["storageClassName"]

    def statefulset_annotations() -> dict:
        return cluster.applied[(namespace, "StatefulSet", "cell")]["metadata"]["annotations"]

    async def run(minute: int) -> None:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                       config, now=NOW + timedelta(minutes=minute))

    try:
        await run(0)
        observe()
        await run(1)  # converged and serving
        observe(namespace_move_volume="vol-old")
        await run(2)
        assert (statefulset_annotations()[HOLD_ANNOTATION], claim_class()) == ("move", STORAGE_CLASS)

        observe(namespace_move_volume="vol-old", **stopped)
        await run(3)
        observe(namespace_move_volume="vol-old", **stopped, backup_job_succeeded=True,
                backup_job_snapshot_id=FRESH_BACKUP)
        await run(4)
        assert statefulset_annotations()[HOLD_ANNOTATION] == "restore"
        assert statefulset_annotations()[PRE_UPGRADE_SNAPSHOT_ANNOTATION] == FRESH_BACKUP
        assert claim_class() == STORAGE_CLASS

        observe(namespace_move_volume="vol-old", **stopped, pv_reclaim_policy="Delete")
        await run(5)
        observe(namespace_move_volume="vol-old", **stopped, pv_reclaim_policy="Retain")
        await run(6)
        assert (await db.select_all_rows(connection))[0].volume_id is None

        observe(namespace_move_volume="vol-old", **stopped, pvc_exists=False, pvc_bound=False, pvc_volume_id=None,
                pv_name=None, pvc_storage_class=None, pv_storage_class=None)
        await run(7)
    finally:
        await connection.close()

    assert [event for event in cluster.events if event[0] in ("job", "retain", "delete claim")] == [
        ("job", "backup"), ("retain", "pv-old"), ("delete claim", namespace), ("job", "restore")]
    # Once the old claim is deleted, only a local claim is applied. The
    # Hetzner claim applied again in the pass that deleted it would bind a new
    # Hetzner volume, and the restore would land there.
    after_delete = cluster.events[cluster.events.index(("delete claim", namespace)) + 1:]
    assert [event for event in after_delete if event[0] == "apply claim"] == [("apply claim", destination)]
    assert claim_class() == destination
    restore = cluster.jobs[-1]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert FRESH_BACKUP in restore and LAST_BACKUP not in restore


async def test_move_acceptance_keeps_old_volume_until_exact_mark_and_provider_absence(cell_db):
    """An acceptance mark cannot hide a retained rollback volume that the provider still holds."""
    import json

    from cellctl import db
    from cellctl.storage.interface import VolumeInfo

    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "accepted-move")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    provider = FakeHetznerVolumeProvider([VolumeInfo("vol-old", "node-old", {})])
    try:
        await db.write_observed(connection, cell_id, {
            "observed_state": "running", "observed_generation": 1, "observed_image": IMAGE_A,
            "ready": True, "volume_id": "vol-new", "relocation_old_volume_id": "vol-old",
            "custody_observation": json.dumps({"retained_old_storage_class": STORAGE_CLASS}),
        })
        cluster.observations[cell_id] = _bound(STORAGE_CLASS, pvc_volume_id="vol-new", pvc_exists=True,
            namespace_move_accepted_volume="vol-old")
        for minute in (0, 1):
            await reconcile.reconcile_once(connection, cluster, FakeB2(), provider, _secrets_config(),
                ClusterConfig(object_storage_bucket="b"), now=NOW + timedelta(minutes=minute))
            assert (await db.select_all_rows(connection))[0].relocation_old_volume_id == "vol-old"
        provider.remove_volume("vol-old")
        await reconcile.reconcile_once(connection, cluster, FakeB2(), provider, _secrets_config(),
            ClusterConfig(object_storage_bucket="b"), now=NOW + timedelta(minutes=2))
        row = (await db.select_all_rows(connection))[0]
        assert row.relocation_old_volume_id is None
        assert row.relocation_accepted_at == NOW + timedelta(minutes=2)
    finally:
        await connection.close()
