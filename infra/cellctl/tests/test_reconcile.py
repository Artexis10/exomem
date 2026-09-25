"""End-to-end tests for the reconcile.py orchestrator (3.3, 3.5, 3.7), wired
to a disposable Postgres and fakes standing in for Kubernetes and B2/Hetzner.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from cellctl import db, reconcile
from cellctl.manifests import STORAGE_CLASS, namespace_name
from cellctl.reconcile import _augment_deletion_observation
from cellctl.state import CellRow, ClusterObservation, RolloutRow
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
from cellctl.storage.interface import VolumeInfo

from .conftest import CellDatabase, insert_tenant, tenant_uuid

IMAGE_A = "registry.example/cell@sha256:" + "a" * 64


def _served(cell_id: str = "aaaaaaaaaaaaaaaa", **overrides: object) -> ClusterObservation:
    """A cell that already exists, is bound and Ready on IMAGE_A -- the
    "clean, converged" baseline most reconcile tests start their second
    pass from. Carries a matching namespace label and PV binding identity
    so it never trips the D4/M4 identity-conflict check by accident."""

    defaults: dict[str, object] = dict(
        namespace_exists=True,
        namespace_cell_label=cell_id,
        pvc_bound=True,
        pvc_uid="pvc-uid-1",
        pv_claim_ref_uid="pvc-uid-1",
        pv_storage_class=STORAGE_CLASS,
        statefulset_exists=True,
        statefulset_image=IMAGE_A,
        statefulset_replicas=1,
        pod_ready=True,
        ready_pod_image=IMAGE_A,
    )
    defaults.update(overrides)
    return ClusterObservation(**defaults)


class FakeClusterGateway:
    def __init__(self) -> None:
        self.applied: dict[tuple, dict] = {}
        self.jobs: list[dict] = []
        self.deleted_namespaces: set[str] = set()
        self.observations: dict[str, ClusterObservation] = {}
        self.pv_claims: set[str] = set()  # namespaces a fake PV still claimRefs to
        self.admission_confined = True
        self.admission_missing: set[str] = set()
        self.admission_checks: list[tuple[str, str, str | None]] = []
        self.events: list[tuple] = []

    def observe(self, cell_id: str, namespace: str) -> ClusterObservation:
        return self.observations.get(cell_id, ClusterObservation())

    def apply_all(self, manifests: list[dict]) -> None:
        for manifest in manifests:
            key = (manifest["metadata"].get("namespace", ""), manifest["kind"], manifest["metadata"]["name"])
            self.applied[key] = manifest
            if manifest["kind"] == "StatefulSet":
                self.events.append(("apply_statefulset", key[0], manifest["spec"]["replicas"]))

    def delete_namespace(self, name: str) -> None:
        self.deleted_namespaces.add(name)

    def delete_job(self, namespace: str, name: str) -> None:
        self.events.append(("delete_job", namespace, name))

    def run_job(self, manifest: dict) -> None:
        self.jobs.append(manifest)

    def namespace_absent(self, name: str) -> bool:
        return name in self.deleted_namespaces

    def pv_absent_for_namespace(self, namespace: str) -> bool:
        return namespace not in self.pv_claims

    def admission_policy_present(self, policy_name: str, binding_name: str, *, param_name: str | None = None) -> bool:
        self.admission_checks.append((policy_name, binding_name, param_name))
        return self.admission_confined and policy_name not in self.admission_missing

    def list_cell_namespaces(self) -> dict[str, str]:
        return {}

    def capacity_inputs(
        self, *, csi_driver: str
    ) -> tuple[dict[str, int | None], dict[str, int], dict[str, int]]:
        return {}, {}, {}


def _secrets_config() -> reconcile.SecretsConfig:
    return reconcile.SecretsConfig(
        cell_token_key_current=b"c" * 32,
        cell_token_key_previous=None,
        cell_token_key_version=1,
        cell_token_key_previous_version=None,
        backup_master_keys={1: b"m" * 32},
        backup_master_key_current_version=1,
    )


def _cluster_config() -> reconcile.ClusterConfig:
    return reconcile.ClusterConfig(object_storage_bucket="exomem-cloud-backups")


async def _seed_cell(cell_db: CellDatabase, cell_id: str, tenant_id: str, *, desired_state: str = "running") -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        tenant = await insert_tenant(owner, tenant_id)
        await owner.execute(
            "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state) VALUES ($1, $2, $3)",
            cell_id,
            tenant,
            desired_state,
        )
        await owner.execute(
            "INSERT INTO exomem_cloud_settings (key, value) VALUES ('cell_image', to_jsonb($1::text)) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", IMAGE_A
        )
    finally:
        await owner.close()


async def test_new_cell_is_applied_and_converges_to_running(cell_db: CellDatabase) -> None:
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    b2 = FakeB2()
    hetzner = FakeHetznerVolumeProvider()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    try:
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        rows = await db.select_all_rows(connection)
        assert rows[0].observed_state == "provisioning"
        namespace = namespace_name("aaaaaaaaaaaaaaaa")
        assert (namespace, "StatefulSet", "cell") in cluster.applied
        assert ("", "Namespace", namespace) in cluster.applied
        assert rows[0].b2_key_id is not None  # created on first pass
        assert rows[0].backup_key_wrapped is not None

        # Simulate the pod becoming ready on the StatefulSet the first pass applied.
        cluster.observations["aaaaaaaaaaaaaaaa"] = _observed_from_applied(cluster, "aaaaaaaaaaaaaaaa")
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        rows = await db.select_all_rows(connection)
        assert rows[0].observed_state == "running"
        assert rows[0].ready is True
        assert rows[0].observed_image == IMAGE_A

        rollout = await db.read_rollout(connection)
        assert rollout.last_good_image == IMAGE_A
    finally:
        await connection.close()


async def test_reused_object_storage_key_is_stable_across_passes(cell_db: CellDatabase) -> None:
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    b2 = FakeB2()
    hetzner = FakeHetznerVolumeProvider()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    try:
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        first_rows = await db.select_all_rows(connection)
        first_key_id = first_rows[0].b2_key_id

        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        second_rows = await db.select_all_rows(connection)
        assert second_rows[0].b2_key_id == first_key_id
        assert b2.key_exists(first_key_id)
    finally:
        await connection.close()


async def test_deletion_runs_to_completion_across_passes(cell_db: CellDatabase) -> None:
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    b2 = FakeB2()
    hetzner = FakeHetznerVolumeProvider()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    try:
        # First: create it and let it become ready with a real b2 key and backup key.
        cluster.observations["aaaaaaaaaaaaaaaa"] = _served()
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        b2_key_id = (await db.select_all_rows(connection))[0].b2_key_id
        b2.seed_object("cells/aaaaaaaaaaaaaaaa/snapshot-1")

        owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
        try:
            await owner.execute(
                "UPDATE exomem_cloud_cells SET desired_state = 'deleted' WHERE cell_id = 'aaaaaaaaaaaaaaaa'"
            )
        finally:
            await owner.close()

        namespace = namespace_name("aaaaaaaaaaaaaaaa")

        # Pass 1: namespace still "exists" in the fake until we simulate its removal.
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        assert namespace in cluster.deleted_namespaces
        rows = await db.select_all_rows(connection)
        assert rows[0].observed_state == "deleting"

        # Simulate the namespace (and its PVC/PV) actually being gone now.
        cluster.observations["aaaaaaaaaaaaaaaa"] = ClusterObservation()

        # Pass 2: namespace absent -> checks backup objects next, deletes them.
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        assert b2.list_object_versions("cells/aaaaaaaaaaaaaaaa/") == []

        # Pass 3: backup objects gone -> deletes the b2 key.
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        assert not b2.key_exists(b2_key_id)

        # Pass 4: everything confirmed absent -> reports deleted, nulls wrapped keys.
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        rows = await db.select_all_rows(connection)
        assert rows[0].observed_state == "deleted"
        assert rows[0].b2_key_id is None
        assert rows[0].backup_key_wrapped is None
    finally:
        await connection.close()


def _row(**overrides: object) -> CellRow:
    defaults: dict[str, object] = dict(
        cell_id="aaaaaaaaaaaaaaaa",
        tenant_id=tenant_uuid("tenant-a"),
        storage_gib=10,
        rollout_priority=1,
        desired_state="deleted",
        desired_image=None,
        generation=1,
        volume_id=None,
        b2_key_id=None,
    )
    defaults.update(overrides)
    return CellRow(**defaults)


def test_pv_absence_check_blocks_on_a_pv_still_claimed_by_the_namespace() -> None:
    # D10 amendment: a PV whose claimRef still names the cell's namespace
    # blocks deletion even though no Hetzner volume id was ever recorded.
    cluster = FakeClusterGateway()
    namespace = namespace_name("aaaaaaaaaaaaaaaa")
    cluster.pv_claims.add(namespace)
    row = _row(volume_id=None)
    observation = _augment_deletion_observation(
        ClusterObservation(), row, cluster, FakeB2(), FakeHetznerVolumeProvider()
    )
    assert observation.pv_absent_confirmed is False


def test_pv_absence_check_blocks_on_a_hetzner_volume_still_present() -> None:
    # Keeps the pre-existing Hetzner-volume-by-id check working alongside
    # the new claimRef check: no PV claims the namespace, but the Hetzner
    # volume itself has not been confirmed gone.
    cluster = FakeClusterGateway()
    hetzner = FakeHetznerVolumeProvider()
    hetzner.add_volume(VolumeInfo(volume_id="vol-1", server_id="node-1", labels={}))
    row = _row(volume_id="vol-1")
    observation = _augment_deletion_observation(ClusterObservation(), row, cluster, FakeB2(), hetzner)
    assert observation.pv_absent_confirmed is False


def test_pv_absence_check_passes_when_neither_check_finds_anything() -> None:
    cluster = FakeClusterGateway()
    row = _row(volume_id=None)
    observation = _augment_deletion_observation(
        ClusterObservation(), row, cluster, FakeB2(), FakeHetznerVolumeProvider()
    )
    assert observation.pv_absent_confirmed is True


async def test_a_converged_ready_cell_is_still_considered_for_its_nightly_backup(
    cell_db: CellDatabase,
) -> None:
    # Regression: reconcile_once's per-row skip was gated on row.is_dirty()
    # alone. A converged, ready row is never "dirty" by generation or
    # readiness, but a nightly backup is a time-based trigger unrelated to
    # dirtiness -- the old gate silently skipped decide() forever for any
    # cell that had ever finished converging, so a nightly backup could
    # never actually start. Found live against a real K3s cluster in 3.10.
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    b2 = FakeB2()
    hetzner = FakeHetznerVolumeProvider()
    now = datetime(2026, 1, 1, 3, tzinfo=UTC)  # inside the default 02:00-05:00 backup window

    try:
        # Converge to running, ready -- exactly the pre-existing
        # "test_new_cell_is_applied_and_converges_to_running" shape.
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        cluster.observations["aaaaaaaaaaaaaaaa"] = _observed_from_applied(cluster, "aaaaaaaaaaaaaaaa")
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        rows = await db.select_all_rows(connection)
        assert rows[0].observed_state == "running"
        assert rows[0].ready is True
        assert rows[0].is_dirty() is False  # the exact condition the old gate mis-skipped on

        # A third pass, same clean/ready observation, same "now": with
        # last_backup_at still NULL, a nightly backup is due right now.
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        rows = await db.select_all_rows(connection)
        assert rows[0].hold_kind == "backup"
    finally:
        await connection.close()


IMAGE_B = "registry.example/cell@sha256:" + "b" * 64


async def test_a_converged_ready_cell_is_still_considered_for_a_fresh_upgrade(
    cell_db: CellDatabase,
) -> None:
    # Regression: the same is_dirty()-gated skip that hid the nightly-backup
    # bug above also hid a D6 upgrade start. A converged, ready row is
    # exactly what select_upgrade_candidate() picks -- but the per-row skip
    # ran before decide() ever saw start_upgrade=True, so a new cell_image
    # setting could sit there forever with the upgrade candidate chosen but
    # never actually started. Found live against a real K3s cluster in 3.10.
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    b2 = FakeB2()
    hetzner = FakeHetznerVolumeProvider()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    try:
        # Converge to running, ready on IMAGE_A -- same shape as above.
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        cluster.observations["aaaaaaaaaaaaaaaa"] = _observed_from_applied(cluster, "aaaaaaaaaaaaaaaa")
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        rows = await db.select_all_rows(connection)
        assert rows[0].observed_state == "running"
        assert rows[0].ready is True
        assert rows[0].is_dirty() is False  # the exact condition the old gate mis-skipped on

        # A new target image lands; a third pass, same clean/ready
        # observation, must still start the upgrade rather than skip it.
        # (last_backup_at is set first so a nightly backup isn't *also* due
        # this same pass -- that has its own, higher-precedence hold and
        # would mask the exact skip this test targets.)
        owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
        try:
            await owner.execute(
                "UPDATE exomem_cloud_cells SET last_backup_at = $1 WHERE cell_id = $2", now, "aaaaaaaaaaaaaaaa"
            )
            await owner.execute(
                "INSERT INTO exomem_cloud_settings (key, value) VALUES ('cell_image', to_jsonb($1::text)) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", IMAGE_B
            )
        finally:
            await owner.close()
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        rows = await db.select_all_rows(connection)
        assert rows[0].hold_kind == "upgrade"
    finally:
        await connection.close()


async def test_run_loop_wakes_on_an_insert_notification_for_a_fresh_row(
    cell_db: CellDatabase,
) -> None:
    """C1 awareness: the schema's exomem_cloud_cells_notify_insert trigger
    pg_notifies on channel db.NOTIFY_CHANNEL for every INSERT, including a
    brand-new cell's row -- generation 1 (the column default) and
    observed_state NULL (no default; the row has never been observed),
    exactly the "generation-1 row with no observed state" case. This proves
    reconcile.run_loop's LISTEN (not just its poll) is what picks up a cell
    created after the loop is already running: the row must converge to
    "provisioning" well inside POLL_INTERVAL_SECONDS (5s), not only after
    the poll timeout would eventually retry anyway.
    """

    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute(
            "INSERT INTO exomem_cloud_settings (key, value) VALUES ('cell_image', to_jsonb($1::text)) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", IMAGE_A
        )
    finally:
        await owner.close()

    cluster = FakeClusterGateway()
    b2 = FakeB2()
    hetzner = FakeHetznerVolumeProvider()
    task = asyncio.create_task(
        reconcile.run_loop(
            cell_db.dsn(role="exomem_cellctl"),
            cluster,
            b2,
            hetzner,
            _secrets_config(),
            _cluster_config(),
        )
    )
    try:
        # Let the loop's first pass (an empty table -- nothing to do) finish
        # and its listener attach, so the INSERT below is what wakes it,
        # not the unconditional first pass every run_loop makes on startup.
        await asyncio.sleep(0.2)

        owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
        try:
            tenant = await insert_tenant(owner, "tenant-notify")
            await owner.execute(
                "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state) "
                "VALUES ('ffffffffffffffff', $1, 'running')",
                tenant,
            )
        finally:
            await owner.close()

        connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
        try:
            deadline = time.monotonic() + 2.0  # well inside the 5s poll interval
            observed = None
            while time.monotonic() < deadline:
                rows = await db.select_all_rows(connection)
                match = next((r for r in rows if r.cell_id == "ffffffffffffffff"), None)
                if match is not None and match.observed_state == "provisioning":
                    observed = match
                    break
                await asyncio.sleep(0.05)
            assert observed is not None, (
                "the fresh row's INSERT notification did not wake run_loop in time"
            )
        finally:
            await connection.close()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class _RaisingObserveGateway(FakeClusterGateway):
    """H4: one row's observe() raises; every other row must still be
    processed and capacity must still be published."""

    def __init__(self, raising_cell_id: str) -> None:
        super().__init__()
        self._raising_cell_id = raising_cell_id
        self.capacity_written = False

    def observe(self, cell_id: str, namespace: str) -> ClusterObservation:
        if cell_id == self._raising_cell_id:
            raise RuntimeError("simulated observe() failure")
        return super().observe(cell_id, namespace)

    def capacity_inputs(self, *, csi_driver: str):
        self.capacity_written = True
        return {"node-1": 10}, {"node-1": 2}, {"node-1": 0}


async def test_one_rows_observe_failure_does_not_abort_the_other_rows_or_capacity(
    cell_db: CellDatabase,
) -> None:
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")  # will raise on observe()
    await _seed_cell(cell_db, "bbbbbbbbbbbbbbbb", "tenant-b")  # must still converge
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _RaisingObserveGateway("aaaaaaaaaaaaaaaa")
    b2 = FakeB2()
    hetzner = FakeHetznerVolumeProvider()
    now = datetime(2026, 1, 1, 3, tzinfo=UTC)

    try:
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        # The healthy row still converged and applied.
        assert rows["bbbbbbbbbbbbbbbb"].observed_state == "provisioning"
        namespace_b = namespace_name("bbbbbbbbbbbbbbbb")
        assert (namespace_b, "StatefulSet", "cell") in cluster.applied
        # The raising row was simply skipped this pass, not fatal to anything.
        assert rows["aaaaaaaaaaaaaaaa"].observed_state is None
        # Capacity is still published at the end of the pass regardless.
        assert cluster.capacity_written is True
        capacity_row = await connection.fetchrow(
            "SELECT cell_slots, attachments_used FROM exomem_cloud_capacity WHERE node = 'node-1'"
        )
        assert capacity_row is not None
        # allocatable=10, non_cell_attachments=0, headroom=0 -> cell_slots=10;
        # attachments_used is published as observed, not subtracted again.
        assert capacity_row["cell_slots"] == 10
        assert capacity_row["attachments_used"] == 2
    finally:
        await connection.close()


async def test_one_rows_apply_failure_does_not_abort_the_other_rows(cell_db: CellDatabase) -> None:
    # H4's other half: a row that raises while being decided/applied (not
    # just observed) must not take any other row down with it either.
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    await _seed_cell(cell_db, "bbbbbbbbbbbbbbbb", "tenant-b")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))

    class _RaisingApplyGateway(FakeClusterGateway):
        def apply_all(self, manifests: list[dict]) -> None:
            for manifest in manifests:
                if manifest["metadata"].get("namespace") == namespace_name("aaaaaaaaaaaaaaaa"):
                    raise RuntimeError("simulated apply() failure")
            super().apply_all(manifests)

    cluster = _RaisingApplyGateway()
    b2 = FakeB2()
    hetzner = FakeHetznerVolumeProvider()
    now = datetime(2026, 1, 1, 3, tzinfo=UTC)

    try:
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        assert rows["bbbbbbbbbbbbbbbb"].observed_state == "provisioning"
        assert (namespace_name("bbbbbbbbbbbbbbbb"), "StatefulSet", "cell") in cluster.applied
    finally:
        await connection.close()


async def test_self_check_skips_the_whole_pass_when_the_admission_policy_is_missing(
    cell_db: CellDatabase,
) -> None:
    # D4: without cellctl's own admission confinement in place, its
    # ClusterRole is close to cluster-admin. Do nothing rather than act
    # unconfined -- Helm can install the Deployment before the policy.
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    cluster.admission_confined = False
    b2 = FakeB2()
    hetzner = FakeHetznerVolumeProvider()
    now = datetime(2026, 1, 1, 3, tzinfo=UTC)

    try:
        await reconcile.reconcile_once(
            connection, cluster, b2, hetzner, _secrets_config(), _cluster_config(), now=now
        )
        assert cluster.applied == {}
        rows = await db.select_all_rows(connection)
        assert rows[0].observed_state is None  # untouched -- nothing ran this pass
    finally:
        await connection.close()


async def test_run_loop_reconnects_after_its_database_session_is_killed(
    cell_db: CellDatabase,
) -> None:
    """H6: run_loop must not spin forever on a dead connection. Killing its
    own backend mid-flight must be followed by a reconnect (with the
    single-writer lock and LISTEN both re-established) and a full pass, not
    silence until the test's own timeout."""

    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute(
            "INSERT INTO exomem_cloud_settings (key, value) VALUES ('cell_image', to_jsonb($1::text)) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", IMAGE_A
        )
    finally:
        await owner.close()

    cluster = FakeClusterGateway()
    b2 = FakeB2()
    hetzner = FakeHetznerVolumeProvider()
    task = asyncio.create_task(
        reconcile.run_loop(
            cell_db.dsn(role="exomem_cellctl"),
            cluster,
            b2,
            hetzner,
            _secrets_config(),
            _cluster_config(),
        )
    )
    try:
        await asyncio.sleep(0.3)  # let the loop connect, lock and complete its first pass

        admin = await asyncpg.connect(cell_db.server.admin_dsn)
        try:
            # Mirror conftest.py's _drop_cell_db exactly: a plain (non-
            # aggregate) query with only `pid <> pg_backend_pid()` as a
            # guard. Postgres does not guarantee qual evaluation order, and
            # wrapping this in count(*)/fetchval (even with an extra
            # `usename =` qual ANDed in) let pg_terminate_backend(pid) fire
            # for this connection's own row before the guard was checked --
            # this backend killed itself the first two times this test was
            # written this way. The two-qual, non-aggregate shape below is
            # the only form proven safe in this suite (it already runs
            # after every single test via the cell_db fixture teardown).
            target_pids = await admin.fetch(
                "SELECT pid FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
                cell_db.name,
            )
            assert len(target_pids) > 0, "no exomem_cellctl backend found to kill"
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                cell_db.name,
            )
        finally:
            await admin.close()

        owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
        try:
            tenant = await insert_tenant(owner, "tenant-reconnect")
            await owner.execute(
                "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state) "
                "VALUES ('eeeeeeeeeeeeeeee', $1, 'running')",
                tenant,
            )
        finally:
            await owner.close()

        connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
        try:
            deadline = time.monotonic() + 20.0
            observed = None
            while time.monotonic() < deadline:
                rows = await db.select_all_rows(connection)
                match = next((r for r in rows if r.cell_id == "eeeeeeeeeeeeeeee"), None)
                if match is not None and match.observed_state == "provisioning":
                    observed = match
                    break
                await asyncio.sleep(0.2)
            assert observed is not None, "run_loop never resumed after its connection was killed"
        finally:
            await connection.close()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class _RaisingCapacityGateway(FakeClusterGateway):
    def capacity_inputs(self, *, csi_driver: str):
        raise ValueError("Invalid value for `drivers`, must not be `None`")


