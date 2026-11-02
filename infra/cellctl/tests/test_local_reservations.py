"""D12 at the consumer boundary: durable identities, prospective growth and physical charges."""

from __future__ import annotations

import base64
import dataclasses
import json
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from cellctl import db, reconcile
from cellctl.capacity import (
    GIB,
    LocalCapacityObservation,
    compute_local_capacity,
    parse_budget_frame,
)
from cellctl.manifests import (
    BACKUP_FAMILY_ANNOTATION,
    BACKUP_OUTCOME_ANNOTATION,
    GROW_STORAGE_ANNOTATION,
    HOLD_ANNOTATION,
    SNAPSHOT_NAME,
    namespace_name,
    snapshot_backup_name,
)
from cellctl.reconcile import ClusterConfig
from cellctl.state import ClusterObservation
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
from cellctl.storage.topolvm import FamilyRequest, target_reserved
from cellctl.storage_config import LocalStorage, StorageConfig

from .conftest import CellDatabase
from .test_reconcile import FakeClusterGateway, _observed_from_applied, _secrets_config, _seed_cell

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
IMAGE = "registry.example/cell@sha256:" + "a" * 64
LOCAL = LocalStorage(reservations=True)
STORAGE = StorageConfig(local=LOCAL)


def frame(**fields):
    return json.dumps({"version": 1, "observed_at": NOW.isoformat(), "refresh_after_seconds": 600,
                       "node_uid": "node-uid", "pool_size_bytes": 100 * GIB, "ordinary_bytes": 24 * GIB,
                       "effective_backup_reserve_bytes": 32 * GIB, "largest_size_bytes": 8 * GIB,
                       "allocations": [{"name": "lv-a", "physical_uuid": "physical-a", "reserved_bytes": 8 * GIB}],
                       **fields})


def test_an_orphan_stays_charged_and_a_counted_volume_is_credited_once():
    budget = parse_budget_frame(frame(), node_uid="node-uid", now=NOW)
    observed = LocalCapacityObservation(
        budgets={"agent": budget}, cell_nodes={"a": "agent", "b": "agent"},
        cell_allocations={"a": ("lv-a", "physical-a", "agent"), "b": ("lv-a", "physical-a", "agent")})
    capacity = compute_local_capacity(observed, local=LOCAL, cell_sizes={"a": 8, "b": 4})
    # 16 GiB of orphans remains unmatched; the 8 GiB row also consumes its extra slot.
    assert capacity["agent"].cell_slots == (100 - 32 - 16) // 4 - 1
    assert capacity["agent"].limit_known
    changed = dataclasses.replace(observed, cell_allocations={"a": ("lv-a", "recreated", "agent")})
    assert compute_local_capacity(changed, local=LOCAL, cell_sizes={"a": 8})["agent"].cell_slots == 10


@pytest.mark.parametrize("fields", [
    {"observed_at": (NOW + timedelta(seconds=1)).isoformat()},
    {"observed_at": (NOW - timedelta(seconds=1201)).isoformat()},
    {"node_uid": "old-node"}, {"ordinary_bytes": True}, {"pool_size_bytes": 2**63},
    {"refresh_after_seconds": 0}, {"allocations": [{"name": "lv", "physical_uuid": "id", "reserved_bytes": -1}]},
])
def test_an_unusable_provider_frame_never_uses_cached_free_space(fields):
    budget = parse_budget_frame(frame(**fields), node_uid="node-uid", now=NOW)
    assert budget is None
    result = compute_local_capacity(LocalCapacityObservation(budgets={"agent": budget},
        free_bytes={"agent": 1000 * GIB}), local=LOCAL, cell_sizes={})
    assert result["agent"].cell_slots == 0
    assert not result["agent"].limit_known


@pytest.mark.parametrize("change", [
    {"physicalUUID": "old-source"}, {"requestedBytes": 7 * GIB}, {"requestedBytes": True},
    {"reservedBytes": True}, {"reservedBytes": 2**63}, {"code": False}, {"reservedBytes": 7 * GIB},
])
def test_growth_rejects_a_stale_or_invalid_provider_acknowledgement(change):
    volume = {"status": {"physicalUUID": "physical", "reservationTargetResult": {
        "physicalUUID": "physical", "requestedBytes": 8 * GIB, "reservedBytes": 8 * GIB, "code": 0, **change}}}
    assert target_reserved(volume, "physical", 8 * GIB) == (False, False)


