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

from .backup_window import DEFAULT_BACKUP_WINDOW, within_backup_window
from .barriers import Barrier
from .capacity import GIB, pool_takes
from .manifests import SNAPSHOT_NAME, snapshot_backup_name
from .rollout import current_image, target_image
from .state import (
    BACKUP_FAILED,
    BACKUP_OUTCOME_FAILED,
    FORWARD_ONLY_HELD,
    GROWTH_AT_CAP,
    GROWTH_NO_ROOM,
    GROWTH_NOT_NEEDED,
    GROWTH_PLANNED,
    IDENTITY_CONFLICT,
    INIT_DEADLINE_EXCEEDED,
    INIT_FAILURE_CODES,
    MOVE,
    MOVE_BACKUP_FAILED,
    RELOCATION_NO_BACKUP,
    RESTORE_FAILED,
    SNAPSHOT_BACKUP,
    UPGRADE_FORWARD_ONLY,
    UPGRADE_STALLED,
    VOLUME_MISSING,
    CellRow,
    ClusterObservation,
    Decision,
    RolloutRow,
    row_hold_kind,
)
from .storage_config import DEFAULT_STORAGE, GROWTH_THRESHOLD_PERCENT, LEGACY_CLASS, StorageConfig
from .upgrade_window import WindowConfig

_SNAPSHOT_ID_RE = re.compile(r"[0-9a-f]{64}")  # always fullmatch: `$` accepts a trailing newline


@dataclass(frozen=True)
class ReconcileConfig:
    # D3 does not pin an exact first-boot init deadline; 10 minutes is the
    # most defensible default.
    init_deadline: timedelta = timedelta(minutes=10)
    import_deadline: timedelta = timedelta(hours=2)
    import_growth_deadline: timedelta = timedelta(minutes=60)
    import_cleanup_deadline: timedelta = timedelta(minutes=2)
    import_cleanup_retry: timedelta = timedelta(hours=1)
    # The cell stops for each attempt, and a person settles the cause of an owner refusal, so retry daily.
    import_cleanup_owner_retry: timedelta = timedelta(hours=24)
    # C6: an export's pack, upload and verification not finished this long per
    # started 10 GiB of its planned archive (exports.export_deadline) fail. A
    # ready export is kept for export_ttl (EXOMEM_EXPORT_TTL_HOURS), and each
    # download link lasts export_link_ttl.
    export_deadline: timedelta = timedelta(minutes=30)
    # add-cloud-media-store D8: the short pause. The runtime stops and the pause
    # pod runs each within export_pause_start; the pause then copies the used
    # bytes at no less than the floor (CELLCTL_EXPORT_PAUSE_COPY_FLOOR_MIB, MiB
    # per second; task 5.5.9 sets it from the rehearsal). Each node runs at most
    # export_concurrency_per_node exports at once (CELLCTL_EXPORT_CONCURRENCY_PER_NODE).
    export_pause_start: timedelta = timedelta(minutes=2)
    export_pause_copy_floor_mib: int = 25
    export_concurrency_per_node: int = 1
    export_ttl: timedelta = timedelta(hours=24)
    export_link_ttl: timedelta = timedelta(seconds=300)
    backup_deadline: timedelta = timedelta(minutes=15)
    # D6 step 4. Production reads it from the chart (cellctl.upgradeReadyDeadlineMinutes,
    # via main.build_reconcile_config) and never uses this value; it is the default of
    # the tests that build a ReconcileConfig() directly.
    upgrade_ready_deadline: timedelta = timedelta(minutes=30)
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
    # move-cloud-cells-to-local-storage D3: a cell on local storage is backed
    # up hourly. Under an hour, so its newest backup is never more than an
    # hour old plus the time one backup takes (RPO <= 1 h).
    snapshot_backup_interval: timedelta = timedelta(minutes=55)
    # D8: the nightly backup window, as [start_hour, end_hour) UTC.
    backup_window: tuple[int, int] = DEFAULT_BACKUP_WINDOW
    # D8: at most this many backup holds run at once across the fleet.
    backup_concurrency: int = 1
    # automate-cloud-releases D4: at most this many upgrade holds run at once
    # on one node. Production reads it from the chart
    # (cellctl.upgradeConcurrencyPerNode, via main.build_reconcile_config).
    upgrade_concurrency_per_node: int = 1
    # automate-cloud-releases D9: the pull-only prepare Job before a node's
    # first upgrade window for a target. The chart's off switch disables it.
    upgrade_prepare_enabled: bool = True
    # D6/D8: the backup-retry-after backoff, doubling from this floor up to
    # this ceiling, reset by a successful backup.
    backup_retry_initial_minutes: int = 15
    backup_retry_max_minutes: int = 240
    # D4: a digest-only re-apply holds the next one back only while it is in
    # flight -- on the current digest, not Ready, and applied this recently.
    render_digest_in_flight: timedelta = timedelta(minutes=10)
    # move-cloud-cells-to-local-storage D4: relocations in flight at once,
    # after the owner's. Sized against the 4 h fleet RTO (design D4).
    relocation_concurrency: int = 4
    # automate-cloud-releases D4: the upgrade windows (cellctl.upgradeWindow.*).
    # main.py always builds it from the chart; None, or `enabled: false`, rolls
    # every eligible cell at once, as before windows.
    upgrade_window: WindowConfig | None = None
    # D3: the chart's copy of the barrier file.
    upgrade_barriers: tuple[Barrier, ...] = ()


DEFAULT_RECONCILE_CONFIG = ReconcileConfig()


@dataclass(frozen=True)
class StorageRoom:
    """D10: what a growth needs to know of the cells' nodes. reconcile.py
    reads it only in a pass where an hourly backup reported its use."""

    # Each node's published free bytes; None where its driver publishes none.
    free_bytes: dict[str, int | None]
    # D6: the largest non-deleted cell's size in GiB, which sets the reserve.
    largest_cell_gib: int


def _identity_conflict(row: CellRow, observation: ClusterObservation, storage: StorageConfig) -> bool:
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
        # D7: any configured cell class, so a cell keeps its volume while
        # cells migrate between classes. Any other class is still foreign.
        or observation.pv_storage_class not in storage.classes
        # Once the row records a volume_id, the bound PV's CSI volumeHandle
        # must be that volume.
        or (row.volume_id is not None and observation.pvc_volume_id != row.volume_id)
    ):
        return True
    return False