async def test_a_capacity_read_failure_does_not_fail_a_pass_whose_rows_succeeded(
    cell_db: CellDatabase,
) -> None:
    await _seed_cell(cell_db, "cccccccccccccccc", "tenant-c")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _RaisingCapacityGateway()
    now = datetime(2026, 1, 1, 3, tzinfo=UTC)

    try:
        await reconcile.reconcile_once(
            connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(), _cluster_config(), now=now
        )
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        assert rows["cccccccccccccccc"].observed_state == "provisioning"
        assert (namespace_name("cccccccccccccccc"), "StatefulSet", "cell") in cluster.applied
    finally:
        await connection.close()


async def test_an_init_deadline_failure_is_observed_every_pass_but_never_re_applied(cell_db: CellDatabase) -> None:
    # D4 "Waiting is normal": the row stays selected, is not re-applied, and
    # recovers to running with the code cleared once its pod is Ready.
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await db.write_observed(
            connection,
            cell_id,
            {"observed_state": "failed", "observed_generation": 1, "last_error_code": "INIT_DEADLINE_EXCEEDED"},
        )
        crash_looping = _served(
            pod_ready=False,
            ready_pod_image=None,
            pod_exists=True,
            pod_created_at=now - timedelta(minutes=30),
            statefulset_row_generation=1,
        )
        cluster.observations[cell_id] = crash_looping
        await reconcile.reconcile_once(
            connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(), _cluster_config(), now=now
        )
        assert cluster.applied == {}
        row = (await db.select_all_rows(connection))[0]
        assert (row.observed_state, row.last_error_code) == ("failed", "INIT_DEADLINE_EXCEEDED")

        cluster.observations[cell_id] = _served(pod_exists=True, pod_init_completed=True, statefulset_row_generation=1)
        await reconcile.reconcile_once(
            connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(), _cluster_config(), now=now
        )
        assert cluster.applied == {}
        row = (await db.select_all_rows(connection))[0]
        assert (row.observed_state, row.ready, row.last_error_code) == ("running", True, None)
    finally:
        await connection.close()