class ReservedGateway(FakeClusterGateway):
    def __init__(self):
        super().__init__()
        self.volumes = {}
        self.requests = []
        self.family_delete_allowed = True

    def delete_family_secret(self, local, *, cell_id, request):
        if not self.family_delete_allowed:
            return False
        name = snapshot_backup_name(SNAPSHOT_NAME, cell_id, None, request)
        self.applied.pop((local.reservation_namespace, "Secret", name), None)
        return True

    def source_logical_volume(self, local, volume_id, node):
        return next(volume for volume in self.volumes.values() if volume["status"]["volumeID"] == volume_id
                    and volume["spec"]["nodeName"] == node)

    def logical_volume(self, local, name):
        return self.volumes[name]

    def patch_logical_volume(self, local, name, spec):
        self.requests.append((name, spec))
        self.volumes[name]["spec"].update(spec)

    def delete_snapshot_backup(self, namespace, snapshot, claim):
        self.events.append(("delete_snapshot_backup", namespace, snapshot, claim))


def source(cell_id):
    return {"metadata": {"name": "lv-" + cell_id}, "spec": {"nodeName": "agent", "deviceClass": "thin"},
            "status": {"volumeID": "volume-" + cell_id, "physicalUUID": "physical-" + cell_id,
                       "backupFamily": {"state": "closed", "closedEpoch": 0}}}


async def test_two_cells_keep_their_requests_across_restart_and_cancel_before_begin(cell_db: CellDatabase):
    cells = ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb")
    cluster = ReservedGateway()
    for index, cell_id in enumerate(cells):
        await _seed_cell(cell_db, cell_id, "tenant-" + str(index))
        cluster.volumes["lv-" + cell_id] = source(cell_id)
        cluster.observations[cell_id] = ClusterObservation(
            namespace_exists=True, namespace_cell_label=cell_id, pvc_bound=True, pvc_exists=True,
            pvc_uid="pvc", pv_claim_ref_uid="pvc", pvc_volume_id="volume-" + cell_id,
            pvc_storage_class=LOCAL.class_name, pv_storage_class=LOCAL.class_name, pv_node="agent",
            pvc_capacity_bytes=10 * GIB, statefulset_exists=True, statefulset_image=IMAGE,
            statefulset_replicas=1, pod_ready=True, ready_pod_image=IMAGE)
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    config = ClusterConfig(object_storage_bucket="test", storage=STORAGE)
    memory = reconcile.LoopMemory()

    async def run(minute):
        await reconcile.reconcile_once(connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(),
            config, memory=memory, now=NOW + timedelta(minutes=minute))

    def observe(cell_id, **overrides):
        annotations = cluster.applied[(namespace_name(cell_id), "StatefulSet", "cell")]["metadata"]["annotations"]
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id,
            pvc_storage_class=LOCAL.class_name, pv_storage_class=LOCAL.class_name, pv_node="agent",
            pvc_volume_id="volume-" + cell_id, pvc_exists=True, pvc_capacity_bytes=10 * GIB,
            statefulset_backup_family_request=annotations.get(BACKUP_FAMILY_ANNOTATION),
            statefulset_backup_outcome=annotations.get(BACKUP_OUTCOME_ANNOTATION),
            statefulset_grow_storage_gib=int(annotations[GROW_STORAGE_ANNOTATION]) if GROW_STORAGE_ANNOTATION in annotations else None,
            **overrides)

    try:
        for minute in range(3):
            await run(minute)
            for cell_id in cells:
                observe(cell_id)
        identities = {cell_id: FamilyRequest.parse(cluster.observations[cell_id].statefulset_backup_family_request)
                      for cell_id in cells}
        assert identities[cells[0]].request_id != identities[cells[1]].request_id
        assert not cluster.requests  # Persisted hold precedes Begin.
        memory = reconcile.LoopMemory()  # A controller restart carries no reservation memory.
        await run(3)
        assert all(spec["backupFamilyRequest"] == identities[name[3:]].spec("Begin") for name, spec in cluster.requests)
        assert not cluster.jobs

        # One source activates while the second request is cancelled before Begin commits.
        first, second = (identities[cell_id] for cell_id in cells)
        cluster.volumes[first.volume_name]["status"]["backupFamily"] = {
            **first.spec("Begin"), "epoch": 1, "closedEpoch": 0, "state": "active",
            "sourceSizeBytes": 10 * GIB, "token": "first-family"}
        admin = await asyncpg.connect(cell_db.dsn())
        await admin.execute("UPDATE exomem_cloud_cells SET desired_state='deleted', generation=generation+1 WHERE cell_id=$1", cells[1])
        await admin.close()
        await run(4)
        assert cluster.volumes[second.volume_name]["spec"]["backupFamilyRequest"] == second.spec("End")
        assert namespace_name(cells[1]) not in cluster.deleted_namespaces
        secret_name = snapshot_backup_name(SNAPSHOT_NAME, cells[0], None, first)
        secret = cluster.applied[(LOCAL.reservation_namespace, "Secret", secret_name)]
        assert len(secret_name) == 63 and secret["immutable"] is True
        assert base64.b64decode(secret["data"]["backup-family-token"]) == b"first-family"
        assert any(job["kind"] == "VolumeSnapshot" for job in cluster.jobs)
        cluster.volumes[second.volume_name]["status"]["backupFamily"] = {
            **second.spec("End"), "epoch": 1, "closedEpoch": 1, "state": "closed"}
        await run(5)
        assert namespace_name(cells[1]) in cluster.deleted_namespaces

        # Serving growth does not change the family's frozen clone size.
        admin = await asyncpg.connect(cell_db.dsn())
        await admin.execute("UPDATE exomem_cloud_cells SET storage_gib=14, generation=generation+1 WHERE cell_id=$1", cells[0])
        await admin.close()
        observe(cells[0], snapshot_exists=True, snapshot_ready=True)
        cluster.applied.pop((namespace_name(cells[0]), "PersistentVolumeClaim", "cell-data"))
        cluster.volumes[first.volume_name]["status"]["reservationTargetResult"] = {
            "physicalUUID": first.physical_source_uuid, "requestedBytes": 14 * GIB, "code": 8}
        await run(6)
        assert (namespace_name(cells[0]), "PersistentVolumeClaim", "cell-data") not in cluster.applied
        observe(cells[0], snapshot_exists=True, snapshot_ready=True)
        await run(6.5)  # The generation-only apply has now been observed; NoRoom keeps the ready runtime.
        blocked = next(row for row in await db.select_all_rows(connection) if row.cell_id == cells[0])
        assert blocked.ready and blocked.observed_state == "running"
        clone = next(job for job in cluster.jobs if job["kind"] == "PersistentVolumeClaim")
        assert clone["spec"]["resources"]["requests"]["storage"] == str(10 * GIB)
        observe(cells[0], snapshot_exists=True, snapshot_ready=True)
        assert cluster.observations[cells[0]].statefulset_grow_storage_gib == 14
        cluster.volumes[first.volume_name]["status"]["reservationTargetResult"].update(code=0, reservedBytes=14 * GIB)
        await run(7)
        assert cluster.applied[(namespace_name(cells[0]), "PersistentVolumeClaim", "cell-data")]["spec"]["resources"]["requests"]["storage"] == "14Gi"
        rows = await db.select_all_rows(connection)
        assert next(row for row in rows if row.cell_id == cells[0]).import_limits is None
    finally:
        await connection.close()


