"""Published import limits and capacity proof before key creation."""
import json
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from cellctl.decide import ReconcileConfig, StorageRoom
from cellctl.imports import ImportRow, decide_import
from cellctl.main import build_storage_config
from cellctl.state import CellRow, ClusterObservation
from cellctl.storage_config import LocalStorage, StorageConfig

NOW = datetime(2026, 1, 1, tzinfo=UTC)
GIB = 1024**3
CELL = "aaaaaaaaaaaaaaaa"


@pytest.mark.parametrize("value,cap", [
    ({"max_cell_gib": 24}, 24),
    ({"local": {"max_cell_gib": 28}}, 28),
    ({"max_cell_gib": 24, "local": {"max_cell_gib": 24}}, 24),
])
def test_storage_cap_supports_attachable_and_legacy_config(monkeypatch, value, cap):
    monkeypatch.setenv("CELLCTL_CELL_STORAGE", json.dumps(value))
    assert build_storage_config().max_cell_gib == cap


def test_conflicting_caps_refuse_configuration(monkeypatch):
    monkeypatch.setenv("CELLCTL_CELL_STORAGE", json.dumps({"max_cell_gib": 24, "local": {"max_cell_gib": 28}}))
    with pytest.raises(ValueError, match="max_cell_gib.*local.max_cell_gib"):
        build_storage_config()


@pytest.mark.parametrize("capacity,generation,pending", [(8, 2, False), (12, 1, False), (12, 2, True)])
def test_growth_wait_keeps_runtime_mounted_without_a_key(capacity, generation, pending):
    row = CellRow(CELL, uuid.uuid4(), 12, 0, "running", None, 2,
                  observed_storage_capacity_bytes=capacity * GIB, observed_storage_generation=generation)
    item = ImportRow(str(uuid.uuid4()), CELL, "adopt", 4, 1, "requested", NOW, required_storage_gib=12)
    observation = ClusterObservation(statefulset_image="example:cell", pod_uses_volume=True,
                                     pvc_resize_pending=pending)
    for ready in (False, True):
        result = decide_import(row, item, replace(observation, pod_ready=ready), {}, now=NOW)
        assert result is None
    failed = decide_import(row, item, observation, {}, now=NOW + timedelta(minutes=60))
    assert failed.import_updates["error_code"] == "IMPORT_STORAGE_UNAVAILABLE"
    assert "recipient" not in failed.import_updates


def test_capacity_proof_allows_prepare_and_local_room_is_checked_again():
    row = CellRow(CELL, uuid.uuid4(), 12, 0, "running", None, 2,
                  observed_storage_capacity_bytes=12 * GIB, observed_storage_generation=2)
    item = ImportRow(str(uuid.uuid4()), CELL, "adopt", 4, 1, "requested", NOW, required_storage_gib=12)
    local = LocalStorage()
    storage = StorageConfig(local=local)
    observation = ClusterObservation(statefulset_image="example:cell", pvc_storage_class=local.class_name,
                                     pv_storage_class=local.class_name, pv_node="node-a", pvc_request_bytes=8 * GIB)
    assert decide_import(row, item, observation, {}, now=NOW).import_action == "prepare"
    unknown = decide_import(row, item, observation, {}, now=NOW, storage=storage,
                            storage_room=StorageRoom({"node-a": None}, 12))
    assert unknown is None
    refused = decide_import(row, item, observation, {}, now=NOW, storage=storage,
                            storage_room=StorageRoom({"node-a": 0}, 12))
    assert refused.import_updates["error_code"] == "IMPORT_STORAGE_NO_ROOM"
    assert "recipient" not in refused.import_updates


def test_limits_come_from_enforced_defaults_and_effective_storage():
    from cellctl.imports import MAX_PARTS, PART_BYTES, UPLOAD_DEADLINE, ImportLimits

    row = CellRow(CELL, uuid.uuid4(), 30, 0, "running", None, 2)
    config = ReconcileConfig(import_growth_deadline=timedelta(minutes=75))
    limits = asdict(ImportLimits.for_cell(row, ClusterObservation(), StorageConfig(), config, None))
    assert limits == dict(max_members=500000, reserve_bytes=256 * 1024**2, space_factor=2,
                          max_storage_gib=30, part_bytes=PART_BYTES, max_parts=MAX_PARTS,
                          upload_window_seconds=int(UPLOAD_DEADLINE.total_seconds()),
                          import_deadline_seconds=7200, import_growth_deadline_seconds=4500)
    local = LocalStorage()
    observation = ClusterObservation(pv_storage_class=local.class_name, pv_node="node-a")
    small = replace(row, storage_gib=4)
    assert ImportLimits.for_cell(small, observation, StorageConfig(local=local), config,
                                 StorageRoom({"node-a": 0}, 4)).max_storage_gib == 4
    assert ImportLimits.for_cell(small, observation, StorageConfig(local=local), config,
                                 StorageRoom({"node-a": 56 * GIB}, 4)).max_storage_gib == 12
    assert ImportLimits.for_cell(small, ClusterObservation(),
        StorageConfig(domain=local.class_name, local=local), config, None).max_storage_gib == 4


