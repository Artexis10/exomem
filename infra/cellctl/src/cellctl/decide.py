"""The D4/D6/D8/D10 per-cell decision function.

`decide()` is a pure function: (row, rollout, observation, now, config) ->
Decision. It never touches the network or the database; reconcile.py's
imperative shell reads observations, calls this, and executes the result.

This is deliberately one dispatcher rather than four separate state
machines, because D4 (routine convergence), D6 (upgrade holds), D8 (backup
holds) and D10 (deletion) all share the same row and the same
`exomem.io/hold` marker; splitting them would just duplicate the branching
that decides which one currently owns the cell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .manifests import STORAGE_CLASS
from .rollout import current_image, target_image
from .state import (
    BACKUP_FAILED,
    IDENTITY_CONFLICT,
    INIT_DEADLINE_EXCEEDED,
    RESTORE_FAILED,
    UPGRADE_STALLED,
    CellRow,
    ClusterObservation,
    Decision,
    RolloutRow,
)

_SNAPSHOT_ID_RE = re.compile(r"[0-9a-f]{64}")  # always fullmatch: `$` accepts a trailing newline


@dataclass(frozen=True)
class ReconcileConfig:
    # D3 does not pin an exact first-boot init deadline; 10 minutes matches
    # the D6 upgrade-readiness deadline as the most defensible default.
    init_deadline: timedelta = timedelta(minutes=10)
    backup_deadline: timedelta = timedelta(minutes=15)
    upgrade_ready_deadline: timedelta = timedelta(minutes=10)
    # D6 step 5: an attempt whose target was never applied within this long
    # of the hold starting ends the same way as a refused target.
    upgrade_stall_bound: timedelta = timedelta(minutes=30)
    # D6 step 4.5: a restore hold not finished this long after it started
    # (the restore Job's own deadline, plus the previous image's readiness)
    # records RESTORE_FAILED. The hold stays; it never starts an image on
    # an unrestored volume.
    restore_bound: timedelta = timedelta(minutes=45)
    # D8: nightly backup is due once this long has passed since the last one.
    backup_interval: timedelta = timedelta(hours=20)
    # D8: the nightly backup window, as [start_hour, end_hour) UTC.
    backup_window: tuple[int, int] = (2, 5)
    # D8: at most this many backup holds run at once across the fleet.
    backup_concurrency: int = 1
    # D6/D8: the backup-retry-after backoff, doubling from this floor up to
    # this ceiling, reset by a successful backup.
    backup_retry_initial_minutes: int = 15
    backup_retry_max_minutes: int = 240
    # D4: a digest-only re-apply holds the next one back only while it is in
    # flight -- on the current digest, not Ready, and applied this recently.
    render_digest_in_flight: timedelta = timedelta(minutes=10)


DEFAULT_RECONCILE_CONFIG = ReconcileConfig()


def _identity_conflict(row: CellRow, observation: ClusterObservation) -> bool:
    # Fail-closed (D4): an existing namespace this cell doesn't already own,
    # or a bound PV whose binding identity or storage class doesn't match,
    # is a conflict. cellctl always labels the namespace it creates in the
    # same apply, so an unlabelled-but-existing namespace was never its own.
    # PersistentVolumes are never labelled (nothing copies a PVC's labels to
    # its PV), so the PV check uses the claimRef binding identity instead.
    if observation.namespace_exists and (
        observation.namespace_cell_label is None or observation.namespace_cell_label != row.cell_id
    ):
        return True
    if observation.pvc_bound and (
        observation.pv_claim_ref_uid != observation.pvc_uid
        or observation.pv_storage_class != STORAGE_CLASS
        # Once the row records a volume_id, the bound PV's CSI volumeHandle
        # must be that volume.
        or (row.volume_id is not None and observation.pvc_volume_id != row.volume_id)
    ):
        return True
    return False


def decide(
    row: CellRow,
    rollout: RolloutRow,
    observation: ClusterObservation,
    *,
    now: datetime,
    cell_image: str | None,
    start_upgrade: bool,
    start_backup: bool = False,
    render_digest: str | None = None,
    config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG,
    refusal_parked: bool = False,
) -> Decision:
    """`refusal_parked` is reconcile.py's in-memory refusal park (D4): the
    row's last apply was refused for its current generation, render digest
    and image, and its backoff has not passed."""

    if _identity_conflict(row, observation):
        return Decision(
            row_updates={
                "observed_state": "failed",
                "ready": False,
                "last_error_code": IDENTITY_CONFLICT,
                "observed_generation": row.generation,
            }
        )

    if row.desired_state == "deleted":
        return _decide_deletion(row, observation)

    # Annotations are the truth (D4): once the StatefulSet can be observed,
    # only its own hold annotation says whether a hold is active. A row that
    # still shows a hold the StatefulSet does not carry is stale -- from a
    # crash between the hold-clearing apply and the row write -- and is
    # corrected below rather than trusted or resumed.
    active_hold = observation.statefulset_hold_kind if observation.statefulset_exists else None
    stale_hold = row.hold_kind is not None and active_hold is None
    # D4: a pass changes the live StatefulSet when its row-generation
    # annotation or its render digest differs from the row. Such a pass
    # never also records convergence; the next pass observes it.
    changes_live = (
        observation.statefulset_row_generation != row.generation
        or observation.statefulset_render_digest != render_digest
    )

    if active_hold == "upgrade":
        decision = _continue_upgrade(row, rollout, observation, now, config, cell_image)
    elif active_hold == "backup":
        decision = _continue_backup(row, observation, now, config)
    elif active_hold == "restore":
        decision = _continue_restore(row, observation, now, config)
    elif row.desired_state == "stopped":
        decision = _decide_stopped(row, observation, changes_live, refusal_parked)
    else:
        read_only = row.desired_state == "read_only"
        already_served = row.observed_state in ("running", "read_only")
        if already_served and start_backup:
            decision = _start_backup(row, observation, now)
        elif start_upgrade:
            decision = _start_upgrade(row, observation, now, cell_image)
        else:
            decision = _routine_converge(
                row, rollout, observation, now, cell_image, read_only, config, changes_live, refusal_parked
            )

    if "hold_kind" not in decision.row_updates:
        # D4: the row's hold columns mirror the annotations in both
        # directions. A mismatch never starts or resumes a hold.
        if stale_hold:
            decision.row_updates = {**decision.row_updates, "hold_kind": None, "hold_started_at": None}
        elif active_hold is not None and (row.hold_kind, row.hold_started_at) != (
            active_hold,
            observation.statefulset_hold_started_at,
        ):
            decision.row_updates = {
                **decision.row_updates,
                "hold_kind": active_hold,
                "hold_started_at": observation.statefulset_hold_started_at,
            }
    if active_hold is not None and "ready" not in decision.row_updates:
        # D4: a pass during a hold writes ready from what it observes, and a
        # pass that changes the live StatefulSet never writes ready=True.
        decision.row_updates = {**decision.row_updates, "ready": observation.pod_ready and not changes_live}
    return decision


def _within_backup_window(now: datetime, config: ReconcileConfig) -> bool:
    # D8: [start, end) UTC hours. A window may cross midnight ("22-3" is
    # hour >= 22 or hour < 3); start == end is empty, which the chart
    # schema rejects.
    start, end = config.backup_window
    if start < end:
        return start <= now.hour < end
    if start > end:
        return now.hour >= start or now.hour < end
    return False


def nightly_backup_due(
    row: CellRow, observation: ClusterObservation, now: datetime, config: ReconcileConfig
) -> bool:
    if not _within_backup_window(now, config):
        return False
    retry_after = observation.statefulset_backup_retry_after
    if retry_after is not None and retry_after > now:
        return False
    if row.last_backup_at is None:
        return True
    return (now - row.last_backup_at) > config.backup_interval


def _next_backup_backoff(
    observation: ClusterObservation, now: datetime, config: ReconcileConfig
) -> tuple[datetime, int]:
    previous_minutes = observation.statefulset_backup_retry_minutes
    if previous_minutes is None:
        next_minutes = config.backup_retry_initial_minutes
    else:
        next_minutes = min(previous_minutes * 2, config.backup_retry_max_minutes)
    return now + timedelta(minutes=next_minutes), next_minutes


def _valid_snapshot_id(snapshot_id: str | None) -> bool:
    return snapshot_id is not None and bool(_SNAPSHOT_ID_RE.fullmatch(snapshot_id))


def _observe_progress(
    row: CellRow,
    observation: ClusterObservation,
    now: datetime,
    *,
    target_generation: int,
    target: str,
    read_only: bool,
    config: ReconcileConfig,
) -> dict[str, object]:
    if not observation.namespace_exists or not observation.pvc_bound:
        return {"observed_state": "provisioning", "ready": False}

    if not observation.pod_ready:
        # D3/D4: the init deadline applies only while the current pod's
        # cell-init has not completed, and is measured from that pod's
        # creation, not from a running init container, so a cell-init that
        # crash-loops still fails at the deadline. Once cell-init completed,
        # a not-Ready server is waiting: an OOM or liveness restart keeps the
        # pod's creation time. cell-init writes no termination message, so
        # the recorded code is always INIT_DEADLINE_EXCEEDED.
        # N5: a node reboot recreates the pod sandbox and re-runs a cell-init
        # that already completed, while the pod keeps its old creation time.
        # That re-run is measured from its own start in this sandbox, and one
        # still waiting to start has no deadline yet.
        anchor = observation.init_started_at if observation.init_rerun else observation.pod_created_at
        deadline_hit = (
            not observation.pod_init_completed and anchor is not None and (now - anchor) > config.init_deadline
        )
        if deadline_hit:
            return {
                "observed_state": "failed",
                "ready": False,
                "last_error_code": INIT_DEADLINE_EXCEEDED,
                "observed_generation": target_generation,
            }
        return {"observed_state": "provisioning", "ready": False}

    # D4: ready and observed_image come only from a pod whose revision
    # matches the StatefulSet's current updateRevision -- k8s_client.py
    # already scopes `pod_ready`/`ready_pod_image` to that pod, so a pod from
    # the previous revision never reaches here as "ready".
    updates: dict[str, object] = {
        "observed_state": "read_only" if read_only else "running",
        "ready": True,
        "observed_image": observation.ready_pod_image or target,
        "observed_generation": target_generation,
        "last_error_code": None,
    }
    if row.node is None and observation.pod_node:
        updates["node"] = observation.pod_node
    if row.volume_id is None and observation.pvc_volume_id:
        updates["volume_id"] = observation.pvc_volume_id
    return updates


def _routine_converge(
    row: CellRow,
    rollout: RolloutRow,
    observation: ClusterObservation,
    now: datetime,
    cell_image: str | None,
    read_only: bool,
    config: ReconcileConfig,
    changes_live: bool,
    refusal_parked: bool,
) -> Decision:
    if observation.statefulset_exists:
        image = current_image(row, observation)
    else:
        image, error = _initial_image(row, rollout, cell_image)
        if image is None:
            return Decision(
                row_updates={"observed_state": "provisioning", "ready": False, "last_error_code": error}
            )

    # D4: a cell failed on the init deadline stays observed without being
    # re-applied until its generation changes; once its pod is Ready, the
    # observation below reports it running or read_only and clears the code.
    # A parked refused row is not re-applied either: it keeps its code and
    # records no observed_generation, since nothing converged.
    init_parked = row.last_error_code == INIT_DEADLINE_EXCEEDED and row.generation == row.observed_generation
    parked = init_parked or refusal_parked
    decision = Decision(apply_manifests=not parked, replicas=1, read_only=read_only, image=image)
    if decision.apply_manifests and changes_live:
        decision.row_updates = {"observed_state": "provisioning", "ready": False}
        return decision
    decision.row_updates = _observe_progress(
        row, observation, now, target_generation=row.generation, target=image, read_only=read_only, config=config
    )
    if refusal_parked:
        decision.row_updates.pop("observed_generation", None)
        if decision.row_updates.get("last_error_code") is None:
            decision.row_updates.pop("last_error_code", None)
    if (
        decision.row_updates.get("ready")
        and cell_image is not None
        and image == cell_image
        and rollout.last_good_image != cell_image
    ):
        decision.rollout_updates = {"last_good_image": cell_image}
    return decision


def _initial_image(row: CellRow, rollout: RolloutRow, cell_image: str | None) -> tuple[str | None, str | None]:
    from .rollout import initial_image

    return initial_image(row, rollout, cell_image)


def _decide_stopped(
    row: CellRow, observation: ClusterObservation, changes_live: bool, refusal_parked: bool
) -> Decision:
    if not observation.namespace_exists and not observation.statefulset_exists:
        return Decision(
            row_updates={
                "observed_state": "stopped",
                "ready": False,
                "observed_generation": row.generation,
                "last_error_code": None,
            }
        )
    image = current_image(row, observation)
    decision = Decision(apply_manifests=not refusal_parked, replicas=0, image=image, read_only=False)
    if decision.apply_manifests and changes_live or observation.pod_exists or observation.pod_terminating:
        decision.row_updates = {"observed_state": "stopping", "ready": False}
    elif refusal_parked:
        decision.row_updates = {"observed_state": "stopped", "ready": False}
    else:
        decision.row_updates = {
            "observed_state": "stopped",
            "ready": False,
            "observed_generation": row.generation,
            "last_error_code": None,
        }
    return decision


def _start_upgrade(
    row: CellRow, observation: ClusterObservation, now: datetime, cell_image: str | None
) -> Decision:
    # D6 step 1: scale to 0 unconditionally to prepare for the pre-upgrade
    # backup, whatever the desired state -- unlike every later step in the
    # attempt, this one is not "the desired replicas", it is "stopped".
    previous = current_image(row, observation)
    read_only = row.desired_state == "read_only"
    return Decision(
        apply_manifests=True,
        replicas=0,
        read_only=read_only,
        image=previous,
        hold_kind="upgrade",
        hold_started_at=now,
        previous_image=previous,
        row_updates={
            "hold_kind": "upgrade",
            "hold_started_at": now,
            "observed_state": "stopping",
        },
    )


def _restart_on_previous_image(
    row: CellRow,
    *,
    previous_image: str | None,
    desired_replicas: int,
    read_only: bool,
    error_code: str,
    backup_retry_after: datetime | None = None,
    backup_retry_minutes: int | None = None,
    delete_backup_job: bool = False,
    pause: bool,
) -> Decision:
    """D6 step 2/3/5: end an upgrade attempt by restarting on the pre-attempt
    image at the desired replicas, clearing the hold, and recording why. Only
    TARGET_REJECTED and UPGRADE_STALLED pause the rollout; BACKUP_FAILED is
    expected to self-heal via backoff and must not stall other cells."""

    rollout_updates: dict[str, object] = {}
    if pause:
        rollout_updates = {"paused": True, "error_code": error_code, "held_cell_id": row.cell_id}
    return Decision(
        apply_manifests=True,
        replicas=desired_replicas,
        read_only=read_only,
        image=previous_image,
        hold_kind=None,
        backup_retry_after=backup_retry_after,
        backup_retry_minutes=backup_retry_minutes,
        delete_backup_job=delete_backup_job,
        row_updates={"hold_kind": None, "hold_started_at": None, "last_error_code": error_code},
        rollout_updates=rollout_updates,
    )


def _continue_upgrade(
    row: CellRow,
    rollout: RolloutRow,
    observation: ClusterObservation,
    now: datetime,
    config: ReconcileConfig,
    cell_image: str | None,
) -> Decision:
    hold_started_at = observation.statefulset_hold_started_at or row.hold_started_at or now
    previous_image = observation.statefulset_previous_image or row.observed_image
    # B1: the pre-upgrade snapshot is this attempt's own. A nightly snapshot
    # up to a day old must never stand in for it.
    snapshot = observation.statefulset_pre_upgrade_snapshot
    target = target_image(row, cell_image)
    read_only = row.desired_state == "read_only"
    desired_replicas = 0 if row.desired_state == "stopped" else 1

    # D6 step 5: stall bound, independent of whether a backup is in progress.
    if observation.statefulset_target_applied_at is None and (now - hold_started_at) > config.upgrade_stall_bound:
        return _restart_on_previous_image(
            row,
            previous_image=previous_image,
            desired_replicas=desired_replicas,
            read_only=read_only,
            error_code=UPGRADE_STALLED,
            pause=True,
        )

    if snapshot is None:
        # Steps 1-2. The backup deadline runs from hold-started-at, so it
        # also bounds the wait for the volume below (D6 step 2).
        if observation.backup_job_failed or (now - hold_started_at) > config.backup_deadline:
            retry_after, retry_minutes = _next_backup_backoff(observation, now, config)
            return _restart_on_previous_image(
                row,
                previous_image=previous_image,
                desired_replicas=desired_replicas,
                read_only=read_only,
                error_code=BACKUP_FAILED,
                backup_retry_after=retry_after,
                backup_retry_minutes=retry_minutes,
                delete_backup_job=True,
                pause=False,
            )
        if observation.backup_job_succeeded and not _valid_snapshot_id(observation.backup_job_snapshot_id):
            # M1/M2: a Job that exits 0 without a real snapshot id is a
            # failure, not a success with nothing to restore.
            retry_after, retry_minutes = _next_backup_backoff(observation, now, config)
            return _restart_on_previous_image(
                row,
                previous_image=previous_image,
                desired_replicas=desired_replicas,
                read_only=read_only,
                error_code=BACKUP_FAILED,
                backup_retry_after=retry_after,
                backup_retry_minutes=retry_minutes,
                delete_backup_job=True,
                pause=False,
            )
        if observation.backup_job_succeeded and _valid_snapshot_id(observation.backup_job_snapshot_id):
            return Decision(
                apply_manifests=True,
                replicas=0,
                read_only=read_only,
                image=previous_image,
                hold_kind="upgrade",
                hold_started_at=hold_started_at,
                previous_image=previous_image,
                pre_upgrade_snapshot=observation.backup_job_snapshot_id,
                clear_backup_retry_after=True,
                # D6 step 2: a real backup, so it also records last_backup_*.
                # The restore still reads only the annotation above.
                row_updates={
                    "observed_state": "stopping",
                    "last_backup_at": now,
                    "last_backup_snapshot": observation.backup_job_snapshot_id,
                },
            )
        # The previous pod must fully release the RWO volume before the
        # backup Job can exclusively mount it. This check must not run once
        # a snapshot is captured, or a canary pod that later uses the volume
        # would be mistaken for "still need to wait" and the StatefulSet
        # yanked back to replicas=0, forever. Found live in 3.10.
        if observation.pod_uses_volume:
            return Decision(
                apply_manifests=True,
                replicas=0,
                read_only=read_only,
                image=previous_image,
                hold_kind="upgrade",
                hold_started_at=hold_started_at,
                previous_image=previous_image,
                row_updates={"observed_state": "stopping"},
            )
        return Decision(
            apply_manifests=True,
            replicas=0,
            read_only=read_only,
            image=previous_image,
            hold_kind="upgrade",
            hold_started_at=hold_started_at,
            previous_image=previous_image,
            run_backup_job=True,
            row_updates={"observed_state": "stopping"},
        )

    # Step 3: apply the target with 1 replica, whatever the desired state
    # (D4 "Holds honour the desired state"), so an attempt is never declared
    # successful for an image that never ran. A refusal of this apply is
    # handled by reconcile.py's apply layer -- decide() is pure and never
    # sees the apply's own result, only what the NEXT observation shows.
    target_applied = observation.statefulset_image == target and (observation.statefulset_replicas or 0) == 1
    if not target_applied:
        return Decision(
            apply_manifests=True,
            replicas=1,
            read_only=read_only,
            image=target,
            hold_kind="upgrade",
            hold_started_at=hold_started_at,
            previous_image=previous_image,
            pre_upgrade_snapshot=snapshot,
            target_applied_at=now,
            row_updates={"observed_state": "provisioning"},
        )

    if not observation.pod_ready:
        # Step 4: the readiness deadline runs from when the target was
        # actually applied, not from the hold's start, because the backup
        # can take most of the backup deadline on its own.
        deadline_anchor = observation.statefulset_target_applied_at or hold_started_at
        if (now - deadline_anchor) > config.upgrade_ready_deadline:
            return Decision(
                apply_manifests=True,
                replicas=0,
                read_only=read_only,
                image=target,
                hold_kind="restore",
                hold_started_at=now,
                previous_image=previous_image,
                pre_upgrade_snapshot=snapshot,
                row_updates={"hold_kind": "restore", "hold_started_at": now, "observed_state": "stopping"},
                # H1: pause in the same decision that switches to restore, so
                # no other cell can start the same known-bad image before
                # the switch lands.
                rollout_updates={
                    "paused": True,
                    "error_code": "UPGRADE_READINESS_TIMEOUT",
                    "held_cell_id": row.cell_id,
                },
            )
        return Decision(
            apply_manifests=True,
            replicas=1,
            read_only=read_only,
            image=target,
            hold_kind="upgrade",
            hold_started_at=hold_started_at,
            previous_image=previous_image,
            pre_upgrade_snapshot=snapshot,
            target_applied_at=observation.statefulset_target_applied_at,
            row_updates={"observed_state": "provisioning"},
        )

    # Ready on the target. Only this exit applies the desired replicas, and
    # it observes rather than declares: the hold columns and the error code
    # are written here, and the next routine pass records observed_state,
    # observed_image and observed_generation from what it sees (D4). D6 step
    # 4 sets last_good_image here, because a cell that is now stopped has no
    # later routine pass that would see it Ready.
    rollout_updates: dict[str, object] = {}
    if target == cell_image and rollout.last_good_image != cell_image:
        rollout_updates = {"last_good_image": cell_image}
    return Decision(
        apply_manifests=True,
        replicas=desired_replicas,
        read_only=read_only,
        image=target,
        hold_kind=None,
        row_updates={"hold_kind": None, "hold_started_at": None, "last_error_code": None},
        rollout_updates=rollout_updates,
    )


def _start_backup(row: CellRow, observation: ClusterObservation, now: datetime) -> Decision:
    image = observation.statefulset_image or row.observed_image
    read_only = row.desired_state == "read_only"
    return Decision(
        apply_manifests=True,
        replicas=0,
        read_only=read_only,
        image=image,
        hold_kind="backup",
        hold_started_at=now,
        row_updates={"hold_kind": "backup", "hold_started_at": now, "observed_state": "stopping"},
    )


def _continue_backup(
    row: CellRow, observation: ClusterObservation, now: datetime, config: ReconcileConfig
) -> Decision:
    hold_started_at = observation.statefulset_hold_started_at or row.hold_started_at or now
    image = observation.statefulset_image or row.observed_image
    desired_replicas = 0 if row.desired_state == "stopped" else 1
    read_only = row.desired_state == "read_only"

    # D8 "Bounded": the backup deadline runs from hold-started-at, so it also
    # bounds the wait for the volume below -- a pod stuck terminating cannot
    # hold the only backup slot forever.
    if observation.backup_job_failed or (now - hold_started_at) > config.backup_deadline:
        retry_after, retry_minutes = _next_backup_backoff(observation, now, config)
        return Decision(
            apply_manifests=True,
            replicas=desired_replicas,
            read_only=read_only,
            image=image,
            hold_kind=None,
            backup_retry_after=retry_after,
            backup_retry_minutes=retry_minutes,
            delete_backup_job=True,
            row_updates={"hold_kind": None, "hold_started_at": None, "last_error_code": BACKUP_FAILED},
        )

    if observation.backup_job_succeeded and not _valid_snapshot_id(observation.backup_job_snapshot_id):
        retry_after, retry_minutes = _next_backup_backoff(observation, now, config)
        return Decision(
            apply_manifests=True,
            replicas=desired_replicas,
            read_only=read_only,
            image=image,
            hold_kind=None,
            backup_retry_after=retry_after,
            backup_retry_minutes=retry_minutes,
            delete_backup_job=True,
            row_updates={"hold_kind": None, "hold_started_at": None, "last_error_code": BACKUP_FAILED},
        )

    if observation.pod_uses_volume:
        return Decision(
            apply_manifests=True,
            replicas=0,
            read_only=read_only,
            image=image,
            hold_kind="backup",
            hold_started_at=hold_started_at,
            row_updates={"observed_state": "stopping"},
        )

    if observation.backup_job_succeeded and _valid_snapshot_id(observation.backup_job_snapshot_id):
        # D4: hold exits observe; they do not declare observed_state/ready.
        # last_backup_at/last_backup_snapshot are D8's own outputs, not part
        # of that prohibition, and belong exactly here (D8 step 4).
        return Decision(
            apply_manifests=True,
            replicas=desired_replicas,
            read_only=read_only,
            image=image,
            hold_kind=None,
            clear_backup_retry_after=True,
            row_updates={
                "hold_kind": None,
                "hold_started_at": None,
                "last_backup_at": now,
                "last_backup_snapshot": observation.backup_job_snapshot_id,
                "last_error_code": None,
            },
        )

    return Decision(
        apply_manifests=True,
        replicas=0,
        read_only=read_only,
        image=image,
        hold_kind="backup",
        hold_started_at=hold_started_at,
        run_backup_job=True,
        row_updates={"observed_state": "stopping"},
    )


def _continue_restore(
    row: CellRow, observation: ClusterObservation, now: datetime, config: ReconcileConfig
) -> Decision:
    decision = _restore_step(row, observation, now, config)
    hold_started_at = observation.statefulset_hold_started_at or row.hold_started_at or now
    if decision.hold_kind == "restore" and now - hold_started_at > config.restore_bound:
        decision.row_updates.setdefault("last_error_code", RESTORE_FAILED)
    return decision


def _restore_step(
    row: CellRow, observation: ClusterObservation, now: datetime, config: ReconcileConfig
) -> Decision:
    hold_started_at = observation.statefulset_hold_started_at or row.hold_started_at or now
    previous_image = observation.statefulset_previous_image or row.observed_image
    snapshot = observation.statefulset_pre_upgrade_snapshot
    read_only = row.desired_state == "read_only"
    desired_replicas = 0 if row.desired_state == "stopped" else 1

    if snapshot is None:
        # D6 resume: a restore hold without a pre-upgrade snapshot cannot
        # arise through the normal procedure. Fail closed rather than guess
        # at another snapshot -- a wrong guess could lose data, a wrong
        # refusal only leaves one cell stopped until the owner looks.
        return Decision(
            apply_manifests=True,
            replicas=0,
            read_only=read_only,
            image=previous_image,
            hold_kind="restore",
            hold_started_at=hold_started_at,
            previous_image=previous_image,
            row_updates={"last_error_code": RESTORE_FAILED},
            rollout_updates={"paused": True, "error_code": RESTORE_FAILED, "held_cell_id": row.cell_id},
        )

    already_restored = observation.statefulset_restored_snapshot == snapshot
    if not already_restored:
        # L1: wait for the previous/canary pod to fully release the RWO
        # volume before the restore Job runs, the same guard the backup path
        # already has -- pods on the same node can share an RWO volume, so a
        # restore next to a terminating pod would corrupt it.
        if observation.pod_uses_volume:
            return Decision(
                apply_manifests=True,
                replicas=0,
                read_only=read_only,
                image=previous_image,
                hold_kind="restore",
                hold_started_at=hold_started_at,
                previous_image=previous_image,
                pre_upgrade_snapshot=snapshot,
                row_updates={"observed_state": "stopping"},
            )
        if observation.restore_job_failed:
            return Decision(
                apply_manifests=True,
                replicas=0,
                read_only=read_only,
                image=previous_image,
                hold_kind="restore",
                hold_started_at=hold_started_at,
                previous_image=previous_image,
                pre_upgrade_snapshot=snapshot,
                row_updates={"last_error_code": RESTORE_FAILED, "observed_state": "stopping"},
            )
        if not observation.restore_job_succeeded:
            return Decision(
                apply_manifests=True,
                replicas=0,
                read_only=read_only,
                image=previous_image,
                hold_kind="restore",
                hold_started_at=hold_started_at,
                previous_image=previous_image,
                pre_upgrade_snapshot=snapshot,
                run_restore_job_snapshot=snapshot,
                row_updates={"observed_state": "stopping"},
            )
        # L2: record that this snapshot has been restored so a Job removed
        # by its TTL is never re-run against an already-restored volume.
        return Decision(
            apply_manifests=True,
            replicas=0,
            read_only=read_only,
            image=previous_image,
            hold_kind="restore",
            hold_started_at=hold_started_at,
            previous_image=previous_image,
            pre_upgrade_snapshot=snapshot,
            restored_snapshot=snapshot,
            row_updates={"observed_state": "stopping"},
        )

    target_applied = (
        observation.statefulset_image == previous_image
        and (observation.statefulset_replicas or 0) == desired_replicas
    )
    if not target_applied:
        return Decision(
            apply_manifests=True,
            replicas=desired_replicas,
            read_only=read_only,
            image=previous_image,
            hold_kind="restore",
            hold_started_at=hold_started_at,
            previous_image=previous_image,
            pre_upgrade_snapshot=snapshot,
            restored_snapshot=snapshot,
            row_updates={"observed_state": "provisioning" if desired_replicas > 0 else "stopping"},
        )

    if desired_replicas > 0 and not observation.pod_ready:
        return Decision(
            apply_manifests=True,
            replicas=desired_replicas,
            read_only=read_only,
            image=previous_image,
            hold_kind="restore",
            hold_started_at=hold_started_at,
            previous_image=previous_image,
            pre_upgrade_snapshot=snapshot,
            restored_snapshot=snapshot,
            row_updates={"observed_state": "provisioning"},
        )

    # D4: hold exits observe; they do not declare. The next routine pass
    # records observed_state/observed_image/observed_generation.
    return Decision(
        apply_manifests=True,
        replicas=desired_replicas,
        read_only=read_only,
        image=previous_image,
        hold_kind=None,
        row_updates={"hold_kind": None, "hold_started_at": None, "last_error_code": None},
    )


def _decide_deletion(row: CellRow, observation: ClusterObservation) -> Decision:
    # D10 step 0 / H9: deletion supersedes every hold. Clear the row's hold
    # columns unconditionally, and never wait on or resume a hold for a
    # deleted row.
    hold_clear: dict[str, object] = (
        {"hold_kind": None, "hold_started_at": None} if row.hold_kind is not None else {}
    )
    if not observation.namespace_absent_confirmed:
        return Decision(
            delete_namespace=True,
            row_updates={**hold_clear, "observed_state": "deleting", "ready": False},
        )
    if not observation.pv_absent_confirmed:
        return Decision(row_updates={**hold_clear, "observed_state": "deleting"})
    if not observation.backup_objects_absent_confirmed:
        return Decision(delete_backup_objects=True, row_updates={**hold_clear, "observed_state": "deleting"})
    if row.b2_key_id is not None and not observation.b2_key_absent_confirmed:
        return Decision(delete_b2_key=True, row_updates={**hold_clear, "observed_state": "deleting"})
    return Decision(
        row_updates={
            **hold_clear,
            "observed_state": "deleted",
            "ready": False,
            "observed_generation": row.generation,
            "b2_key_id": None,
            "b2_key_wrapped": None,
            "b2_key_version": None,
            "backup_key_wrapped": None,
            "backup_key_version": None,
        }
    )
