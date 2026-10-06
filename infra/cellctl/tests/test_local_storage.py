"""move-cloud-cells-to-local-storage, phase 2: cellctl on TopoLVM local volumes.

Each test names the behaviour only it pins. With no local class configured,
cellctl must behave exactly as before; the unchanged-production test proves
that on an existing cell's render digest and manifests.
"""

from __future__ import annotations

import base64
import dataclasses
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from cellctl import reconcile
from cellctl.decide import StorageRoom, decide
from cellctl.manifests import STORAGE_CLASS, namespace_name
from cellctl.reconcile import ClusterConfig
from cellctl.state import IDENTITY_CONFLICT, CellRow, ClusterObservation, RolloutRow
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
from cellctl.storage_config import LocalStorage, StorageConfig

from .conftest import CellDatabase, tenant_uuid
from .test_reconcile import FakeClusterGateway, _secrets_config, _seed_cell

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
IMAGE_A = "registry.example/cell@sha256:" + "a" * 64
LOCAL = LocalStorage()
# The local class is configured, but Hetzner volumes stay the domain (phases 2-6).
MIGRATING = StorageConfig(local=LOCAL)
# After the cutover (7.2): new claims and published capacity are local.
CUT_OVER = StorageConfig(domain=LOCAL.class_name, local=LOCAL)


def _row(**overrides) -> CellRow:
    defaults = dict(
        cell_id="aaaaaaaaaaaaaaaa", tenant_id=tenant_uuid("tenant-a"), storage_gib=10, rollout_priority=1,
        desired_state="running", desired_image=None, generation=1, observed_generation=1,
        observed_state="running", observed_image=IMAGE_A, ready=True,
    )
    defaults.update(overrides)
    return CellRow(**defaults)


def _bound(storage_class: str, **overrides) -> ClusterObservation:
    defaults = dict(
        namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", pvc_bound=True, pvc_uid="pvc-1",
        pv_claim_ref_uid="pvc-1", pv_storage_class=storage_class, pvc_storage_class=storage_class,
        statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=1, pod_ready=True,
        ready_pod_image=IMAGE_A,
    )
    defaults.update(overrides)
    return ClusterObservation(**defaults)


def _decide(row: CellRow, observation: ClusterObservation, storage: StorageConfig):
    return decide(row, RolloutRow(), observation, now=NOW, cell_image=IMAGE_A, start_upgrade=False, storage=storage)


# --- 2.1: configured classes ------------------------------------------------------


@pytest.mark.parametrize(
    ("storage", "bound_class"),
    # After the cutover a cell still on a Hetzner volume, and before it a cell
    # already on the local class: neither may fail as a foreign volume.
    [(CUT_OVER, STORAGE_CLASS), (MIGRATING, LOCAL.class_name)],
)
def test_a_cell_bound_to_any_configured_class_is_not_an_identity_conflict(storage, bound_class) -> None:
    decision = _decide(_row(), _bound(bound_class), storage)

    assert decision.row_updates.get("last_error_code") != IDENTITY_CONFLICT
    assert decision.row_updates.get("observed_state") != "failed"


@pytest.mark.parametrize(("existing_claim_class", "rendered_class"), [(None, LOCAL.class_name), (STORAGE_CLASS, STORAGE_CLASS)])
async def test_after_the_cutover_only_a_new_claim_takes_the_local_class(
    cell_db: CellDatabase, existing_claim_class: str | None, rendered_class: str
) -> None:
    # storageClassName is immutable: re-rendering the domain over a cell that
    # is still on a Hetzner volume would be refused on every pass.
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    observation = _bound(existing_claim_class) if existing_claim_class else ClusterObservation()
    cluster = FakeClusterGateway()
    cluster.observations["aaaaaaaaaaaaaaaa"] = observation
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        await reconcile.reconcile_once(
            connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
            ClusterConfig(object_storage_bucket="exomem-cloud-backups", storage=CUT_OVER), now=NOW,
        )
    finally:
        await connection.close()

    claim = cluster.applied[(namespace_name("aaaaaaaaaaaaaaaa"), "PersistentVolumeClaim", "cell-data")]
    assert claim["spec"]["storageClassName"] == rendered_class


@pytest.mark.parametrize(("listed", "stop_confirmed", "absent"), [
    ("ready", False, False),
    # Only partitioned: never relocated.
    ("not-ready", False, False),
    ("out-of-service", True, False),
    # Gone from the API: reconcile.py decides how long that must last.
    ("absent", False, True),
])
def test_a_local_volume_is_matched_to_the_node_its_pv_is_pinned_to(listed: str, stop_confirmed: bool,
                                                                    absent: bool) -> None:
    from types import SimpleNamespace as NS

    from cellctl.k8s_client import ClusterClient

    from .test_k8s_client import NAMESPACE, _client, _Core

    affinity = NS(required=NS(node_selector_terms=[NS(match_expressions=[
        NS(key=LOCAL.topology_key, operator="In", values=["agent-2"])], match_fields=None)]))
    pvc = NS(metadata=NS(name="cell-data", namespace=NAMESPACE, uid="uid-1"),
             spec=NS(volume_name="pv-1", storage_class_name=LOCAL.class_name), status=NS(phase="Bound"))
    pv = NS(metadata=NS(name="pv-1"), spec=NS(claim_ref=NS(uid="uid-1"), storage_class_name=LOCAL.class_name,
                                              csi=NS(volume_handle="lv-1"), node_affinity=affinity,
                                              persistent_volume_reclaim_policy="Delete"))
    client: ClusterClient = _client(hold_started_at=None, jobs={}, pods=[])
    client._core = _Core([], pvcs=[pvc], pvs=[pv])
    taints = [NS(key="node.kubernetes.io/out-of-service", effect="NoExecute")] if listed == "out-of-service" else []
    node = NS(metadata=NS(name="agent-2"), spec=NS(taints=taints),
              status=NS(conditions=[NS(type="Ready", status="True" if listed == "ready" else "Unknown")]))
    client._core.list_node = lambda: NS(items=[] if listed == "absent" else [node])
    client._storage_config = MIGRATING

    observed = client.observe_cells({"aaaaaaaaaaaaaaaa": NAMESPACE})["aaaaaaaaaaaaaaaa"]

    assert (observed.pv_node, observed.pv_node_stop_confirmed, observed.pv_node_absent) == (
        "agent-2", stop_confirmed, absent)


def test_with_no_local_class_an_existing_cells_render_digest_does_not_move() -> None:
    # The digest is every converged cell's restart trigger: a value that moves
    # with no local class configured restarts every production cell on the
    # next deploy. Captured from c298b7954, before local storage existed, for a
    # Hetzner cell with production's chart defaults.
    from cellctl.reconcile import _compute_render_digest

    row = _row(generation=3, backup_key_version=1, b2_key_version=1)
    config = ClusterConfig(
        object_storage_bucket="b",
        job_egress_except=("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"),
    )

    assert _compute_render_digest(row, config, _secrets_config(), STORAGE_CLASS) == (
        "3a08b2c74eae86e9d927d4b127ee8e7f64703145889368dc1675c9ca0f0cd2e0"
    )


# --- 2.6: the empty-vault guard --------------------------------------------------


def test_a_local_cells_first_backup_changes_its_secret_but_not_its_pod_template_or_digest() -> None:
    # D5: the backed-up key reaches cell-init through an optional reference
    # the template carries from the start, so recording the first backup
    # restarts no cell.
    from cellctl.manifests import CellManifestSpec, render_secret, render_statefulset
    from cellctl.reconcile import _compute_render_digest

    spec = CellManifestSpec(cell_id="aaaaaaaaaaaaaaaa", image=IMAGE_A, replicas=1, read_only=False,
                            storage_class=LOCAL.class_name, local_volume=True)
    backed_up = dataclasses.replace(spec, backed_up=True)
    config = ClusterConfig(object_storage_bucket="b", storage=MIGRATING)

    assert render_statefulset(backed_up) == render_statefulset(spec)
    assert "backed-up" not in render_secret(spec)["data"]
    assert base64.b64decode(render_secret(backed_up)["data"]["backed-up"]) == b"true"
    assert _compute_render_digest(_row(last_backup_at=NOW), config, _secrets_config(), LOCAL.class_name) == (
        _compute_render_digest(_row(), config, _secrets_config(), LOCAL.class_name)
    )


def test_a_refusing_cell_init_puts_its_code_on_the_row_before_the_init_deadline() -> None:
    observation = _bound(LOCAL.class_name, pod_ready=False, pod_exists=True, pod_created_at=NOW,
                         statefulset_row_generation=2, init_error_code="CELL_INIT_EMPTY_VOLUME_REFUSED")

    decision = _decide(_row(ready=False, generation=2), observation, MIGRATING)

    assert decision.row_updates["last_error_code"] == "CELL_INIT_EMPTY_VOLUME_REFUSED"
    assert decision.row_updates["observed_state"] == "provisioning"


@pytest.mark.parametrize(
    ("message", "recorded"),
    # Only cell-init's own fixed refusal reaches the row: the message is
    # container output, never trusted as free text.
    [("CELL_INIT_EMPTY_VOLUME_REFUSED", "CELL_INIT_EMPTY_VOLUME_REFUSED"), ("/data/vault: anything", None)],
)
def test_the_refusal_is_read_from_a_crash_looping_cell_init(message: str, recorded: str | None) -> None:
    from types import SimpleNamespace as NS

    from .test_k8s_client import CELL_ID, NAMESPACE, _cell_pod, _client

    waiting = NS(terminated=None, running=None, waiting=NS(reason="CrashLoopBackOff"))
    refused = NS(terminated=NS(exit_code=1, message=message, started_at=None), running=None, waiting=None)
    pod = _cell_pod(init_state=waiting, last_state=refused)

    observed = _client(hold_started_at=None, jobs={}, pods=[pod]).observe(CELL_ID, NAMESPACE)

    assert observed.init_error_code == recorded