def _observed_from_applied(cluster: FakeClusterGateway, cell_id: str, **overrides: object) -> ClusterObservation:
    """What ClusterClient.observe() reports right after the last apply: the
    StatefulSet's own image, replicas and annotations read back, plus a pod
    that is Ready on its update revision unless overridden."""

    from cellctl.k8s_client import _parse_timestamp
    from cellctl.manifests import (
        HOLD_ANNOTATION,
        HOLD_STARTED_ANNOTATION,
        PRE_UPGRADE_SNAPSHOT_ANNOTATION,
        PREVIOUS_IMAGE_ANNOTATION,
        RENDER_DIGEST_ANNOTATION,
        RENDER_DIGEST_APPLIED_AT_ANNOTATION,
        RESTORED_SNAPSHOT_ANNOTATION,
        ROW_GENERATION_ANNOTATION,
        TARGET_APPLIED_ANNOTATION,
    )

    statefulset = cluster.applied[(namespace_name(cell_id), "StatefulSet", "cell")]
    annotations = statefulset["metadata"]["annotations"]
    image = statefulset["spec"]["template"]["spec"]["containers"][0]["image"]
    running = statefulset["spec"]["replicas"] > 0
    fields: dict[str, object] = dict(
        namespace_exists=True,
        namespace_cell_label=cell_id,
        pvc_bound=True,
        pvc_uid="pvc-uid-1",
        pv_claim_ref_uid="pvc-uid-1",
        pv_storage_class=STORAGE_CLASS,
        statefulset_exists=True,
        statefulset_image=image,
        statefulset_replicas=statefulset["spec"]["replicas"],
        statefulset_hold_kind=annotations.get(HOLD_ANNOTATION),
        statefulset_hold_started_at=_parse_timestamp(annotations.get(HOLD_STARTED_ANNOTATION)),
        statefulset_previous_image=annotations.get(PREVIOUS_IMAGE_ANNOTATION),
        statefulset_pre_upgrade_snapshot=annotations.get(PRE_UPGRADE_SNAPSHOT_ANNOTATION),
        statefulset_target_applied_at=_parse_timestamp(annotations.get(TARGET_APPLIED_ANNOTATION)),
        statefulset_restored_snapshot=annotations.get(RESTORED_SNAPSHOT_ANNOTATION),
        statefulset_render_digest=annotations.get(RENDER_DIGEST_ANNOTATION),
        statefulset_render_digest_applied_at=_parse_timestamp(annotations.get(RENDER_DIGEST_APPLIED_AT_ANNOTATION)),
        statefulset_row_generation=int(annotations[ROW_GENERATION_ANNOTATION])
        if ROW_GENERATION_ANNOTATION in annotations
        else None,
        pod_exists=running,
        pod_uses_volume=running,
        pod_ready=running,
        pod_init_completed=running,
        ready_pod_image=image if running else None,
    )
    fields.update(overrides)
    return ClusterObservation(**fields)


async def _converge(connection, cluster: FakeClusterGateway, cell_id: str, now: datetime) -> None:
    for _ in range(2):
        await reconcile.reconcile_once(
            connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(), _cluster_config(), now=now
        )
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
    await reconcile.reconcile_once(
        connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(), _cluster_config(), now=now
    )


async def test_an_env_flip_is_recorded_only_by_the_pass_after_it_was_applied(cell_db: CellDatabase) -> None:
    # M4: the pass that applies READ_ONLY must not also record convergence
    # from the pod it saw before the apply.
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge(connection, cluster, cell_id, now)
        row = (await db.select_all_rows(connection))[0]
        assert (row.observed_state, row.ready, row.observed_generation) == ("running", True, 1)

        owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
        try:
            await owner.execute("UPDATE exomem_cloud_cells SET desired_state = 'read_only' WHERE cell_id = $1", cell_id)
        finally:
            await owner.close()
        await reconcile.reconcile_once(
            connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(), _cluster_config(), now=now
        )
        env = cluster.applied[(namespace_name(cell_id), "StatefulSet", "cell")]["spec"]["template"]["spec"]["containers"][0]["env"]
        assert {"name": "EXOMEM_CLOUD_READ_ONLY", "value": "1"} in env
        row = (await db.select_all_rows(connection))[0]
        assert row.observed_generation == 1 and row.ready is False

        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
        await reconcile.reconcile_once(
            connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(), _cluster_config(), now=now
        )
        row = (await db.select_all_rows(connection))[0]
        assert (row.observed_state, row.ready, row.observed_generation) == ("read_only", True, 2)
    finally:
        await connection.close()


async def test_a_notification_during_a_pass_triggers_the_next_pass(cell_db: CellDatabase, monkeypatch) -> None:
    # D4: a pending notification is cleared before a pass starts, never
    # after it, so one that arrives during a pass triggers the next one.
    real_pass = reconcile.reconcile_once
    passes: list[float] = []

    async def pass_that_is_notified_midway(connection, *args, **kwargs):
        passes.append(time.monotonic())
        await real_pass(connection, *args, **kwargs)
        if len(passes) == 1:
            owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
            try:
                await owner.execute("SELECT pg_notify($1, 'mid-pass')", db.NOTIFY_CHANNEL)
            finally:
                await owner.close()
            await asyncio.sleep(0.3)  # the notification lands while this pass is still running

    monkeypatch.setattr(reconcile, "reconcile_once", pass_that_is_notified_midway)
    task = asyncio.create_task(
        reconcile.run_loop(
            cell_db.dsn(role="exomem_cellctl"), FakeClusterGateway(), FakeB2(), FakeHetznerVolumeProvider(),
            _secrets_config(), _cluster_config(),
        )
    )
    try:
        deadline = time.monotonic() + 2.0  # well inside the 5 s poll interval
        while time.monotonic() < deadline and len(passes) < 2:
            await asyncio.sleep(0.05)
        assert len(passes) >= 2, "a notification received during a pass was dropped"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_timed_out_pass_reopens_the_database_session(cell_db: CellDatabase, monkeypatch) -> None:
    # The command timeout is what detects a half-open session on the client
    # side; the loop must then reopen it rather than retry on it forever.
    real_connect = db.connect
    connects: list[str] = []
    calls = {"passes": 0}

    async def counting_connect(dsn: str):
        connects.append(dsn)
        return await real_connect(dsn)

    async def pass_that_times_out_once(connection, *args, **kwargs):
        calls["passes"] += 1
        if calls["passes"] == 1:
            raise TimeoutError

    monkeypatch.setattr(db, "connect", counting_connect)
    monkeypatch.setattr(reconcile, "reconcile_once", pass_that_times_out_once)
    task = asyncio.create_task(
        reconcile.run_loop(
            cell_db.dsn(role="exomem_cellctl"), FakeClusterGateway(), FakeB2(), FakeHetznerVolumeProvider(),
            _secrets_config(), _cluster_config(),
        )
    )
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(connects) < 2:
            await asyncio.sleep(0.05)
        assert len(connects) >= 2
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_session_lost_inside_a_row_aborts_the_pass_so_the_loop_reconnects(cell_db: CellDatabase) -> None:
    # The per-row boundary isolates one row's failure, but a lost session is
    # every row's failure: it must reach run_loop's reconnect, not be logged
    # once per row against a dead connection.
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))

    class _PartitionedGateway(FakeClusterGateway):
        def apply_all(self, manifests: list[dict]) -> None:
            connection.terminate()
            super().apply_all(manifests)

    with pytest.raises(asyncpg.InterfaceError):
        await reconcile.reconcile_once(
            connection, _PartitionedGateway(), FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(),
            _cluster_config(), now=datetime(2026, 1, 1, 12, tzinfo=UTC),
        )


# --- D4 digest gating: one in-flight re-apply at a time ---------------------

_NOW_DIGEST = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _clean_row(cell_id: str, **overrides: object) -> CellRow:
    defaults: dict[str, object] = dict(
        cell_id=cell_id,
        tenant_id=tenant_uuid(f"tenant-{cell_id[0]}"),
        storage_gib=10,
        rollout_priority=1,
        desired_state="running",
        desired_image=None,
        generation=1,
        observed_generation=1,
        observed_state="running",
        ready=True,
    )
    defaults.update(overrides)
    return CellRow(**defaults)


def _select(rows, observations, digests, now=_NOW_DIGEST):
    return reconcile._select_render_digest_candidate(rows, observations, digests, now, reconcile.DEFAULT_RECONCILE_CONFIG)


def test_a_cell_not_ready_for_another_reason_never_blocks_the_digest_rollout() -> None:
    a, b, broken = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"
    rows = [_clean_row(a), _clean_row(b), _clean_row(broken, observed_state="failed", ready=False)]
    observations = {
        a: _served(a, statefulset_render_digest="old"),
        b: _served(b, statefulset_render_digest="old"),
        broken: _served(broken, statefulset_render_digest="old", pod_ready=False),
    }
    assert _select(rows, observations, {a: "new", b: "new", broken: "new"}) == a