def test_only_the_current_claim_can_offset_a_provider_charge():
    from types import SimpleNamespace as NS

    from cellctl.k8s_client import ClusterClient

    current = NS(metadata=NS(namespace=namespace_name("aaaaaaaaaaaaaaaa"), name="cell-data", uid="current"),
                 spec=NS(volume_name="pv-current"))

    def pv(name, uid, handle, node="agent"):
        affinity = NS(required=NS(node_selector_terms=[NS(match_expressions=[
            NS(key=LOCAL.topology_key, operator="In", values=[node])], match_fields=None)]))
        return NS(metadata=NS(name=name), spec=NS(claim_ref=NS(namespace=current.metadata.namespace,
            name="cell-data", uid=uid), storage_class_name=LOCAL.class_name, node_affinity=affinity,
            csi=NS(volume_handle=handle, driver=LOCAL.driver)))

    class Core:
        def list_node(self):
            return NS(items=[NS(metadata=NS(name="agent", uid="node-uid", labels={}, annotations={
                LOCAL.reservation_annotation: frame(observed_at=datetime.now(UTC).isoformat())}),
                spec=NS(taints=None, unschedulable=False),
                status=NS(conditions=[NS(type="Ready", status="True")]))])

        def list_persistent_volume(self):
            return NS(items=[pv("pv-current", "current", "handle"), pv("retained", "old", "old-handle", "retired-agent")])

        def list_persistent_volume_claim_for_all_namespaces(self, **kwargs):
            return NS(items=[current])

    class Custom:
        def list_cluster_custom_object(self, *args):
            return {"items": [{"metadata": {"name": "lv-a"}, "spec": {"nodeName": "agent", "size": "8Gi",
                "deviceClass": "thin"}, "status": {"volumeID": "handle", "physicalUUID": "physical-a"}}]}

    client = ClusterClient.__new__(ClusterClient)
    client._core, client._custom = Core(), Custom()
    observed = client.local_capacity_inputs(LOCAL)
    assert observed.cell_nodes == {"aaaaaaaaaaaaaaaa": "agent"}
    assert observed.cell_allocations == {"aaaaaaaaaaaaaaaa": ("lv-a", "physical-a", "agent")}
    assert observed.budgets["agent"].ordinary_bytes == 24 * GIB