# --- 2.2: capacity from the local pool ---------------------------------------------

GIB = 1024**3


def _volume(node: str, gib: int, *, created: bool = True, deleting: bool = False):
    from cellctl.capacity import LogicalVolumeRecord

    return LogicalVolumeRecord(node=node, size=gib * GIB, device_class=LOCAL.device_class, created=created,
                               deleting=deleting)


def test_a_pass_during_a_backup_publishes_the_same_slots() -> None:
    # Recorded the way spike 1.1 measured TopoLVM at ratio 1.0: a snapshot and
    # its clone each take their full size from the published free bytes, and
    # a volume whose object exists before lvmd creates it has taken nothing.
    from cellctl.capacity import LocalCapacityObservation, compute_local_capacity

    cells = {"aaaaaaaaaaaaaaaa": 4, "bbbbbbbbbbbbbbbb": 4}
    idle = LocalCapacityObservation(free_bytes={"agent-1": 92 * GIB}, volumes=(_volume("agent-1", 4), _volume("agent-1", 4)))
    backing_up = LocalCapacityObservation(
        free_bytes={"agent-1": 84 * GIB},
        volumes=(*idle.volumes, _volume("agent-1", 4), _volume("agent-1", 4), _volume("agent-1", 4, created=False)),
    )

    # (100 GiB pool - 2 x 4 GiB x 2 concurrent backups) / the 4 GiB local default
    assert compute_local_capacity(idle, local=LOCAL, cell_sizes=cells)["agent-1"].cell_slots == 21
    assert compute_local_capacity(backing_up, local=LOCAL, cell_sizes=cells)["agent-1"].cell_slots == 21


def test_a_cell_larger_than_the_default_takes_more_slots_and_a_larger_reserve() -> None:
    from cellctl.capacity import LocalCapacityObservation, compute_local_capacity

    observation = LocalCapacityObservation(free_bytes={"agent-1": 370 * GIB}, volumes=(_volume("agent-1", 30),),
                                           cell_nodes={"aaaaaaaaaaaaaaaa": "agent-1"})

    published = compute_local_capacity(observation, local=LOCAL, cell_sizes={"aaaaaaaaaaaaaaaa": 30})

    # (400 - 2 x 30 x 2) / 4 = 70 default slots, of which the 30 GiB cell,
    # counted as one row by admission, takes ceil(30 / 4) - 1 = 7 more.
    assert published["agent-1"].cell_slots == 63


async def test_once_local_is_the_domain_only_nodes_with_a_pool_publish_slots(cell_db: CellDatabase) -> None:
    # The control-plane server keeps its Hetzner attachment allowance but has
    # no cell pool; after the cutover it must publish nothing.
    from cellctl.capacity import CapacityObservation, LocalCapacityObservation

    cluster = FakeClusterGateway()
    cluster.capacity_inputs = lambda **kwargs: CapacityObservation(allocatable={"server": 16}, attachments_used={"server": 3})
    cluster.local_capacity_inputs = lambda local: LocalCapacityObservation(
        free_bytes={"server": None, "agent-1": 100 * GIB})
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        await reconcile.reconcile_once(
            connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
            ClusterConfig(object_storage_bucket="b", storage=CUT_OVER), now=NOW,
        )
        published = await connection.fetch("SELECT node, cell_slots FROM exomem_cloud_capacity")
    finally:
        await connection.close()

    assert {record["node"]: record["cell_slots"] for record in published} == {"server": 0, "agent-1": 21}


def test_a_shared_local_worker_publishes_the_same_slots_while_a_cell_backs_up() -> None:
    # D3: the hourly backup Job runs beside the serving pod, so the qualified
    # footprint carries one Job; otherwise every backup would close admission.
    from dataclasses import replace
    from decimal import Decimal

    from cellctl.capacity import (
        CapacityConfig,
        CapacityObservation,
        NodeCapacity,
        NodeObservation,
        PodReservation,
        SharedWorkerPolicy,
        compute_shared_capacity,
    )
    from cellctl.manifests import ResourceSettings

    policy = SharedWorkerPolicy(mode="all-shared", profile="qualified-test", topology_key="topology.kubernetes.io/zone",
                                topology_value="test-zone", occupancy=6,
                                resources=ResourceSettings(cpu_request="1", memory_request="2Gi"),
                                reserve_cpu="500m", reserve_memory="1Gi")
    node = NodeObservation(name="worker", cpu=Decimal("3.5"), memory=Decimal(8 * GIB),
                           labels={"exomem.io/shared-profile": policy.profile, policy.topology_key: policy.topology_value},
                           ready=True, schedulable=True, pressure=False,
                           taints=(("exomem.io/shared-profile", policy.profile, "NoSchedule"),))
    serving = PodReservation(node="worker", cell_id="aaaaaaaaaaaaaaaa", cpu=Decimal(1), memory=Decimal(2 * GIB))
    backup_job = PodReservation(node="worker", cell_id="aaaaaaaaaaaaaaaa", cpu=Decimal("0.1"), memory=Decimal(256 * 1024**2))
    idle = CapacityObservation(nodes={"worker": node}, pods=(serving,))
    storage = {"worker": NodeCapacity(cell_slots=6, attachments_used=1, limit_known=True)}

    def slots(observation) -> int:
        return compute_shared_capacity(observation, policy=policy, config=CapacityConfig(),
                                       committed=frozenset({"aaaaaaaaaaaaaaaa"}), storage_slots=storage)["worker"].cell_slots

    assert slots(replace(idle, pods=(serving, backup_job))) == slots(idle) == 2


def test_the_pool_is_read_from_topolvms_node_annotation_and_logical_volumes() -> None:
    # Field names as TopoLVM 17.2.0's topolvm.io/v1 LogicalVolume CRD and
    # node annotation publish them; a wrong name would read as an empty pool.
    from types import SimpleNamespace as NS

    from cellctl.k8s_client import ClusterClient

    def node(name: str, annotations: dict) -> NS:
        return NS(metadata=NS(name=name, labels={}, annotations=annotations),
                  spec=NS(taints=None, unschedulable=False),
                  status=NS(conditions=[NS(type="Ready", status="True")]))

    pinned = NS(required=NS(node_selector_terms=[NS(match_expressions=[
        NS(key=LOCAL.topology_key, operator="In", values=["agent-1"])], match_fields=None)]))
    cell_pv = NS(metadata=NS(name="pv-1"), spec=NS(
        claim_ref=NS(namespace="exo-cell-aaaaaaaaaaaaaaaa", name="cell-data"),
        storage_class_name=LOCAL.class_name, node_affinity=pinned))

    class Core:
        def list_node(self):
            return NS(items=[node("agent-1", {"capacity.topolvm.io/thin": "85899345920"}), node("server", {})])

        def list_persistent_volume(self):
            return NS(items=[cell_pv])

    class Custom:
        def list_cluster_custom_object(self, group, version, plural):
            assert (group, version, plural) == ("topolvm.io", "v1", "logicalvolumes")
            return {"items": [{"metadata": {"name": "pvc-1"},
                               "spec": {"nodeName": "agent-1", "size": "10Gi", "deviceClass": "thin"},
                               "status": {"volumeID": "7f6c"}}]}

    client = ClusterClient.__new__(ClusterClient)
    client._core, client._custom = Core(), Custom()

    observed = client.local_capacity_inputs(LOCAL)

    assert observed.free_bytes == {"agent-1": 80 * GIB, "server": None}
    assert [(v.node, v.size, v.device_class, v.created) for v in observed.volumes] == [("agent-1", 10 * GIB, "thin", True)]
    assert observed.cell_nodes == {"aaaaaaaaaaaaaaaa": "agent-1"}


# --- 2.3: the hourly online backup ---------------------------------------------------

STARTED = datetime(2026, 1, 1, 11, 58, tzinfo=UTC)
SNAPSHOT_ID = "c" * 64


def _local(**overrides) -> ClusterObservation:
    return _bound(LOCAL.class_name, **{"statefulset_row_generation": 1, "pv_node": "agent-1", **overrides})


def _in_hold(**overrides) -> ClusterObservation:
    return _local(statefulset_hold_kind="snapshot-backup", statefulset_hold_started_at=STARTED, **overrides)


def _step(observation: ClusterObservation, row: CellRow | None = None, now: datetime = NOW):
    return decide(row or _row(hold_kind="backup", hold_started_at=STARTED), RolloutRow(), observation, now=now,
                  cell_image=IMAGE_A, start_upgrade=False, storage=MIGRATING)


def test_an_hourly_backup_starts_without_stopping_the_cell() -> None:
    decision = decide(_row(last_backup_at=datetime(2026, 1, 1, 10, tzinfo=UTC)), RolloutRow(), _local(), now=NOW,
                      cell_image=IMAGE_A, start_upgrade=False, start_backup=True, storage=MIGRATING)

    assert (decision.hold_kind, decision.replicas, decision.create_snapshot) == ("snapshot-backup", 1, True)
    # The row's hold column keeps the C1 vocabulary (upgrade, backup, restore).
    assert decision.row_updates["hold_kind"] == "backup"