def test_only_a_cell_in_flight_on_the_current_digest_holds_the_next_one_back() -> None:
    a, b = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    rows = [_clean_row(a, observed_state="provisioning", ready=False), _clean_row(b)]
    digests = {a: "new", b: "new"}

    def observations(applied_at):
        return {
            a: _served(a, statefulset_render_digest="new", statefulset_render_digest_applied_at=applied_at, pod_ready=False),
            b: _served(b, statefulset_render_digest="old"),
        }

    assert _select(rows, observations(_NOW_DIGEST - timedelta(minutes=2)), digests) is None
    # Re-applied more than 10 minutes ago and still not Ready: no longer in flight.
    assert _select(rows, observations(_NOW_DIGEST - timedelta(minutes=11)), digests) == b


def test_a_row_whose_statefulset_was_refused_is_never_a_digest_candidate() -> None:
    a, b = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    rows = [_clean_row(a, last_error_code="MANIFEST_IMMUTABLE"), _clean_row(b)]
    observations = {a: _served(a, statefulset_render_digest="old"), b: _served(b, statefulset_render_digest="old")}
    digests = {a: "new", b: "new"}
    config = reconcile.DEFAULT_RECONCILE_CONFIG
    parked = frozenset({a})
    assert reconcile._select_render_digest_candidate(rows, observations, digests, _NOW_DIGEST, config, parked, parked) == b
    # Parked for another object only: its rotation is not held back.
    assert reconcile._select_render_digest_candidate(rows, observations, digests, _NOW_DIGEST, config, parked) == a


def _rotated_secrets() -> reconcile.SecretsConfig:
    return reconcile.SecretsConfig(
        cell_token_key_current=b"n" * 32,
        cell_token_key_previous=b"c" * 32,
        cell_token_key_version=2,
        cell_token_key_previous_version=1,
        backup_master_keys={1: b"m" * 32},
        backup_master_key_current_version=1,
    )


async def test_a_bearer_rotation_reaches_every_healthy_cell_past_a_broken_one(cell_db: CellDatabase) -> None:
    # NEW5: one broken serving cell must not stall a fleet-wide rotation, and
    # the healthy cells still restart one at a time.
    from cellctl.manifests import RENDER_DIGEST_APPLIED_AT_ANNOTATION

    a, b, broken = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"
    for cell_id in (a, b, broken):
        await _seed_cell(cell_db, cell_id, f"tenant-{cell_id[0]}")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    now = _NOW_DIGEST

    async def run(secrets: reconcile.SecretsConfig) -> None:
        await reconcile.reconcile_once(
            connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), secrets, _cluster_config(), now=now
        )

    def rotated(cell_id: str) -> bool:
        return "cell-token-previous" in cluster.applied[(namespace_name(cell_id), "Secret", "cell-credentials")]["data"]

    try:
        for _ in range(3):
            await run(_secrets_config())
            for cell_id in (a, b, broken):
                cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
        # The broken cell's pod crash-loops for a reason of its own.
        cluster.observations[broken] = _observed_from_applied(cluster, broken, pod_ready=False, ready_pod_image=None)

        await run(_rotated_secrets())
        assert (rotated(a), rotated(b)) == (True, False)
        statefulset = cluster.applied[(namespace_name(a), "StatefulSet", "cell")]
        assert statefulset["metadata"]["annotations"][RENDER_DIGEST_APPLIED_AT_ANNOTATION] == now.isoformat()

        # A restarts on the new template: in flight, so B waits.
        cluster.observations[a] = _observed_from_applied(cluster, a, pod_ready=False, ready_pod_image=None)
        now += timedelta(seconds=5)
        await run(_rotated_secrets())
        assert rotated(b) is False
        # A routine re-apply of A carries the applied-at forward.
        statefulset = cluster.applied[(namespace_name(a), "StatefulSet", "cell")]
        assert statefulset["metadata"]["annotations"][RENDER_DIGEST_APPLIED_AT_ANNOTATION] == _NOW_DIGEST.isoformat()

        # A is Ready again: B goes next, although the broken cell is still down.
        cluster.observations[a] = _observed_from_applied(cluster, a)
        now += timedelta(seconds=5)
        await run(_rotated_secrets())
        assert rotated(b) is True
    finally:
        await connection.close()


# --- D4 refusals ---------------------------------------------------------------

from kubernetes.client.rest import ApiException  # noqa: E402

_ATTEMPT_SNAPSHOT = "e" * 64


class _RefusingGateway(FakeClusterGateway):
    """Answers the StatefulSet apply for `cell_id` with `status` while armed."""

    def __init__(self, cell_id: str, status: int, *, only_image: str | None = None, times: int | None = None) -> None:
        super().__init__()
        self.cell_id = cell_id
        self.status = status
        self.only_image = only_image
        self.times = times
        self.armed = False
        self.attempts = 0

    def apply_all(self, manifests: list[dict]) -> None:
        for manifest in manifests:
            if (
                self.armed
                and manifest["kind"] == "StatefulSet"
                and manifest["metadata"]["namespace"] == namespace_name(self.cell_id)
                and (self.only_image is None or manifest["spec"]["template"]["spec"]["containers"][0]["image"] == self.only_image)
            ):
                self.attempts += 1
                if self.times is None or self.attempts <= self.times:
                    raise ApiException(status=self.status, reason="refused by the test")
        super().apply_all(manifests)


async def _pass(connection, cluster, now, secrets=None, memory=None) -> None:
    await reconcile.reconcile_once(
        connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), secrets or _secrets_config(), _cluster_config(),
        now=now, memory=memory,
    )


async def _set_cell_image(cell_db: CellDatabase, image: str) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute("INSERT INTO exomem_cloud_settings (key, value) VALUES ('cell_image', to_jsonb($1::text)) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", image)
    finally:
        await owner.close()


async def _drive_to_first_target_apply(connection, cluster, cell_id: str, now: datetime) -> datetime:
    """From a converged cell and a new cell_image: start the hold, run and
    record the attempt's own backup. The next pass applies the target."""

    await _pass(connection, cluster, now)  # hold starts, scaled to 0
    cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
    now += timedelta(seconds=5)
    await _pass(connection, cluster, now)  # backup Job runs
    cluster.observations[cell_id] = _observed_from_applied(
        cluster, cell_id, backup_job_succeeded=True, backup_job_snapshot_id=_ATTEMPT_SNAPSHOT
    )
    now += timedelta(seconds=30)
    await _pass(connection, cluster, now)  # snapshot recorded
    cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
    return now + timedelta(seconds=5)


async def test_a_refused_routine_apply_is_parked_until_its_backoff_or_a_generation_change(cell_db: CellDatabase) -> None:
    # L6: a 422 (an immutable field, a PVC shrink) is a refusal. It sets
    # MANIFEST_IMMUTABLE, records no observed_generation, and is not
    # re-applied every 5 seconds; a generation change unparks it at once.
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _RefusingGateway(cell_id, 422)
    cluster.armed = True
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        for step in range(10):
            await _pass(connection, cluster, now + timedelta(seconds=5 * step), memory=memory)
        assert cluster.attempts == 1
        row = (await db.select_all_rows(connection))[0]
        assert (row.last_error_code, row.observed_generation) == ("MANIFEST_IMMUTABLE", None)

        owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
        try:
            await owner.execute("UPDATE exomem_cloud_cells SET desired_state = 'read_only' WHERE cell_id = $1", cell_id)
        finally:
            await owner.close()
        await _pass(connection, cluster, now + timedelta(minutes=1), memory=memory)
        assert cluster.attempts == 2
    finally:
        await connection.close()


async def test_a_throttled_routine_apply_is_transient_and_retried_next_pass(cell_db: CellDatabase) -> None:
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _RefusingGateway(cell_id, 429, times=1)
    cluster.armed = True
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _pass(connection, cluster, now)
        row = (await db.select_all_rows(connection))[0]
        assert row.last_error_code is None
        await _pass(connection, cluster, now + timedelta(seconds=5))
        assert cluster.attempts == 2
        assert (namespace_name(cell_id), "StatefulSet", "cell") in cluster.applied
    finally:
        await connection.close()