def test_manifest_passes_published_limits_to_the_job():
    from cellctl.imports import MAX_MEMBERS, RESERVE_BYTES
    from cellctl.manifests import CellManifestSpec, render_import_job

    item = ImportRow(str(uuid.uuid4()), CELL, "adopt", 4, 1, "uploaded", NOW)
    job = render_import_job(CellManifestSpec(cell_id=CELL, image="example:cell", replicas=0, read_only=False), item, "import",
                            deadline_seconds=7200, url="https://storage.example/import")
    env = {entry["name"]: entry.get("value") for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["EXOMEM_IMPORT_MAX_MEMBERS"] == str(MAX_MEMBERS)
    assert env["EXOMEM_IMPORT_RESERVE_BYTES"] == str(RESERVE_BYTES)


def test_growth_deadline_setting_uses_minutes(monkeypatch):
    from cellctl.main import build_reconcile_config

    monkeypatch.setenv("CELLCTL_UPGRADE_READY_DEADLINE_MINUTES", "30")
    monkeypatch.setenv("CELLCTL_IMPORT_GROWTH_DEADLINE_MINUTES", "75")
    assert build_reconcile_config().import_growth_deadline == timedelta(minutes=75)


@pytest.mark.parametrize("free_gib", [None, 0])
async def test_local_growth_retains_accepted_claim_through_failure_and_later_reconcile(cell_db, free_gib):
    import asyncpg

    from cellctl import db, reconcile
    from cellctl.capacity import LocalCapacityObservation
    from cellctl.manifests import namespace_name
    from cellctl.storage.fake_b2 import FakeB2
    from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
    from tests.test_reconcile import (
        FakeClusterGateway,
        _observed_from_applied,
        _secrets_config,
        _seed_cell,
    )

    await _seed_cell(cell_db, CELL, "local-import")
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    local = LocalStorage()
    cluster = FakeClusterGateway()
    available = 200 * GIB
    cluster.local_capacity_inputs = lambda local: LocalCapacityObservation(free_bytes={"node-a": available})
    jobs = {}
    cluster.observe_import_jobs = lambda namespace, import_id: jobs
    config = reconcile.ClusterConfig(object_storage_bucket="b", storage=StorageConfig(domain=local.class_name, local=local))
    namespace = namespace_name(CELL)
    async def run():
        await reconcile.reconcile_once(controller, cluster, FakeB2(), FakeHetznerVolumeProvider(),
                                       _secrets_config(), config, now=NOW)
    def observe():
        cluster.observations[CELL] = _observed_from_applied(cluster, CELL, pvc_exists=True,
            pvc_storage_class=local.class_name, pv_storage_class=local.class_name, pv_node="node-a",
            pvc_resource_version="42", pvc_capacity_bytes=10 * GIB, pod_uses_volume=True)
    def claim_bytes():
        from cellctl.k8s_client import quantity

        claim = cluster.applied[(namespace, "PersistentVolumeClaim", "cell-data")]
        return int(quantity(claim["spec"]["resources"]["requests"]["storage"]))
    try:
        await controller.execute("UPDATE exomem_cloud_cells SET last_backup_at=$2 WHERE cell_id=$1", CELL, NOW)
        await run()
        observe()
        await run()
        published = (await db.select_all_rows(controller))[0].import_limits
        assert published["max_storage_gib"] == 20
        # Admission uses the earlier published room; the actual node reading changes before render.
        available = None if free_gib is None else free_gib * GIB
        await app.execute("UPDATE exomem_cloud_cells SET storage_gib=14 WHERE cell_id=$1", CELL)
        import_id = await app.fetchval("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files,required_storage_gib) "
            "VALUES($1,'adopt',4,1,14) RETURNING import_id", CELL)
        observe()
        await run()
        item = await db.read_import(controller, CELL)
        assert claim_bytes() == 10 * GIB and item.recipient is None
        if free_gib is None:
            assert item.error_code is None and not cluster.jobs
            assert (await db.select_all_rows(controller))[0].hold_kind is None
        else:
            assert item.error_code == "IMPORT_STORAGE_NO_ROOM"
            assert (await db.select_all_rows(controller))[0].hold_kind == "import"
            # A cleanup render still cannot use the admitted desired size.
            observe()
            await run()
            assert claim_bytes() == 10 * GIB
        # Once failure cleanup has released the hold, ordinary reconciliation keeps the same rule.
        await controller.execute("UPDATE exomem_cloud_imports SET observed_state='failed',error_code='IMPORT_STORAGE_UNAVAILABLE' "
                                 "WHERE import_id=$1", import_id)
        await controller.execute("UPDATE exomem_cloud_cells SET hold_kind=NULL,hold_started_at=NULL WHERE cell_id=$1", CELL)
        cluster.applied[(namespace, "StatefulSet", "cell")]["metadata"]["annotations"].pop("exomem.io/hold", None)
        observe()
        await run()
        assert claim_bytes() == 10 * GIB
        assert (await db.select_all_rows(controller))[0].storage_gib == 14
        available = 200 * GIB
        observe()
        await run()
        assert claim_bytes() == 14 * GIB
    finally:
        await app.close()
        await controller.close()


async def test_limit_publication_does_not_bump_generation_or_rewrite_unchanged_limits(cell_db, monkeypatch):
    import asyncpg

    from cellctl import db, reconcile
    from cellctl.storage.fake_b2 import FakeB2
    from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
    from tests.test_reconcile import (
        FakeClusterGateway,
        _cluster_config,
        _observed_from_applied,
        _secrets_config,
        _seed_cell,
    )

    await _seed_cell(cell_db, CELL, "published-limits")
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    writes = []
    write = db.write_observed
    async def record(connection, cell_id, updates):
        if "import_limits" in updates:
            writes.append(updates["import_limits"])
        await write(connection, cell_id, updates)
    monkeypatch.setattr(db, "write_observed", record)
    try:
        for _ in range(3):
            await reconcile.reconcile_once(controller, cluster, FakeB2(), FakeHetznerVolumeProvider(),
                _secrets_config(), _cluster_config(), now=NOW)
            cluster.observations[CELL] = _observed_from_applied(cluster, CELL)
        row = (await db.select_all_rows(controller))[0]
        assert row.generation == 1 and row.import_limits == writes[0] and len(writes) == 1
    finally:
        await controller.close()