def test_an_hourly_backup_reads_a_clone_of_its_snapshot_and_ends_once_both_are_gone() -> None:
    assert _step(_in_hold(snapshot_exists=True)).create_clone is False  # not ready yet
    assert _step(_in_hold(snapshot_exists=True, snapshot_ready=True)).create_clone is True
    waiting = _step(_in_hold(snapshot_exists=True, snapshot_ready=True, clone_exists=True))
    assert not waiting.run_backup_job  # the clone is not bound yet
    copying = _step(_in_hold(snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True))
    assert copying.run_backup_job and copying.replicas == 1

    taken = STARTED.replace(minute=59)
    done = _step(_in_hold(snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True,
                          snapshot_created_at=taken, backup_job_succeeded=True, backup_job_snapshot_id=SNAPSHOT_ID))
    assert done.row_updates["last_backup_snapshot"] == SNAPSHOT_ID
    # The backup holds the volume as of its snapshot, not as of the upload's end.
    assert done.row_updates["last_backup_at"] == taken
    assert (done.backup_outcome, done.delete_snapshot_backup, done.hold_kind) == (SNAPSHOT_ID, True, "snapshot-backup")

    cleaning = _step(_in_hold(statefulset_backup_outcome=SNAPSHOT_ID, clone_exists=True),
                     _row(hold_kind="backup", hold_started_at=STARTED, last_backup_at=NOW, last_backup_snapshot=SNAPSHOT_ID))
    assert (cleaning.delete_snapshot_backup, cleaning.hold_kind) == (True, "snapshot-backup")
    finished = _step(_in_hold(statefulset_backup_outcome=SNAPSHOT_ID),
                     _row(hold_kind="backup", hold_started_at=STARTED, last_backup_at=NOW, last_backup_snapshot=SNAPSHOT_ID))
    assert (finished.hold_kind, finished.row_updates["hold_kind"], finished.replicas) == (None, None, 1)


def test_a_failed_hourly_backup_backs_off_and_still_removes_its_clone_and_snapshot() -> None:
    late = STARTED.replace(hour=12, minute=20)  # past the 15-minute backup deadline

    decision = _step(_in_hold(snapshot_exists=True, clone_exists=True), now=late)

    assert decision.row_updates["last_error_code"] == "BACKUP_FAILED"
    assert decision.backup_retry_after is not None
    assert (decision.backup_outcome, decision.delete_snapshot_backup, decision.hold_kind) == ("failed", True, "snapshot-backup")


def test_a_desired_state_change_during_an_hourly_backup_applies_at_once() -> None:
    decision = _step(_in_hold(snapshot_exists=True), _row(desired_state="read_only", generation=2,
                                                          hold_kind="backup", hold_started_at=STARTED))

    assert (decision.read_only, decision.replicas, decision.hold_kind) == (True, 1, "snapshot-backup")


@pytest.mark.parametrize(("last_backup", "prune"), [(datetime(2026, 1, 1, 1, 30, tzinfo=UTC), True),
                                                    (datetime(2026, 1, 1, 10, 50, tzinfo=UTC), False)])
def test_only_the_first_hourly_backup_after_0200_utc_prunes(last_backup: datetime, prune: bool) -> None:
    decision = _step(_in_hold(snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True),
                     _row(hold_kind="backup", hold_started_at=STARTED, last_backup_at=last_backup))

    assert decision.run_backup_job and decision.backup_prune is prune


def test_hourly_backups_run_two_at_a_time_per_node_and_leave_hetzner_cells_to_the_nightly_window() -> None:
    from cellctl.decide import ReconcileConfig
    from cellctl.reconcile import _select_backup_candidates

    an_hour_ago = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    ids = ["aaaaaaaaaaaaaaa" + c for c in "abcde"]
    rows = [_row(cell_id=cell_id, last_backup_at=an_hour_ago) for cell_id in ids]
    observations = {
        ids[0]: _local(), ids[1]: _local(), ids[2]: _local(),
        ids[3]: _local(pv_node="agent-2"),
        ids[4]: _bound(STORAGE_CLASS),  # a Hetzner cell, outside its 02:00-05:00 window at noon
    }

    chosen = _select_backup_candidates(rows, observations, NOW, ReconcileConfig(), storage=MIGRATING)

    assert chosen == {ids[0], ids[1], ids[3]}


def test_a_local_cells_quota_admits_the_clone_and_one_backup_job_beside_the_serving_pod() -> None:
    from cellctl.manifests import CellManifestSpec, render_resource_quota

    spec = CellManifestSpec(cell_id="aaaaaaaaaaaaaaaa", image=IMAGE_A, replicas=1, read_only=False, storage_gib=10,
                            storage_class=LOCAL.class_name, local_volume=True)

    hard = render_resource_quota(spec)["spec"]["hard"]

    # Serving 250m/1Gi requests and a 2-CPU limit, plus the Job's 100m/256Mi and 1 CPU.
    assert {key: hard[key] for key in ("persistentvolumeclaims", "requests.storage", "requests.cpu",
                                       "requests.memory", "limits.cpu")} == {
        "persistentvolumeclaims": "2", "requests.storage": "20Gi", "requests.cpu": "350m",
        "requests.memory": "1280Mi", "limits.cpu": "3",
    }


def test_the_backup_job_reads_the_clone_read_only_and_carries_no_node_selector() -> None:
    # D3: the clone's PV is pinned to the cell's node, so the Job follows it.
    from cellctl.manifests import (
        CellManifestSpec,
        render_backup_job,
        render_clone_claim,
        render_volume_snapshot,
    )

    spec = CellManifestSpec(cell_id="aaaaaaaaaaaaaaaa", image=IMAGE_A, replicas=1, read_only=False,
                            storage_class=LOCAL.class_name, local_volume=True, hold_kind="snapshot-backup",
                            hold_started_at=STARTED.isoformat())
    snapshot = render_volume_snapshot(spec, snapshot_class=LOCAL.snapshot_class)
    clone = render_clone_claim(spec, clone_class=LOCAL.clone_class)
    job = render_backup_job(spec, bucket_name="b", endpoint="https://s3.example", claim_name=clone["metadata"]["name"],
                            retention=None)

    assert snapshot["spec"] == {"volumeSnapshotClassName": LOCAL.snapshot_class,
                                "source": {"persistentVolumeClaimName": "cell-data"}}
    assert clone["spec"]["storageClassName"] == LOCAL.clone_class
    assert clone["spec"]["dataSourceRef"] == {"apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot",
                                              "name": snapshot["metadata"]["name"]}
    pod = job["spec"]["template"]["spec"]
    assert pod["volumes"][0]["persistentVolumeClaim"] == {"claimName": clone["metadata"]["name"], "readOnly": True}
    assert "nodeSelector" not in pod and "affinity" not in pod
    assert "forget" not in pod["containers"][0]["command"][2]


def test_the_current_holds_snapshot_and_clone_are_observed_by_name() -> None:
    from types import SimpleNamespace as NS

    from kubernetes.client.rest import ApiException

    from cellctl.manifests import CLONE_CLAIM_NAME, SNAPSHOT_NAME, hold_job_name

    from .test_k8s_client import CELL_ID, CURRENT_HOLD, NAMESPACE, _client

    snapshot_name, clone_name = hold_job_name(SNAPSHOT_NAME, CURRENT_HOLD), hold_job_name(CLONE_CLAIM_NAME, CURRENT_HOLD)

    class Custom:
        def get_namespaced_custom_object(self, group, version, namespace, plural, name):
            if (group, plural, namespace, name) != ("snapshot.storage.k8s.io", "volumesnapshots", NAMESPACE, snapshot_name):
                raise ApiException(status=404)
            return {"status": {"readyToUse": True, "creationTime": "2026-01-01T11:59:00Z"}}

    client = _client(hold_started_at=CURRENT_HOLD, jobs={}, pods=[])
    client._apps._statefulset.metadata.annotations["exomem.io/hold"] = "snapshot-backup"
    client._custom = Custom()

    def read_claim(name, namespace):
        if (name, namespace) != (clone_name, NAMESPACE):
            raise ApiException(status=404)
        return NS(status=NS(phase="Bound"), metadata=NS(deletion_timestamp=None))

    client._core.read_namespaced_persistent_volume_claim = read_claim

    observed = client.observe(CELL_ID, NAMESPACE)

    assert (observed.snapshot_exists, observed.snapshot_ready, observed.clone_exists, observed.clone_bound) == (True,) * 4
    assert observed.snapshot_created_at == datetime(2026, 1, 1, 11, 59, tzinfo=UTC)