async def test_a_throttled_first_target_apply_never_pauses_the_fleet(cell_db: CellDatabase) -> None:
    # D6 step 3 uses the D4 classification: a 429 is transient, retried on
    # the next pass, and never TARGET_REJECTED.
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _RefusingGateway(cell_id, 429, only_image=IMAGE_B, times=1)
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge(connection, cluster, cell_id, now - timedelta(minutes=5))
        await _set_cell_image(cell_db, IMAGE_B)
        now = await _drive_to_first_target_apply(connection, cluster, cell_id, now)
        cluster.armed = True
        await _pass(connection, cluster, now)
        rollout = await db.read_rollout(connection)
        row = (await db.select_all_rows(connection))[0]
        assert rollout.paused is False and row.last_error_code != "TARGET_REJECTED" and row.hold_kind == "upgrade"
        await _pass(connection, cluster, now + timedelta(seconds=5))
        statefulset = cluster.applied[(namespace_name(cell_id), "StatefulSet", "cell")]
        assert statefulset["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE_B
    finally:
        await connection.close()


async def test_a_refused_first_target_apply_ends_the_attempt_as_target_rejected(cell_db: CellDatabase) -> None:
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _RefusingGateway(cell_id, 403, only_image=IMAGE_B)
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge(connection, cluster, cell_id, now - timedelta(minutes=5))
        await _set_cell_image(cell_db, IMAGE_B)
        now = await _drive_to_first_target_apply(connection, cluster, cell_id, now)
        cluster.armed = True
        await _pass(connection, cluster, now)
        rollout = await db.read_rollout(connection)
        row = (await db.select_all_rows(connection))[0]
        assert (rollout.paused, rollout.error_code, row.last_error_code) == (True, "TARGET_REJECTED", "TARGET_REJECTED")
        statefulset = cluster.applied[(namespace_name(cell_id), "StatefulSet", "cell")]
        assert statefulset["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE_A
        assert statefulset["spec"]["replicas"] == 1
    finally:
        await connection.close()


async def test_a_refused_digest_candidate_does_not_stall_the_rest_of_the_rotation(cell_db: CellDatabase) -> None:
    # NEW6: the refused cell is parked with MANIFEST_IMMUTABLE and is never
    # the digest candidate again, so the next cell rotates.
    a, b = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    for cell_id in (a, b):
        await _seed_cell(cell_db, cell_id, f"tenant-{cell_id[0]}")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _RefusingGateway(a, 422)
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        for _ in range(3):
            await _pass(connection, cluster, now, memory=memory)
            for cell_id in (a, b):
                cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
        cluster.armed = True
        for step in range(4):
            await _pass(
                connection, cluster, now + timedelta(seconds=5 * (step + 1)), secrets=_rotated_secrets(), memory=memory
            )
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        assert rows[a].last_error_code == "MANIFEST_IMMUTABLE"
        assert cluster.attempts == 1
        assert "cell-token-previous" in cluster.applied[(namespace_name(b), "Secret", "cell-credentials")]["data"]
    finally:
        await connection.close()


def test_a_refused_row_starts_no_upgrade_but_is_backed_up_unless_its_statefulset_was_refused() -> None:
    from cellctl.rollout import select_upgrade_candidate

    owner, parked, tenant = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"
    rows = [
        _clean_row(owner, rollout_priority=0, observed_image=IMAGE_B),
        _clean_row(parked, last_error_code="MANIFEST_IMMUTABLE", observed_image=IMAGE_A),
        _clean_row(tenant, rollout_priority=2, observed_image=IMAGE_A),
    ]
    observations = {
        owner: _served(owner, statefulset_image=IMAGE_B),
        parked: _served(parked),
        tenant: _served(tenant),
    }
    now = datetime(2026, 1, 1, 3, tzinfo=UTC)
    pick = select_upgrade_candidate(
        rows, observations, RolloutRow(), IMAGE_B, now=now, any_cell_already_upgrading=False, refused=frozenset({parked})
    )
    assert pick == tenant
    config = dataclasses.replace(reconcile.DEFAULT_RECONCILE_CONFIG, backup_concurrency=3)
    assert parked in reconcile._select_backup_candidates(rows, observations, now, config)
    assert parked not in reconcile._select_backup_candidates(rows, observations, now, config, frozenset({parked}))


async def test_a_pod_stuck_terminating_cannot_hold_the_only_backup_slot_all_night(cell_db: CellDatabase) -> None:
    # NEW7 / D8 "Bounded": the backup deadline runs from hold-started-at, so
    # it also bounds the wait for the volume; the failed hold deletes its
    # Job before the cell restarts, and the other due cell gets the slot.
    from cellctl.manifests import BACKUP_JOB_NAME, hold_job_name

    a, b = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    for cell_id in (a, b):
        await _seed_cell(cell_db, cell_id, f"tenant-{cell_id[0]}")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    window = datetime(2026, 1, 1, 2, tzinfo=UTC)
    try:
        for _ in range(3):
            await _pass(connection, cluster, window - timedelta(hours=3))
            for cell_id in (a, b):
                cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
        await _pass(connection, cluster, window)
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        held = next(cell_id for cell_id, row in rows.items() if row.hold_kind == "backup")
        other = b if held == a else a
        # The held cell's old pod never finishes terminating (a CSI unmount hang).
        cluster.observations[held] = _observed_from_applied(
            cluster, held, pod_exists=True, pod_terminating=True, pod_uses_volume=True, pod_ready=False
        )
        cluster.events.clear()
        for minutes in (5, 10, 16):
            await _pass(connection, cluster, window + timedelta(minutes=minutes))
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        assert rows[held].hold_kind is None and rows[held].last_error_code == "BACKUP_FAILED"
        job = hold_job_name(BACKUP_JOB_NAME, window.isoformat())
        held_events = [event for event in cluster.events if event[1] == namespace_name(held)]
        assert held_events[-2:] == [("delete_job", namespace_name(held), job), ("apply_statefulset", namespace_name(held), 1)]

        cluster.observations[held] = _observed_from_applied(cluster, held)
        await _pass(connection, cluster, window + timedelta(minutes=17))
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        assert rows[other].hold_kind == "backup"
    finally:
        await connection.close()


# --- D4 per-object refusals ----------------------------------------------------


class _ObjectRefusingGateway(FakeClusterGateway):
    """Applies each manifest in order, as ClusterClient does, answering any
    manifest `refuse(manifest)` names a status for with that ApiException."""

    def __init__(self, refuse=None) -> None:
        super().__init__()
        self.refuse = refuse
        self.refused: list[tuple[str, str]] = []

    def apply_all(self, manifests: list[dict]) -> None:
        for manifest in manifests:
            status = self.refuse(manifest) if self.refuse else None
            if status:
                self.refused.append((manifest["kind"], manifest["metadata"]["name"]))
                raise ApiException(status=status, reason="refused by the test")
            super().apply_all([manifest])


def _statefulset(cluster: FakeClusterGateway, cell_id: str) -> dict:
    return cluster.applied[(namespace_name(cell_id), "StatefulSet", "cell")]


def _refuse_kind(cell_id: str, kind: str, status: int = 422, *, name: str | None = None):
    def refuse(manifest: dict) -> int | None:
        metadata = manifest["metadata"]
        in_cell = metadata.get("namespace", metadata["name"]) == namespace_name(cell_id)
        if in_cell and manifest["kind"] == kind and (name is None or metadata["name"] == name):
            return status
        return None

    return refuse


async def _set_storage(cell_db: CellDatabase, cell_id: str, gib: int, *, desired_state: str | None = None) -> None:
    # storage_gib is not a generation column; a desired_state change alongside
    # it makes the next pass a routine apply that meets the PVC refusal.
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute(
            "UPDATE exomem_cloud_cells SET storage_gib = $2, desired_state = coalesce($3, desired_state) WHERE cell_id = $1",
            cell_id,
            gib,
            desired_state,
        )
    finally:
        await owner.close()


async def _converge_all(connection, cluster, cell_ids, now, memory) -> None:
    for _ in range(3):
        await _pass(connection, cluster, now, memory=memory)
        for cell_id in cell_ids:
            cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)


async def test_a_refused_pvc_inside_a_backup_hold_does_not_stop_the_hold_exiting_on_its_bound(cell_db: CellDatabase) -> None:
    # HIGH-1 (N1): a plan downgrade mid-hold makes every apply refuse the PVC
    # shrink. The StatefulSet still gets its own attempt, so the hold exits
    # on its deadline and the tenant is back at its desired replicas, while
    # the PVC refusal stays recorded.
    from cellctl.manifests import HOLD_ANNOTATION

    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _ObjectRefusingGateway()
    memory = reconcile.LoopMemory()
    window = datetime(2026, 1, 1, 2, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [cell_id], window - timedelta(hours=3), memory)
        await _pass(connection, cluster, window, memory=memory)
        assert _statefulset(cluster, cell_id)["metadata"]["annotations"].get(HOLD_ANNOTATION) == "backup"

        await _set_storage(cell_db, cell_id, 5)
        cluster.refuse = _refuse_kind(cell_id, "PersistentVolumeClaim")
        for minute in range(1, 61):
            cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
            await _pass(connection, cluster, window + timedelta(minutes=minute), memory=memory)

        statefulset = _statefulset(cluster, cell_id)
        assert (statefulset["spec"]["replicas"], statefulset["metadata"]["annotations"].get(HOLD_ANNOTATION)) == (1, None)
        row = (await db.select_all_rows(connection))[0]
        assert (row.hold_kind, row.last_error_code, row.observed_generation) == (None, "MANIFEST_IMMUTABLE", 1)
        pvc = cluster.applied[(namespace_name(cell_id), "PersistentVolumeClaim", "cell-data")]
        assert pvc["spec"]["resources"]["requests"]["storage"] == "10Gi"
    finally:
        await connection.close()


async def test_a_rotation_start_with_its_secret_refused_leaves_the_running_template_untouched(cell_db: CellDatabase) -> None:
    # R1: the rotated template references cell-token-previous, which only
    # the refused Secret carries. Outside a hold the StatefulSet is held
    # back, so the healthy pod keeps running; the other objects still apply.
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _ObjectRefusingGateway()
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [cell_id], now, memory)
        before = _statefulset(cluster, cell_id)
        cluster.applied.pop((namespace_name(cell_id), "Service", "cell"))
        cluster.refuse = _refuse_kind(cell_id, "Secret")
        await _pass(connection, cluster, now + timedelta(seconds=5), secrets=_rotated_secrets(), memory=memory)

        assert _statefulset(cluster, cell_id) is before
        assert (namespace_name(cell_id), "Service", "cell") in cluster.applied
        row = (await db.select_all_rows(connection))[0]
        assert row.last_error_code == "MANIFEST_IMMUTABLE"
    finally:
        await connection.close()


async def test_a_new_cell_whose_network_policy_is_refused_gets_no_statefulset(cell_db: CellDatabase) -> None:
    # R1: a StatefulSet that does not exist yet is created only when every
    # earlier object applied, so a new cell never starts unisolated.
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _ObjectRefusingGateway(_refuse_kind(cell_id, "NetworkPolicy", name="runtime-ingress"))
    try:
        await _pass(connection, cluster, datetime(2026, 1, 1, 12, tzinfo=UTC), memory=reconcile.LoopMemory())
        assert (namespace_name(cell_id), "StatefulSet", "cell") not in cluster.applied
        assert (namespace_name(cell_id), "NetworkPolicy", "default-deny") in cluster.applied
        row = (await db.select_all_rows(connection))[0]
        assert row.last_error_code == "MANIFEST_IMMUTABLE"
    finally:
        await connection.close()


async def test_a_pvc_only_refusal_on_a_running_cell_still_lets_a_rotation_reach_it(cell_db: CellDatabase) -> None:
    # R1 is narrow: a refused PVC never gates the StatefulSet outside a hold,
    # or a permanent plan-downgrade refusal would block rotation forever.
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _ObjectRefusingGateway()
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [cell_id], now, memory)
        await _set_storage(cell_db, cell_id, 5, desired_state="read_only")
        cluster.refuse = _refuse_kind(cell_id, "PersistentVolumeClaim")
        await _pass(connection, cluster, now + timedelta(seconds=5), memory=memory)
        assert cluster.refused == [("PersistentVolumeClaim", "cell-data")]
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
        await _pass(connection, cluster, now + timedelta(seconds=10), secrets=_rotated_secrets(), memory=memory)

        env = _statefulset(cluster, cell_id)["spec"]["template"]["spec"]["containers"][0]["env"]
        assert any(entry["name"] == "EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS" for entry in env)
        assert "cell-token-previous" in cluster.applied[(namespace_name(cell_id), "Secret", "cell-credentials")]["data"]
    finally:
        await connection.close()


# --- D4 refusal parking: keyed on (generation, digest, image), with backoff ----


async def _set_desired_state(cell_db: CellDatabase, cell_id: str, desired_state: str) -> None:
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await owner.execute("UPDATE exomem_cloud_cells SET desired_state = $2 WHERE cell_id = $1", cell_id, desired_state)
    finally:
        await owner.close()