def test_a_timed_out_family_ends_the_original_request_and_keeps_its_hold():
    from cellctl.decide import decide
    from cellctl.state import SNAPSHOT_BACKUP, RolloutRow

    from .test_local_storage import _bound, _row

    cell_id = "aaaaaaaaaaaaaaaa"
    request = FamilyRequest("lv-" + cell_id, "physical-" + cell_id, 0, "a" * 32)
    observation = _bound(LOCAL.class_name, statefulset_hold_kind=SNAPSHOT_BACKUP,
        statefulset_hold_started_at=NOW - timedelta(hours=2),
        statefulset_backup_family_request=request.annotation(), pvc_volume_id="volume-" + cell_id)
    row = _row(hold_kind="backup", hold_started_at=observation.statefulset_hold_started_at)
    decision = decide(row, RolloutRow(), observation, now=NOW, cell_image=IMAGE, storage=STORAGE, start_upgrade=False)
    cluster = ReservedGateway()
    cluster.volumes[request.volume_name] = source(cell_id)
    reconcile._local_family(cluster, row, observation, decision, LOCAL)
    assert cluster.requests == [(request.volume_name, {"backupFamilyRequest": request.spec("End")})]
    assert decision.hold_kind == SNAPSHOT_BACKUP
    assert decision.backup_family_request == request.annotation()
    assert decision.delete_snapshot_backup
    assert not decision.create_snapshot


async def test_a_missing_reservation_boundary_retries_only_local_cells(cell_db: CellDatabase):
    from .test_reconcile import _served

    local_id, legacy_id = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    for index, cell_id in enumerate((local_id, legacy_id)):
        await _seed_cell(cell_db, cell_id, "tenant-" + str(index))
    cluster = ReservedGateway()
    cluster.admission_missing.add("exomem-cellctl-reservations")
    cluster.observations[local_id] = dataclasses.replace(_served(local_id),
        pvc_storage_class=LOCAL.class_name, pv_storage_class=LOCAL.class_name)
    cluster.observations[legacy_id] = _served(legacy_id)
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        await reconcile.reconcile_once(connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(),
            ClusterConfig(object_storage_bucket="test", storage=STORAGE), now=NOW)
    finally:
        await connection.close()
    assert (namespace_name(local_id), "StatefulSet", "cell") not in cluster.applied
    assert (namespace_name(legacy_id), "StatefulSet", "cell") in cluster.applied


def test_a_closed_request_is_cleared_before_the_next_backup_can_allocate():
    from cellctl.state import SNAPSHOT_BACKUP, Decision

    from .test_local_storage import _bound, _row

    cell_id = "aaaaaaaaaaaaaaaa"
    request = FamilyRequest("lv-" + cell_id, "physical-" + cell_id, 0, "a" * 32)
    cluster = ReservedGateway()
    volume = source(cell_id)
    volume["spec"]["backupFamilyRequest"] = request.spec("End")
    volume["status"]["backupFamily"] = {**request.spec("End"), "epoch": 1, "closedEpoch": 1, "state": "closed"}
    cluster.volumes[request.volume_name] = volume
    observation = _bound(LOCAL.class_name, statefulset_backup_family_request=request.annotation(),
                         pvc_volume_id="volume-" + cell_id)
    decision = Decision(hold_kind=SNAPSHOT_BACKUP, hold_started_at=NOW, create_snapshot=True)
    assert reconcile._local_family(cluster, _row(), observation, decision, LOCAL) is None
    assert decision.backup_family_request is None
    assert not decision.create_snapshot
    assert cluster.requests[-1][1]["backupFamilyRequest"] == request.spec("End")


def test_an_empty_pool_keeps_the_first_cells_future_backup_reserve():
    budget = parse_budget_frame(frame(pool_size_bytes=64 * GIB, ordinary_bytes=0,
        effective_backup_reserve_bytes=0, largest_size_bytes=0, allocations=[]), node_uid="node-uid", now=NOW)
    result = compute_local_capacity(LocalCapacityObservation(budgets={"agent": budget}), local=LOCAL, cell_sizes={})
    # The first 4 GiB source needs 2 children for each of 2 concurrent backups: 16 GiB.
    assert result["agent"].cell_slots == (64 - 16) // 4