async def test_an_hourly_backup_through_the_controller_records_the_backup_and_leaves_no_clone(cell_db: CellDatabase) -> None:
    from cellctl import db
    from cellctl.manifests import BACKUP_OUTCOME_ANNOTATION, HOLD_ANNOTATION

    from .test_reconcile import _observed_from_applied

    class Gateway(FakeClusterGateway):
        def __init__(self) -> None:
            super().__init__()
            self.cleaned: list[tuple[str, str, str]] = []

        def delete_snapshot_backup(self, namespace: str, snapshot: str, claim: str) -> None:
            self.cleaned.append((namespace, snapshot, claim))

    cell_id, namespace = "aaaaaaaaaaaaaaaa", namespace_name("aaaaaaaaaaaaaaaa")
    await _seed_cell(cell_db, cell_id, "tenant-a")
    cluster = Gateway()
    config = ClusterConfig(object_storage_bucket="b", storage=CUT_OVER)
    local = dict(pv_storage_class=LOCAL.class_name, pvc_storage_class=LOCAL.class_name, pv_node="agent-1")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))

    def observe(**overrides) -> None:
        statefulset = cluster.applied[(namespace, "StatefulSet", "cell")]
        outcome = statefulset["metadata"]["annotations"].get(BACKUP_OUTCOME_ANNOTATION)
        cluster.observations[cell_id] = _observed_from_applied(
            cluster, cell_id, **local, statefulset_backup_outcome=outcome, **overrides)

    async def run(minute: int) -> None:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                       config, now=NOW + timedelta(minutes=minute))

    try:
        await run(0)
        observe()
        await run(1)  # converged and running; never backed up, so due at once
        observe()
        await run(2)
        statefulset = cluster.applied[(namespace, "StatefulSet", "cell")]
        assert (statefulset["metadata"]["annotations"][HOLD_ANNOTATION], statefulset["spec"]["replicas"]) == (
            "snapshot-backup", 1)
        snapshot = cluster.jobs[-1]
        assert snapshot["kind"] == "VolumeSnapshot"

        observe(snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True)
        await run(3)
        job = cluster.jobs[-1]
        clone_name = job["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
        assert job["kind"] == "Job" and clone_name.startswith("cell-clone-")

        observe(snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True,
                backup_job_succeeded=True, backup_job_snapshot_id=SNAPSHOT_ID)
        await run(4)
        assert cluster.cleaned == [(namespace, snapshot["metadata"]["name"], clone_name)]
        secret = cluster.applied[(namespace, "Secret", "cell-credentials")]
        assert "backed-up" in secret["data"]

        observe()  # the clone and snapshot are gone
        await run(5)
        statefulset = cluster.applied[(namespace, "StatefulSet", "cell")]
        assert HOLD_ANNOTATION not in statefulset["metadata"]["annotations"]
        row = (await db.select_all_rows(connection))[0]
        assert (row.last_backup_snapshot, row.hold_kind) == (SNAPSHOT_ID, None)
    finally:
        await connection.close()


# --- 2.5: the deletion proof on local storage -------------------------------------


def _remaining(**overrides):
    from cellctl.storage.topolvm import LocalVolumeState, LogicalVolume

    defaults = dict(
        claimed_pvs=(), snapshot_contents=(),
        volumes=(LogicalVolume(name="lv-cell", volume_id="vol-1", node="agent-1"),
                 LogicalVolume(name="lv-snap", volume_id="vol-2", node="agent-1", source="lv-cell"),
                 LogicalVolume(name="lv-other", volume_id="vol-9", node="agent-1")),
        present_nodes=frozenset({"agent-1"}),
    )
    defaults.update(overrides)
    return LocalVolumeState(**defaults)


@pytest.mark.parametrize("kept", ["volume", "snapshot volume", "snapshot content", "clone pv"])
def test_a_deleted_cells_local_data_is_absent_only_when_its_volume_snapshots_and_clones_are(kept: str) -> None:
    from cellctl.storage.topolvm import LogicalVolume, cell_data_absent

    namespace = "exo-cell-aaaaaaaaaaaaaaaa"
    other = LogicalVolume(name="lv-other", volume_id="vol-9", node="agent-1")
    state = {
        "volume": _remaining(volumes=(LogicalVolume(name="lv-cell", volume_id="vol-1", node="agent-1"), other)),
        "snapshot volume": _remaining(volumes=(LogicalVolume(name="lv-snap", volume_id="vol-2", node="agent-1",
                                                             source="lv-cell"),
                                               LogicalVolume(name="lv-cell", volume_id="vol-1", node="agent-1"))),
        "snapshot content": _remaining(volumes=(other,), snapshot_contents=((namespace, None),)),
        "clone pv": _remaining(volumes=(other,), claimed_pvs=((namespace, "agent-1", "vol-3"),)),
    }[kept]

    assert cell_data_absent(state, namespace=namespace, volume_id="vol-1") is False
    assert cell_data_absent(_remaining(volumes=(other,)), namespace=namespace, volume_id="vol-1") is True


def test_a_deleted_cells_volume_on_a_node_gone_from_the_api_counts_as_absent() -> None:
    # D4: a retained PV and its logical volume on a node removed from the
    # cluster can never be cleaned by the driver; they hold no reachable data.
    from cellctl.storage.topolvm import cell_data_absent

    namespace = "exo-cell-aaaaaaaaaaaaaaaa"
    on_removed_node = _remaining(claimed_pvs=((namespace, "agent-1", "vol-old"),), present_nodes=frozenset({"agent-2"}))

    assert cell_data_absent(on_removed_node, namespace=namespace, volume_id="vol-1") is True


def test_a_volume_retained_on_a_stopped_node_still_in_the_cluster_blocks_the_deletion_proof() -> None:
    # A node stopped for repair can rejoin with its disk. Until it is removed
    # from the cluster, a relocated cell's old volume there is still its data,
    # and its backups and keys must not go.
    from types import SimpleNamespace as NS

    from cellctl.k8s_client import ClusterClient
    from cellctl.storage.topolvm import cell_data_absent

    namespace = "exo-cell-aaaaaaaaaaaaaaaa"
    affinity = NS(required=NS(node_selector_terms=[NS(match_expressions=[
        NS(key=LOCAL.topology_key, operator="In", values=["agent-1"])], match_fields=None)]))
    retained = NS(metadata=NS(name="pv-old"), spec=NS(
        claim_ref=NS(namespace=namespace, uid="uid-old"), storage_class_name=LOCAL.class_name,
        csi=NS(volume_handle="vol-old"), node_affinity=affinity, persistent_volume_reclaim_policy="Retain"))
    stopped = NS(metadata=NS(name="agent-1"), spec=NS(taints=[NS(key="node.kubernetes.io/out-of-service",
                                                                 effect="NoExecute")]),
                 status=NS(conditions=[NS(type="Ready", status="Unknown")]))

    class Core:
        def list_node(self):
            return NS(items=[stopped])

        def list_persistent_volume(self):
            return NS(items=[retained])

    class Custom:
        def list_cluster_custom_object(self, group, version, plural):
            return {"items": []}

    client = ClusterClient.__new__(ClusterClient)
    client._core, client._custom = Core(), Custom()

    state = client.local_volume_state(LOCAL)

    assert cell_data_absent(state, namespace=namespace, volume_id="vol-new") is False


def test_with_local_storage_configured_a_deleting_cell_waits_for_its_logical_volume() -> None:
    from cellctl.reconcile import _augment_deletion_observation
    from cellctl.storage.topolvm import LogicalVolume

    cluster = FakeClusterGateway()
    cluster.local_volume_state = lambda local: _remaining(volumes=(LogicalVolume(name="lv", volume_id="vol-1", node="agent-1"),))

    observation = _augment_deletion_observation(ClusterObservation(), _row(desired_state="deleted", volume_id="vol-1"),
                                                cluster, FakeB2(), FakeHetznerVolumeProvider(), storage=MIGRATING)

    assert observation.pv_absent_confirmed is False


# --- 2.4: operator-triggered relocation -------------------------------------------------

LAST_BACKUP = "d" * 64
LOST = dict(pv_name="pv-old", pvc_volume_id="vol-old", pvc_exists=True, pv_node_stop_confirmed=True, pv_node_lost=True,
            pod_exists=False, pod_ready=False)


def _relocating(**overrides) -> ClusterObservation:
    defaults = dict(statefulset_hold_kind="restore", statefulset_hold_started_at=STARTED,
                    statefulset_relocation_volume="vol-old", statefulset_pre_upgrade_snapshot=LAST_BACKUP,
                    statefulset_previous_image=IMAGE_A, statefulset_replicas=0, **LOST)
    defaults.update(overrides)
    return _local(**defaults)


def _relocated_row(**overrides) -> CellRow:
    defaults = dict(volume_id="vol-old", node="agent-1", last_backup_at=STARTED, last_backup_snapshot=LAST_BACKUP,
                    hold_kind="restore", hold_started_at=STARTED)
    defaults.update(overrides)
    return _row(**defaults)


def test_a_cell_whose_node_is_confirmed_stopped_is_relocated_from_its_last_backup() -> None:
    decision = decide(_row(volume_id="vol-old", last_backup_at=STARTED, last_backup_snapshot=LAST_BACKUP), RolloutRow(),
                      _local(**LOST), now=NOW, cell_image=IMAGE_A, start_upgrade=False, start_relocation=True,
                      storage=MIGRATING)

    assert (decision.hold_kind, decision.replicas) == ("restore", 0)
    assert (decision.pre_upgrade_snapshot, decision.relocation_volume) == (LAST_BACKUP, "vol-old")


def test_a_cell_with_no_backup_is_not_relocated_onto_an_empty_volume() -> None:
    decision = _step(_local(**LOST), _row(volume_id="vol-old"))

    assert decision.hold_kind is None
    assert decision.row_updates["last_error_code"] == "RELOCATION_NO_BACKUP"


@pytest.mark.parametrize("storage", [StorageConfig(), MIGRATING], ids=["hetzner", "local"])
def test_a_cell_whose_recorded_volume_has_no_claim_is_never_given_a_fresh_one(storage: StorageConfig) -> None:
    # A new claim is a new empty volume, and a cell without the D5 guard
    # would initialise a blank vault over the tenant's.
    row = _row(volume_id="vol-1", last_backup_at=STARTED, last_backup_snapshot=LAST_BACKUP, ready=False)
    missing = ClusterObservation(namespace_exists=True, namespace_cell_label=row.cell_id, statefulset_exists=True,
                                 statefulset_image=IMAGE_A, statefulset_replicas=1, statefulset_row_generation=1)

    decision = decide(row, RolloutRow(), missing, now=NOW, cell_image=IMAGE_A, start_upgrade=False, storage=storage)

    assert decision.apply_manifests is False
    assert decision.row_updates["last_error_code"] == "VOLUME_MISSING"


def test_a_cell_whose_volume_the_operator_marked_lost_is_relocated_from_its_last_backup() -> None:
    row = _row(volume_id="vol-1", last_backup_at=STARTED, last_backup_snapshot=LAST_BACKUP)
    marked = ClusterObservation(namespace_exists=True, namespace_cell_label=row.cell_id, statefulset_exists=True,
                                statefulset_image=IMAGE_A, statefulset_replicas=1, statefulset_row_generation=1,
                                namespace_volume_lost="vol-1")

    decision = decide(row, RolloutRow(), marked, now=NOW, cell_image=IMAGE_A, start_upgrade=False,
                      start_relocation=True, storage=MIGRATING)

    assert (decision.hold_kind, decision.replicas, decision.relocation_volume) == ("restore", 0, "vol-1")
    # A mark naming any other volume is stale and moves nothing.
    stale = dataclasses.replace(marked, namespace_volume_lost="vol-0")
    assert decide(row, RolloutRow(), stale, now=NOW, cell_image=IMAGE_A, start_upgrade=False, start_relocation=True,
                  storage=MIGRATING).row_updates["last_error_code"] == "VOLUME_MISSING"


def test_the_owner_relocates_alone_then_the_rest_a_bounded_number_at_a_time() -> None:
    from cellctl.decide import DEFAULT_RECONCILE_CONFIG
    from cellctl.reconcile import _select_relocation_candidates

    config = dataclasses.replace(DEFAULT_RECONCILE_CONFIG, relocation_concurrency=2)
    owner, broken_id, *tenants = ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc", "dddddddddddddddd",
                                  "eeeeeeeeeeeeeeee")
    rows = [_row(cell_id=owner, rollout_priority=0, last_backup_snapshot=LAST_BACKUP),
            # Its recorded snapshot id is not a restic id: never a candidate, never in the way.
            _row(cell_id=broken_id, rollout_priority=1, last_backup_snapshot="latest"),
            *[_row(cell_id=cell_id, rollout_priority=2, last_backup_snapshot=LAST_BACKUP) for cell_id in tenants]]
    lost = {row.cell_id: _local(**LOST) for row in rows}

    assert _select_relocation_candidates(rows, lost, MIGRATING, config) == {owner}
    owner_restoring = {**lost, owner: _relocating()}
    assert _select_relocation_candidates(rows, owner_restoring, MIGRATING, config) == set()
    owner_done = {**lost, owner: _local()}
    assert _select_relocation_candidates(rows, owner_done, MIGRATING, config) == set(tenants[:2])
    one_running = {**owner_done, tenants[0]: _relocating()}
    assert _select_relocation_candidates(rows, one_running, MIGRATING, config) == {tenants[1]}