async def test_a_new_cell_refused_for_a_bad_cell_image_provisions_once_the_setting_is_fixed(cell_db: CellDatabase) -> None:
    # N2: the image to render is part of the park key, so fixing the
    # cell_image setting unparks the row on the very next pass. Until then
    # the refused row is not re-applied at loop speed.
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    await _set_cell_image(cell_db, "registry.example/cell:latest")  # an operator slip: a tag, not a digest
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))

    def image_pin(manifest: dict) -> int | None:
        if manifest["kind"] != "StatefulSet":
            return None
        return None if "@sha256:" in manifest["spec"]["template"]["spec"]["containers"][0]["image"] else 403

    cluster = _ObjectRefusingGateway(image_pin)
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        for step in range(6):
            await _pass(connection, cluster, now + timedelta(seconds=5 * step), memory=memory)
        assert cluster.refused == [("StatefulSet", "cell")]
        row = (await db.select_all_rows(connection))[0]
        assert (row.last_error_code, row.observed_generation) == ("MANIFEST_IMMUTABLE", None)

        await _set_cell_image(cell_db, IMAGE_A)
        await _pass(connection, cluster, now + timedelta(seconds=35), memory=memory)
        assert _statefulset(cluster, cell_id)["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE_A
        assert (await db.select_all_rows(connection))[0].observed_state == "provisioning"
    finally:
        await connection.close()


async def test_one_refused_apply_on_a_running_cell_does_not_stop_its_backup_or_rotation(cell_db: CellDatabase) -> None:
    # N3: a permanent PVC refusal (a plan downgrade) parks the row, but the
    # night's backup still runs and a bearer rotation still reaches it, and
    # the refused object is retried on the backoff, never at loop speed.
    from cellctl.manifests import HOLD_ANNOTATION

    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _ObjectRefusingGateway()
    memory = reconcile.LoopMemory()
    window = datetime(2026, 1, 2, 2, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [cell_id], window - timedelta(days=1, hours=3), memory)
        await _set_storage(cell_db, cell_id, 5, desired_state="read_only")
        cluster.refuse = _refuse_kind(cell_id, "PersistentVolumeClaim")
        await _pass(connection, cluster, window - timedelta(days=1), memory=memory)
        assert cluster.refused == [("PersistentVolumeClaim", "cell-data")]
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
        await _pass(connection, cluster, window - timedelta(days=1) + timedelta(seconds=5), memory=memory)
        for minute in range(0, 180, 5):
            if _statefulset(cluster, cell_id)["metadata"]["annotations"].get(HOLD_ANNOTATION) == "backup":
                cluster.observations[cell_id] = _observed_from_applied(
                    cluster, cell_id, backup_job_succeeded=True, backup_job_snapshot_id=_ATTEMPT_SNAPSHOT
                )
            else:
                cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
            await _pass(connection, cluster, window + timedelta(minutes=minute), secrets=_rotated_secrets(), memory=memory)

        row = (await db.select_all_rows(connection))[0]
        assert row.last_backup_snapshot == _ATTEMPT_SNAPSHOT
        assert "cell-token-previous" in cluster.applied[(namespace_name(cell_id), "Secret", "cell-credentials")]["data"]
        assert row.last_error_code == "MANIFEST_IMMUTABLE"
        assert len(cluster.refused) <= 15, len(cluster.refused)  # 37 passes; the backoff retries only a handful
    finally:
        await connection.close()


async def test_a_transient_refusal_clears_after_the_backoff_with_no_key_change(cell_db: CellDatabase) -> None:
    # A deploy-skew 403 heals itself: the row is parked for the 2-minute
    # backoff, retried once it passes, and success clears the refusal.
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _RefusingGateway(cell_id, 403, times=1)
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [cell_id], now, memory)
        await _set_desired_state(cell_db, cell_id, "read_only")
        cluster.armed = True
        for step in range(24):  # the two minutes of the backoff, one pass every 5 s
            await _pass(connection, cluster, now + timedelta(seconds=5 * step), memory=memory)
        assert cluster.attempts == 1
        await _pass(connection, cluster, now + timedelta(minutes=2), memory=memory)
        assert cluster.attempts == 2
        env = _statefulset(cluster, cell_id)["spec"]["template"]["spec"]["containers"][0]["env"]
        assert {"name": "EXOMEM_CLOUD_READ_ONLY", "value": "1"} in env
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
        await _pass(connection, cluster, now + timedelta(minutes=2, seconds=5), memory=memory)
        row = (await db.select_all_rows(connection))[0]
        assert (row.observed_state, row.last_error_code, row.observed_generation) == ("read_only", None, 2)
    finally:
        await connection.close()


async def test_the_refusal_backoff_doubles_from_two_minutes_caps_at_an_hour_and_resets_on_restart(
    cell_db: CellDatabase,
) -> None:
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _RefusingGateway(cell_id, 422)
    memory = reconcile.LoopMemory()
    start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [cell_id], start - timedelta(minutes=1), memory)
        await _set_desired_state(cell_db, cell_id, "read_only")
        cluster.armed = True
        attempts_at: list[timedelta] = []
        for step in range(0, 4 * 60 * 2):  # four hours, one pass every 30 s
            at = timedelta(seconds=30 * step)
            before = cluster.attempts
            await _pass(connection, cluster, start + at, memory=memory)
            if cluster.attempts > before:
                attempts_at.append(at)
        gaps = [int((later - earlier).total_seconds() // 60) for earlier, later in zip(attempts_at, attempts_at[1:], strict=False)]
        assert gaps[:7] == [2, 4, 8, 16, 32, 60, 60], gaps

        # A restarted cellctl has no memory: it retries each refused row
        # once, then parks it again.
        restarted = reconcile.LoopMemory()
        before = cluster.attempts
        for step in range(3):
            await _pass(connection, cluster, start + timedelta(hours=5, seconds=5 * step), memory=restarted)
        assert cluster.attempts == before + 1
    finally:
        await connection.close()


async def test_a_parked_canary_holds_the_rollout_visibly_and_clears_once_it_unparks(cell_db: CellDatabase) -> None:
    owner, tenant = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    await _seed_cell(cell_db, owner, "tenant-a")
    await _seed_cell(cell_db, tenant, "tenant-b")
    admin = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await admin.execute("UPDATE exomem_cloud_cells SET rollout_priority = 0 WHERE cell_id = $1", owner)
    finally:
        await admin.close()
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _ObjectRefusingGateway()
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [owner, tenant], now, memory)
        await _set_storage(cell_db, owner, 5, desired_state="read_only")
        cluster.refuse = _refuse_kind(owner, "PersistentVolumeClaim")
        await _pass(connection, cluster, now + timedelta(seconds=5), memory=memory)
        cluster.observations[owner] = _observed_from_applied(cluster, owner)
        await _pass(connection, cluster, now + timedelta(seconds=6), memory=memory)  # observed Ready, still parked
        await _set_cell_image(cell_db, IMAGE_B)
        await _pass(connection, cluster, now + timedelta(seconds=10), memory=memory)
        rollout = await db.read_rollout(connection)
        assert (rollout.paused, rollout.error_code, rollout.held_cell_id) == (False, "CANARY_PARKED", owner)
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        assert rows[owner].hold_kind is None and rows[tenant].hold_kind is None

        await _set_storage(cell_db, owner, 10, desired_state="running")  # undone: a new generation, a new key
        cluster.refuse = None
        await _pass(connection, cluster, now + timedelta(seconds=15), memory=memory)
        cluster.observations[owner] = _observed_from_applied(cluster, owner)
        await _pass(connection, cluster, now + timedelta(seconds=20), memory=memory)
        await _pass(connection, cluster, now + timedelta(seconds=25), memory=memory)
        rollout = await db.read_rollout(connection)
        assert (rollout.error_code, rollout.held_cell_id) == (None, None)
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        assert rows[owner].hold_kind == "upgrade"
    finally:
        await connection.close()


# --- D4 orphans ----------------------------------------------------------------


class _OrphanGateway(FakeClusterGateway):
    def __init__(self, labelled: dict[str, str]) -> None:
        super().__init__()
        self.labelled = labelled
        self.namespace_lists = 0

    def list_cell_namespaces(self) -> dict[str, str]:
        self.namespace_lists += 1
        return self.labelled


async def test_orphan_namespaces_are_logged_by_cell_id_at_most_every_ten_minutes_and_never_deleted(
    cell_db: CellDatabase, caplog
) -> None:
    import logging

    known, orphan = "aaaaaaaaaaaaaaaa", "zzzzzzzzzzzzzzzz"
    await _seed_cell(cell_db, known, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _OrphanGateway({namespace_name(known): known, namespace_name(orphan): orphan})
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)

    async def run(at: datetime) -> None:
        await reconcile.reconcile_once(
            connection, cluster, FakeB2(), FakeHetznerVolumeProvider(), _secrets_config(), _cluster_config(),
            now=at, memory=memory,
        )

    try:
        with caplog.at_level(logging.WARNING, logger="cellctl"):
            await run(now)
        orphan_logs = [record.getMessage() for record in caplog.records if orphan in record.getMessage()]
        assert len(orphan_logs) == 1
        assert not any(known in record.getMessage() for record in caplog.records if "orphan" in record.getMessage())
        assert cluster.deleted_namespaces == set()

        await run(now + timedelta(minutes=9))
        assert cluster.namespace_lists == 1
        await run(now + timedelta(minutes=10))
        assert cluster.namespace_lists == 2
    finally:
        await connection.close()


async def test_a_canary_stopped_mid_attempt_runs_the_target_before_any_tenant_starts(cell_db: CellDatabase) -> None:
    # NEW1: the owner's row turns stopped after its pre-upgrade backup. The
    # target must still run with 1 replica until Ready, the exit scales it to
    # 0 on the target, and only then may a tenant start its own attempt.
    owner, tenant = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    await _seed_cell(cell_db, owner, "tenant-a")
    await _seed_cell(cell_db, tenant, "tenant-b")
    admin = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)

    def statefulset(cell_id: str) -> dict:
        return cluster.applied[(namespace_name(cell_id), "StatefulSet", "cell")]

    try:
        await admin.execute("UPDATE exomem_cloud_cells SET rollout_priority = 0 WHERE cell_id = $1", owner)
        for _ in range(3):
            await _pass(connection, cluster, now - timedelta(minutes=5))
            for cell_id in (owner, tenant):
                cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
        await _set_cell_image(cell_db, IMAGE_B)
        now = await _drive_to_first_target_apply(connection, cluster, owner, now)
        await admin.execute("UPDATE exomem_cloud_cells SET desired_state = 'stopped' WHERE cell_id = $1", owner)

        await _pass(connection, cluster, now)
        assert statefulset(owner)["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE_B
        assert statefulset(owner)["spec"]["replicas"] == 1
        cluster.observations[owner] = _observed_from_applied(cluster, owner, pod_ready=False, ready_pod_image=None)
        await _pass(connection, cluster, now + timedelta(seconds=5))
        assert statefulset(owner)["spec"]["replicas"] == 1
        assert statefulset(tenant)["metadata"]["annotations"].get("exomem.io/hold") is None

        cluster.observations[owner] = _observed_from_applied(cluster, owner)
        await _pass(connection, cluster, now + timedelta(seconds=10))
        assert statefulset(owner)["spec"]["replicas"] == 0
        assert statefulset(owner)["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE_B
        assert "exomem.io/hold" not in statefulset(owner)["metadata"]["annotations"]
        assert (await db.read_rollout(connection)).last_good_image == IMAGE_B

        cluster.observations[owner] = _observed_from_applied(cluster, owner)
        await _pass(connection, cluster, now + timedelta(seconds=15))
        assert statefulset(tenant)["metadata"]["annotations"].get("exomem.io/hold") == "upgrade"
    finally:
        await connection.close()
        await admin.close()


async def test_the_readiness_timeout_pause_is_written_before_the_restore_switch_is_applied(cell_db: CellDatabase) -> None:
    # D6 step 4.1: a crash between the two leaves the rollout paused.
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))

    class _DiesAfterApply(FakeClusterGateway):
        dying = False

        def apply_all(self, manifests: list[dict]) -> None:
            super().apply_all(manifests)
            # Objects are applied one at a time; die once the StatefulSet,
            # which carries the restore switch, has landed.
            if self.dying and any(manifest["kind"] == "StatefulSet" for manifest in manifests):
                raise RuntimeError("cellctl killed right after the apply landed")

    cluster = _DiesAfterApply()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge(connection, cluster, cell_id, now - timedelta(minutes=5))
        await _set_cell_image(cell_db, IMAGE_B)
        now = await _drive_to_first_target_apply(connection, cluster, cell_id, now)
        await _pass(connection, cluster, now)  # target applied
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id, pod_ready=False, ready_pod_image=None)
        cluster.dying = True
        await _pass(connection, cluster, now + timedelta(minutes=11))
        statefulset = cluster.applied[(namespace_name(cell_id), "StatefulSet", "cell")]
        assert statefulset["metadata"]["annotations"]["exomem.io/hold"] == "restore"
        rollout = await db.read_rollout(connection)
        assert (rollout.paused, rollout.error_code) == (True, "UPGRADE_READINESS_TIMEOUT")
    finally:
        await connection.close()