def storage_receipt(
    row: CellRow,
    observation: ClusterObservation,
    storage: StorageConfig,
    *,
    now: datetime,
) -> dict[str, object]:
    """Report a real bound claim; pod readiness and requested bytes are not capacity proof."""

    values: dict[str, object] = {
        "observed_storage_pvc_uid": None,
        "observed_storage_pvc_version": None,
        "observed_storage_capacity_bytes": None,
        "observed_storage_generation": None,
    }
    capacity = observation.pvc_capacity_bytes
    if (
        observation.namespace_exists
        and not _identity_conflict(row, observation, storage)
        and observation.pvc_bound
        and not observation.pvc_terminating
        and observation.pvc_uid
        and observation.pvc_resource_version
        and capacity is not None
        and capacity > 0
    ):
        target = row.size_gib if storage.is_local(observation.pv_storage_class) else row.storage_gib
        values.update({
            "observed_storage_pvc_uid": observation.pvc_uid,
            "observed_storage_pvc_version": observation.pvc_resource_version,
            "observed_storage_capacity_bytes": capacity,
            "observed_storage_generation": row.generation if capacity >= target * GIB and not observation.pvc_resize_pending else None,
        })
    if all(getattr(row, key) == value for key, value in values.items()):
        return {}
    return {**values, "observed_storage_at": now}


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
    storage: StorageConfig = DEFAULT_STORAGE,
    start_relocation: bool = False,
    storage_room: StorageRoom | None = None,
    crossing: bool = False,
    canary: bool = False,
) -> Decision:
    """`refusal_parked` is reconcile.py's in-memory refusal park (D4): the
    row's last apply was refused for its current generation, render digest
    and image, and its backoff has not passed. `crossing` says that the
    cell's upgrade attempt moves it across a one-way `cell` barrier
    (automate-cloud-releases D3), so a readiness miss holds instead of restoring.
    `canary` says that the row is the owner's cell, which goes first in a rollout."""

    if _identity_conflict(row, observation, storage):
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

    # D4: the volume a relocation would replace. reconcile.py starts them
    # owner first, then a bounded number at a time.
    lost_volume = relocation_volume(row, observation, storage)
    if lost_volume is not None and not can_relocate(row):
        # Nothing to restore from, and an empty volume must never stand in
        # for the tenant's: the cell stays where it is for the operator.
        decision = Decision(row_updates={"last_error_code": RELOCATION_NO_BACKUP, "ready": False})
    elif lost_volume is not None and start_relocation:
        decision = _start_relocation(row, observation, now, lost_volume, row.last_backup_snapshot)
    elif active_hold == "restore" and observation.statefulset_relocation_volume:
        decision = _continue_relocation(row, observation, now, config)
    elif claim_missing(observation) and row.volume_id is not None:
        # A fresh claim would be a new, empty volume. Only a relocation, above,
        # creates one for a cell that has had a volume.
        decision = Decision(row_updates={
            "observed_state": "failed", "ready": False, "last_error_code": VOLUME_MISSING,
            "observed_generation": row.generation,
        })
    elif active_hold in ("import", "export"):
        # C5 and C6 reconciliation own these holds, and this branch runs only
        # when neither decides, as when no row owns the hold. Routine
        # convergence never restarts their volume. An unowned hold still ends:
        # every Job of its kind that uses the volume starts before the kind's
        # deadline and runs at most that long, so after twice the deadline none
        # can use the volume. An export's only such Job is its pause.
        from .exports import pause_window  # exports.py imports this module

        started = observation.statefulset_hold_started_at or row.hold_started_at or now
        deadline = config.import_deadline if active_hold == "import" else pause_window(row, config)
        if now - started < 2 * deadline:
            decision = Decision(apply_manifests=True, replicas=0, image=current_image(row, observation),
                                hold_kind=active_hold, hold_started_at=row.hold_started_at,
                                row_updates={"ready": False, "observed_state": "stopping"})
        else:
            decision = Decision(apply_manifests=True, replicas=0 if row.desired_state == "stopped" else 1,
                                read_only=row.desired_state == "read_only", image=current_image(row, observation),
                                row_updates={"hold_kind": None, "hold_started_at": None, "ready": False,
                                             "observed_state": "stopping"})
    elif active_hold == SNAPSHOT_BACKUP:
        decision = _continue_snapshot_backup(
            row, rollout, observation, now, config, cell_image, changes_live, storage, storage_room
        )
    elif active_hold == "upgrade":
        decision = _continue_upgrade(row, rollout, observation, now, config, cell_image, crossing, canary, storage)
    elif active_hold == "backup":
        decision = _continue_backup(row, observation, now, config)
    elif active_hold == "restore":
        decision = _continue_restore(row, observation, now, config)
    elif active_hold == MOVE:
        decision = _continue_move(row, observation, now, config, storage)
    elif active_hold is None and (move_volume := planned_move_volume(row, observation, storage)) is not None:
        # Before any start: a marked cell moves first, whatever else is due.
        decision = _start_move(row, observation, now, move_volume)
    elif row.desired_state == "stopped":
        decision = _decide_stopped(row, observation, changes_live, refusal_parked)
    else:
        read_only = row.desired_state == "read_only"
        already_served = row.observed_state in ("running", "read_only")
        if already_served and start_backup and storage.is_local(observation.pv_storage_class):
            decision = _start_snapshot_backup(row, rollout, observation, now, config, cell_image, changes_live)
        elif already_served and start_backup:
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
            row_hold_kind(active_hold),
            observation.statefulset_hold_started_at,
        ):
            decision.row_updates = {
                **decision.row_updates,
                "hold_kind": row_hold_kind(active_hold),
                "hold_started_at": observation.statefulset_hold_started_at,
            }
    if "last_backup_snapshot" in decision.row_updates:
        # Every recorded backup also records the use its Job measured, as of the backup.
        decision.row_updates = {**decision.row_updates, **usage_updates(
            observation.backup_job_used_bytes, observation.backup_job_total_bytes,
            decision.row_updates.get("last_backup_at"))}
    if active_hold is not None and "ready" not in decision.row_updates:
        # D4: a pass during a hold writes ready from what it observes, and a
        # pass that changes the live StatefulSet never writes ready=True.
        decision.row_updates = {**decision.row_updates, "ready": observation.pod_ready and not changes_live}
    return decision


def usage_updates(used: int | None, total: int | None, observed_at: datetime | None) -> dict[str, object]:
    """The C1 usage columns for one measurement, written together, or none when
    the measurement is missing or does not add up (0 <= used <= total, total > 0)."""

    if used is None or total is None or observed_at is None or not 0 <= used <= total or total == 0:
        return {}
    return {"used_bytes": used, "filesystem_bytes": total, "usage_observed_at": observed_at}