def test_a_node_briefly_gone_from_the_api_is_not_lost() -> None:
    # `kubectl delete node` on a healthy agent: its kubelet registers the node
    # again within seconds, and its cells must stay where they are. Each
    # return restarts the clock.
    memory = reconcile.LoopMemory()
    gone = {"aaaaaaaaaaaaaaaa": _local(pv_node_absent=True)}
    back = {"aaaaaaaaaaaaaaaa": _local()}

    for minute, observations in ((0, gone), (4, gone), (5, back), (6, gone), (10, gone)):
        decided = reconcile._with_lost_nodes(observations, memory, NOW + timedelta(minutes=minute))
        assert decided["aaaaaaaaaaaaaaaa"].pv_node_lost is False, minute


def test_a_node_gone_from_the_api_for_five_minutes_is_lost() -> None:
    memory = reconcile.LoopMemory()
    gone = {"aaaaaaaaaaaaaaaa": _local(pv_node_absent=True)}

    reconcile._with_lost_nodes(gone, memory, NOW)
    decided = reconcile._with_lost_nodes(gone, memory, NOW + reconcile.NODE_ABSENCE_GRACE)

    assert decided["aaaaaaaaaaaaaaaa"].pv_node_lost is True


def test_a_deleted_cells_volume_on_a_node_briefly_gone_still_blocks_the_deletion_proof() -> None:
    # The same re-registration must not let a deleted cell report deleted
    # while its volume is still on that agent's disk.
    from cellctl.storage.topolvm import cell_data_absent

    namespace = "exo-cell-aaaaaaaaaaaaaaaa"
    state = _remaining(claimed_pvs=((namespace, "agent-1", "vol-old"),), present_nodes=frozenset({"agent-2"}))
    memory = reconcile.LoopMemory()

    just_gone = reconcile._with_recent_absences(state, NOW, memory)
    assert cell_data_absent(just_gone, namespace=namespace, volume_id="vol-1") is False
    long_gone = reconcile._with_recent_absences(state, NOW + reconcile.NODE_ABSENCE_GRACE, memory)
    assert cell_data_absent(long_gone, namespace=namespace, volume_id="vol-1") is True


async def test_no_relocation_starts_while_a_cell_is_unobserved(cell_db: CellDatabase) -> None:
    # The unobserved cell may be the owner's, which must relocate first.
    from cellctl import db
    from cellctl.manifests import RELOCATION_ANNOTATION

    owner, tenant = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"

    class Gateway(FakeClusterGateway):
        def observe(self, cell_id: str, namespace: str) -> ClusterObservation:
            if cell_id == owner:
                raise RuntimeError("observe failed")
            return super().observe(cell_id, namespace)

    await _seed_cell(cell_db, owner, "tenant-a")
    await _seed_cell(cell_db, tenant, "tenant-b")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    await db.write_observed(connection, tenant, {
        "observed_state": "running", "observed_generation": 1, "observed_image": IMAGE_A, "volume_id": "vol-old",
        "node": "agent-1", "last_backup_at": STARTED, "last_backup_snapshot": LAST_BACKUP})
    cluster = Gateway()
    cluster.observations[tenant] = _bound(LOCAL.class_name, statefulset_row_generation=1, **LOST, pv_node="agent-1",
                                          pv_reclaim_policy="Delete", namespace_cell_label=tenant)
    try:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                       ClusterConfig(object_storage_bucket="b", storage=MIGRATING), now=NOW)
    finally:
        await connection.close()

    # The tenant still converges where it is; it only does not start relocating.
    statefulset = cluster.applied[(namespace_name(tenant), "StatefulSet", "cell")]
    assert RELOCATION_ANNOTATION not in statefulset["metadata"].get("annotations", {})


async def test_a_refused_relocation_step_ends_that_relocation_and_frees_the_fleet(cell_db: CellDatabase) -> None:
    # A refused Retain patch never succeeds on its own; an owner stuck in it
    # would hold back every other cell's relocation forever.
    from kubernetes.client.rest import ApiException

    from cellctl import db
    from cellctl.decide import DEFAULT_RECONCILE_CONFIG
    from cellctl.reconcile import _select_relocation_candidates
    from cellctl.state import RELOCATION_REFUSED

    from .test_reconcile import _observed_from_applied

    class Gateway(FakeClusterGateway):
        def retain_volume(self, name: str) -> None:
            raise ApiException(status=422, reason="Invalid")

    owner, tenant = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    await _seed_cell(cell_db, owner, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    await db.write_observed(connection, owner, {
        "observed_state": "running", "observed_generation": 1, "observed_image": IMAGE_A, "volume_id": "vol-old",
        "node": "agent-1", "last_backup_at": STARTED, "last_backup_snapshot": LAST_BACKUP})
    cluster = Gateway()
    lost = dict(pv_storage_class=LOCAL.class_name, pvc_storage_class=LOCAL.class_name, pv_node="agent-1", **LOST)
    cluster.observations[owner] = _bound(LOCAL.class_name, statefulset_row_generation=1, **lost,
                                         pv_reclaim_policy="Delete")

    async def run(minute: int) -> None:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                       ClusterConfig(object_storage_bucket="b", storage=MIGRATING),
                                       now=NOW + timedelta(minutes=minute))

    try:
        await run(0)
        cluster.observations[owner] = _observed_from_applied(
            cluster, owner, **lost, statefulset_relocation_volume="vol-old", pv_reclaim_policy="Delete",
            pod_uses_volume=False)
        await run(1)
        (row,) = await db.select_all_rows(connection)
    finally:
        await connection.close()

    assert (row.last_error_code, row.volume_id) == (RELOCATION_REFUSED, "vol-old")
    rows = [row, _row(cell_id=tenant, rollout_priority=row.rollout_priority + 1, last_backup_snapshot=LAST_BACKUP)]
    observations = {owner: cluster.observations[owner], tenant: _local(**LOST)}
    assert _select_relocation_candidates(rows, observations, MIGRATING, DEFAULT_RECONCILE_CONFIG) == {tenant}


def test_relocation_retains_the_old_volume_before_it_deletes_the_claim() -> None:
    dead_pod = _step(_relocating(pod_exists=True, pod_uses_volume=True, pv_reclaim_policy="Delete"), _relocated_row())
    assert (dead_pod.retain_volume, dead_pod.delete_claim) == (None, False)

    retain = _step(_relocating(pv_reclaim_policy="Delete"), _relocated_row())
    assert (retain.retain_volume, retain.delete_claim) == ("pv-old", False)

    delete = _step(_relocating(pv_reclaim_policy="Retain"), _relocated_row())
    assert (delete.retain_volume, delete.delete_claim) == (None, True)
    assert (delete.row_updates["volume_id"], delete.row_updates["node"]) == (None, None)


def test_a_relocated_cell_starts_only_after_its_restore_succeeds() -> None:
    claim_gone = dict(pvc_exists=False, pvc_bound=False, pvc_volume_id=None, pv_name=None, pv_node=None,
                      pv_node_lost=False)
    row = _relocated_row(volume_id=None, node=None)

    restoring = _step(_relocating(**claim_gone), row)
    assert (restoring.run_restore_job_snapshot, restoring.replicas, restoring.relocation_volume) == (
        LAST_BACKUP, 0, "vol-old")

    failed = _step(_relocating(**claim_gone, restore_job_failed=True), row)
    assert (failed.replicas, failed.hold_kind, failed.row_updates["last_error_code"]) == (0, "restore", "RESTORE_FAILED")
    assert (failed.retain_volume, failed.delete_claim) == (None, False)

    restored = _step(_relocating(**claim_gone, restore_job_succeeded=True), row)
    starting = _step(_relocating(**claim_gone, statefulset_restored_snapshot=LAST_BACKUP), row)
    assert (restored.replicas, starting.replicas) == (0, 1)