def test_the_render_digest_covers_the_renderer_version(monkeypatch) -> None:
    # D4: a new cellctl release that changes the rendered manifests bumps
    # RENDER_VERSION, which changes every cell's digest and so reaches cells
    # that already converged.
    row = _clean_row("aaaaaaaaaaaaaaaa", backup_key_version=1, b2_key_version=1)
    before = reconcile._compute_render_digest(row, _cluster_config(), _secrets_config())
    monkeypatch.setattr(reconcile, "RENDER_VERSION", reconcile.RENDER_VERSION + 1)
    after = reconcile._compute_render_digest(row, _cluster_config(), _secrets_config())
    assert before != after


# --- D4 API budget -------------------------------------------------------------


class _CountingGateway(FakeClusterGateway):
    def __init__(self) -> None:
        super().__init__()
        self.observed: list[str] = []

    def observe(self, cell_id: str, namespace: str) -> ClusterObservation:
        self.observed.append(cell_id)
        return super().observe(cell_id, namespace)


class _CountingHetzner(FakeHetznerVolumeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def get_volume(self, volume_id: str):
        self.calls += 1
        return super().get_volume(volume_id)


async def test_a_row_observed_as_deleted_is_not_observed_again(cell_db: CellDatabase) -> None:
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a", desired_state="deleted")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _CountingGateway()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await db.write_observed(connection, cell_id, {"observed_state": "deleted", "observed_generation": 1})
        for step in range(3):
            await _pass(connection, cluster, now + timedelta(seconds=5 * step))
        assert cluster.observed == []
    finally:
        await connection.close()


async def test_a_deleting_row_asks_hetzner_at_most_once_a_minute(cell_db: CellDatabase) -> None:
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a", desired_state="deleted")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()  # the namespace is already gone
    hetzner = _CountingHetzner()
    hetzner.add_volume(VolumeInfo(volume_id="vol-1", server_id="node-1", labels={}))
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)

    async def run(at: datetime) -> None:
        await reconcile.reconcile_once(
            connection, cluster, FakeB2(), hetzner, _secrets_config(), _cluster_config(), now=at, memory=memory
        )

    try:
        await db.write_observed(connection, cell_id, {"volume_id": "vol-1", "observed_state": "deleting"})
        for step in range(12):  # 55 seconds of 5-second passes
            await run(now + timedelta(seconds=5 * step))
        assert hetzner.calls == 1
        hetzner.remove_volume("vol-1")
        await run(now + timedelta(seconds=60))
        assert hetzner.calls == 2
        await run(now + timedelta(seconds=65))
        await run(now + timedelta(seconds=70))
        row = (await db.select_all_rows(connection))[0]
        assert row.observed_state == "deleted"
        assert hetzner.calls == 2
    finally:
        await connection.close()


async def test_observed_at_is_written_on_every_observation_including_a_converged_row(cell_db: CellDatabase) -> None:
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _pass(connection, cluster, now)
        assert (await db.select_all_rows(connection))[0].observed_at == now
        await _converge(connection, cluster, cell_id, now)
        row = (await db.select_all_rows(connection))[0]
        assert row.is_dirty() is False
        later = now + timedelta(minutes=3)
        await _pass(connection, cluster, later)
        assert (await db.select_all_rows(connection))[0].observed_at == later
    finally:
        await connection.close()


# --- D9: a node no longer in the cluster offers no slots ----------------------


class _NodesGateway(FakeClusterGateway):
    def __init__(self) -> None:
        super().__init__()
        self.allocatable: dict[str, int | None] = {}

    def capacity_inputs(self, *, csi_driver: str):
        return dict(self.allocatable), {node: 1 for node in self.allocatable}, {}


async def test_a_vanished_node_gets_zero_slots_and_a_rejoining_node_its_count_back(cell_db: CellDatabase) -> None:
    # D9 (amended): cellctl holds only INSERT and UPDATE on C1c, so a node
    # that left the cluster is zeroed rather than deleted; admission sums
    # cell_slots, so the zero row offers nothing.
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _NodesGateway()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)

    async def slots() -> dict[str, int]:
        records = await connection.fetch("SELECT node, cell_slots FROM exomem_cloud_capacity")
        return {record["node"]: record["cell_slots"] for record in records}

    try:
        # The test runs under the fixture grants: a DELETE would have failed.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute("DELETE FROM exomem_cloud_capacity WHERE node = 'nobody'")

        cluster.allocatable = {"node-1": 16, "node-2": 16}
        await _pass(connection, cluster, now)
        assert await slots() == {"node-1": 16, "node-2": 16}

        cluster.allocatable = {"node-1": 16}
        await _pass(connection, cluster, now + timedelta(seconds=5))
        assert await slots() == {"node-1": 16, "node-2": 0}

        cluster.allocatable = {"node-1": 16, "node-2": 12}
        await _pass(connection, cluster, now + timedelta(seconds=10))
        assert await slots() == {"node-1": 16, "node-2": 12}
    finally:
        await connection.close()


# --- D4 content-private logging -------------------------------------------------

_LEAK_SENTINEL = "cell-bearer-SENTINEL-5f1c"


class _LeakyResponse:
    status = 429
    reason = "Too Many Requests"
    data = f'{{"kind":"Secret","data":{{"cell-token":"{_LEAK_SENTINEL}"}}}}'

    def getheaders(self):
        return {"X-Echo": _LEAK_SENTINEL}


def _leaky_api_exception() -> ApiException:
    return ApiException(http_resp=_LeakyResponse())


def _rendered(caplog) -> str:
    # The formatter main.py's logging.basicConfig installs; it appends the
    # traceback (and with it str(exception)) for any record with exc_info.
    import logging

    formatter = logging.Formatter(logging.BASIC_FORMAT)
    return "\n".join(formatter.format(record) for record in caplog.records)


class _LeakyGateway(FakeClusterGateway):
    def __init__(self, where: str) -> None:
        super().__init__()
        self.where = where

    def observe(self, cell_id: str, namespace: str) -> ClusterObservation:
        if self.where == "observe":
            raise _leaky_api_exception()
        return super().observe(cell_id, namespace)

    def apply_all(self, manifests: list[dict]) -> None:
        if self.where == "apply":
            raise _leaky_api_exception()
        super().apply_all(manifests)

    def capacity_inputs(self, *, csi_driver: str):
        if self.where == "capacity":
            raise _leaky_api_exception()
        return {}, {}, {}

    def list_cell_namespaces(self) -> dict[str, str]:
        if self.where == "orphans":
            raise _leaky_api_exception()
        return {}


async def test_a_failing_row_logs_its_class_status_and_reason_never_the_response_body(cell_db: CellDatabase, caplog) -> None:
    import logging

    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    expected_line = {
        "apply": f"cell {cell_id}",
        "observe": f"cell {cell_id}",
        "capacity": "capacity read failed",
        "orphans": "orphan check failed",
    }
    try:
        for where, marker in expected_line.items():
            caplog.clear()
            with caplog.at_level(logging.DEBUG, logger="cellctl"):
                await _pass(connection, _LeakyGateway(where), datetime(2026, 1, 1, 12, tzinfo=UTC))
            text = _rendered(caplog)
            error_lines = [line for line in text.splitlines() if marker in line and "ApiException" in line]
            assert error_lines, (where, text)
            assert "status=429" in error_lines[0] and "Too Many Requests" in error_lines[0], (where, error_lines[0])
            assert _LEAK_SENTINEL not in text, (where, text)
    finally:
        await connection.close()


class _LeakyRefusalResponse(_LeakyResponse):
    status = 422
    reason = "Unprocessable Entity"


class _LeakySecretRefusalGateway(FakeClusterGateway):
    def apply_all(self, manifests: list[dict]) -> None:
        for manifest in manifests:
            if manifest["kind"] == "Secret":
                raise ApiException(http_resp=_LeakyRefusalResponse())
            super().apply_all([manifest])


async def test_a_refused_object_is_logged_by_kind_and_name_never_its_body(cell_db: CellDatabase, caplog) -> None:
    import logging

    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        with caplog.at_level(logging.DEBUG, logger="cellctl"):
            await _pass(connection, _LeakySecretRefusalGateway(), datetime(2026, 1, 1, 12, tzinfo=UTC))
        text = _rendered(caplog)
        assert f"cell {cell_id}: Secret cell-credentials status=422 reason=Unprocessable Entity" in text, text
        assert _LEAK_SENTINEL not in text, text
    finally:
        await connection.close()


async def test_a_failing_pass_logs_only_the_exception_class(cell_db: CellDatabase, monkeypatch, caplog) -> None:
    import logging

    async def leaking_pass(*args, **kwargs):
        raise RuntimeError(_LEAK_SENTINEL)

    monkeypatch.setattr(reconcile, "reconcile_once", leaking_pass)
    with caplog.at_level(logging.DEBUG, logger="cellctl"):
        task = asyncio.create_task(
            reconcile.run_loop(
                cell_db.dsn(role="exomem_cellctl"), FakeClusterGateway(), FakeB2(), FakeHetznerVolumeProvider(),
                _secrets_config(), _cluster_config(),
            )
        )
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not any("pass failed" in r.getMessage() for r in caplog.records):
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    text = _rendered(caplog)
    assert any("pass failed" in line and "RuntimeError" in line for line in text.splitlines()), text
    assert _LEAK_SENTINEL not in text, text


# --- a row that fails to observe freezes fleet-wide selection -------------------


class _ObserveFailsFor(FakeClusterGateway):
    def __init__(self) -> None:
        super().__init__()
        self.failing: set[str] = set()

    def observe(self, cell_id: str, namespace: str) -> ClusterObservation:
        if cell_id in self.failing:
            raise RuntimeError("simulated observe() failure")
        return super().observe(cell_id, namespace)