def test_an_unbound_larger_commitment_keeps_its_prospective_backup_reserve_and_extra_slot():
    budget = parse_budget_frame(frame(pool_size_bytes=64 * GIB, ordinary_bytes=0,
        effective_backup_reserve_bytes=0, largest_size_bytes=0, allocations=[]), node_uid="node-uid", now=NOW)
    result = compute_local_capacity(LocalCapacityObservation(budgets={"agent": budget}),
        local=LOCAL, cell_sizes={"pending": 8})
    # The 8 GiB source raises the reserve to 32 GiB and costs one extra default-size slot.
    assert result["agent"].cell_slots == (64 - 32) // 4 - 1


@pytest.mark.parametrize("condition", ["fresh", "expired", "different-physical-volume", "unbound"])
async def test_local_import_limits_require_a_fresh_bound_physical_identity(cell_db: CellDatabase, condition):
    from .test_local_storage import _bound

    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "limits-tenant")
    cluster = ReservedGateway()
    cluster.volumes["lv-" + cell_id] = source(cell_id)
    cluster.observations[cell_id] = _bound(LOCAL.class_name, pvc_exists=True,
        pvc_bound=condition != "unbound", pvc_volume_id="volume-" + cell_id, pv_node="agent",
        pvc_capacity_bytes=10 * GIB)
    budget = parse_budget_frame(frame(observed_at=(NOW - timedelta(seconds=1201) if condition == "expired" else NOW).isoformat(),
        allocations=[{"name": "lv-" + cell_id, "physical_uuid": "physical-" + cell_id, "reserved_bytes": 10 * GIB}]),
        node_uid="node-uid", now=NOW)
    physical_uuid = "other-incarnation" if condition == "different-physical-volume" else "physical-" + cell_id
    cluster.local_capacity_inputs = lambda local: LocalCapacityObservation(budgets={"agent": budget},
        cell_allocations={cell_id: ("lv-" + cell_id, physical_uuid, "agent")},
        cell_volume_ids={cell_id: "volume-" + cell_id})
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        await db.write_observed(controller, cell_id, {"import_limits": {"max_storage_gib": 99}})
        await reconcile.reconcile_once(controller, cluster, FakeHetznerVolumeProvider(), _secrets_config(),
            ClusterConfig(object_storage_bucket="test", storage=STORAGE), now=NOW)
        row = (await db.select_all_rows(controller))[0]
        assert row.generation == 1
        if condition == "fresh":
            assert row.import_limits["max_storage_gib"] == STORAGE.max_cell_gib
        else:
            assert row.import_limits is None
    finally:
        await controller.close()


