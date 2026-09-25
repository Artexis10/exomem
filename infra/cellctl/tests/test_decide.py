"""Red-first tests for the D4/D6/D8/D10 reconcile decision engine (3.3, 3.4, 3.6, 3.7).

Round 17: rewritten against the amended design.md (D4-D11) and the two
independent review reports. Several tests below are the required
regressions that start from a row/observation that already carries a
nightly `last_backup_snapshot`, proving B1/H1/H9 stay fixed even though a
nightly backup ran before the upgrade attempt -- the exact condition the
original bug needed to reproduce, and the one the K3s test used to mask by
NULLing the column.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cellctl.decide import ReconcileConfig, decide, nightly_backup_due
from cellctl.manifests import STORAGE_CLASS
from cellctl.rollout import select_upgrade_candidate
from cellctl.state import (
    BACKUP_FAILED,
    IDENTITY_CONFLICT,
    NO_GOOD_IMAGE,
    RESTORE_FAILED,
    UPGRADE_STALLED,
    CellRow,
    ClusterObservation,
    RolloutRow,
)

NOW = datetime(2026, 1, 1, 3, tzinfo=UTC)  # 03:00 UTC -- inside the default 02:00-05:00 backup window
IMAGE_A = "registry.example/cell@sha256:" + "a" * 64
IMAGE_B = "registry.example/cell@sha256:" + "b" * 64
NIGHTLY_SNAPSHOT = "0" * 64  # a real-looking nightly snapshot id already on the row
ATTEMPT_SNAPSHOT = "1" * 64
CONFIG = ReconcileConfig()


def row(**overrides) -> CellRow:
    defaults = dict(
        cell_id="aaaaaaaaaaaaaaaa",
        tenant_id="tenant-a",
        storage_gib=10,
        rollout_priority=1,
        desired_state="running",
        desired_image=None,
        generation=1,
    )
    defaults.update(overrides)
    return CellRow(**defaults)


def obs(**overrides) -> ClusterObservation:
    return ClusterObservation(**overrides)


def rollout(**overrides) -> RolloutRow:
    return RolloutRow(**overrides)


def bound_pvc(**overrides) -> dict:
    """A bound PVC/PV pair for this cell's own identity, so identity-conflict
    tests can start from a non-conflicting baseline and change one field."""

    defaults = dict(pvc_bound=True, pvc_uid="pvc-uid-1", pv_claim_ref_uid="pvc-uid-1", pv_storage_class=STORAGE_CLASS)
    defaults.update(overrides)
    return defaults


# --- New cell requested -----------------------------------------------------


def test_new_cell_with_nothing_observed_reports_provisioning() -> None:
    decision = decide(
        row(),
        rollout(),
        obs(),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.apply_manifests is True
    assert decision.image == IMAGE_A
    assert decision.row_updates["observed_state"] == "provisioning"
    assert decision.row_updates["ready"] is False


def test_new_cell_becomes_running_and_ready_once_pod_ready() -> None:
    decision = decide(
        row(),
        rollout(),
        obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc(), statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_row_generation=1, pod_ready=True, pvc_volume_id="vol-1", ready_pod_image=IMAGE_A),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["observed_state"] == "running"
    assert decision.row_updates["ready"] is True
    assert decision.row_updates["observed_image"] == IMAGE_A
    assert decision.row_updates["observed_generation"] == 1
    assert decision.row_updates["volume_id"] == "vol-1"


def test_observed_image_falls_back_to_target_when_no_ready_pod_image_is_observed() -> None:
    # A test fixture (or an older observer) that sets pod_ready=True without
    # ready_pod_image must still record something sane, rather than None.
    decision = decide(
        row(),
        rollout(),
        obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc(), statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_row_generation=1, pod_ready=True),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["observed_image"] == IMAGE_A


def test_first_provisioning_ready_sets_last_good_image() -> None:
    decision = decide(
        row(),
        rollout(last_good_image=None),
        obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc(), statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_row_generation=1, pod_ready=True),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.rollout_updates == {"last_good_image": IMAGE_A}


def test_first_release_fails_with_no_last_good_image_waits_with_no_good_image() -> None:
    decision = decide(
        row(),
        rollout(paused=True, last_good_image=None),
        obs(),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.apply_manifests is False
    assert decision.row_updates["last_error_code"] == NO_GOOD_IMAGE
    assert decision.row_updates["observed_state"] == "provisioning"


def test_paused_new_cell_starts_on_last_good_image_not_cell_image() -> None:
    decision = decide(
        row(),
        rollout(paused=True, last_good_image=IMAGE_B),
        obs(),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.image == IMAGE_B


# --- Idempotent restart mid-apply -------------------------------------------


def test_deciding_twice_from_the_same_inputs_is_identical() -> None:
    observation = obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc())
    first = decide(row(), rollout(), observation, now=NOW, cell_image=IMAGE_A, start_upgrade=False, config=CONFIG)
    second = decide(row(), rollout(), observation, now=NOW, cell_image=IMAGE_A, start_upgrade=False, config=CONFIG)
    assert first == second
    assert second.row_updates["observed_state"] == "provisioning"


# --- Foreign namespace / identity conflict ----------------------------------


def test_foreign_namespace_marks_failed_and_touches_nothing() -> None:
    decision = decide(
        row(),
        rollout(),
        obs(namespace_exists=True, namespace_cell_label="bbbbbbbbbbbbbbbb"),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.apply_manifests is False
    assert decision.delete_namespace is False
    assert decision.row_updates["observed_state"] == "failed"
    assert decision.row_updates["last_error_code"] == IDENTITY_CONFLICT
    assert decision.row_updates["observed_generation"] == 1


def test_unlabelled_existing_namespace_is_a_conflict() -> None:
    # SR-M4: cellctl always labels the namespace it creates in the same
    # apply, so an unlabelled-but-existing namespace was never its own --
    # the dead "missing label -> not a conflict" branch is gone.
    decision = decide(
        row(),
        rollout(),
        obs(namespace_exists=True, namespace_cell_label=None),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["last_error_code"] == IDENTITY_CONFLICT


def test_pv_claim_ref_uid_mismatch_is_a_conflict() -> None:
    # SR-M4: nothing labels a PV, so identity is checked via the claimRef
    # binding identity, not a (never-set) label.
    decision = decide(
        row(),
        rollout(),
        obs(pvc_bound=True, pvc_uid="pvc-uid-a", pv_claim_ref_uid="some-other-uid", pv_storage_class=STORAGE_CLASS),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["last_error_code"] == IDENTITY_CONFLICT


def test_pv_wrong_storage_class_is_a_conflict() -> None:
    decision = decide(
        row(),
        rollout(),
        obs(pvc_bound=True, pvc_uid="pvc-uid-a", pv_claim_ref_uid="pvc-uid-a", pv_storage_class="some-other-class"),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["last_error_code"] == IDENTITY_CONFLICT


def test_matching_pv_binding_identity_is_not_a_conflict() -> None:
    decision = decide(
        row(),
        rollout(),
        obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc()),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert "last_error_code" not in decision.row_updates or decision.row_updates.get("last_error_code") is None
    assert decision.row_updates.get("observed_state") != "failed"


# --- Init deadline (measured from the pod's own creation) -------------------


def test_pod_past_the_init_deadline_fails_even_though_it_is_still_crash_looping() -> None:
    # D3/M1 amendment: cell-init writes no termination message and the
    # deadline runs from the pod's own creation, not from a running init
    # container -- a crash-looping cell-init reaches the deadline exactly
    # the same as one that is simply slow.
    decision = decide(
        row(),
        rollout(),
        obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc(), statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_row_generation=1, pod_created_at=NOW - timedelta(minutes=11)),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["observed_state"] == "failed"
    assert decision.row_updates["last_error_code"] == "INIT_DEADLINE_EXCEEDED"


def test_pod_within_the_init_deadline_stays_provisioning() -> None:
    decision = decide(
        row(),
        rollout(),
        obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc(), pod_created_at=NOW - timedelta(minutes=5)),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["observed_state"] == "provisioning"
    assert "last_error_code" not in decision.row_updates


def test_no_pod_created_at_observed_yet_never_hits_the_deadline() -> None:
    decision = decide(
        row(),
        rollout(),
        obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc()),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["observed_state"] == "provisioning"
    assert "last_error_code" not in decision.row_updates


def test_a_long_running_pod_whose_init_completed_is_waiting_not_failed() -> None:
    # D4 "Waiting is normal": an OOM or liveness restart keeps the pod's
    # creation time. Once cell-init has completed, a not-Ready server is
    # waiting, never failed on time alone.
    decision = decide(
        row(observed_generation=1, observed_state="running", ready=True, observed_image=IMAGE_A),
        rollout(),
        obs(
            namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc(),
            statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=1,
            statefulset_row_generation=1,
            pod_exists=True, pod_created_at=NOW - timedelta(days=3), pod_init_completed=True,
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["observed_state"] == "provisioning"
    assert "last_error_code" not in decision.row_updates


def _rebooted_cell_obs(**overrides) -> ClusterObservation:
    """A long-running pod whose sandbox a node reboot recreated: its
    creationTimestamp is days old and cell-init runs again (N5)."""

    defaults = dict(
        namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc(),
        statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=1,
        statefulset_row_generation=1,
        pod_exists=True, pod_uses_volume=True, pod_created_at=NOW - timedelta(days=3), pod_init_completed=False,
        init_rerun=True,
    )
    defaults.update(overrides)
    return ClusterObservation(**defaults)


def test_a_cell_init_re_run_after_a_node_reboot_is_measured_from_its_own_start() -> None:
    served = row(observed_generation=1, observed_state="running", ready=True, observed_image=IMAGE_A)
    for observation in (
        _rebooted_cell_obs(init_started_at=NOW - timedelta(seconds=20)),
        _rebooted_cell_obs(init_started_at=None),  # the re-run is still waiting to start (a volume attaching)
    ):
        decision = decide(served, rollout(), observation, now=NOW, cell_image=IMAGE_A, start_upgrade=False, config=CONFIG)
        assert decision.row_updates["observed_state"] == "provisioning"
        assert "last_error_code" not in decision.row_updates
    stuck = decide(
        served, rollout(), _rebooted_cell_obs(init_started_at=NOW - timedelta(minutes=11)),
        now=NOW, cell_image=IMAGE_A, start_upgrade=False, config=CONFIG,
    )
    assert stuck.row_updates["last_error_code"] == "INIT_DEADLINE_EXCEEDED"


def _init_failed_row(**overrides) -> CellRow:
    defaults = dict(
        observed_state="failed",
        observed_generation=1,
        last_error_code="INIT_DEADLINE_EXCEEDED",
        observed_image=IMAGE_A,
    )
    defaults.update(overrides)
    return row(**defaults)


def _init_failed_obs(**overrides) -> ClusterObservation:
    defaults = dict(
        namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc(),
        statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=1,
        statefulset_row_generation=1,
        pod_exists=True, pod_uses_volume=True, pod_created_at=NOW - timedelta(minutes=30),
    )
    defaults.update(overrides)
    return ClusterObservation(**defaults)


def test_an_init_deadline_failure_stays_selected_but_an_identity_conflict_is_terminal() -> None:
    assert _init_failed_row().is_dirty() is True
    assert _init_failed_row(last_error_code="IDENTITY_CONFLICT").is_dirty() is False


def test_an_init_deadline_failure_is_observed_but_not_re_applied() -> None:
    decision = decide(
        _init_failed_row(), rollout(), _init_failed_obs(), now=NOW, cell_image=IMAGE_A,
        start_upgrade=False, config=CONFIG,
    )
    assert decision.apply_manifests is False
    assert decision.row_updates["observed_state"] == "failed"
    assert decision.row_updates["last_error_code"] == "INIT_DEADLINE_EXCEEDED"


def test_an_init_deadline_failure_recovers_once_its_pod_is_ready() -> None:
    decision = decide(
        _init_failed_row(desired_state="read_only"),
        rollout(),
        _init_failed_obs(pod_ready=True, ready_pod_image=IMAGE_A, pod_init_completed=True),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.apply_manifests is False
    assert decision.row_updates["observed_state"] == "read_only"
    assert decision.row_updates["ready"] is True
    assert decision.row_updates["last_error_code"] is None


def test_an_init_deadline_failure_is_re_applied_once_its_generation_changes() -> None:
    decision = decide(
        _init_failed_row(generation=2), rollout(), _init_failed_obs(), now=NOW, cell_image=IMAGE_A,
        start_upgrade=False, config=CONFIG,
    )
    assert decision.apply_manifests is True


# --- Stopped desired state ---------------------------------------------------


def test_never_run_cell_stopped_creates_nothing() -> None:
    decision = decide(
        row(desired_state="stopped"),
        rollout(),
        obs(),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.apply_manifests is False
    assert decision.row_updates["observed_state"] == "stopped"


def test_stopping_a_running_cell_scales_to_zero() -> None:
    decision = decide(
        row(desired_state="stopped", observed_image=IMAGE_A),
        rollout(),
        obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", statefulset_exists=True, statefulset_image=IMAGE_A, pod_exists=True),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.apply_manifests is True
    assert decision.replicas == 0
    assert decision.row_updates["observed_state"] == "stopping"


# --- Stale hold (D4: annotations are the truth) -----------------------------


def test_a_stale_row_hold_with_no_statefulset_annotation_is_cleared_not_resumed() -> None:
    # D4: a row that still shows a hold the StatefulSet does not carry is
    # stale -- from a crash between the hold-clearing apply and the row
    # write -- and must be corrected, never resumed.
    decision = decide(
        row(hold_kind="upgrade", hold_started_at=NOW - timedelta(minutes=1), observed_image=IMAGE_A, ready=True),
        rollout(),
        obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc(), statefulset_exists=True, statefulset_image=IMAGE_A, pod_ready=True),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["hold_kind"] is None
    assert decision.row_updates["hold_started_at"] is None
    # And it took the *routine* path, not an upgrade continuation.
    assert decision.hold_kind is None


# --- Nightly backup hold, including desired-state flip during it ------------


def test_nightly_backup_starts_a_hold_only_when_selected_by_the_caller() -> None:
    already_served = row(last_backup_at=NOW - timedelta(hours=21), observed_image=IMAGE_A, observed_state="running")
    observation = obs(statefulset_exists=True, statefulset_image=IMAGE_A, pod_ready=True)

    not_selected = decide(
        already_served, rollout(), observation, now=NOW, cell_image=IMAGE_A, start_upgrade=False,
        start_backup=False, config=CONFIG,
    )
    assert not_selected.hold_kind != "backup"

    selected = decide(
        already_served, rollout(), observation, now=NOW, cell_image=IMAGE_A, start_upgrade=False,
        start_backup=True, config=CONFIG,
    )
    assert selected.hold_kind == "backup"
    assert selected.replicas == 0
    assert selected.row_updates["hold_kind"] == "backup"


def test_nightly_backup_due_respects_the_window_the_interval_and_the_backoff() -> None:
    due_row = row(last_backup_at=NOW - timedelta(hours=21))
    assert nightly_backup_due(due_row, obs(), NOW, CONFIG) is True
    outside_window = NOW.replace(hour=12)
    assert nightly_backup_due(due_row, obs(), outside_window, CONFIG) is False
    not_yet_due = row(last_backup_at=NOW - timedelta(hours=5))
    assert nightly_backup_due(not_yet_due, obs(), NOW, CONFIG) is False
    backed_off = obs(statefulset_backup_retry_after=NOW + timedelta(minutes=10))
    assert nightly_backup_due(due_row, backed_off, NOW, CONFIG) is False
    backoff_expired = obs(statefulset_backup_retry_after=NOW - timedelta(minutes=1))
    assert nightly_backup_due(due_row, backoff_expired, NOW, CONFIG) is True


def test_desired_state_flips_to_read_only_during_backup_hold_stays_stopped_then_resumes_read_only() -> None:
    held_row = row(
        desired_state="read_only",  # flipped mid-hold
        hold_kind="backup",
        hold_started_at=NOW - timedelta(minutes=2),
        observed_image=IMAGE_A,
    )
    mid_hold = decide(
        held_row,
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="backup",
            statefulset_hold_started_at=NOW - timedelta(minutes=2),
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert mid_hold.replicas == 0
    assert mid_hold.row_updates["observed_state"] == "stopping"

    finished = decide(
        held_row,
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="backup",
            statefulset_hold_started_at=NOW - timedelta(minutes=2),
            backup_job_succeeded=True,
            backup_job_snapshot_id=NIGHTLY_SNAPSHOT,
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert finished.hold_kind is None
    assert finished.read_only is True
    assert finished.replicas == 1
    # D4: hold exits observe; they do not declare observed_state. `ready` is
    # written from what the pass observes: no pod is Ready yet.
    assert "observed_state" not in finished.row_updates
    assert finished.row_updates["ready"] is False
    assert finished.row_updates["last_backup_snapshot"] == NIGHTLY_SNAPSHOT


def test_backup_hold_honours_a_flip_to_stopped() -> None:
    # H2: replicas must come from desired_state throughout every hold, not a
    # hardcoded 1.
    held_row = row(desired_state="stopped", hold_kind="backup", hold_started_at=NOW, observed_image=IMAGE_A)
    finished = decide(
        held_row,
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="backup",
            statefulset_hold_started_at=NOW,
            backup_job_succeeded=True,
            backup_job_snapshot_id=NIGHTLY_SNAPSHOT,
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert finished.replicas == 0
    assert finished.hold_kind is None


def test_nightly_backup_deadline_miss_restarts_cell_with_backup_failed_and_sets_backoff() -> None:
    decision = decide(
        row(hold_kind="backup", hold_started_at=NOW - timedelta(minutes=20), observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="backup",
            statefulset_hold_started_at=NOW - timedelta(minutes=20),
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind is None
    assert decision.replicas == 1
    assert decision.row_updates["last_error_code"] == BACKUP_FAILED
    assert decision.row_updates["hold_kind"] is None
    # H3: the very next attempt must not be allowed for another 15 minutes.
    assert decision.backup_retry_after == NOW + timedelta(minutes=15)
    assert decision.backup_retry_minutes == 15


def test_backup_backoff_doubles_on_a_second_consecutive_failure_capped_at_four_hours() -> None:
    decision = decide(
        row(hold_kind="backup", hold_started_at=NOW - timedelta(minutes=20), observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="backup",
            statefulset_hold_started_at=NOW - timedelta(minutes=20),
            statefulset_backup_retry_minutes=180,  # a previous failure already backed off to 3h
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.backup_retry_minutes == 240  # doubled from 180, capped at 240
    another = decide(
        row(hold_kind="backup", hold_started_at=NOW - timedelta(minutes=20), observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="backup",
            statefulset_hold_started_at=NOW - timedelta(minutes=20),
            statefulset_backup_retry_minutes=240,
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert another.backup_retry_minutes == 240  # stays capped


def test_backup_success_clears_the_retry_backoff() -> None:
    decision = decide(
        row(hold_kind="backup", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="backup",
            statefulset_hold_started_at=NOW,
            statefulset_backup_retry_minutes=60,
            backup_job_succeeded=True,
            backup_job_snapshot_id=NIGHTLY_SNAPSHOT,
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.clear_backup_retry_after is True


def test_backup_job_succeeding_with_no_valid_snapshot_id_counts_as_backup_failed() -> None:
    # M1/M2: a Job that exits 0 with an empty/invalid termination message
    # (the `cut`-swallows-the-real-exit-status bug) must never be recorded
    # as a successful backup.
    for bad_snapshot in (None, "", "not-hex", "a" * 63):
        decision = decide(
            row(hold_kind="backup", hold_started_at=NOW, observed_image=IMAGE_A),
            rollout(),
            obs(
                statefulset_exists=True,
                statefulset_image=IMAGE_A,
                statefulset_hold_kind="backup",
                statefulset_hold_started_at=NOW,
                backup_job_succeeded=True,
                backup_job_snapshot_id=bad_snapshot,
            ),
            now=NOW,
            cell_image=IMAGE_A,
            start_upgrade=False,
            config=CONFIG,
        )
        assert decision.row_updates["last_error_code"] == BACKUP_FAILED, bad_snapshot
        assert "last_backup_snapshot" not in decision.row_updates, bad_snapshot


# --- Upgrade attempt (D6) ----------------------------------------------------


def test_upgrade_attempt_walks_stop_backup_apply_ready_and_sets_last_good_image() -> None:
    base = row(observed_image=IMAGE_A, ready=True)

    starting = decide(
        base,
        rollout(last_good_image=IMAGE_A),
        obs(statefulset_image=IMAGE_A, statefulset_replicas=1, pod_ready=True),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=True,
        config=CONFIG,
    )
    assert starting.hold_kind == "upgrade"
    assert starting.replicas == 0
    assert starting.previous_image == IMAGE_A

    held_row = row(observed_image=IMAGE_A, hold_kind="upgrade", hold_started_at=NOW)

    backing_up = decide(
        held_row,
        rollout(last_good_image=IMAGE_A),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert backing_up.run_backup_job is True
    assert backing_up.pre_upgrade_snapshot is None

    backed_up = decide(
        held_row,
        rollout(last_good_image=IMAGE_A),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            backup_job_succeeded=True,
            backup_job_snapshot_id=ATTEMPT_SNAPSHOT,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert backed_up.pre_upgrade_snapshot == ATTEMPT_SNAPSHOT
    assert backed_up.image == IMAGE_A  # still stopped on previous image this pass

    applying = decide(
        held_row,
        rollout(last_good_image=IMAGE_A),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert applying.image == IMAGE_B
    assert applying.replicas == 1
    assert applying.target_applied_at == NOW
    assert applying.row_updates["observed_state"] == "provisioning"

    ready = decide(
        held_row,
        rollout(last_good_image=IMAGE_A),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_B,
            statefulset_replicas=1,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
            statefulset_target_applied_at=NOW,
            statefulset_row_generation=1,
            pod_ready=True,
            ready_pod_image=IMAGE_B,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert ready.hold_kind is None
    # D4: hold exits observe; they do not declare. The row_updates here are
    # only the hold clearing -- the NEXT routine pass records
    # observed_state/observed_image/observed_generation. D6 step 4 sets
    # last_good_image on success.
    assert ready.row_updates == {"hold_kind": None, "hold_started_at": None, "last_error_code": None, "ready": True}
    assert ready.rollout_updates == {"last_good_image": IMAGE_B}

    converged = decide(
        held_row,  # still shows the (now stale) hold; the StatefulSet does not
        rollout(last_good_image=IMAGE_A),
        obs(
            namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa",
            **bound_pvc(),
            statefulset_exists=True,
            statefulset_image=IMAGE_B,
            statefulset_replicas=1,
            statefulset_row_generation=1,
            pod_ready=True,
            ready_pod_image=IMAGE_B,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert converged.row_updates["observed_state"] == "running"
    assert converged.row_updates["observed_image"] == IMAGE_B
    assert converged.rollout_updates == {"last_good_image": IMAGE_B}


# --- B1 regression: the pre-upgrade snapshot is never the row's nightly one -


def test_upgrade_with_an_existing_nightly_snapshot_still_backs_up_and_uses_only_its_own_snapshot() -> None:
    """B1: every served cell gets a `last_backup_snapshot` from its first
    nightly backup. An upgrade attempt starting from such a row must still
    run the full stop/wait/backup sequence and must never treat the row's
    nightly snapshot as this attempt's pre-upgrade snapshot."""

    held_row = row(
        observed_image=IMAGE_A,
        hold_kind="upgrade",
        hold_started_at=NOW,
        last_backup_at=NOW - timedelta(hours=5),
        last_backup_snapshot=NIGHTLY_SNAPSHOT,
    )

    # The StatefulSet carries no pre-upgrade-snapshot annotation yet -- this
    # attempt has not run its own backup. The row's nightly snapshot must be
    # completely ignored.
    pass2 = decide(
        held_row,
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert pass2.run_backup_job is True
    assert pass2.replicas == 0
    assert pass2.image == IMAGE_A
    assert pass2.pre_upgrade_snapshot is None

    # Even once the attempt's own backup Job succeeds with a DIFFERENT id
    # than the nightly one, only the attempt's own id is ever recorded.
    backed_up = decide(
        held_row,
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            backup_job_succeeded=True,
            backup_job_snapshot_id=ATTEMPT_SNAPSHOT,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert backed_up.pre_upgrade_snapshot == ATTEMPT_SNAPSHOT
    assert backed_up.pre_upgrade_snapshot != NIGHTLY_SNAPSHOT
    # D6 step 2: it is a real backup, so the row's last_backup_snapshot
    # moves to it too; the restore below still reads only the annotation.
    assert backed_up.row_updates["last_backup_snapshot"] == ATTEMPT_SNAPSHOT

    # If the canary then times out, the restore targets the ATTEMPT's own
    # snapshot, never the nightly one.
    canary_timeout = decide(
        held_row,
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_B,
            statefulset_replicas=1,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW - timedelta(minutes=11),
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
            statefulset_target_applied_at=NOW - timedelta(minutes=11),
            pod_ready=False,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert canary_timeout.hold_kind == "restore"
    assert canary_timeout.pre_upgrade_snapshot == ATTEMPT_SNAPSHOT
    restoring = decide(
        held_row,
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_hold_kind="restore",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert restoring.run_restore_job_snapshot == ATTEMPT_SNAPSHOT
    assert restoring.run_restore_job_snapshot != NIGHTLY_SNAPSHOT


def test_restore_hold_with_no_pre_upgrade_snapshot_fails_closed_never_falls_back_to_nightly() -> None:
    """M10/L9/B1: a restore hold reaching decide() with no
    pre-upgrade-snapshot annotation cannot happen through the normal
    procedure. It must fail closed with RESTORE_FAILED and pause the
    rollout -- it must never fall back to the row's nightly snapshot, even
    though one is sitting right there on the row."""

    decision = decide(
        row(hold_kind="restore", hold_started_at=NOW, observed_image=IMAGE_A, last_backup_snapshot=NIGHTLY_SNAPSHOT),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_hold_kind="restore",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            # No statefulset_pre_upgrade_snapshot at all.
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.run_restore_job_snapshot is None
    assert decision.row_updates["last_error_code"] == RESTORE_FAILED
    assert decision.rollout_updates == {
        "paused": True,
        "error_code": RESTORE_FAILED,
        "held_cell_id": "aaaaaaaaaaaaaaaa",
    }
    # The hold is kept, not cleared -- it never silently resumes serving on
    # an unrestored, possibly-migrated volume.
    assert decision.hold_kind == "restore"


def test_upgrade_waits_for_no_pod_on_volume_before_backing_up() -> None:
    decision = decide(
        row(hold_kind="upgrade", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            pod_uses_volume=True,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.run_backup_job is False
    assert decision.replicas == 0
    assert decision.row_updates["observed_state"] == "stopping"


def test_upgrade_does_not_revert_to_previous_image_once_the_canary_pod_exists() -> None:
    decision = decide(
        row(hold_kind="upgrade", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_B,
            statefulset_replicas=1,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
            statefulset_target_applied_at=NOW,
            pod_uses_volume=True,  # the canary pod on IMAGE_B, not the old one
            pod_ready=False,  # the canary never becomes ready
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind == "upgrade"
    assert decision.image == IMAGE_B
    assert decision.replicas == 1
    assert decision.row_updates["observed_state"] == "provisioning"


def test_upgrade_backup_deadline_miss_restarts_current_image_with_backup_failed_no_pause() -> None:
    decision = decide(
        row(hold_kind="upgrade", hold_started_at=NOW - timedelta(minutes=16), observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW - timedelta(minutes=16),
            statefulset_previous_image=IMAGE_A,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind is None
    assert decision.image == IMAGE_A
    assert decision.replicas == 1
    assert decision.row_updates["last_error_code"] == BACKUP_FAILED
    # BACKUP_FAILED is expected to self-heal -- it must not stall other cells.
    assert decision.rollout_updates == {}


def test_canary_fails_readiness_timeout_switches_to_restore_and_pauses_in_the_same_decision() -> None:
    # H1: the pause must land in the SAME decision that switches to restore,
    # before the switch is applied, so no other cell can start the same
    # known-bad image in between.
    decision = decide(
        row(hold_kind="upgrade", hold_started_at=NOW - timedelta(minutes=11), observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_B,
            statefulset_replicas=1,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW - timedelta(minutes=11),
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
            statefulset_target_applied_at=NOW - timedelta(minutes=11),
            pod_ready=False,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind == "restore"
    assert decision.replicas == 0
    assert decision.row_updates["hold_kind"] == "restore"
    assert decision.rollout_updates == {
        "paused": True,
        "error_code": "UPGRADE_READINESS_TIMEOUT",
        "held_cell_id": "aaaaaaaaaaaaaaaa",
    }


def test_readiness_deadline_is_anchored_to_target_applied_at_not_hold_started_at() -> None:
    # M5: a 12-minute-old hold (longer than the 10-minute readiness deadline)
    # whose backup alone took 11 of those minutes, and whose target was only
    # just applied, must not be treated as already timed out.
    decision = decide(
        row(hold_kind="upgrade", hold_started_at=NOW - timedelta(minutes=12), observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_B,
            statefulset_replicas=1,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW - timedelta(minutes=12),
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
            statefulset_target_applied_at=NOW - timedelta(seconds=5),  # just applied
            pod_ready=False,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind == "upgrade"  # still waiting, not restoring


def test_upgrade_stall_bound_ends_the_attempt_when_the_target_is_never_applied() -> None:
    # D6 step 5: 30 minutes with no target-applied-at at all.
    decision = decide(
        row(hold_kind="upgrade", hold_started_at=NOW - timedelta(minutes=31), observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW - timedelta(minutes=31),
            statefulset_previous_image=IMAGE_A,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind is None
    assert decision.image == IMAGE_A
    assert decision.replicas == 1
    assert decision.row_updates["last_error_code"] == UPGRADE_STALLED
    assert decision.rollout_updates == {
        "paused": True,
        "error_code": UPGRADE_STALLED,
        "held_cell_id": "aaaaaaaaaaaaaaaa",
    }


def _upgrade_hold_obs(**overrides) -> ClusterObservation:
    """An upgrade hold whose own pre-upgrade backup already landed."""

    defaults = dict(
        namespace_exists=True,
        namespace_cell_label="aaaaaaaaaaaaaaaa",
        **bound_pvc(),
        statefulset_exists=True,
        statefulset_image=IMAGE_A,
        statefulset_replicas=0,
        statefulset_hold_kind="upgrade",
        statefulset_hold_started_at=NOW - timedelta(minutes=3),
        statefulset_previous_image=IMAGE_A,
        statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
    )
    defaults.update(overrides)
    return ClusterObservation(**defaults)


def test_stopped_mid_canary_still_applies_the_target_with_one_replica() -> None:
    # D4 "Holds honour the desired state": the upgrade target runs with 1
    # replica until it is Ready or the D6 deadline passes, whatever the
    # desired state, so an attempt is never declared successful for an
    # image that never ran.
    decision = decide(
        row(desired_state="stopped", generation=2, hold_kind="upgrade", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(),
        _upgrade_hold_obs(),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind == "upgrade"
    assert decision.image == IMAGE_B
    assert decision.replicas == 1
    assert decision.read_only is False
    assert decision.target_applied_at == NOW


def test_stopped_mid_canary_keeps_the_target_at_one_replica_while_it_starts() -> None:
    decision = decide(
        row(desired_state="stopped", generation=2, hold_kind="upgrade", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(),
        _upgrade_hold_obs(
            statefulset_image=IMAGE_B,
            statefulset_replicas=1,
            statefulset_target_applied_at=NOW - timedelta(minutes=2),
            pod_exists=True,
            pod_uses_volume=True,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind == "upgrade"
    assert decision.image == IMAGE_B
    assert decision.replicas == 1


def test_stopped_mid_canary_exits_only_once_the_target_is_ready_then_scales_to_zero() -> None:
    # Only the hold exit applies the desired replicas; it never declares.
    decision = decide(
        row(desired_state="stopped", generation=2, hold_kind="upgrade", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(last_good_image=IMAGE_A),
        _upgrade_hold_obs(
            statefulset_image=IMAGE_B,
            statefulset_replicas=1,
            statefulset_target_applied_at=NOW - timedelta(minutes=2),
            pod_exists=True,
            pod_uses_volume=True,
            pod_ready=True,
            ready_pod_image=IMAGE_B,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind is None
    assert decision.image == IMAGE_B
    assert decision.replicas == 0
    assert "observed_generation" not in decision.row_updates
    assert "observed_state" not in decision.row_updates
    assert "observed_image" not in decision.row_updates
    # D6 step 4: on success, set last_good_image.
    assert decision.rollout_updates == {"last_good_image": IMAGE_B}


def test_stopped_mid_canary_that_never_gets_ready_is_restored_not_declared() -> None:
    decision = decide(
        row(desired_state="stopped", generation=2, hold_kind="upgrade", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(),
        _upgrade_hold_obs(
            statefulset_image=IMAGE_B,
            statefulset_replicas=1,
            statefulset_target_applied_at=NOW - timedelta(minutes=11),
            pod_exists=True,
            pod_uses_volume=True,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind == "restore"
    assert decision.replicas == 0
    assert decision.rollout_updates["error_code"] == "UPGRADE_READINESS_TIMEOUT"


def test_a_successful_pre_upgrade_backup_also_records_last_backup() -> None:
    # D6 step 2: the pre-upgrade backup is a real backup, so it also writes
    # last_backup_at and last_backup_snapshot; the restore still reads only
    # the attempt's own annotation.
    decision = decide(
        row(hold_kind="upgrade", hold_started_at=NOW, observed_image=IMAGE_A, last_backup_snapshot=NIGHTLY_SNAPSHOT),
        rollout(),
        _upgrade_hold_obs(
            statefulset_pre_upgrade_snapshot=None,
            backup_job_succeeded=True,
            backup_job_snapshot_id=ATTEMPT_SNAPSHOT,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.pre_upgrade_snapshot == ATTEMPT_SNAPSHOT
    assert decision.row_updates["last_backup_at"] == NOW
    assert decision.row_updates["last_backup_snapshot"] == ATTEMPT_SNAPSHOT


def test_read_only_desired_state_is_honoured_throughout_an_upgrade_hold() -> None:
    # H2: the canary and the post-upgrade pod must run with
    # EXOMEM_CLOUD_READ_ONLY set, not run read-write while the row claims
    # read_only.
    decision = decide(
        row(desired_state="read_only", hold_kind="upgrade", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.replicas == 1
    assert decision.read_only is True


def test_restore_after_canary_failure_runs_job_then_starts_previous_image_and_clears_the_hold() -> None:
    held_row = row(hold_kind="restore", hold_started_at=NOW, observed_image=IMAGE_A)

    running_restore = decide(
        held_row,
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_hold_kind="restore",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert running_restore.run_restore_job_snapshot == ATTEMPT_SNAPSHOT

    restored = decide(
        held_row,
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_hold_kind="restore",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
            restore_job_succeeded=True,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert restored.image == IMAGE_A
    assert restored.replicas == 0
    assert restored.restored_snapshot == ATTEMPT_SNAPSHOT
    assert restored.row_updates["observed_state"] == "stopping"

    applying_previous = decide(
        held_row,
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_hold_kind="restore",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
            statefulset_restored_snapshot=ATTEMPT_SNAPSHOT,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert applying_previous.image == IMAGE_A
    assert applying_previous.replicas == 1
    assert applying_previous.row_updates["observed_state"] == "provisioning"

    ready = decide(
        held_row,
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_replicas=1,
            statefulset_hold_kind="restore",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
            statefulset_restored_snapshot=ATTEMPT_SNAPSHOT,
            statefulset_row_generation=1,
            pod_ready=True,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert ready.hold_kind is None
    # D4: hold exits observe; they do not declare. `ready` is what the pass saw.
    assert ready.row_updates == {"hold_kind": None, "hold_started_at": None, "last_error_code": None, "ready": True}


def test_a_ttld_restore_job_is_never_re_run_once_restored_snapshot_is_recorded() -> None:
    # L2: the restore Job's 300s TTL can expire while the previous image is
    # still starting -- observation shows neither succeeded nor failed
    # (the Job is simply gone), but `restored_snapshot` already proves it
    # ran once, so it must not be re-run.
    decision = decide(
        row(hold_kind="restore", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_hold_kind="restore",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
            statefulset_restored_snapshot=ATTEMPT_SNAPSHOT,
            # No restore_job_succeeded/failed -- the Job's TTL already expired.
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.run_restore_job_snapshot is None


def test_restore_waits_for_the_canary_pod_to_leave_the_volume_before_running() -> None:
    # L1: the same guard the backup path already had, now also on restore.
    decision = decide(
        row(hold_kind="restore", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_hold_kind="restore",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
            pod_uses_volume=True,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.run_restore_job_snapshot is None
    assert decision.replicas == 0
    assert decision.row_updates["observed_state"] == "stopping"


def test_restore_failure_holds_stopped_with_restore_failed_and_retries() -> None:
    decision = decide(
        row(hold_kind="restore", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True,
            statefulset_hold_kind="restore",
            statefulset_hold_started_at=NOW,
            statefulset_previous_image=IMAGE_A,
            statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
            restore_job_failed=True,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind == "restore"
    assert decision.replicas == 0
    assert decision.row_updates["last_error_code"] == RESTORE_FAILED


def test_no_flap_while_paused_existing_cell_keeps_its_image() -> None:
    decision = decide(
        row(observed_image=IMAGE_A, ready=True),
        rollout(paused=True, last_good_image=IMAGE_A),
        obs(statefulset_image=IMAGE_A, statefulset_replicas=1, pod_ready=True, namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc()),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.image == IMAGE_A


# --- H9 regression: deletion mid-upgrade never wedges the fleet -------------


def test_deletion_mid_upgrade_clears_the_hold_columns_even_with_a_nightly_snapshot_on_the_row() -> None:
    """H9: a cell can be deleted while it is mid an upgrade attempt. D10
    supersedes the hold unconditionally and must clear hold_kind/
    hold_started_at so this row never counts toward D6's one-at-a-time
    rule again -- reproduced starting from a row that also carries a
    nightly last_backup_snapshot, so nothing about B1 masks this path."""

    decision = decide(
        row(
            desired_state="deleted",
            hold_kind="upgrade",
            hold_started_at=NOW,
            observed_image=IMAGE_A,
            last_backup_at=NOW - timedelta(hours=5),
            last_backup_snapshot=NIGHTLY_SNAPSHOT,
        ),
        rollout(),
        obs(
            namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa",
            statefulset_exists=True,
            statefulset_image=IMAGE_A,
            statefulset_hold_kind="upgrade",
            statefulset_hold_started_at=NOW,
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["hold_kind"] is None
    assert decision.row_updates["hold_started_at"] is None
    assert decision.row_updates["observed_state"] == "deleting"
    assert decision.delete_namespace is True


# --- Upgrade candidate selection (canary first) -----------------------------


def test_select_upgrade_candidate_picks_lowest_rollout_priority() -> None:
    rows = [
        row(cell_id="bbbbbbbbbbbbbbbb", rollout_priority=2, observed_image=IMAGE_A, ready=True),
        row(cell_id="aaaaaaaaaaaaaaaa", rollout_priority=1, observed_image=IMAGE_A, ready=True),
    ]
    observations = {r.cell_id: obs(statefulset_image=IMAGE_A) for r in rows}
    winner = select_upgrade_candidate(
        rows, observations, rollout(), IMAGE_B, now=NOW, any_cell_already_upgrading=False
    )
    assert winner == "aaaaaaaaaaaaaaaa"


def test_select_upgrade_candidate_none_when_another_cell_already_upgrading() -> None:
    rows = [row(observed_image=IMAGE_A, ready=True)]
    observations = {rows[0].cell_id: obs(statefulset_image=IMAGE_A)}
    winner = select_upgrade_candidate(
        rows, observations, rollout(), IMAGE_B, now=NOW, any_cell_already_upgrading=True
    )
    assert winner is None


def test_select_upgrade_candidate_none_while_paused() -> None:
    rows = [row(observed_image=IMAGE_A, ready=True)]
    observations = {rows[0].cell_id: obs(statefulset_image=IMAGE_A)}
    winner = select_upgrade_candidate(
        rows, observations, rollout(paused=True), IMAGE_B, now=NOW, any_cell_already_upgrading=False
    )
    assert winner is None


def test_owner_transiently_ineligible_makes_the_fleet_wait_not_skip_to_a_friend() -> None:
    # M6: if the owner's cell (lowest rollout_priority) is a candidate but
    # transiently ineligible -- here, in a backup hold -- no other cell may
    # start an attempt in its place.
    owner = row(cell_id="aaaaaaaaaaaaaaaa", rollout_priority=0, observed_image=IMAGE_A, ready=True, hold_kind="backup")
    friend = row(cell_id="bbbbbbbbbbbbbbbb", rollout_priority=1, observed_image=IMAGE_A, ready=True)
    rows = [owner, friend]
    observations = {
        owner.cell_id: obs(
            statefulset_exists=True,statefulset_image=IMAGE_A, statefulset_hold_kind="backup"),
        friend.cell_id: obs(statefulset_image=IMAGE_A),
    }
    winner = select_upgrade_candidate(
        rows, observations, rollout(), IMAGE_B, now=NOW, any_cell_already_upgrading=False
    )
    assert winner is None


def test_owner_up_to_date_lets_the_next_priority_candidate_go() -> None:
    owner = row(cell_id="aaaaaaaaaaaaaaaa", rollout_priority=0, observed_image=IMAGE_B, ready=True)
    friend = row(cell_id="bbbbbbbbbbbbbbbb", rollout_priority=1, observed_image=IMAGE_A, ready=True)
    rows = [owner, friend]
    observations = {
        owner.cell_id: obs(statefulset_image=IMAGE_B),  # already on the target
        friend.cell_id: obs(statefulset_image=IMAGE_A),
    }
    winner = select_upgrade_candidate(
        rows, observations, rollout(), IMAGE_B, now=NOW, any_cell_already_upgrading=False
    )
    assert winner == friend.cell_id


def test_after_the_owner_an_ineligible_non_owner_candidate_is_skipped_not_waited_for() -> None:
    owner = row(cell_id="aaaaaaaaaaaaaaaa", rollout_priority=0, observed_image=IMAGE_B, ready=True)
    blocked = row(cell_id="bbbbbbbbbbbbbbbb", rollout_priority=1, observed_image=IMAGE_A, ready=True, hold_kind="backup")
    third = row(cell_id="cccccccccccccccc", rollout_priority=2, observed_image=IMAGE_A, ready=True)
    rows = [owner, blocked, third]
    observations = {
        owner.cell_id: obs(statefulset_image=IMAGE_B),
        blocked.cell_id: obs(
            statefulset_exists=True,statefulset_image=IMAGE_A, statefulset_hold_kind="backup"),
        third.cell_id: obs(statefulset_image=IMAGE_A),
    }
    winner = select_upgrade_candidate(
        rows, observations, rollout(), IMAGE_B, now=NOW, any_cell_already_upgrading=False
    )
    assert winner == third.cell_id


def test_candidate_inside_a_backup_backoff_window_is_not_eligible() -> None:
    candidate = row(cell_id="aaaaaaaaaaaaaaaa", rollout_priority=0, observed_image=IMAGE_A, ready=True)
    rows = [candidate]
    observations = {
        candidate.cell_id: obs(statefulset_image=IMAGE_A, statefulset_backup_retry_after=NOW + timedelta(minutes=5)),
    }
    winner = select_upgrade_candidate(
        rows, observations, rollout(), IMAGE_B, now=NOW, any_cell_already_upgrading=False
    )
    assert winner is None


# --- Deletion (D10) -----------------------------------------------------------


def test_deletion_first_deletes_the_namespace() -> None:
    decision = decide(
        row(desired_state="deleted", observed_image=IMAGE_A),
        rollout(),
        obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa"),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.delete_namespace is True
    assert decision.row_updates["observed_state"] == "deleting"


def test_deletion_then_checks_pv_absence() -> None:
    decision = decide(
        row(desired_state="deleted"),
        rollout(),
        obs(namespace_absent_confirmed=True),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.delete_namespace is False
    assert decision.row_updates["observed_state"] == "deleting"


def test_deletion_then_deletes_backup_objects() -> None:
    decision = decide(
        row(desired_state="deleted"),
        rollout(),
        obs(namespace_absent_confirmed=True, pv_absent_confirmed=True),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.delete_backup_objects is True


def test_deletion_then_deletes_the_b2_key_when_one_exists() -> None:
    decision = decide(
        row(desired_state="deleted", b2_key_id="key-1"),
        rollout(),
        obs(namespace_absent_confirmed=True, pv_absent_confirmed=True, backup_objects_absent_confirmed=True),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.delete_b2_key is True


def test_deletion_completes_and_nulls_the_wrapped_keys() -> None:
    decision = decide(
        row(desired_state="deleted", b2_key_id="key-1", generation=3),
        rollout(),
        obs(
            namespace_absent_confirmed=True,
            pv_absent_confirmed=True,
            backup_objects_absent_confirmed=True,
            b2_key_absent_confirmed=True,
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["observed_state"] == "deleted"
    assert decision.row_updates["b2_key_id"] is None
    assert decision.row_updates["backup_key_wrapped"] is None
    assert decision.row_updates["observed_generation"] == 3


def test_a_never_run_cell_can_still_be_deleted() -> None:
    decision = decide(
        row(desired_state="deleted", b2_key_id=None),
        rollout(),
        obs(namespace_absent_confirmed=True, pv_absent_confirmed=True, backup_objects_absent_confirmed=True),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["observed_state"] == "deleted"


# --- Same-pass convergence (D4 write-back) -----------------------------------


def _converged_obs(**overrides) -> ClusterObservation:
    defaults = dict(
        namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc(),
        statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=1,
        statefulset_row_generation=1, statefulset_render_digest="digest-1",
        pod_exists=True, pod_uses_volume=True, pod_ready=True, ready_pod_image=IMAGE_A, pod_init_completed=True,
    )
    defaults.update(overrides)
    return ClusterObservation(**defaults)


def test_a_pass_that_changes_the_row_generation_never_records_convergence() -> None:
    # M4: an env flip applied in this pass restarts the pod, so the Ready pod
    # seen before the apply proves nothing about the new template.
    decision = decide(
        row(desired_state="read_only", generation=2, observed_generation=1, observed_state="running", ready=True),
        rollout(),
        _converged_obs(),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        render_digest="digest-1",
        config=CONFIG,
    )
    assert decision.apply_manifests is True and decision.read_only is True
    assert "observed_generation" not in decision.row_updates
    assert decision.row_updates["ready"] is False
    assert decision.row_updates["observed_state"] == "provisioning"


def test_a_pass_that_changes_the_render_digest_never_records_convergence() -> None:
    decision = decide(
        row(observed_generation=1, observed_state="running", ready=True),
        rollout(),
        _converged_obs(),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        render_digest="digest-2",
        config=CONFIG,
    )
    assert "observed_generation" not in decision.row_updates
    assert decision.row_updates["ready"] is False


def test_a_pass_that_changes_nothing_records_convergence_from_the_ready_pod() -> None:
    decision = decide(
        row(generation=1, observed_generation=None, observed_state="provisioning"),
        rollout(),
        _converged_obs(),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        render_digest="digest-1",
        config=CONFIG,
    )
    assert decision.row_updates["observed_generation"] == 1
    assert decision.row_updates["ready"] is True
    assert decision.row_updates["observed_state"] == "running"


def test_a_stop_applied_in_this_pass_is_not_recorded_as_converged() -> None:
    decision = decide(
        row(desired_state="stopped", generation=2, observed_generation=1, observed_state="running", ready=True),
        rollout(),
        _converged_obs(pod_exists=False, pod_uses_volume=False, pod_ready=False, ready_pod_image=None),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        render_digest="digest-1",
        config=CONFIG,
    )
    assert decision.replicas == 0
    assert "observed_generation" not in decision.row_updates
    assert decision.row_updates["observed_state"] == "stopping"


# --- Backup window (D8) ------------------------------------------------------


def _due_hours(window: tuple[int, int]) -> list[int]:
    config = ReconcileConfig(backup_window=window)
    return [hour for hour in range(24) if nightly_backup_due(row(), obs(), NOW.replace(hour=hour), config)]


def test_a_backup_window_may_cross_midnight() -> None:
    assert _due_hours((2, 5)) == [2, 3, 4]
    assert _due_hours((22, 3)) == [0, 1, 2, 22, 23]
    assert _due_hours((4, 4)) == []


def test_a_parked_refused_row_is_not_re_applied_and_records_no_convergence() -> None:
    # D4: the park lives in reconcile.py's memory; decide() only honours it.
    for desired_state in ("running", "stopped"):
        refused = row(desired_state=desired_state, observed_state="stopping", last_error_code="MANIFEST_IMMUTABLE")
        assert refused.is_dirty(refusal_parked=True) is False
        assert refused.is_dirty() is True  # not parked (backoff passed, key changed, or cellctl restarted)
        decision = decide(
            refused, rollout(), _converged_obs(pod_ready=False), now=NOW, cell_image=IMAGE_A,
            start_upgrade=False, render_digest="digest-1", config=CONFIG, refusal_parked=True,
        )
        assert decision.apply_manifests is False, desired_state
        assert decision.row_updates.get("last_error_code", "MANIFEST_IMMUTABLE") == "MANIFEST_IMMUTABLE"
        assert "observed_generation" not in decision.row_updates, desired_state
        retried = decide(
            refused, rollout(), _converged_obs(), now=NOW, cell_image=IMAGE_A,
            start_upgrade=False, render_digest="digest-1", config=CONFIG,
        )
        assert retried.apply_manifests is True, desired_state


# --- Backup deadline bounds the wait for the volume (D8 "Bounded") ------------


def test_a_backup_hold_stuck_on_a_terminating_pod_ends_at_the_backup_deadline() -> None:
    decision = decide(
        row(hold_kind="backup", hold_started_at=NOW - timedelta(minutes=16), observed_image=IMAGE_A, observed_state="stopping"),
        rollout(),
        obs(
            statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=0,
            statefulset_hold_kind="backup", statefulset_hold_started_at=NOW - timedelta(minutes=16),
            pod_exists=True, pod_terminating=True, pod_uses_volume=True,
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind is None
    assert decision.replicas == 1
    assert decision.row_updates["last_error_code"] == BACKUP_FAILED
    assert decision.backup_retry_after is not None
    assert decision.delete_backup_job is True


def test_an_upgrade_hold_stuck_before_its_backup_ends_at_the_backup_deadline() -> None:
    decision = decide(
        row(hold_kind="upgrade", hold_started_at=NOW - timedelta(minutes=16), observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=0,
            statefulset_hold_kind="upgrade", statefulset_hold_started_at=NOW - timedelta(minutes=16),
            statefulset_previous_image=IMAGE_A, pod_exists=True, pod_terminating=True, pod_uses_volume=True,
        ),
        now=NOW,
        cell_image=IMAGE_B,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.hold_kind is None
    assert decision.image == IMAGE_A and decision.replicas == 1
    assert decision.row_updates["last_error_code"] == BACKUP_FAILED
    assert decision.delete_backup_job is True


def test_a_failed_backup_job_is_deleted_before_the_cell_restarts() -> None:
    for kind, image in (("backup", IMAGE_A), ("upgrade", IMAGE_B)):
        decision = decide(
            row(hold_kind=kind, hold_started_at=NOW - timedelta(minutes=2), observed_image=IMAGE_A),
            rollout(),
            obs(
                statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=0,
                statefulset_hold_kind=kind, statefulset_hold_started_at=NOW - timedelta(minutes=2),
                statefulset_previous_image=IMAGE_A, backup_job_failed=True,
            ),
            now=NOW,
            cell_image=image,
            start_upgrade=False,
            config=CONFIG,
        )
        assert decision.row_updates["last_error_code"] == BACKUP_FAILED, kind
        assert decision.delete_backup_job is True, kind


# --- Canary first (D6) --------------------------------------------------------


def test_a_stopped_canary_off_the_target_holds_the_rollout() -> None:
    owner = row(cell_id="aaaaaaaaaaaaaaaa", rollout_priority=0, desired_state="stopped", observed_image=IMAGE_A)
    tenant = row(cell_id="bbbbbbbbbbbbbbbb", rollout_priority=1, observed_image=IMAGE_A, ready=True)
    observations = {
        owner.cell_id: obs(statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=0),
        tenant.cell_id: obs(statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=1),
    }
    pick = select_upgrade_candidate([owner, tenant], observations, rollout(), IMAGE_B, now=NOW, any_cell_already_upgrading=False)
    assert pick is None


def test_a_stopped_canary_already_on_the_target_lets_the_tenants_go() -> None:
    owner = row(cell_id="aaaaaaaaaaaaaaaa", rollout_priority=0, desired_state="stopped", observed_image=IMAGE_B)
    tenant = row(cell_id="bbbbbbbbbbbbbbbb", rollout_priority=1, observed_image=IMAGE_A, ready=True)
    observations = {
        owner.cell_id: obs(statefulset_exists=True, statefulset_image=IMAGE_B, statefulset_replicas=0),
        tenant.cell_id: obs(statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=1),
    }
    pick = select_upgrade_candidate([owner, tenant], observations, rollout(), IMAGE_B, now=NOW, any_cell_already_upgrading=False)
    assert pick == tenant.cell_id


def test_a_canary_not_yet_provisioned_holds_the_rollout() -> None:
    owner = row(cell_id="aaaaaaaaaaaaaaaa", rollout_priority=0, observed_state="provisioning")
    tenant = row(cell_id="bbbbbbbbbbbbbbbb", rollout_priority=1, observed_image=IMAGE_A, ready=True)
    observations = {
        owner.cell_id: obs(),
        tenant.cell_id: obs(statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=1),
    }
    pick = select_upgrade_candidate([owner, tenant], observations, rollout(), IMAGE_B, now=NOW, any_cell_already_upgrading=False)
    assert pick is None


def test_a_backup_snapshot_id_with_a_trailing_newline_is_a_failed_backup() -> None:
    decision = decide(
        row(hold_kind="backup", hold_started_at=NOW, observed_image=IMAGE_A),
        rollout(),
        obs(
            statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=0,
            statefulset_hold_kind="backup", statefulset_hold_started_at=NOW,
            backup_job_succeeded=True, backup_job_snapshot_id=ATTEMPT_SNAPSHOT + "\n",
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["last_error_code"] == BACKUP_FAILED


def test_a_bound_pv_whose_volume_handle_differs_from_the_recorded_volume_is_a_conflict() -> None:
    # D4: once the row records a volume_id, the bound PV's CSI volumeHandle
    # must equal it.
    def verdict(volume_id, handle):
        decision = decide(
            row(volume_id=volume_id, observed_image=IMAGE_A),
            rollout(),
            obs(namespace_exists=True, namespace_cell_label="aaaaaaaaaaaaaaaa", **bound_pvc(), pvc_volume_id=handle),
            now=NOW,
            cell_image=IMAGE_A,
            start_upgrade=False,
            config=CONFIG,
        )
        return decision.row_updates.get("last_error_code")

    assert verdict("vol-1", "vol-2") == IDENTITY_CONFLICT
    assert verdict("vol-1", None) == IDENTITY_CONFLICT
    assert verdict("vol-1", "vol-1") != IDENTITY_CONFLICT
    assert verdict(None, "vol-2") != IDENTITY_CONFLICT


# --- Hold columns mirror the annotations (D4 "Annotations are the truth") ----


def test_a_statefulset_hold_the_row_does_not_show_is_written_to_the_row() -> None:
    started = NOW - timedelta(minutes=1)
    decision = decide(
        row(hold_kind=None, observed_image=IMAGE_A, ready=True),
        rollout(),
        obs(
            statefulset_exists=True, statefulset_image=IMAGE_A, statefulset_replicas=0,
            statefulset_hold_kind="backup", statefulset_hold_started_at=started,
        ),
        now=NOW,
        cell_image=IMAGE_A,
        start_upgrade=False,
        config=CONFIG,
    )
    assert decision.row_updates["hold_kind"] == "backup"
    assert decision.row_updates["hold_started_at"] == started
    # A pass during a hold writes ready from what it observes.
    assert decision.row_updates["ready"] is False


def test_a_hold_pass_writes_ready_from_the_observed_pod() -> None:
    held = dict(
        statefulset_exists=True, statefulset_image=IMAGE_B, statefulset_replicas=1,
        statefulset_hold_kind="upgrade", statefulset_hold_started_at=NOW - timedelta(minutes=3),
        statefulset_previous_image=IMAGE_A, statefulset_pre_upgrade_snapshot=ATTEMPT_SNAPSHOT,
        statefulset_target_applied_at=NOW - timedelta(minutes=1),
    )
    waiting = decide(
        row(hold_kind="upgrade", hold_started_at=NOW - timedelta(minutes=3), observed_image=IMAGE_A, ready=True),
        rollout(), obs(**held), now=NOW, cell_image=IMAGE_B, start_upgrade=False, config=CONFIG,
    )
    assert waiting.hold_kind == "upgrade"
    assert waiting.row_updates["ready"] is False
    assert "hold_kind" not in waiting.row_updates  # already mirrored