def nightly_backup_due(
    row: CellRow, observation: ClusterObservation, now: datetime, config: ReconcileConfig
) -> bool:
    if not within_backup_window(now.hour, config.backup_window):
        return False
    retry_after = observation.statefulset_backup_retry_after
    if retry_after is not None and retry_after > now:
        return False
    if row.last_backup_at is None:
        return True
    return (now - row.last_backup_at) > config.backup_interval


def hourly_backup_due(
    row: CellRow, observation: ClusterObservation, now: datetime, config: ReconcileConfig
) -> bool:
    """D3: a cell on local storage is backed up hourly, at any hour, unless
    its last attempt failed and its backoff has not passed."""

    retry_after = observation.statefulset_backup_retry_after
    if retry_after is not None and retry_after > now:
        return False
    return row.last_backup_at is None or now - row.last_backup_at >= config.snapshot_backup_interval


def prune_due(row: CellRow, hold_started_at: datetime) -> bool:
    """D3: `forget --prune` runs on a cell's first backup after 02:00 UTC each
    day. Keyed on the hold's start, so every pass of one hold agrees."""

    cutoff = hold_started_at.replace(hour=2, minute=0, second=0, microsecond=0)
    if hold_started_at < cutoff:
        cutoff -= timedelta(days=1)
    return row.last_backup_at is None or row.last_backup_at < cutoff


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
        # pod's creation time. A refusal cell-init reports (D5) is recorded
        # at once and kept at the deadline; any other failure is recorded as
        # INIT_DEADLINE_EXCEEDED.
        # N5: a node reboot recreates the pod sandbox and re-runs a cell-init
        # that already completed, while the pod keeps its old creation time.
        # That re-run is measured from its own start in this sandbox, and one
        # still waiting to start has no deadline yet.
        anchor = observation.init_started_at if observation.init_rerun else observation.pod_created_at
        deadline_hit = (
            not observation.pod_init_completed and anchor is not None and (now - anchor) > config.init_deadline
        )
        refusal = observation.init_error_code
        if deadline_hit:
            return {
                "observed_state": "failed",
                "ready": False,
                "last_error_code": refusal or INIT_DEADLINE_EXCEEDED,
                "observed_generation": target_generation,
            }
        if refusal:
            return {"observed_state": "provisioning", "ready": False, "last_error_code": refusal}
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
    # A cell that has served restarts on the image it ran, even when its
    # StatefulSet is gone: the rollout target would be an upgrade with no
    # pre-upgrade backup. Only a cell that never served starts on the target.
    if observation.statefulset_exists or row.observed_image is not None:
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
    init_parked = row.last_error_code in INIT_FAILURE_CODES and row.generation == row.observed_generation
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
    # automate-cloud-releases D4: the attempt pins its target here, and every
    # later pass reads the pin, never cell_image.
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
        upgrade_target=target_image(row, cell_image),
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
    error_code: str | None,
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
    crossing: bool = False,
    canary: bool = False,
    storage: StorageConfig = DEFAULT_STORAGE,
) -> Decision:
    hold_started_at = observation.statefulset_hold_started_at or row.hold_started_at or now
    previous_image = observation.statefulset_previous_image or row.observed_image
    # B1: the pre-upgrade snapshot is this attempt's own. A nightly snapshot
    # up to a day old must never stand in for it.
    snapshot = observation.statefulset_pre_upgrade_snapshot
    # automate-cloud-releases D4: the attempt's pinned target, whatever cell_image
    # became since it started.
    # An attempt that a cellctl before the pin started carries none. Once its target is
    # applied, the applied image is the target, and the attempt continues pinned to it.
    target = observation.statefulset_upgrade_target
    if target is None and observation.statefulset_target_applied_at is not None:
        target = observation.statefulset_image
    read_only = row.desired_state == "read_only"
    desired_replicas = 0 if row.desired_state == "stopped" else 1

    def hold(**fields: object) -> Decision:
        return Decision(apply_manifests=True, read_only=read_only, hold_kind="upgrade",
                        hold_started_at=hold_started_at, previous_image=previous_image,
                        upgrade_target=target, **fields)

    if observation.statefulset_upgrade_phase == FORWARD_ONLY_HELD:
        return _forward_only_held(row, rollout, observation, now, cell_image, previous_image, read_only, hold,
                                  canary)

    def end_attempt(**kwargs: object) -> Decision:
        # automate-cloud-releases D6: an attempt that ends while its
        # VolumeSnapshot or clone still exists removes them first and keeps
        # the hold for the pass that observes them gone, so a clone never
        # outlives its hold and the quota's second claim is free before the
        # next hold asks for it.
        if observation.snapshot_exists or observation.clone_exists:
            return hold(
                replicas=desired_replicas,
                image=previous_image,
                pre_upgrade_volume_snapshot=observation.statefulset_pre_upgrade_volume_snapshot,
                pre_upgrade_snapshot=snapshot,
                target_applied_at=observation.statefulset_target_applied_at,
                delete_snapshot_backup=True,
                delete_backup_job=True,
                row_updates={"observed_state": "provisioning" if desired_replicas else "stopping"},
            )
        return _restart_on_previous_image(
            row,
            previous_image=previous_image,
            desired_replicas=desired_replicas,
            read_only=read_only,
            **kwargs,
        )

    # D6 step 5: stall bound, independent of whether a backup is in progress.
    if observation.statefulset_target_applied_at is None and (now - hold_started_at) > config.upgrade_stall_bound:
        return end_attempt(
            error_code=UPGRADE_STALLED,
            pause=True,
        )

    # automate-cloud-releases D4: a pause ends every attempt that has not applied
    # its pinned target. The cell restarts on its current image, and nothing is
    # recorded as a failure: the rollout is paused for another cell's reason.
    # An attempt with no pin and nothing applied ends the same way: cell_image may
    # have moved past what the canary ran, and a new pinned attempt starts by the
    # rollout rules.
    if (rollout.paused and observation.statefulset_target_applied_at is None) or target is None:
        return end_attempt(
            error_code=None,
            delete_backup_job=True,
            pause=False,
        )

    # automate-cloud-releases D6: a cell on snapshot-capable local storage
    # takes a VolumeSnapshot after the stop instead of running the stopped
    # backup; its restore point and backup then run beside the attempt.
    if storage.is_local(observation.pv_storage_class or observation.pvc_storage_class):
        return _continue_upgrade_local(
            row, rollout, observation, now, config, cell_image, crossing, hold_started_at,
            previous_image, target, read_only, desired_replicas, hold
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
            return hold(
                replicas=0,
                image=previous_image,
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
            return hold(replicas=0, image=previous_image, row_updates={"observed_state": "stopping"})
        return hold(replicas=0, image=previous_image, run_backup_job=True, row_updates={"observed_state": "stopping"})

    # Step 3: apply the target with 1 replica, whatever the desired state
    # (D4 "Holds honour the desired state"), so an attempt is never declared
    # successful for an image that never ran. A refusal of this apply is
    # handled by reconcile.py's apply layer -- decide() is pure and never
    # sees the apply's own result, only what the NEXT observation shows.
    target_applied = observation.statefulset_image == target and (observation.statefulset_replicas or 0) == 1
    if not target_applied:
        return hold(
            replicas=1,
            image=target,
            pre_upgrade_snapshot=snapshot,
            target_applied_at=now,
            row_updates={"observed_state": "provisioning"},
        )

    if not observation.pod_ready:
        # Step 4: the readiness deadline runs from when the target was
        # actually applied, not from the hold's start, because the backup
        # can take most of the backup deadline on its own.
        deadline_anchor = observation.statefulset_target_applied_at or hold_started_at
        if (now - deadline_anchor) > config.upgrade_ready_deadline and crossing:
            # automate-cloud-releases D3: the target may have changed state that
            # the previous image cannot read, so the cell stays stopped under its
            # upgrade hold, keeps the attempt's restore point, and waits for a person.
            return hold(
                replicas=0,
                image=target,
                pre_upgrade_snapshot=snapshot,
                target_applied_at=observation.statefulset_target_applied_at,
                upgrade_phase=FORWARD_ONLY_HELD,
                row_updates={"observed_state": "stopping", "last_error_code": UPGRADE_FORWARD_ONLY},
                rollout_updates={"paused": True, "error_code": UPGRADE_FORWARD_ONLY, "held_cell_id": row.cell_id},
            )
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
        return hold(
            replicas=1,
            image=target,
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


def _continue_upgrade_local(
    row: CellRow,
    rollout: RolloutRow,
    observation: ClusterObservation,
    now: datetime,
    config: ReconcileConfig,
    cell_image: str | None,
    crossing: bool,
    hold_started_at: datetime,
    previous_image: str | None,
    target: str | None,
    read_only: bool,
    desired_replicas: int,
    hold,
) -> Decision:
    """automate-cloud-releases D6: a local cell's restore point is the
    VolumeSnapshot taken after the stop. The target applies the moment the
    snapshot is ready; the read-only clone and its restic backup Job run
    beside it, and a backup failure never ends the attempt. The hold outlives
    Ready until the Job ends, then removes the clone and the snapshot before
    it clears, so the quota's second claim is free before any other hold --
    the hourly one included -- can ask for it."""

    vol_name = observation.statefulset_pre_upgrade_volume_snapshot
    restic_id = observation.statefulset_pre_upgrade_snapshot
    # Recorded once, then carried: the restic id the clone's backup Job
    # reported, or its absence once that Job or its deadline is past.
    recorded: dict[str, object] = {}
    if restic_id is None and observation.backup_job_succeeded and _valid_snapshot_id(
            observation.backup_job_snapshot_id):
        restic_id = observation.backup_job_snapshot_id
        recorded = {
            "last_backup_at": observation.snapshot_created_at or now,
            "last_backup_snapshot": restic_id,
        }
    job_over = observation.backup_job_succeeded or observation.backup_job_failed
    backup_deadline_passed = (now - hold_started_at) > config.backup_deadline
    backup_done = restic_id is not None or job_over or backup_deadline_passed

    # The next clone-side step, while the backup is still possible: create the
    # clone, wait for it to bind, then run the Job on it. A snapshot that is
    # already gone cannot be cloned, so nothing more is asked.
    backup_steps: dict[str, object] = {}
    if not backup_done and observation.snapshot_exists and observation.snapshot_ready:
        if not observation.clone_exists:
            backup_steps["create_clone"] = True
        elif observation.clone_bound and not observation.backup_job_running:
            backup_steps["run_backup_job"] = True

    def step(row_updates: dict[str, object], **fields: object) -> Decision:
        updates = {**recorded, **row_updates}
        if restic_id is not None:
            fields.setdefault("pre_upgrade_snapshot", restic_id)
            if recorded:
                fields.setdefault("clear_backup_retry_after", True)
        fields.setdefault("pre_upgrade_volume_snapshot", vol_name)
        fields.setdefault("target_applied_at", observation.statefulset_target_applied_at)
        return hold(row_updates=updates, **fields)

    target_applied = observation.statefulset_image == target and (observation.statefulset_replicas or 0) == 1

    # Steps 1-2: the VolumeSnapshot is taken only once the pod object is gone,
    # when the node has flushed the volume. Its name goes in the annotation at
    # once, so a restarted cellctl resumes the same restore point.
    if vol_name is None:
        if target_applied or observation.statefulset_target_applied_at is not None:
            if restic_id is None:
                # The annotation is gone after the target applied and the
                # attempt recorded no off-site snapshot either: no restore
                # point exists, so fail closed rather than label a
                # post-upgrade capture as one.
                return Decision(
                    apply_manifests=True,
                    replicas=0,
                    read_only=read_only,
                    image=target,
                    hold_kind="restore",
                    hold_started_at=now,
                    previous_image=previous_image,
                    row_updates={"hold_kind": "restore", "hold_started_at": now,
                                 "observed_state": "stopping", "last_error_code": RESTORE_FAILED},
                    rollout_updates={"paused": True, "error_code": RESTORE_FAILED,
                                     "held_cell_id": row.cell_id},
                )
            # A hold that predates the VolumeSnapshot path continues on its
            # restic restore point alone: no snapshot taken now could still be
            # pre-upgrade state, so the readiness check below runs as before.
        elif observation.pod_exists:
            return hold(replicas=0, image=previous_image, row_updates={"observed_state": "stopping"})
        return step(
            {"observed_state": "stopping"},
            replicas=0,
            image=previous_image,
            create_snapshot=True,
            pre_upgrade_volume_snapshot=snapshot_backup_name(
                SNAPSHOT_NAME, row.cell_id, hold_started_at.isoformat()),
        )

    if not target_applied:
        if not observation.snapshot_exists:
            if observation.pod_exists:
                # A pod on the pre-target image still holds the volume.
                return step({"observed_state": "stopping"}, replicas=0, image=previous_image)
            # The recorded snapshot vanished before the target was applied:
            # recreate it, since until the target lands every snapshot is
            # still pre-upgrade state.
            return step({"observed_state": "stopping"}, replicas=0, image=previous_image,
                        create_snapshot=True)
        if not observation.snapshot_ready:
            return step({"observed_state": "stopping"}, replicas=0, image=previous_image)
        # Step 3: the target applies at once, beside the clone's creation.
        return step({"observed_state": "provisioning"}, replicas=1, image=target,
                    target_applied_at=now, **backup_steps)

    if not observation.pod_ready:
        # Step 4/6: the readiness deadline runs from when the target was
        # actually applied, as on the restic path.
        deadline_anchor = observation.statefulset_target_applied_at or hold_started_at
        if (now - deadline_anchor) > config.upgrade_ready_deadline and crossing:
            # automate-cloud-releases D3: the forward-only hold keeps the
            # attempt's whole restore point -- snapshot and clone included.
            return step(
                {"observed_state": "stopping", "last_error_code": UPGRADE_FORWARD_ONLY},
                replicas=0,
                image=target,
                upgrade_phase=FORWARD_ONLY_HELD,
                rollout_updates={"paused": True, "error_code": UPGRADE_FORWARD_ONLY,
                                 "held_cell_id": row.cell_id},
            )
        if (now - deadline_anchor) > config.upgrade_ready_deadline:
            if restic_id is None and vol_name is None:
                # No restore point at all: never switch an unrestored volume
                # toward an image; hold stopped and pause the rollout.
                return Decision(
                    apply_manifests=True,
                    replicas=0,
                    read_only=read_only,
                    image=target,
                    hold_kind="restore",
                    hold_started_at=now,
                    previous_image=previous_image,
                    row_updates={"hold_kind": "restore", "hold_started_at": now,
                                 "observed_state": "stopping", "last_error_code": RESTORE_FAILED},
                    rollout_updates={"paused": True, "error_code": RESTORE_FAILED,
                                     "held_cell_id": row.cell_id},
                )
            return Decision(
                apply_manifests=True,
                replicas=0,
                read_only=read_only,
                image=target,
                hold_kind="restore",
                hold_started_at=now,
                previous_image=previous_image,
                pre_upgrade_snapshot=restic_id,
                pre_upgrade_volume_snapshot=vol_name,
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
        return step({"observed_state": "provisioning"}, replicas=1, image=target, **backup_steps)

    # Step 5: Ready. The hold stays until the backup Job ends; then the clone
    # and the snapshot go before the hold clears.
    if not backup_done or observation.snapshot_exists or observation.clone_exists:
        fields: dict[str, object] = dict(backup_steps)
        if backup_done:
            fields["delete_snapshot_backup"] = True
            fields["delete_backup_job"] = not job_over
        return step({"observed_state": "provisioning"}, replicas=desired_replicas, image=target, **fields)

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


def _forward_only_held(
    row: CellRow,
    rollout: RolloutRow,
    observation: ClusterObservation,
    now: datetime,
    cell_image: str | None,
    previous_image: str | None,
    read_only: bool,
    hold,
    canary: bool,
) -> Decision:
    """automate-cloud-releases D3: the forward-only hold. While the rollout stays
    paused, the cell stays stopped on its failed target, with the attempt's restore
    point kept, and no target is applied again. Only a person's clear of the pause
    ends it: one new attempt then starts. It keeps the pre-attempt image as its
    previous image, so it crosses again and a second miss holds again.

    The canary's new attempt goes toward its current target. Any other cell keeps the
    canary-first rule: it goes toward its own desired_image when a person set one, and
    otherwise retries its pinned target, which the canary ran before the attempt began.
    It never takes a cell_image that the canary has not run. Waiting for the canary
    instead would deadlock, because the held cell owns the only attempt slot."""

    if canary:
        resumed_target = target_image(row, cell_image)
    else:
        resumed_target = row.desired_image or observation.statefulset_upgrade_target or observation.statefulset_image

    if rollout.paused:
        return hold(
            replicas=0,
            image=observation.statefulset_image,
            pre_upgrade_snapshot=observation.statefulset_pre_upgrade_snapshot,
            pre_upgrade_volume_snapshot=observation.statefulset_pre_upgrade_volume_snapshot,
            target_applied_at=observation.statefulset_target_applied_at,
            upgrade_phase=FORWARD_ONLY_HELD,
            row_updates={"observed_state": "stopped"},
        )
    return Decision(
        apply_manifests=True,
        replicas=0,
        read_only=read_only,
        image=observation.statefulset_image,
        hold_kind="upgrade",
        hold_started_at=now,
        previous_image=previous_image,
        upgrade_target=resumed_target,
        # The quota admits one clone: the held attempt's snapshot and clone
        # go before the new attempt asks for its own.
        delete_snapshot_backup=True,
        row_updates={"hold_kind": "upgrade", "hold_started_at": now, "observed_state": "stopping",
                     "last_error_code": None},
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


def _serving_hold(
    row: CellRow,
    rollout: RolloutRow,
    observation: ClusterObservation,
    now: datetime,
    config: ReconcileConfig,
    cell_image: str | None,
    changes_live: bool,
    *,
    hold_started_at: datetime,
    **steps: object,
) -> Decision:
    """D3: a pass of a hold that does not stop the cell. Replicas, read-only
    and the observed columns follow the desired state as on a routine pass,
    so a desired-state change applies at once; the hold adds only its steps."""

    if row.desired_state == "stopped":
        decision = _decide_stopped(row, observation, changes_live, refusal_parked=False)
    else:
        decision = _routine_converge(
            row, rollout, observation, now, cell_image, row.desired_state == "read_only", config, changes_live,
            refusal_parked=False,
        )
    decision.apply_manifests = decision.image is not None
    decision.hold_kind = SNAPSHOT_BACKUP
    decision.hold_started_at = hold_started_at
    for name, value in steps.items():
        setattr(decision, name, value)
    return decision


def _start_snapshot_backup(
    row: CellRow,
    rollout: RolloutRow,
    observation: ClusterObservation,
    now: datetime,
    config: ReconcileConfig,
    cell_image: str | None,
    changes_live: bool,
) -> Decision:
    decision = _serving_hold(
        row, rollout, observation, now, config, cell_image, changes_live, hold_started_at=now, create_snapshot=True
    )
    decision.row_updates = {**decision.row_updates, "hold_kind": row_hold_kind(SNAPSHOT_BACKUP), "hold_started_at": now}
    return decision


def _continue_snapshot_backup(
    row: CellRow,
    rollout: RolloutRow,
    observation: ClusterObservation,
    now: datetime,
    config: ReconcileConfig,
    cell_image: str | None,
    changes_live: bool,
    storage: StorageConfig,
    storage_room: StorageRoom | None,
) -> Decision:
    """D3: snapshot, read-only clone, restic on the clone, then remove the
    clone and the snapshot whatever the outcome. The outcome is recorded on
    the StatefulSet the pass it is seen, and the hold ends only once both
    are gone, so a clone never outlives its hold and the quota's second
    claim is free for the next one.

    D10: the pass that records the outcome also plans the cell's growth from
    the use the backup measured, on the StatefulSet beside the outcome, so a
    retried pass plans the same size. The hold's exit records it on the row,
    and the larger claim is applied then: until the clone is gone it holds
    the quota's second share, which the larger claim needs."""

    hold_started_at = observation.statefulset_hold_started_at or row.hold_started_at or now

    def step(**steps: object) -> Decision:
        return _serving_hold(
            row, rollout, observation, now, config, cell_image, changes_live, hold_started_at=hold_started_at, **steps
        )

    outcome = observation.statefulset_backup_outcome
    grow_to = observation.statefulset_grow_storage_gib
    if outcome is not None:
        if observation.snapshot_exists or observation.clone_exists:
            # Once the outcome is on the StatefulSet the Job has served: its
            # finished pod would hold the clone's claim until the Job's TTL.
            return step(
                backup_outcome=outcome,
                grow_storage_gib=grow_to,
                delete_snapshot_backup=True,
                delete_backup_job=True,
            )
        decision = step()
        decision.hold_kind = None
        decision.row_updates = {**decision.row_updates, "hold_kind": None, "hold_started_at": None}
        if grow_to is not None and grow_to > row.size_gib:
            decision.row_updates["grown_storage_gib"] = grow_to
        return decision

    snapshot_id = observation.backup_job_snapshot_id
    if observation.backup_job_succeeded and _valid_snapshot_id(snapshot_id):
        verdict, grow_to = plan_growth(row, observation, storage, storage_room)
        decision = step(backup_outcome=snapshot_id, grow_storage_gib=grow_to, storage_growth=verdict,
                        clear_backup_retry_after=True, delete_snapshot_backup=True)
        # The backup holds the volume as of its snapshot, not the upload's end.
        # A snapshot that reports no time is dated by its hold, which began first.
        decision.row_updates = {
            **decision.row_updates, "last_backup_at": observation.snapshot_created_at or hold_started_at,
            "last_backup_snapshot": snapshot_id, "last_error_code": None,
        }
        return decision
    if observation.backup_job_failed or observation.backup_job_succeeded or now - hold_started_at > config.backup_deadline:
        # A Job that exits 0 without a real snapshot id is a failure too (M1/M2).
        retry_after, retry_minutes = _next_backup_backoff(observation, now, config)
        decision = step(backup_outcome=BACKUP_OUTCOME_FAILED, backup_retry_after=retry_after,
                        backup_retry_minutes=retry_minutes, delete_snapshot_backup=True)
        decision.row_updates = {**decision.row_updates, "last_error_code": BACKUP_FAILED}
        return decision

    if not observation.snapshot_exists:
        return step(create_snapshot=True)
    if not observation.snapshot_ready:
        return step()
    if not observation.clone_exists:
        return step(create_clone=True)
    if not observation.clone_bound:
        return step()
    return step(run_backup_job=True, backup_prune=prune_due(row, hold_started_at))


def plan_growth(
    row: CellRow, observation: ClusterObservation, storage: StorageConfig, room: StorageRoom | None
) -> tuple[str | None, int | None]:
    """D10: what one hourly backup's measured use asks of a local cell, as
    (verdict, size to grow to). Past 80% use the cell grows one default size,
    never past the cap, and only while its node's published free bytes cover
    the step and the snapshot reserve the new size implies (D6). No verdict
    when nothing was measured or the node pools could not be read."""

    local = storage.local
    used, total = observation.backup_job_used_bytes, observation.backup_job_total_bytes
    if local is None or not storage.is_local(observation.pv_storage_class) or used is None or not total or (room is None and not local.reservations):
        return None, None
    if used * 100 <= total * GROWTH_THRESHOLD_PERCENT:
        return GROWTH_NOT_NEEDED, None
    if row.size_gib >= storage.max_cell_gib:
        return GROWTH_AT_CAP, None
    grown = min(row.size_gib + local.default_cell_gib, storage.max_cell_gib)
    if local.reservations:
        # The provider reserves the retained target; cached capacity never authorizes growth.
        return GROWTH_PLANNED, grown
    free = room.free_bytes.get(observation.pv_node) if observation.pv_node else None
    if not pool_takes(local, free_bytes=free, added_bytes=(grown - row.size_gib) * GIB,
                      largest_cell_gib=max(room.largest_cell_gib, grown)):
        return GROWTH_NO_ROOM, None
    return GROWTH_PLANNED, grown


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
    # automate-cloud-releases D6: a local cell's attempt restores its clone
    # first -- a Job mounts it read-only beside the volume and rewrites the
    # backup's paths. The quota admits one clone, and the attempt's backup
    # Job and this restore share it. The restic snapshot the clone's backup
    # recorded is the fallback when the clone is already gone.
    vol_snapshot = observation.statefulset_pre_upgrade_volume_snapshot
    use_clone = vol_snapshot is not None and observation.clone_exists
    restore_point = vol_snapshot if use_clone else snapshot
    read_only = row.desired_state == "read_only"
    desired_replicas = 0 if row.desired_state == "stopped" else 1

    def hold(**fields: object) -> Decision:
        fields.setdefault("replicas", 0)
        return Decision(
            apply_manifests=True,
            read_only=read_only,
            image=previous_image,
            hold_kind="restore",
            hold_started_at=hold_started_at,
            previous_image=previous_image,
            pre_upgrade_snapshot=snapshot,
            pre_upgrade_volume_snapshot=vol_snapshot,
            **fields,
        )

    if restore_point is None:
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
            # Paused once, when the row first records it, so an owner who
            # has looked and resumes the rollout is not re-paused each pass.
            rollout_updates=(
                {}
                if row.last_error_code == RESTORE_FAILED
                else {"paused": True, "error_code": RESTORE_FAILED, "held_cell_id": row.cell_id}
            ),
        )

    already_restored = (observation.statefulset_restored_snapshot is not None
                        and observation.statefulset_restored_snapshot in (vol_snapshot, snapshot))
    if not already_restored:
        # L1: wait for the previous/canary pod to fully release the RWO
        # volume before the restore Job runs, the same guard the backup path
        # already has -- pods on the same node can share an RWO volume, so a
        # restore next to a terminating pod would corrupt it.
        if observation.pod_uses_volume:
            return hold(row_updates={"observed_state": "stopping"})
        if observation.restore_job_failed:
            return hold(row_updates={"last_error_code": RESTORE_FAILED, "observed_state": "stopping"})
        if not observation.restore_job_succeeded:
            if (vol_snapshot is not None and not observation.clone_exists
                    and observation.snapshot_exists and observation.snapshot_ready):
                # The attempt's clone is gone (or was never made) while its
                # snapshot is still there: remake the clone, wait for it to
                # bind, and the next pass runs the clone restore. Only a
                # missing snapshot falls through to the restic point.
                return hold(create_clone=True, row_updates={"observed_state": "stopping"})
            if use_clone:
                if not observation.clone_bound:
                    return hold(row_updates={"observed_state": "stopping"})
                return hold(run_clone_restore_job=True, row_updates={"observed_state": "stopping"})
            return hold(run_restore_job_snapshot=restore_point, row_updates={"observed_state": "stopping"})
        # L2: record that this snapshot has been restored so a Job removed
        # by its TTL is never re-run against an already-restored volume.
        return hold(restored_snapshot=restore_point, row_updates={"observed_state": "stopping"})

    target_applied = (
        observation.statefulset_image == previous_image
        and (observation.statefulset_replicas or 0) == desired_replicas
    )
    if not target_applied:
        return hold(
            replicas=desired_replicas,
            restored_snapshot=restore_point,
            row_updates={"observed_state": "provisioning" if desired_replicas > 0 else "stopping"},
        )

    if desired_replicas > 0 and not observation.pod_ready:
        return hold(
            replicas=desired_replicas,
            restored_snapshot=restore_point,
            row_updates={"observed_state": "provisioning"},
        )

    if observation.snapshot_exists or observation.clone_exists:
        # automate-cloud-releases D6: the attempt's clone and snapshot go
        # while the hold still owns them, so a clone never outlives its hold.
        return hold(
            replicas=desired_replicas,
            restored_snapshot=restore_point,
            delete_snapshot_backup=True,
            row_updates={"observed_state": "provisioning" if desired_replicas > 0 else "stopping"},
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


def claim_missing(observation: ClusterObservation) -> bool:
    return not observation.pvc_exists and not observation.pvc_bound


def relocation_volume(row: CellRow, observation: ClusterObservation, storage: StorageConfig) -> str | None:
    """D4: the volume a relocation from backup would replace, or None.

    Either the node holding the cell's local volume is lost (stopped by the
    node-removal rule, or gone from the API), or the cell has no claim and the
    operator marked its recorded volume lost after finding it on no disk. A
    mark naming any other volume is stale. Only a cell not already in an
    upgrade, backup or restore hold is relocated; an hourly backup on a lost
    node can never finish, so it yields."""

    if storage.local is None:
        return None
    if observation.statefulset_exists and observation.statefulset_hold_kind not in (None, SNAPSHOT_BACKUP):
        return None
    if observation.pv_node_lost and storage.is_local(observation.pv_storage_class):
        return observation.pvc_volume_id
    if claim_missing(observation) and row.volume_id is not None and observation.namespace_volume_lost == row.volume_id:
        return row.volume_id
    return None


def can_relocate(row: CellRow) -> bool:
    """A relocation restores the last backup; without a real one there is
    nothing to restore and the cell stays where it is."""

    return _valid_snapshot_id(row.last_backup_snapshot)


def _start_relocation(
    row: CellRow, observation: ClusterObservation, now: datetime, lost_volume: str, snapshot: str | None
) -> Decision:
    """D4: recreate the cell elsewhere from `snapshot`, as a restore hold that
    starts the cell only once the restore succeeds. A lost node's cell
    restores its last backup; a planned move restores the backup it just took."""

    image = current_image(row, observation)
    return Decision(
        apply_manifests=True,
        replicas=0,
        read_only=row.desired_state == "read_only",
        image=image,
        hold_kind="restore",
        hold_started_at=now,
        previous_image=image,
        pre_upgrade_snapshot=snapshot,
        relocation_volume=lost_volume,
        # An hourly hold on the lost node can never finish; its clone and
        # snapshot go with it.
        delete_snapshot_backup=observation.statefulset_hold_kind == SNAPSHOT_BACKUP,
        row_updates={"hold_kind": "restore", "hold_started_at": now, "observed_state": "stopping", "ready": False},
    )


def _continue_relocation(
    row: CellRow, observation: ClusterObservation, now: datetime, config: ReconcileConfig
) -> Decision:
    """D4: retire the claim on the lost node, then restore into a new one.

    Retain is set before the claim is deleted, so deleting it deletes no
    data, and admission refuses the delete otherwise. The row's volume id is
    released before a new claim can bind, so the identity check, never
    skipped, then records the new volume as this cell's."""

    old_volume = observation.statefulset_relocation_volume
    hold_started_at = observation.statefulset_hold_started_at or row.hold_started_at or now

    def hold(**fields: object) -> Decision:
        return Decision(
            apply_manifests=True,
            replicas=0,
            read_only=row.desired_state == "read_only",
            image=observation.statefulset_previous_image or row.observed_image,
            hold_kind="restore",
            hold_started_at=hold_started_at,
            previous_image=observation.statefulset_previous_image or row.observed_image,
            pre_upgrade_snapshot=observation.statefulset_pre_upgrade_snapshot,
            relocation_volume=old_volume,
            **fields,
        )

    released = {"volume_id": None, "node": None}
    if observation.pvc_exists and observation.pvc_volume_id == old_volume:
        if observation.pod_uses_volume:
            # The node's out-of-service taint has Kubernetes force the
            # cell's pod off it; the claim is in use until then.
            return hold(row_updates={"observed_state": "stopping"})
        if observation.pv_reclaim_policy != "Retain":
            return hold(retain_volume=observation.pv_name, row_updates={"observed_state": "stopping"})
        return hold(delete_claim=True, row_updates={"observed_state": "stopping", **released})
    if row.volume_id == old_volume:
        return hold(row_updates={"observed_state": "stopping", **released})
    if observation.pvc_terminating:
        return hold(row_updates={"observed_state": "stopping"})
    decision = _continue_restore(row, observation, now, config)
    if decision.hold_kind == "restore":
        decision.relocation_volume = old_volume
    return decision


def planned_move_volume(row: CellRow, observation: ClusterObservation, storage: StorageConfig) -> str | None:
    """Tasks 6.4 and 7.3: the volume an operator's planned move leaves, or None.

    The operator marks the cell's namespace with `exomem.io/move-volume`
    naming the volume the row records, which the cell's claim is bound to. A
    mark naming any other volume is stale and moves nothing, as a
    `volume-lost` mark is: a mark left after a finished move names the old
    volume, so it never moves the cell again. A move needs a local class to
    move to, and a node that can still run the cell's backup: a lost node's
    cell is relocated from its last backup instead."""

    target = observation.namespace_move_storage_class or (storage.local.class_name if storage.local else None)
    if target not in storage.classes or target == LEGACY_CLASS:
        return None
    if row.volume_id is None or observation.namespace_move_volume != row.volume_id:
        return None
    if not observation.pvc_bound or observation.pvc_terminating or observation.pvc_volume_id != row.volume_id:
        return None
    if observation.pv_node_lost:
        return None
    return row.volume_id


def _start_move(row: CellRow, observation: ClusterObservation, now: datetime, volume: str) -> Decision:
    """A planned move stops the cell on its own volume, claim untouched."""

    image = current_image(row, observation)
    return Decision(
        apply_manifests=True,
        replicas=0,
        read_only=row.desired_state == "read_only",
        image=image,
        hold_kind=MOVE,
        hold_started_at=now,
        relocation_volume=volume,
        row_updates={"hold_kind": row_hold_kind(MOVE), "hold_started_at": now, "observed_state": "stopping",
                     "relocation_target_storage_class": observation.namespace_move_storage_class,
                     "relocation_old_volume_id": volume, "relocation_accepted_at": None,
                     "ready": False},
    )


def _continue_move(
    row: CellRow, observation: ClusterObservation, now: datetime, config: ReconcileConfig, storage: StorageConfig
) -> Decision:
    """A planned move's first step: a fresh stopped backup of the cell's own
    volume, through the stopped backup's Job. Only once it succeeds does the
    move touch the claim, continuing as a relocation that restores that
    backup (D4), so no write the cell acknowledged before it stopped is lost.

    A failed backup leaves the cell stopped on its untouched claim, and the
    hold records the outcome so the backup is not run again. Until the
    backup succeeds, the operator's mark is what keeps the cell stopped: once
    it no longer names the cell's volume, the cell starts there as before. A
    backup that succeeded is restored whatever happens next, a lost node
    included: the cell's pod was gone before it ran, so nothing has written
    the volume since, and the last hourly backup would lose those writes."""

    hold_started_at = observation.statefulset_hold_started_at or row.hold_started_at or now
    old_volume = observation.statefulset_relocation_volume or row.volume_id
    image = current_image(row, observation)
    read_only = row.desired_state == "read_only"

    def hold(**fields: object) -> Decision:
        return Decision(apply_manifests=True, replicas=0, read_only=read_only, image=image, hold_kind=MOVE,
                        hold_started_at=hold_started_at, relocation_volume=old_volume, **fields)

    snapshot_id = observation.backup_job_snapshot_id
    if (observation.backup_job_succeeded and _valid_snapshot_id(snapshot_id)
            and observation.statefulset_backup_outcome != BACKUP_OUTCOME_FAILED):
        decision = _start_relocation(row, observation, now, old_volume, snapshot_id)
        # Its finished pod would hold the old claim until the Job expires.
        decision.delete_backup_job = True
        decision.clear_backup_retry_after = True
        decision.row_updates = {**decision.row_updates, "last_backup_at": now, "last_backup_snapshot": snapshot_id,
                                "last_error_code": None}
        return decision
    if planned_move_volume(row, observation, storage) != old_volume:
        return Decision(
            apply_manifests=True,
            replicas=0 if row.desired_state == "stopped" else 1,
            read_only=read_only,
            image=image,
            hold_kind=None,
            # A backup still running would read the volume the cell writes again.
            delete_backup_job=True,
            row_updates={"hold_kind": None, "hold_started_at": None, "last_error_code": None},
        )
    failed = hold(backup_outcome=BACKUP_OUTCOME_FAILED,
                  row_updates={"observed_state": "stopping", "last_error_code": MOVE_BACKUP_FAILED})
    if observation.statefulset_backup_outcome == BACKUP_OUTCOME_FAILED:
        return failed
    if observation.backup_job_failed or observation.backup_job_succeeded or now - hold_started_at > config.backup_deadline:
        # A Job that exits 0 without a real snapshot id is a failure too (M1/M2).
        return failed
    if observation.pod_uses_volume:
        return hold(row_updates={"observed_state": "stopping"})
    return hold(run_backup_job=True, row_updates={"observed_state": "stopping"})


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
    # Every cell's exports and imports sit under its prefix in the staging bucket, which cellctl's
    # staging key empties. Deleting a cell removes all of its data, so this comes first (D9).
    if not observation.staging_objects_absent_confirmed:
        return Decision(delete_staging_objects=True, row_updates={**hold_clear, "observed_state": "deleting"})
    # The edge must first be shown to serve a set without the cell, so no upload lands once the cell reads
    # deleted. The edge's purge pass checks its own loaded set again before each hide, and the grant's
    # grace authorizes the hide (automate-cell-storage-deletion D3).
    if observation.media_revocation_pending:
        return Decision(row_updates={**hold_clear, "observed_state": "deleting"})
    # No backup object is deleted here, and no key. The key custodian and the media edge purge a new cell's
    # prefixes on its deletion grant (automate-cell-storage-deletion). Only an old cell's prefix in the old
    # bucket stays the operator's to empty, with its key, so only an old cell raises the clean-up alert (D6).
    # Its key ID stays on the row as the mark of an old cell, so a restarted cellctl raises the alert again;
    # the ID is no secret, and the wrapped key goes.
    return Decision(
        storage_cleanup_pending=row.has_stored_object_store_key,
        row_updates={
            **hold_clear,
            "observed_state": "deleted",
            "ready": False,
            "observed_generation": row.generation,
            "b2_key_id": row.b2_key_id if row.has_stored_object_store_key else None,
            "b2_key_wrapped": None,
            "b2_key_version": None,
            "backup_key_wrapped": None,
            "backup_key_version": None,
        }
    )