@pytest.mark.parametrize(
    ("taints", "ready", "confirmed"),
    [
        ([("node.kubernetes.io/out-of-service", "NoExecute")], "False", True),
        ([], "Unknown", False),  # only partitioned: never relocated
        ([("node.kubernetes.io/out-of-service", "NoExecute")], "True", False),  # alive: a mistaken taint
    ],
)
def test_a_node_counts_as_stopped_only_when_tainted_out_of_service_and_not_ready(taints, ready, confirmed) -> None:
    from types import SimpleNamespace as NS

    from cellctl.k8s_client import _stop_confirmed

    node = NS(spec=NS(taints=[NS(key=key, effect=effect) for key, effect in taints]),
              status=NS(conditions=[NS(type="Ready", status=ready)]))

    assert _stop_confirmed(node) is confirmed


async def test_a_relocation_through_the_controller_retains_the_old_volume_and_restores_into_a_local_claim(
    cell_db: CellDatabase,
) -> None:
    from cellctl import db
    from cellctl.manifests import HOLD_ANNOTATION, RELOCATION_ANNOTATION

    from .test_reconcile import _observed_from_applied

    class Gateway(FakeClusterGateway):
        def __init__(self) -> None:
            super().__init__()
            self.volume_writes: list[tuple[str, str]] = []

        def retain_volume(self, name: str) -> None:
            self.volume_writes.append(("retain", name))

        def delete_claim(self, namespace: str) -> None:
            self.volume_writes.append(("delete claim", namespace))

    cell_id, namespace = "aaaaaaaaaaaaaaaa", namespace_name("aaaaaaaaaaaaaaaa")
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    await db.write_observed(connection, cell_id, {
        "observed_state": "running", "observed_generation": 1, "observed_image": IMAGE_A, "volume_id": "vol-old",
        "node": "agent-1", "last_backup_at": STARTED, "last_backup_snapshot": LAST_BACKUP})
    cluster = Gateway()
    # Hetzner volumes are still the domain; the relocated claim must be local anyway.
    config = ClusterConfig(object_storage_bucket="b", storage=MIGRATING)
    local = dict(pv_storage_class=LOCAL.class_name, pvc_storage_class=LOCAL.class_name, pv_node="agent-1")
    cluster.observations[cell_id] = _bound(LOCAL.class_name, statefulset_row_generation=1, **LOST, pv_node="agent-1",
                                           pv_reclaim_policy="Delete")

    def observe(**overrides) -> None:
        annotations = cluster.applied[(namespace, "StatefulSet", "cell")]["metadata"]["annotations"]
        cluster.observations[cell_id] = _observed_from_applied(
            cluster, cell_id, **{**local, **LOST, "statefulset_relocation_volume": annotations.get(RELOCATION_ANNOTATION),
                                 **overrides})

    async def run(minute: int) -> None:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                       config, now=NOW + timedelta(minutes=minute))

    try:
        await run(0)
        annotations = cluster.applied[(namespace, "StatefulSet", "cell")]["metadata"]["annotations"]
        assert (annotations[HOLD_ANNOTATION], annotations[RELOCATION_ANNOTATION]) == ("restore", "vol-old")

        observe(pv_reclaim_policy="Delete", pod_exists=False, pod_uses_volume=False, pod_ready=False)
        await run(1)
        observe(pv_reclaim_policy="Retain", pod_exists=False, pod_uses_volume=False, pod_ready=False)
        await run(2)
        assert cluster.volume_writes == [("retain", "pv-old"), ("delete claim", namespace)]
        assert (await db.select_all_rows(connection))[0].volume_id is None

        observe(pvc_exists=False, pvc_bound=False, pvc_volume_id=None, pv_name=None, pv_node=None,
                pv_node_lost=False, pvc_storage_class=None, pod_exists=False, pod_uses_volume=False,
                pod_ready=False)
        await run(3)
        claim = cluster.applied[(namespace, "PersistentVolumeClaim", "cell-data")]
        assert claim["spec"]["storageClassName"] == LOCAL.class_name
        assert cluster.jobs[-1]["metadata"]["labels"]["exomem.io/cell-job"] == "restore"
    finally:
        await connection.close()


@pytest.mark.parametrize("storage,skipped", [(StorageConfig(), False), (MIGRATING, True)], ids=["hetzner", "local"])
async def test_with_local_storage_a_pass_needs_the_guard_that_keeps_claim_deletes_to_retained_volumes(
    cell_db: CellDatabase, storage: StorageConfig, skipped: bool
) -> None:
    # Relocation deletes a cell's claim, and only admission keeps that to a
    # retained volume. A Hetzner-only chart renders no such guard, so
    # requiring it there would stop every pass.
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    cluster.admission_missing = {reconcile.RETAINED_DELETE_POLICY_NAME}
    try:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                       ClusterConfig(object_storage_bucket="b", storage=storage), now=NOW)
        assert (cluster.applied == {}) is skipped
    finally:
        await connection.close()


# --- 2.8: the backup-age alert ----------------------------------------------------------

@pytest.mark.parametrize(
    ("storage_class", "age", "stale"),
    [(LOCAL.class_name, timedelta(hours=2, minutes=1), True),
     (STORAGE_CLASS, timedelta(hours=2, minutes=1), False),
     (STORAGE_CLASS, timedelta(hours=26, minutes=1), True)],
    ids=["hourly-past-2h", "nightly-at-2h", "nightly-past-26h"],
)
def test_a_running_cells_backup_is_stale_past_its_own_schedule(storage_class, age, stale) -> None:
    from cellctl.alerts import stale_backups

    row = _row(last_backup_at=NOW - age)
    observations = {row.cell_id: _bound(storage_class)}

    assert stale_backups([row], observations, NOW, storage=MIGRATING) == ([row.cell_id] if stale else [])


def test_a_cell_never_backed_up_is_stale_from_its_creation() -> None:
    # The case that matters most: backups that never worked leave no
    # last_backup_at to age.
    from cellctl.alerts import stale_backups

    row = _row(last_backup_at=None, created_at=NOW - timedelta(hours=3))

    assert stale_backups([row], {row.cell_id: _bound(LOCAL.class_name)}, NOW, storage=MIGRATING) == [row.cell_id]


