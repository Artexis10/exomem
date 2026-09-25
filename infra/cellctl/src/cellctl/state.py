"""Row, observation and decision shapes for the D4/D6/D8/D10 state machine.

Kept separate from decide.py so tests can build fixtures without importing
the decision logic itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

DESIRED_STATES = ("running", "read_only", "stopped", "deleted")
TERMINAL_OBSERVED_STATES = frozenset({"running", "read_only", "stopped", "deleted", "failed"})
HOLD_KINDS = ("upgrade", "backup", "restore")

IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
INIT_DEADLINE_EXCEEDED = "INIT_DEADLINE_EXCEEDED"
BACKUP_FAILED = "BACKUP_FAILED"
RESTORE_FAILED = "RESTORE_FAILED"
NO_GOOD_IMAGE = "NO_GOOD_IMAGE"
TARGET_REJECTED = "TARGET_REJECTED"
UPGRADE_STALLED = "UPGRADE_STALLED"
MANIFEST_IMMUTABLE = "MANIFEST_IMMUTABLE"
# D4/D6: the rollout waits on an owner cell whose last apply was refused.
CANARY_PARKED = "CANARY_PARKED"


@dataclass(frozen=True)
class CellRow:
    cell_id: str
    tenant_id: str
    storage_gib: int
    rollout_priority: int
    desired_state: str
    desired_image: str | None
    generation: int
    observed_generation: int | None = None
    observed_state: str | None = None
    observed_image: str | None = None
    ready: bool = False
    last_error_code: str | None = None
    observed_at: datetime | None = None
    node: str | None = None
    volume_id: str | None = None
    last_backup_at: datetime | None = None
    last_backup_snapshot: str | None = None
    backup_key_wrapped: bytes | None = None
    backup_key_version: int | None = None
    b2_key_id: str | None = None
    b2_key_wrapped: bytes | None = None
    b2_key_version: int | None = None
    hold_kind: str | None = None
    hold_started_at: datetime | None = None

    def is_dirty(self, *, refusal_parked: bool = False) -> bool:
        # D4: a refused row (MANIFEST_IMMUTABLE) is dirty unless cellctl holds
        # it parked in memory, so a restarted cellctl retries it once.
        if self.last_error_code == MANIFEST_IMMUTABLE:
            return not refusal_parked
        return (
            self.generation != self.observed_generation
            or self.observed_state not in TERMINAL_OBSERVED_STATES
            or (self.observed_state in ("running", "read_only") and not self.ready)
            # D4: `failed` is terminal only for an identity conflict. A cell
            # failed on the init deadline stays observed.
            or (self.observed_state == "failed" and self.last_error_code != IDENTITY_CONFLICT)
        )


@dataclass(frozen=True)
class RolloutRow:
    paused: bool = False
    error_code: str | None = None
    held_cell_id: str | None = None
    last_good_image: str | None = None


@dataclass(frozen=True)
class ClusterObservation:
    """What cellctl can see for one cell's namespace this pass.

    All fields default to "nothing exists yet", the correct starting point
    for a brand-new cell.
    """

    namespace_exists: bool = False
    namespace_cell_label: str | None = None

    pvc_bound: bool = False
    pvc_volume_id: str | None = None
    pvc_uid: str | None = None
    pv_claim_ref_uid: str | None = None
    pv_storage_class: str | None = None

    statefulset_exists: bool = False
    statefulset_image: str | None = None
    statefulset_replicas: int | None = None
    statefulset_hold_kind: str | None = None
    statefulset_hold_started_at: datetime | None = None
    statefulset_previous_image: str | None = None
    statefulset_pre_upgrade_snapshot: str | None = None
    statefulset_target_applied_at: datetime | None = None
    statefulset_restored_snapshot: str | None = None
    statefulset_backup_retry_after: datetime | None = None
    statefulset_backup_retry_minutes: int | None = None
    statefulset_render_digest: str | None = None
    statefulset_render_digest_applied_at: datetime | None = None
    statefulset_row_generation: int | None = None

    pod_exists: bool = False
    pod_ready: bool = False
    pod_terminating: bool = False
    pod_uses_volume: bool = False
    pod_node: str | None = None
    pod_created_at: datetime | None = None
    # D4: whether the current pod's cell-init init container has terminated
    # with exit code 0. The init deadline applies only while it has not.
    pod_init_completed: bool = False
    ready_pod_image: str | None = None
    init_running: bool = False
    # N5/D4: this pod's cell-init already completed once, in an earlier pod
    # sandbox (a node reboot recreates the sandbox and re-runs it), and
    # `init_started_at` is when its current run in this sandbox started.
    init_rerun: bool = False
    init_started_at: datetime | None = None
    init_error_code: str | None = None

    backup_job_running: bool = False
    backup_job_started_at: datetime | None = None
    backup_job_succeeded: bool = False
    backup_job_failed: bool = False
    backup_job_snapshot_id: str | None = None

    restore_job_running: bool = False
    restore_job_succeeded: bool = False
    restore_job_failed: bool = False

    namespace_absent_confirmed: bool = False
    pv_absent_confirmed: bool = False
    provider_volume_absent_confirmed: bool = False
    backup_objects_absent_confirmed: bool = False
    b2_key_absent_confirmed: bool = False


@dataclass
class Decision:
    """What the reconcile loop should do this pass for one cell."""

    apply_manifests: bool = False
    replicas: int = 0
    read_only: bool = False
    image: str | None = None
    hold_kind: str | None = None
    hold_started_at: datetime | None = None
    previous_image: str | None = None
    pre_upgrade_snapshot: str | None = None
    target_applied_at: datetime | None = None
    restored_snapshot: str | None = None

    # D6/D8 backup backoff (exomem.io/backup-retry-after): set to start a new
    # backoff, or clear_backup_retry_after=True to reset it on success. When
    # neither is set, reconcile.py carries the currently observed value
    # forward unchanged, because this annotation must survive holds ending.
    backup_retry_after: datetime | None = None
    backup_retry_minutes: int | None = None
    clear_backup_retry_after: bool = False

    delete_namespace: bool = False
    delete_backup_objects: bool = False
    # D4/D8: a BACKUP_FAILED hold exit deletes this hold's backup Job before
    # the cell restarts, so the ResourceQuota does not hold the restart back.
    delete_backup_job: bool = False
    delete_b2_key: bool = False
    run_backup_job: bool = False
    run_restore_job_snapshot: str | None = None

    row_updates: dict[str, object] = field(default_factory=dict)
    rollout_updates: dict[str, object] = field(default_factory=dict)