async def test_an_owner_that_fails_to_observe_never_lets_a_tenant_become_the_canary(cell_db: CellDatabase) -> None:
    # Dropping the owner's row made the lowest remaining priority the owner,
    # so a tenant started an upgrade on an untested image.
    owner, tenant = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    await _seed_cell(cell_db, owner, "tenant-a")
    await _seed_cell(cell_db, tenant, "tenant-b")
    admin = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await admin.execute("UPDATE exomem_cloud_cells SET rollout_priority = 0 WHERE cell_id = $1", owner)
    finally:
        await admin.close()
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _ObserveFailsFor()
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [owner, tenant], now, memory)
        await _set_cell_image(cell_db, IMAGE_B)
        cluster.failing = {owner}
        for step in range(3):
            await _pass(connection, cluster, now + timedelta(seconds=5 * (step + 1)), memory=memory)
            cluster.observations[tenant] = _observed_from_applied(cluster, tenant)
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        assert rows[tenant].hold_kind is None
        statefulset = cluster.applied[(namespace_name(tenant), "StatefulSet", "cell")]
        assert statefulset["metadata"]["annotations"].get("exomem.io/hold") is None
    finally:
        await connection.close()


async def test_a_held_cell_that_fails_to_observe_blocks_a_second_upgrade(cell_db: CellDatabase) -> None:
    # One upgrade at a time: the owner's hold must still count while its
    # observation is failing.
    owner, tenant = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    await _seed_cell(cell_db, owner, "tenant-a")
    await _seed_cell(cell_db, tenant, "tenant-b")
    admin = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await admin.execute("UPDATE exomem_cloud_cells SET rollout_priority = 0 WHERE cell_id = $1", owner)
    finally:
        await admin.close()
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _ObserveFailsFor()
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [owner, tenant], now, memory)
        await _set_cell_image(cell_db, IMAGE_B)
        await _pass(connection, cluster, now + timedelta(seconds=5), memory=memory)
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        assert rows[owner].hold_kind == "upgrade"
        cluster.failing = {owner}
        for step in range(3):
            await _pass(connection, cluster, now + timedelta(seconds=10 + 5 * step), memory=memory)
            cluster.observations[tenant] = _observed_from_applied(cluster, tenant)
        rows = {row.cell_id: row for row in await db.select_all_rows(connection)}
        assert rows[tenant].hold_kind is None
    finally:
        await connection.close()


async def test_a_parked_canary_is_published_even_when_a_resumed_rollout_still_names_it(cell_db: CellDatabase) -> None:
    # An owner resume that clears only paused/error_code leaves held_cell_id
    # on the owner; that must not hide CANARY_PARKED.
    owner, tenant = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    await _seed_cell(cell_db, owner, "tenant-a")
    await _seed_cell(cell_db, tenant, "tenant-b")
    admin = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        await admin.execute("UPDATE exomem_cloud_cells SET rollout_priority = 0 WHERE cell_id = $1", owner)
    finally:
        await admin.close()
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _ObjectRefusingGateway()
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [owner, tenant], now, memory)
        await db.write_rollout(connection, {"paused": False, "error_code": None, "held_cell_id": owner})
        await _set_storage(cell_db, owner, 5, desired_state="read_only")
        cluster.refuse = _refuse_kind(owner, "PersistentVolumeClaim")
        await _pass(connection, cluster, now + timedelta(seconds=5), memory=memory)
        cluster.observations[owner] = _observed_from_applied(cluster, owner)
        await _pass(connection, cluster, now + timedelta(seconds=6), memory=memory)
        await _set_cell_image(cell_db, IMAGE_B)
        await _pass(connection, cluster, now + timedelta(seconds=10), memory=memory)
        rollout = await db.read_rollout(connection)
        assert (rollout.paused, rollout.error_code, rollout.held_cell_id) == (False, "CANARY_PARKED", owner)
    finally:
        await connection.close()


async def test_reverting_storage_gib_unparks_a_refused_pvc_shrink(cell_db: CellDatabase) -> None:
    # storage_gib renders into the PVC and the quota but bumps no generation,
    # so it must be part of the park key, or undoing a refused shrink leaves
    # the row parked for the whole backoff.
    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _ObjectRefusingGateway()
    memory = reconcile.LoopMemory()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [cell_id], now, memory)
        await _set_storage(cell_db, cell_id, 5, desired_state="read_only")
        cluster.refuse = _refuse_kind(cell_id, "PersistentVolumeClaim")
        await _pass(connection, cluster, now + timedelta(seconds=5), memory=memory)
        assert (await db.select_all_rows(connection))[0].last_error_code == "MANIFEST_IMMUTABLE"

        await _set_storage(cell_db, cell_id, 10)
        cluster.refuse = None
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id)
        await _pass(connection, cluster, now + timedelta(seconds=10), memory=memory)
        pvc = next(m for (ns, kind, _), m in cluster.applied.items() if ns == namespace_name(cell_id) and kind == "PersistentVolumeClaim")
        assert pvc["spec"]["resources"]["requests"]["storage"] == "10Gi"
        assert (await db.select_all_rows(connection))[0].last_error_code is None
    finally:
        await connection.close()


def test_a_backup_hold_whose_statefulset_is_refused_does_not_hold_the_only_backup_slot() -> None:
    # The hold cannot exit without its StatefulSet, but it must not starve
    # every other cell's nightly backup while it waits.
    stuck, due = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    rows = [
        _clean_row(stuck, last_error_code="MANIFEST_IMMUTABLE", hold_kind="backup"),
        _clean_row(due),
    ]
    observations = {stuck: _served(stuck, statefulset_hold_kind="backup"), due: _served(due)}
    now = datetime(2026, 1, 1, 3, tzinfo=UTC)
    config = dataclasses.replace(reconcile.DEFAULT_RECONCILE_CONFIG, backup_concurrency=1)
    assert reconcile._select_backup_candidates(rows, observations, now, config) == set()
    assert reconcile._select_backup_candidates(rows, observations, now, config, frozenset({stuck})) == {due}


class _JobRefusingGateway(FakeClusterGateway):
    def __init__(self) -> None:
        super().__init__()
        self.job_attempts = 0

    def run_job(self, manifest: dict) -> None:
        self.job_attempts += 1
        raise ApiException(status=403, reason="refused by the test")


async def test_a_refused_backup_job_is_recorded_parked_and_retried_on_the_backoff(cell_db: CellDatabase) -> None:
    # A Job apply refusal is a D4 refusal like any other: the pass's row
    # updates still land, the row records MANIFEST_IMMUTABLE, and the Job is
    # retried on the backoff, never at loop speed.
    from cellctl.manifests import HOLD_ANNOTATION

    cell_id = "aaaaaaaaaaaaaaaa"
    await _seed_cell(cell_db, cell_id, "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _JobRefusingGateway()
    memory = reconcile.LoopMemory()
    window = datetime(2026, 1, 2, 2, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [cell_id], window - timedelta(hours=3), memory)
        await _pass(connection, cluster, window, memory=memory)  # the backup hold starts
        assert _statefulset(cluster, cell_id)["metadata"]["annotations"].get(HOLD_ANNOTATION) == "backup"
        cluster.observations[cell_id] = _observed_from_applied(cluster, cell_id, pod_exists=False, pod_ready=False)
        for step in range(12):  # one minute of passes
            await _pass(connection, cluster, window + timedelta(seconds=5 * (step + 1)), memory=memory)
        row = (await db.select_all_rows(connection))[0]
        assert row.last_error_code == "MANIFEST_IMMUTABLE"
        assert row.observed_state == "stopping"
        assert cluster.job_attempts == 1
    finally:
        await connection.close()


def test_a_failed_restore_stops_blocking_the_fleet_only_once_the_owner_resumes_the_rollout() -> None:
    # Keyed on the hold's age, not its error code: a refusal inside the hold
    # overwrites RESTORE_FAILED with MANIFEST_IMMUTABLE.
    from cellctl.rollout import select_upgrade_candidate

    owner, broken, tenant = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    config = reconcile.DEFAULT_RECONCILE_CONFIG
    old_hold = now - config.restore_bound - timedelta(minutes=1)
    rows = [
        _clean_row(owner, rollout_priority=0, observed_image=IMAGE_B),
        _clean_row(
            broken, rollout_priority=1, observed_image=IMAGE_A, hold_kind="restore",
            hold_started_at=old_hold, last_error_code="MANIFEST_IMMUTABLE",
        ),
        _clean_row(tenant, rollout_priority=2, observed_image=IMAGE_A),
    ]
    observations = {
        owner: _served(owner, statefulset_image=IMAGE_B),
        broken: _served(broken, statefulset_hold_kind="restore", statefulset_hold_started_at=old_hold, pod_ready=False),
        tenant: _served(tenant),
    }

    def pick(rollout: RolloutRow) -> str | None:
        restoring = any(
            reconcile._restore_blocks_upgrades(row, observations[row.cell_id], rollout, now, config) for row in rows
        )
        return select_upgrade_candidate(rows, observations, rollout, IMAGE_B, now=now, any_cell_already_upgrading=restoring)

    assert pick(RolloutRow(paused=True)) is None
    assert pick(RolloutRow(paused=False)) == tenant
    # A restore hold still inside its bound blocks either way.
    young = now - timedelta(minutes=5)
    observations[broken] = dataclasses.replace(observations[broken], statefulset_hold_started_at=young)
    assert reconcile._restore_blocks_upgrades(rows[1], observations[broken], RolloutRow(paused=False), now, config)


async def test_an_unobserved_cell_does_not_stop_the_rest_of_the_fleets_backups(cell_db: CellDatabase) -> None:
    due, broken = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    await _seed_cell(cell_db, due, "tenant-a")
    await _seed_cell(cell_db, broken, "tenant-b")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = _ObserveFailsFor()
    memory = reconcile.LoopMemory()
    window = datetime(2026, 1, 2, 3, tzinfo=UTC)
    try:
        await _converge_all(connection, cluster, [due, broken], window - timedelta(hours=4), memory)
        cluster.failing = {broken}
        await _pass(connection, cluster, window, memory=memory)
        statefulset = cluster.applied[(namespace_name(due), "StatefulSet", "cell")]
        assert statefulset["metadata"]["annotations"].get("exomem.io/hold") == "backup"
    finally:
        await connection.close()


def test_an_unobserved_cell_whose_row_holds_a_backup_still_occupies_its_slot() -> None:
    due, broken = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    rows = [_clean_row(due)]
    observations = {due: _served(due)}
    now = datetime(2026, 1, 1, 3, tzinfo=UTC)
    config = dataclasses.replace(reconcile.DEFAULT_RECONCILE_CONFIG, backup_concurrency=1)
    held = [_clean_row(broken, hold_kind="backup")]
    assert reconcile._select_backup_candidates(rows, observations, now, config) == {due}
    assert reconcile._select_backup_candidates(rows, observations, now, config, unobserved=held) == set()


async def test_the_pass_is_skipped_without_the_isolation_policy_too(cell_db: CellDatabase) -> None:
    # A fresh cell namespace has no NetworkPolicy until cellctl applies one;
    # the isolation policy is what keeps a pod out of it until then.
    await _seed_cell(cell_db, "aaaaaaaaaaaaaaaa", "tenant-a")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    cluster.admission_missing = {"exomem-cellctl-isolation"}
    try:
        await _pass(connection, cluster, datetime(2026, 1, 1, 12, tzinfo=UTC))
        assert cluster.applied == {}
        assert ("exomem-cellctl-isolation", "exomem-cellctl-isolation", "default-deny") in cluster.admission_checks
    finally:
        await connection.close()