@pytest.mark.parametrize("outcome", ["accepted", "no-room"])
async def test_guarded_import_growth_waits_for_the_provider_before_key_creation(cell_db: CellDatabase, outcome):
    from cellctl.imports import ImportJob

    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "growth-import-tenant")
    cluster = ReservedGateway()
    cluster.volumes["lv-" + cell_id] = source(cell_id)
    jobs = {}
    cluster.observe_import_jobs = lambda namespace, import_id: jobs.copy()
    budget = parse_budget_frame(frame(ordinary_bytes=(60 if outcome == "no-room" else 10) * GIB,
        largest_size_bytes=10 * GIB, effective_backup_reserve_bytes=40 * GIB,
        allocations=[{"name": "lv-" + cell_id, "physical_uuid": "physical-" + cell_id, "reserved_bytes": 10 * GIB}]),
        node_uid="node-uid", now=NOW)
    cluster.local_capacity_inputs = lambda local: LocalCapacityObservation(budgets={"agent": budget},
        cell_allocations={cell_id: ("lv-" + cell_id, "physical-" + cell_id, "agent")},
        cell_volume_ids={cell_id: "volume-" + cell_id})
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    config = ClusterConfig(object_storage_bucket="test", storage=StorageConfig(domain=LOCAL.class_name, local=LOCAL))

    def observe(*, capacity=10, pending=False, mounted=True):
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id,
            pvc_exists=True, pvc_volume_id="volume-" + cell_id, pvc_resource_version="rv",
            pvc_storage_class=LOCAL.class_name, pv_storage_class=LOCAL.class_name, pv_node="agent",
            pvc_capacity_bytes=capacity * GIB, pvc_resize_pending=pending, pod_uses_volume=mounted)

    async def run():
        await reconcile.reconcile_once(controller, cluster, FakeHetznerVolumeProvider(), _secrets_config(),
            config, now=NOW)

    def claim_size():
        return cluster.applied[(namespace_name(cell_id), "PersistentVolumeClaim", "cell-data")]["spec"]["resources"]["requests"]["storage"]

    try:
        await db.write_observed(controller, cell_id, {"last_backup_at": NOW})
        await run()
        observe()
        await run()  # Fresh physical identity publishes limits before admission.
        assert (await db.select_all_rows(controller))[0].import_limits["max_storage_gib"] == 20
        # The API atomically accepts ceil((2 * 6 GiB + 256 MiB) / GiB) and raises desired storage.
        required = 13
        async with app.transaction():
            await app.execute("UPDATE exomem_cloud_cells SET storage_gib=$2 WHERE cell_id=$1", cell_id, required)
            imported = await app.fetchval("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files,required_storage_gib) "
                "VALUES($1,'adopt',$2,1,$3) RETURNING import_id", cell_id, 6 * GIB, required)
        assert (await db.read_import(controller, cell_id)).required_storage_gib == required
        await run()  # No acknowledgement yet: the runtime remains mounted and no key exists.
        assert claim_size() == "10Gi" and not cluster.jobs
        row = (await db.select_all_rows(controller))[0]
        assert row.hold_kind is None and row.storage_gib == required
        observe()
        result = {"physicalUUID": "physical-" + cell_id, "requestedBytes": required * GIB,
                  "code": 0 if outcome == "accepted" else 8}
        if outcome == "accepted":
            result["reservedBytes"] = required * GIB
        cluster.volumes["lv-" + cell_id]["status"]["reservationTargetResult"] = result
        await run()
        item = await db.read_import(controller, cell_id)
        assert item.import_id == str(imported) and item.recipient is None
        if outcome == "no-room":
            assert item.error_code == "IMPORT_STORAGE_NO_ROOM" and item.observed_state == "cleanup_running"
            assert claim_size() == "10Gi" and not cluster.jobs
            assert (await db.select_all_rows(controller))[0].storage_gib == required
            observe(mounted=False)
            await run()  # Cleanup cannot bypass the same provider target guard.
            assert claim_size() == "10Gi"
            assert cluster.jobs[-1]["spec"]["template"]["spec"]["containers"][0]["command"][6] == "discard"
        else:
            assert claim_size() == f"{required}Gi" and not cluster.jobs
            observe(capacity=required, pending=True)
            await run()
            assert not cluster.jobs and (await db.select_all_rows(controller))[0].hold_kind is None
            observe(capacity=required)
            await run()  # Capacity and resize conditions permit the prepare hold.
            observe(capacity=required, mounted=False)
            await run()
            assert cluster.jobs[-1]["spec"]["template"]["spec"]["containers"][0]["command"][6] == "prepare"
            jobs["prepare"] = ImportJob(succeeded=True, report={"recipient": "age1" + "a" * 58})
            await run()
            assert (await db.read_import(controller, cell_id)).observed_state == "key_ready"
    finally:
        await app.close()
        await controller.close()


def test_a_large_cell_on_another_pool_does_not_take_this_pools_backup_room():
    big = parse_budget_frame(frame(pool_size_bytes=200 * GIB, ordinary_bytes=20 * GIB,
        effective_backup_reserve_bytes=80 * GIB, largest_size_bytes=20 * GIB,
        allocations=[{"name": "large", "physical_uuid": "large-uuid", "reserved_bytes": 20 * GIB}]),
        node_uid="node-uid", now=NOW)
    small = parse_budget_frame(frame(pool_size_bytes=64 * GIB, ordinary_bytes=0,
        effective_backup_reserve_bytes=0, largest_size_bytes=0, allocations=[]), node_uid="node-uid", now=NOW)
    observed = LocalCapacityObservation(budgets={"big": big, "small": small}, cell_nodes={"large-cell": "big"},
        cell_allocations={"large-cell": ("large", "large-uuid", "big")})
    result = compute_local_capacity(observed, local=LOCAL, cell_sizes={"large-cell": 20})
    assert result["big"].cell_slots == (200 - 80) // 4 - 4
    assert result["small"].cell_slots == (64 - 16) // 4


