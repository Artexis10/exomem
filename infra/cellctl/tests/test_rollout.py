"""D6 image selection from the desired row and platform release."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from cellctl.decide import ReconcileConfig
from cellctl.manifests import CellManifestSpec, render_prepare_job, render_resource_quota
from cellctl.reconcile import _run_prepare_jobs
from cellctl.rollout import initial_image, select_upgrade_candidate, select_upgrade_candidates
from cellctl.state import CellRow, ClusterObservation, RolloutRow

CELL_ID = "aaaaaaaaaaaaaaaa"
PLATFORM_IMAGE = "registry.example/cell@sha256:" + "a" * 64
DESIRED_IMAGE = "registry.example/cell@sha256:" + "b" * 64


def test_row_desired_image_overrides_platform_image_for_new_and_existing_cells() -> None:
    row = CellRow(
        cell_id=CELL_ID,
        tenant_id=UUID(int=1),
        storage_gib=10,
        rollout_priority=1,
        desired_state="running",
        desired_image=DESIRED_IMAGE,
        generation=1,
        observed_image=PLATFORM_IMAGE,
        ready=True,
    )
    rollout = RolloutRow()

    assert initial_image(row, rollout, PLATFORM_IMAGE) == (DESIRED_IMAGE, None)
    assert select_upgrade_candidate(
        [row],
        {CELL_ID: ClusterObservation(statefulset_image=PLATFORM_IMAGE, pod_ready=True)},
        rollout,
        PLATFORM_IMAGE,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        any_cell_already_upgrading=False,
    ) == CELL_ID


# --- automate-cloud-releases 1.9: per-node upgrade concurrency --------------

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
IMAGE_A = PLATFORM_IMAGE
IMAGE_B = DESIRED_IMAGE


def _rollout_row(**overrides) -> CellRow:
    defaults = dict(
        cell_id="aaaaaaaaaaaaaaaa",
        tenant_id=UUID(int=1),
        storage_gib=10,
        rollout_priority=2,
        desired_state="running",
        desired_image=None,
        generation=1,
        observed_image=IMAGE_A,
        ready=True,
    )
    defaults.update(overrides)
    return CellRow(**defaults)


def _obs(**overrides) -> ClusterObservation:
    return ClusterObservation(**{"statefulset_image": IMAGE_A, "pod_ready": True, **overrides})


def _cells(nodes: list[str | None]) -> tuple[list[CellRow], dict[str, ClusterObservation]]:
    rows = [_rollout_row(cell_id=f"{i:016x}", rollout_priority=10 + i) for i in range(len(nodes))]
    observations = {row.cell_id: _obs(pv_node=node) for row, node in zip(rows, nodes, strict=True)}
    return rows, observations


def test_two_due_cells_on_one_node_start_once_but_a_cell_on_another_node_starts_too() -> None:
    rows, observations = _cells(["agent-1", "agent-1", "agent-2"])
    owner = _rollout_row(cell_id="ffffffffffffffff", rollout_priority=0, observed_image=IMAGE_B)
    rows.append(owner)
    observations[owner.cell_id] = _obs(statefulset_image=IMAGE_B, pv_node="agent-1")

    chosen = select_upgrade_candidates(
        rows,
        observations,
        RolloutRow(),
        IMAGE_B,
        now=NOW,
        restore_active=False,
        concurrency_per_node=1,
    )

    assert chosen == {rows[0].cell_id, rows[2].cell_id}


def test_while_the_canary_is_behind_its_target_no_other_attempt_starts_on_any_node() -> None:
    rows, observations = _cells(["agent-1", "agent-2"])
    owner = _rollout_row(cell_id="ffffffffffffffff", rollout_priority=0, observed_image=IMAGE_A)
    rows.append(owner)
    observations[owner.cell_id] = _obs(
        statefulset_image=IMAGE_A, pv_node="agent-3", pod_ready=False
    )

    # The canary is itself starting; tenants on free nodes must still wait.
    assert (
        select_upgrade_candidates(
            rows,
            observations,
            RolloutRow(),
            IMAGE_B,
            now=NOW,
            restore_active=False,
            concurrency_per_node=4,
        )
        == set()
    )


def test_an_ineligible_canary_blocks_every_tenant_attempt_even_an_older_candidate() -> None:
    rows, observations = _cells(["agent-1", "agent-2"])
    owner = _rollout_row(
        cell_id="ffffffffffffffff",
        rollout_priority=0,
        observed_image=IMAGE_A,
        desired_state="stopped",
    )
    rows.append(owner)
    observations[owner.cell_id] = ClusterObservation(
        statefulset_image=IMAGE_A,
        pv_node="agent-3",
        statefulset_candidate_since=datetime(2026, 1, 1, 11, tzinfo=UTC),
    )
    # A tenant whose candidate-since is older than the canary's still waits.
    observations[rows[0].cell_id] = _obs(
        pv_node="agent-1", statefulset_candidate_since=datetime(2026, 1, 1, 10, tzinfo=UTC)
    )

    assert (
        select_upgrade_candidates(
            rows,
            observations,
            RolloutRow(),
            IMAGE_B,
            now=NOW,
            restore_active=False,
            concurrency_per_node=4,
            due=frozenset(r.cell_id for r in rows),
        )
        == set()
    )


def test_a_canary_whose_attempt_ended_on_an_older_pin_still_blocks_until_it_runs_the_new_target() -> (
    None
):
    rows, observations = _cells(["agent-1"])
    owner = _rollout_row(
        cell_id="ffffffffffffffff",
        rollout_priority=0,
        observed_image="registry.example/cell@sha256:" + "c" * 64,
    )
    rows.append(owner)
    observations[owner.cell_id] = ClusterObservation(
        statefulset_image="registry.example/cell@sha256:" + "c" * 64,
        pod_ready=True,
        pv_node="agent-3",
    )

    # The canary ran an older pinned image, cell_image moved to IMAGE_B: only
    # the canary's own attempt may start; every tenant waits for it.
    assert select_upgrade_candidates(
        rows,
        observations,
        RolloutRow(),
        IMAGE_B,
        now=NOW,
        restore_active=False,
        concurrency_per_node=4,
    ) == {owner.cell_id}


def test_a_restore_hold_on_one_node_blocks_new_attempts_on_every_node() -> None:
    rows, observations = _cells(["agent-1", "agent-2"])
    owner = _rollout_row(cell_id="ffffffffffffffff", rollout_priority=0, observed_image=IMAGE_B)
    restoring = _rollout_row(cell_id="eeeeeeeeeeeeeeee", rollout_priority=1, hold_kind="restore")
    rows += [owner, restoring]
    observations[owner.cell_id] = _obs(statefulset_image=IMAGE_B, pv_node="agent-3")
    observations[restoring.cell_id] = ClusterObservation(
        statefulset_exists=True, statefulset_hold_kind="restore", pv_node="agent-4"
    )

    assert (
        select_upgrade_candidates(
            rows,
            observations,
            RolloutRow(),
            IMAGE_B,
            now=NOW,
            restore_active=True,
            concurrency_per_node=4,
        )
        == set()
    )


# --- automate-cloud-releases 5.2 (D9): the pull-only prepare Job ------------


class _RecordingCluster:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def run_job(self, manifest: dict) -> None:
        self.jobs.append(manifest)


def _windowed(cell_id: str, node: str, hour: int = 13) -> ClusterObservation:
    return _obs(pv_node=node, statefulset_upgrade_window=f"{hour} default")


def test_the_prepare_job_is_pinned_to_its_node_and_pulls_the_target() -> None:
    spec = CellManifestSpec(
        cell_id=CELL_ID, image=IMAGE_B, replicas=0, read_only=False, storage_gib=10
    )
    job = render_prepare_job(spec, node="agent-1", image=IMAGE_B)
    pod = job["spec"]["template"]["spec"]

    assert pod["nodeName"] == "agent-1"
    assert pod["containers"][0]["image"] == IMAGE_B
    assert "volumes" not in pod  # pull-only: it mounts nothing
    # The shape the exomem-cellctl-scope admission admits: a cell-prepare
    # Job pinned by nodeName alone, never a selector, toleration or affinity.
    # A change here without the policy's exception stops every apply.
    assert re.fullmatch(r"cell-prepare-[0-9a-f]{16}", job["metadata"]["name"])
    for field in ("nodeSelector", "tolerations", "affinity", "runtimeClassName",
                  "priorityClassName", "hostAliases"):
        assert field not in pod
    # Long enough that a finished Job still exists at the next pass in the
    # window, keeping the per-node/per-target dedupe a no-op.
    assert job["spec"]["ttlSecondsAfterFinished"] == 26 * 60 * 60


def test_one_prepare_job_per_node_per_target_and_not_recreated_each_pass() -> None:
    rows, observations = _cells(["agent-1", "agent-1", "agent-2"])
    for row in rows:
        observations[row.cell_id] = _windowed(row.cell_id, observations[row.cell_id].pv_node)
    cluster = _RecordingCluster()

    _run_prepare_jobs(cluster, rows, observations, frozenset(), IMAGE_B, NOW, ReconcileConfig())
    first_pass = list(cluster.jobs)
    _run_prepare_jobs(cluster, rows, observations, frozenset(), IMAGE_B, NOW, ReconcileConfig())

    # One Job per node with a windowed cell, never a second Job for the second cell.
    assert {j["metadata"]["namespace"] for j in first_pass} == {
        "exo-cell-0000000000000000",
        "exo-cell-0000000000000002",
    }
    # A second pass applies the identical objects: the apiserver receives the
    # same named Job, never a new one.
    assert cluster.jobs[2:] == first_pass
    assert len({j["metadata"]["name"] for j in first_pass}) == 2


def test_a_cell_past_its_window_hour_asks_for_no_prepare_job() -> None:
    rows, observations = _cells(["agent-1"])
    observations[rows[0].cell_id] = _windowed(rows[0].cell_id, "agent-1", hour=6)
    cluster = _RecordingCluster()

    _run_prepare_jobs(cluster, rows, observations, frozenset(), IMAGE_B, NOW, ReconcileConfig())
    assert cluster.jobs == []

    # The hour before the window, the Job goes.
    _run_prepare_jobs(
        cluster, rows, observations, frozenset(), IMAGE_B, NOW.replace(hour=5), ReconcileConfig()
    )
    assert len(cluster.jobs) == 1


def test_the_prepare_job_fits_the_cell_namespaces_quota() -> None:
    spec = CellManifestSpec(
        cell_id=CELL_ID,
        image=IMAGE_B,
        replicas=1,
        read_only=False,
        storage_gib=10,
        local_volume=True,
    )
    quota = render_resource_quota(spec)["spec"]["hard"]
    job = render_prepare_job(spec, node="agent-1", image=IMAGE_B)
    pod = job["spec"]["template"]["spec"]

    # Serving pod + backup Job + clone's Job + prepare: four pods in a quota of five.
    assert int(quota["pods"]) >= 4
    # Its requests sit far below one job slot of the quota's job budget.
    assert pod["containers"][0]["resources"]["requests"]["cpu"] == "10m"


def test_the_off_switch_runs_no_prepare_job() -> None:
    rows, observations = _cells(["agent-1"])
    observations[rows[0].cell_id] = _windowed(rows[0].cell_id, "agent-1")
    cluster = _RecordingCluster()

    _run_prepare_jobs(
        cluster,
        rows,
        observations,
        frozenset(rows[0].cell_id for _ in [0]),
        IMAGE_B,
        NOW,
        ReconcileConfig(upgrade_prepare_enabled=False),
    )
    assert cluster.jobs == []


def test_a_node_that_already_runs_the_target_gets_no_prepare_job() -> None:
    """After the Job's TTL removed it, the next pass must not re-apply it:
    a node where a cell already runs the target has the image pulled, so the
    per-node dedupe survives the Job's own lifetime."""
    rows, observations = _cells(["agent-1", "agent-1"])
    on_target = _rollout_row(cell_id="ffffffffffffffff", observed_image=IMAGE_B)
    rows.append(on_target)
    for row in rows:
        observations[row.cell_id] = _windowed(row.cell_id, "agent-1")
    observations[on_target.cell_id] = _obs(pv_node="agent-1", statefulset_image=IMAGE_B)
    cluster = _RecordingCluster()

    _run_prepare_jobs(cluster, rows, observations, frozenset(), IMAGE_B, NOW, ReconcileConfig())

    assert cluster.jobs == []


def test_clone_claim_name_follows_the_annotated_snapshot() -> None:
    from cellctl.manifests import CLONE_CLAIM_NAME, SNAPSHOT_NAME, clone_claim_name

    assert clone_claim_name("cell-snapshot-abc123") == "cell-clone-abc123"
    assert clone_claim_name(SNAPSHOT_NAME + "-x").startswith(CLONE_CLAIM_NAME)
    assert clone_claim_name("other-name") == "other-name"