def test_the_alert_is_the_receivers_exact_transition_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    # Substrate's receiver answers anything else with 400, and the alert
    # would never reach anyone.
    import json
    import re

    from cellctl import alerts

    sent = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def geturl(self):
            return "https://receiver.example/api/exomem/alerts/token"

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            return Response()

    monkeypatch.setattr(alerts.urllib.request, "build_opener", lambda *handlers: Opener())

    alerts.deliver("https://receiver.example/api/exomem/alerts/token", alert=alerts.BACKUP_ALERT, active=True,
                   observed_at=NOW)

    (request,) = sent
    body = json.loads(request.data)
    assert sorted(body) == ["active", "alert", "job", "schema_version", "transition_id"]
    assert body["schema_version"] == 1 and body["active"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", body["transition_id"])
    assert request.get_header("X-exomem-alert-transition") == body["transition_id"]
    for label in (body["job"], body["alert"]):
        assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", label)


async def test_a_stale_backup_fires_once_and_resolves_once_through_the_alert_receiver(
    cell_db: CellDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cellctl import alerts, db

    delivered: list[tuple[str, bool]] = []

    def deliver(url: str, *, alert: str, active: bool, observed_at: datetime) -> None:
        if alert == alerts.BACKUP_ALERT:
            delivered.append((url, active))

    monkeypatch.setattr(alerts, "deliver", deliver)

    class Gateway(FakeClusterGateway):
        def read_secret_value(self, namespace: str, name: str, key: str) -> str:
            assert (namespace, name, key) == ("exomem-platform", "exomem-hosted-alert-delivery", "url")
            return "https://receiver.example/alerts/token"

    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = Gateway()
    cluster.observations[cell_id] = _bound(LOCAL.class_name, statefulset_row_generation=1)
    config = ClusterConfig(object_storage_bucket="b", storage=MIGRATING,
                           alert_delivery_secret=("exomem-platform", "exomem-hosted-alert-delivery", "url"))
    memory = reconcile.LoopMemory()

    async def run(at: datetime) -> None:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                       config, now=at, memory=memory)

    try:
        await db.write_observed(connection, cell_id, {
            "observed_state": "running", "observed_generation": 1, "observed_image": IMAGE_A, "ready": True,
            "last_backup_at": NOW - timedelta(hours=3)})
        await run(NOW)
        await run(NOW + timedelta(minutes=1))
        await db.write_observed(connection, cell_id, {"last_backup_at": NOW + timedelta(minutes=2)})
        await run(NOW + timedelta(minutes=3))
    finally:
        await connection.close()

    assert delivered == [("https://receiver.example/alerts/token", True), ("https://receiver.example/alerts/token", False)]


async def test_a_receiver_that_is_down_is_asked_again_a_minute_later_not_every_pass(
    cell_db: CellDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fast loop runs every few seconds; retrying each pass would hammer a
    # receiver that is already failing.
    from cellctl import alerts, db

    attempts: list[datetime] = []

    def deliver(url: str, *, alert: str, active: bool, observed_at: datetime) -> None:
        if alert != alerts.BACKUP_ALERT:
            return
        attempts.append(observed_at)
        if len(attempts) == 1:
            raise RuntimeError("alert delivery failed")

    monkeypatch.setattr(alerts, "deliver", deliver)

    class Gateway(FakeClusterGateway):
        def read_secret_value(self, namespace: str, name: str, key: str) -> str:
            return "https://receiver.example/alerts/token"

    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = Gateway()
    cluster.observations[cell_id] = _bound(LOCAL.class_name, statefulset_row_generation=1)
    config = ClusterConfig(object_storage_bucket="b", storage=MIGRATING,
                           alert_delivery_secret=("exomem-platform", "exomem-hosted-alert-delivery", "url"))
    memory = reconcile.LoopMemory()
    try:
        await db.write_observed(connection, cell_id, {
            "observed_state": "running", "observed_generation": 1, "observed_image": IMAGE_A, "ready": True,
            "last_backup_at": NOW - timedelta(hours=3)})
        for at in (NOW, NOW + timedelta(seconds=10), NOW + timedelta(seconds=61), NOW + timedelta(seconds=70)):
            await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(),
                                           _secrets_config(), config, now=at, memory=memory)
    finally:
        await connection.close()

    # Failed at NOW, skipped ten seconds later, delivered after the minute, then folded.
    assert attempts == [NOW, NOW + timedelta(seconds=61)]


# --- 2.9: reconciling volumes after an etcd restore ---------------------------------------


def test_after_an_etcd_restore_each_volume_on_disk_is_matched_to_its_row_or_reported(tmp_path) -> None:
    # Recorded the way list-cell-volumes.yml saves `lvs --reportformat json`:
    # the thin pool and its internal volumes are listed too, and only thin
    # volumes in the pool are cell, snapshot or clone volumes.
    import json

    from cellctl.readopt import classify, read_host_volumes

    def lv(name: str, attr: str = "Vwi-aotz--", pool: str = "pool0") -> dict:
        return {"lv_name": name, "lv_size": "10737418240", "pool_lv": pool, "lv_attr": attr}

    (tmp_path / "agent-1.json").write_text(json.dumps({"report": [{"lv": [
        lv("pool0", "twi-aotz--", ""), lv("[pool0_tmeta]", "ewi-ao----", ""),
        lv("vol-a"), lv("vol-b"), lv("vol-stray"), lv("vol-snapshot", "Vri---tz-k"),
    ]}]}), encoding="utf-8")

    (tmp_path / "agent-2.json").write_text(json.dumps({"report": [{"lv": [lv("vol-twice")]}]}), encoding="utf-8")
    (tmp_path / "agent-3.json").write_text(json.dumps({"report": [{"lv": [lv("vol-twice")]}]}), encoding="utf-8")

    report = classify(
        host=read_host_volumes(tmp_path, pool="pool0"),
        rows={"aaaaaaaaaaaaaaaa": ("vol-a", "agent-1"), "bbbbbbbbbbbbbbbb": ("vol-b", "agent-1"),
              "cccccccccccccccc": ("vol-gone", "agent-1"), "dddddddddddddddd": ("vol-unlisted", "agent-9"),
              "eeeeeeeeeeeeeeee": ("vol-twice", "agent-2")},
        objects={"vol-b", "vol-snapshot"},
    )

    # vol-a lost its LogicalVolume object with the older etcd: re-adopt it.
    assert report.readopt == [("aaaaaaaaaaaaaaaa", "agent-1", "vol-a")]
    assert report.in_place == ["bbbbbbbbbbbbbbbb"]
    # Its own node was listed and holds no vol-gone: that cell is relocated from its backup.
    assert report.relocate == ["cccccccccccccccc"]
    # Its node wrote no listing (unreachable): nothing is known about its volume yet.
    assert report.unverified == ["dddddddddddddddd"]
    # One name on two disks: neither may be re-adopted until an operator looks.
    assert report.conflict == [("eeeeeeeeeeeeeeee", "vol-twice", ["agent-2", "agent-3"])]
    # Neither a row nor an object claims it; only an operator on the host releases it.
    assert report.unclaimed == [("agent-1", "vol-stray")]


# --- 2.10: a local cell grows online before it fills ---------------------------------------

MEASURED_SNAPSHOT = "e" * 64
# More free bytes on agent-1 than any step and reserve these tests take.
ROOMY = StorageRoom(free_bytes={"agent-1": 200 * GIB}, largest_cell_gib=10)


def _backed_up(used: int, total: int = 100, **overrides) -> ClusterObservation:
    """The pass that sees this hold's backup Job finish, with the clone's
    filesystem use the Job reported, in bytes."""

    return _in_hold(snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True,
                    backup_job_succeeded=True, backup_job_snapshot_id=MEASURED_SNAPSHOT,
                    backup_job_used_bytes=used, backup_job_total_bytes=total, **overrides)


def _measure(observation: ClusterObservation, row: CellRow | None = None, *, storage: StorageConfig = MIGRATING,
             room: StorageRoom | None = ROOMY):
    return decide(row or _row(hold_kind="backup", hold_started_at=STARTED), RolloutRow(), observation, now=NOW,
                  cell_image=IMAGE_A, start_upgrade=False, storage=storage, storage_room=room)


def test_the_backup_jobs_use_report_is_what_cellctl_reads(tmp_path) -> None:
    # The Job and cellctl must agree on the termination message, or no cell
    # ever grows. This runs the rendered command under sh, with restic stubbed
    # and the Job's paths moved under tmp_path, and reads the message back as
    # ClusterClient.observe() does.
    import os
    import subprocess

    from cellctl.manifests import CellManifestSpec, hold_job_name, render_backup_job

    from .test_k8s_client import CELL_ID, CURRENT_HOLD, NAMESPACE, _client, _job, _job_pod

    bin_dir, data = tmp_path / "bin", tmp_path / "data"
    bin_dir.mkdir()
    (data / "vault").mkdir(parents=True)
    (data / "host").mkdir()
    restic = bin_dir / "restic"
    restic.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = backup ]; then echo '{{\"message_type\":\"summary\",\"snapshot_id\":\"{SNAPSHOT_ID}\"}}'; fi\n",
        encoding="utf-8",
    )
    restic.chmod(0o755)
    spec = CellManifestSpec(cell_id=CELL_ID, image=IMAGE_A, replicas=1, read_only=False,
                            hold_kind="snapshot-backup", hold_started_at=CURRENT_HOLD)
    shell, flag, script = render_backup_job(spec, bucket_name="b", endpoint="https://s3.example",
                                            retention=None)["spec"]["template"]["spec"]["containers"][0]["command"]
    for path, moved in (("/dev/termination-log", tmp_path / "termination-log"),
                        ("/tmp/backup.json", tmp_path / "backup.json"), ("/data", data)):
        script = script.replace(path, str(moved))

    subprocess.run([shell, flag, script], check=True, env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})

    backup = hold_job_name("cell-backup", CURRENT_HOLD)
    message = (tmp_path / "termination-log").read_text(encoding="utf-8")
    observed = _client(hold_started_at=CURRENT_HOLD, jobs={backup: _job(succeeded=True)},
                       pods=[_job_pod(backup, snapshot_id=message)]).observe(CELL_ID, NAMESPACE)
    filesystem = os.statvfs(data)
    assert observed.backup_job_snapshot_id == SNAPSHOT_ID
    assert observed.backup_job_total_bytes == filesystem.f_blocks * filesystem.f_frsize
    assert 0 < observed.backup_job_used_bytes <= observed.backup_job_total_bytes


async def test_a_local_cell_past_80_percent_use_grows_one_default_size_without_a_restart(
    cell_db: CellDatabase,
) -> None:
    # Its hourly backup measures the filesystem. The claim and its quota grow
    # once the hold's clone is gone, since the clone holds the quota's second
    # share until then; the row records the size, and the pod template, so
    # the running pod, stays as it was.
    from cellctl import db
    from cellctl.capacity import LocalCapacityObservation
    from cellctl.manifests import (
        BACKUP_OUTCOME_ANNOTATION,
        GROW_STORAGE_ANNOTATION,
        HOLD_ANNOTATION,
    )

    from .test_reconcile import _observed_from_applied

    cell_id, namespace = "aaaaaaaaaaaaaaaa", namespace_name("aaaaaaaaaaaaaaaa")
    await _seed_cell(cell_db, cell_id, "tenant-a")  # storage_gib 10
    cluster = FakeClusterGateway()
    cluster.delete_snapshot_backup = lambda namespace, snapshot, claim: None
    cluster.local_capacity_inputs = lambda local: LocalCapacityObservation(free_bytes={"agent-1": 200 * GIB})
    config = ClusterConfig(object_storage_bucket="b", storage=CUT_OVER)
    local = dict(pv_storage_class=LOCAL.class_name, pvc_storage_class=LOCAL.class_name, pv_node="agent-1")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))

    def applied(kind: str, name: str) -> dict:
        return cluster.applied[(namespace, kind, name)]

    def observe(**overrides) -> None:
        annotations = applied("StatefulSet", "cell")["metadata"]["annotations"]
        grow_to = annotations.get(GROW_STORAGE_ANNOTATION)
        cluster.observations[cell_id] = _observed_from_applied(
            cluster, cell_id, **local, statefulset_backup_outcome=annotations.get(BACKUP_OUTCOME_ANNOTATION),
            statefulset_grow_storage_gib=int(grow_to) if grow_to else None, **overrides)

    async def run(minute: int) -> None:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                       config, now=NOW + timedelta(minutes=minute))

    try:
        await run(0)
        observe()
        await run(1)
        observe()
        await run(2)  # the hourly hold starts
        template = applied("StatefulSet", "cell")["spec"]["template"]
        observe(snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True)
        await run(3)
        observe(snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True,
                backup_job_succeeded=True, backup_job_snapshot_id=MEASURED_SNAPSHOT,
                backup_job_used_bytes=9 * GIB, backup_job_total_bytes=10 * GIB)
        await run(4)  # 90% used
        assert applied("PersistentVolumeClaim", "cell-data")["spec"]["resources"]["requests"]["storage"] == "10Gi"

        observe()  # the clone and snapshot are gone
        await run(5)

        assert applied("PersistentVolumeClaim", "cell-data")["spec"]["resources"]["requests"]["storage"] == "14Gi"
        assert applied("ResourceQuota", "cell-quota")["spec"]["hard"]["requests.storage"] == "28Gi"
        statefulset = applied("StatefulSet", "cell")
        assert HOLD_ANNOTATION not in statefulset["metadata"]["annotations"]
        assert statefulset["spec"]["template"] == template
        assert (await db.select_all_rows(connection))[0].grown_storage_gib == 14
    finally:
        await connection.close()