async def test_a_rollback_keeps_the_larger_accepted_claim_pending_without_an_early_key(cell_db: CellDatabase):
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "rollback-import-tenant")
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    cluster = ReservedGateway()
    cluster.volumes["lv-" + cell_id] = source(cell_id)
    cluster.volumes["lv-" + cell_id]["status"]["reservationTargetResult"] = {
        "physicalUUID": "physical-" + cell_id, "requestedBytes": 28 * GIB, "code": 3}
    cluster.observe_import_jobs = lambda namespace, import_id: {}
    config = ClusterConfig(object_storage_bucket="test", storage=StorageConfig(domain=LOCAL.class_name, local=LOCAL))

    async def run():
        await reconcile.reconcile_once(controller, cluster, FakeHetznerVolumeProvider(), _secrets_config(),
            config, now=NOW)

    try:
        await db.write_observed(controller, cell_id, {"last_backup_at": NOW})
        await run()
        # Kubernetes retained a request above the current cap while the database restored its earlier 10 GiB row.
        claim = cluster.applied[(namespace_name(cell_id), "PersistentVolumeClaim", "cell-data")]
        claim["spec"]["resources"]["requests"]["storage"] = "28Gi"
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id, pvc_exists=True,
            pvc_volume_id="volume-" + cell_id, pvc_resource_version="rv", pv_node="agent",
            pvc_storage_class=LOCAL.class_name, pv_storage_class=LOCAL.class_name,
            pvc_capacity_bytes=10 * GIB, pvc_requested_bytes=28 * GIB, pod_uses_volume=True)
        await app.execute("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files,required_storage_gib) "
                          "VALUES($1,'adopt',4,1,14)", cell_id)
        await run()
        assert cluster.applied[(namespace_name(cell_id), "PersistentVolumeClaim", "cell-data")]["spec"]["resources"]["requests"]["storage"] == "28Gi"
        assert not cluster.jobs
        assert (await db.read_import(controller, cell_id)).recipient is None
        assert (await db.select_all_rows(controller))[0].hold_kind is None
        assert cluster.volumes["lv-" + cell_id]["spec"]["reservationTargetBytes"] == 28 * GIB
        quota = cluster.applied[(namespace_name(cell_id), "ResourceQuota", "cell-quota")]
        assert quota["spec"]["hard"]["requests.storage"] == "56Gi"
        # A fresh controller repeats the same target, retaining the existing claim while identity/ack lags.
        await run()
        assert cluster.applied[(namespace_name(cell_id), "PersistentVolumeClaim", "cell-data")]["spec"]["resources"]["requests"]["storage"] == "28Gi" and not cluster.jobs
    finally:
        await app.close()
        await controller.close()


def test_an_accepted_resize_is_pending_before_kubernetes_sets_its_condition():
    from types import SimpleNamespace as NS

    from cellctl.storage_config import DEFAULT_STORAGE, LEGACY_CLASS

    from .test_k8s_client import CELL_ID, NAMESPACE, _client, _Core

    claim = NS(metadata=NS(name="cell-data", namespace=NAMESPACE, uid="claim", resource_version="rv"),
        spec=NS(storage_class_name=LEGACY_CLASS, volume_name="pv", resources=NS(requests={"storage": "14Gi"})),
        status=NS(phase="Bound", capacity={"storage": "10Gi"}, conditions=[]))
    volume = NS(metadata=NS(name="pv"), spec=NS(claim_ref=NS(uid="claim"), storage_class_name=LEGACY_CLASS,
        csi=NS(volume_handle="volume")))
    client = _client(hold_started_at=None, jobs={}, pods=[])
    client._storage_config = DEFAULT_STORAGE
    client._core = _Core([], pvcs=[claim], pvs=[volume])
    observed = client.observe(CELL_ID, NAMESPACE)
    assert observed.pvc_requested_bytes == 14 * GIB
    assert observed.pvc_capacity_bytes == 10 * GIB
    assert observed.pvc_resize_pending


def test_a_physically_closed_family_keeps_its_identity_until_secret_deletion_succeeds():
    from cellctl.state import SNAPSHOT_BACKUP, Decision

    from .test_local_storage import _bound, _row

    cell_id = "aaaaaaaaaaaaaaaa"
    request = FamilyRequest("lv-" + cell_id, "physical-" + cell_id, 0, "a" * 32)
    cluster = ReservedGateway()
    cluster.family_delete_allowed = False
    volume = source(cell_id)
    volume["spec"]["backupFamilyRequest"] = request.spec("End")
    volume["status"]["backupFamily"] = {**request.spec("End"), "epoch": 1, "closedEpoch": 1, "state": "closed"}
    cluster.volumes[request.volume_name] = volume
    observation = _bound(LOCAL.class_name, statefulset_backup_family_request=request.annotation(),
        statefulset_hold_started_at=NOW, statefulset_backup_outcome="a" * 64, pvc_volume_id="volume-" + cell_id)
    decision = Decision()
    reconcile._local_family(cluster, _row(), observation, decision, LOCAL)
    assert decision.hold_kind == SNAPSHOT_BACKUP and decision.backup_family_request == request.annotation()
    cluster.family_delete_allowed = True
    decision = Decision()
    reconcile._local_family(cluster, _row(), dataclasses.replace(observation, backup_family_secret_referenced=True), decision, LOCAL)
    assert decision.hold_kind == SNAPSHOT_BACKUP and decision.backup_family_request == request.annotation()
    decision = Decision()
    reconcile._local_family(cluster, _row(), observation, decision, LOCAL)
    assert decision.hold_kind is None and decision.backup_family_request is None