def test_a_local_cell_at_80_percent_use_keeps_its_size() -> None:
    decision = _measure(_backed_up(used=80, total=100))

    assert decision.grow_storage_gib is None


@pytest.mark.parametrize(("size", "grown"), [(18, 20), (20, None)], ids=["clamped-to-the-cap", "at-the-cap"])
def test_a_local_cell_never_grows_past_the_configured_cap(size: int, grown: int | None) -> None:
    capped = StorageConfig(local=LocalStorage(max_cell_gib=20))

    decision = _measure(_backed_up(used=95), _row(storage_gib=size, hold_kind="backup", hold_started_at=STARTED),
                        storage=capped)

    assert decision.grow_storage_gib == grown


async def test_a_cell_whose_node_has_no_room_keeps_its_size_and_raises_the_growth_alert(
    cell_db: CellDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Growing into the node's snapshot reserve would starve the hourly backups
    # of every cell on it. 14 GiB needs its 4 GiB step plus a 56 GiB reserve
    # (2 x 14 GiB x 2 backups at once); the node publishes 59 GiB free.
    from cellctl import alerts, db
    from cellctl.capacity import LocalCapacityObservation
    from cellctl.manifests import BACKUP_OUTCOME_ANNOTATION, GROW_STORAGE_ANNOTATION

    from .test_reconcile import _observed_from_applied

    delivered: list[tuple[str, bool]] = []
    monkeypatch.setattr(alerts, "deliver",
                        lambda url, *, alert, active, observed_at: delivered.append((alert, active)))

    class Gateway(FakeClusterGateway):
        def read_secret_value(self, namespace: str, name: str, key: str) -> str:
            return "https://receiver.example/alerts/token"

        def delete_snapshot_backup(self, namespace: str, snapshot: str, claim: str) -> None:
            pass

        def local_capacity_inputs(self, local: LocalStorage) -> LocalCapacityObservation:
            return LocalCapacityObservation(free_bytes={"agent-1": 59 * GIB})

    cell_id, namespace = "aaaaaaaaaaaaaaaa", namespace_name("aaaaaaaaaaaaaaaa")
    await _seed_cell(cell_db, cell_id, "tenant-a")  # storage_gib 10
    cluster = Gateway()
    config = ClusterConfig(object_storage_bucket="b", storage=CUT_OVER,
                           alert_delivery_secret=("exomem-platform", "exomem-hosted-alert-delivery", "url"))
    local = dict(pv_storage_class=LOCAL.class_name, pvc_storage_class=LOCAL.class_name, pv_node="agent-1")
    memory = reconcile.LoopMemory()
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))

    def observe(**overrides) -> None:
        annotations = cluster.applied[(namespace, "StatefulSet", "cell")]["metadata"]["annotations"]
        grow_to = annotations.get(GROW_STORAGE_ANNOTATION)
        cluster.observations[cell_id] = _observed_from_applied(
            cluster, cell_id, **local, statefulset_backup_outcome=annotations.get(BACKUP_OUTCOME_ANNOTATION),
            statefulset_grow_storage_gib=int(grow_to) if grow_to else None, **overrides)

    async def run(minute: int) -> None:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                       config, now=NOW + timedelta(minutes=minute), memory=memory)

    try:
        await run(0)
        observe()
        await run(1)
        observe()
        await run(2)
        observe(snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True)
        await run(3)
        observe(snapshot_exists=True, snapshot_ready=True, clone_exists=True, clone_bound=True,
                backup_job_succeeded=True, backup_job_snapshot_id=MEASURED_SNAPSHOT,
                backup_job_used_bytes=9 * GIB, backup_job_total_bytes=10 * GIB)
        await run(4)
        observe()
        await run(5)

        claim = cluster.applied[(namespace, "PersistentVolumeClaim", "cell-data")]
        assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"
        assert (await db.select_all_rows(connection))[0].grown_storage_gib is None
        assert [active for alert, active in delivered if alert == alerts.GROWTH_ALERT][-1:] == [True]
    finally:
        await connection.close()


def test_after_a_restart_the_growth_alert_waits_until_every_local_cell_is_measured_again() -> None:
    # The verdicts live in cellctl's memory. Resolving the alert before every
    # serving local cell has been measured again would send a false
    # "resolved", then a fresh "firing" an hour later.
    from cellctl.alerts import growth_blocked

    rows = [_row(cell_id="aaaaaaaaaaaaaaaa"), _row(cell_id="bbbbbbbbbbbbbbbb")]
    observations = {row.cell_id: _local() for row in rows}

    assert growth_blocked(rows, observations, {}, storage=MIGRATING) is None
    assert growth_blocked(rows, observations, {"aaaaaaaaaaaaaaaa": "fits"}, storage=MIGRATING) is None
    assert growth_blocked(rows, observations, {"aaaaaaaaaaaaaaaa": "no-room"}, storage=MIGRATING) is True
    assert growth_blocked(rows, observations, {row.cell_id: "fits" for row in rows}, storage=MIGRATING) is False


def test_one_backup_grows_a_cell_at_most_once() -> None:
    # The Job reports the same use on every later pass of the hold, and a pass
    # whose apply was refused runs again. Only the pass that records the
    # outcome plans a size, and the hold's exit records it once.
    cleaning = _measure(_backed_up(used=95, statefulset_backup_outcome=MEASURED_SNAPSHOT,
                                   statefulset_grow_storage_gib=14))
    assert cleaning.grow_storage_gib == 14

    exit_pass = _in_hold(statefulset_backup_outcome=MEASURED_SNAPSHOT, statefulset_grow_storage_gib=14,
                         backup_job_succeeded=True, backup_job_snapshot_id=MEASURED_SNAPSHOT,
                         backup_job_used_bytes=95, backup_job_total_bytes=100)
    assert _measure(exit_pass).row_updates["grown_storage_gib"] == 14
    # The exit again, its apply refused after the row recorded 14 GiB.
    grown = _row(grown_storage_gib=14, hold_kind="backup", hold_started_at=STARTED)
    assert "grown_storage_gib" not in _measure(exit_pass, grown).row_updates


async def test_a_grown_cell_never_renders_a_claim_smaller_than_its_grown_size(cell_db: CellDatabase) -> None:
    # Kubernetes refuses a smaller claim, so a later routine apply rendered at
    # storage_gib would leave the cell refused until its size changed.
    from cellctl import db

    from .test_reconcile import _observed_from_applied, _set_storage

    cell_id, namespace = "aaaaaaaaaaaaaaaa", namespace_name("aaaaaaaaaaaaaaaa")
    await _seed_cell(cell_db, cell_id, "tenant-a")  # storage_gib 10
    cluster = FakeClusterGateway()
    config = ClusterConfig(object_storage_bucket="b", storage=MIGRATING)
    local = dict(pv_storage_class=LOCAL.class_name, pvc_storage_class=LOCAL.class_name, pv_node="agent-1")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))

    async def run(minute: int) -> None:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                       config, now=NOW + timedelta(minutes=minute))

    try:
        await run(0)
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id, **local)
        await db.write_observed(connection, cell_id, {"grown_storage_gib": 14})
        await _set_storage(cell_db, cell_id, 10, desired_state="read_only")  # a routine re-apply
        await run(1)

        claim = cluster.applied[(namespace, "PersistentVolumeClaim", "cell-data")]
        quota = cluster.applied[(namespace, "ResourceQuota", "cell-quota")]
        assert claim["spec"]["resources"]["requests"]["storage"] == "14Gi"
        assert quota["spec"]["hard"]["requests.storage"] == "28Gi"
    finally:
        await connection.close()


async def test_a_grown_cell_takes_the_slots_and_reserve_of_its_grown_size(cell_db: CellDatabase) -> None:
    # Admission counts each cell as one row, so capacity alone charges a grown
    # cell's extra slots; charging its storage_gib would over-fill the pool.
    from cellctl import db
    from cellctl.capacity import LocalCapacityObservation

    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")  # storage_gib 10
    cluster = FakeClusterGateway()
    cluster.local_capacity_inputs = lambda local: LocalCapacityObservation(
        free_bytes={"agent-1": 84 * GIB}, volumes=(_volume("agent-1", 16),), cell_nodes={cell_id: "agent-1"})
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        await db.write_observed(connection, cell_id, {"grown_storage_gib": 16})
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
                                       ClusterConfig(object_storage_bucket="b", storage=CUT_OVER), now=NOW)
        slots = await connection.fetchval("SELECT cell_slots FROM exomem_cloud_capacity WHERE node = 'agent-1'")
    finally:
        await connection.close()

    # (100 GiB pool - 2 x 16 GiB x 2 backups at once) / 4 = 9, less the grown
    # cell's ceil(16 / 4) - 1 = 3 more slots.
    assert slots == 6


def test_a_cell_on_a_hetzner_volume_never_grows() -> None:
    # A migration rollback rebinds a cell's retained Hetzner volume (design
    # D7), so a recorded hourly hold can finish on one. Even with room on the
    # node, a Hetzner volume keeps its size until its cell migrates again.
    hetzner = _backed_up(used=95, pv_storage_class=STORAGE_CLASS, pvc_storage_class=STORAGE_CLASS)

    decision = _measure(hetzner)

    assert decision.grow_storage_gib is None