def test_family_secret_deletion_is_exact_and_only_not_found_counts_as_success():
    from types import SimpleNamespace as NS
    from unittest.mock import Mock

    from kubernetes.client.rest import ApiException

    from cellctl.k8s_client import ClusterClient

    cell_id = "aaaaaaaaaaaaaaaa"
    old = FamilyRequest("source", "physical", 0, "a" * 32)
    newer = FamilyRequest("source", "physical", 1, "b" * 32)
    client = ClusterClient.__new__(ClusterClient)
    client._core = NS(delete_namespaced_secret=Mock(side_effect=ApiException(status=404)))
    assert client.delete_family_secret(LOCAL, cell_id=cell_id, request=old)
    assert client._core.delete_namespaced_secret.call_args.args == (
        snapshot_backup_name(SNAPSHOT_NAME, cell_id, None, old), LOCAL.reservation_namespace)
    assert snapshot_backup_name(SNAPSHOT_NAME, cell_id, None, old) != snapshot_backup_name(SNAPSHOT_NAME, cell_id, None, newer)
    client._core.delete_namespaced_secret.side_effect = ApiException(status=403)
    assert not client.delete_family_secret(LOCAL, cell_id=cell_id, request=old)


@pytest.mark.parametrize("reference", ["exact-retained", "other-secret", "failed-list", "partial-list"])
def test_snapshot_content_references_keep_only_the_exact_family_capability(reference):
    from types import SimpleNamespace as NS
    from unittest.mock import Mock

    from kubernetes.client.rest import ApiException

    from cellctl.k8s_client import (
        DELETION_SECRET_NAME_ANNOTATION,
        DELETION_SECRET_NAMESPACE_ANNOTATION,
    )
    from cellctl.state import SNAPSHOT_BACKUP

    from .test_k8s_client import CELL_ID, CURRENT_HOLD, NAMESPACE, _client

    request = FamilyRequest("source", "physical", 0, "a" * 32)
    client = _client(hold_started_at=CURRENT_HOLD, jobs={}, pods=[])
    client._storage_config = STORAGE
    client._core.list_node = lambda: NS(items=[])
    client._core.read_namespaced_persistent_volume_claim = Mock(side_effect=ApiException(status=404))
    client._apps._statefulset.metadata.annotations.update(
        {HOLD_ANNOTATION: SNAPSHOT_BACKUP, BACKUP_FAMILY_ANNOTATION: request.annotation()})
    name = snapshot_backup_name(SNAPSHOT_NAME, CELL_ID, None, request)
    content = {"metadata": {"name": "retained-content", "deletionTimestamp": NOW.isoformat(), "annotations": {
        DELETION_SECRET_NAME_ANNOTATION: name if reference == "exact-retained" else "another-request",
        DELETION_SECRET_NAMESPACE_ANNOTATION: LOCAL.reservation_namespace}},
        "spec": {"deletionPolicy": "Retain", "volumeSnapshotRef": {"name": name, "namespace": NAMESPACE, "uid": "another-uid"}}}
    client._custom = NS(get_namespaced_custom_object=Mock(side_effect=ApiException(status=404)),
        list_cluster_custom_object=Mock(return_value={"items": [content]}))
    if reference == "failed-list":
        client._custom.list_cluster_custom_object.side_effect = ApiException(status=503)
    elif reference == "partial-list":
        client._custom.list_cluster_custom_object.return_value = {"items": [], "metadata": {"continue": "next-page"}}
    observed = client.observe(CELL_ID, NAMESPACE)
    assert observed.backup_family_secret_referenced is {"exact-retained": True, "other-secret": False, "failed-list": None, "partial-list": None}[reference]
    client._custom.list_cluster_custom_object.assert_called_once_with("snapshot.storage.k8s.io", "v1", "volumesnapshotcontents")
