"""Row, observation and decision shapes for the D4/D6/D8/D10 state machine.

Kept separate from decide.py so tests can build fixtures without importing
the decision logic itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

DESIRED_STATES = ("running", "read_only", "stopped", "deleted")
TERMINAL_OBSERVED_STATES = frozenset({"running", "read_only", "stopped", "deleted", "failed"})
HOLD_KINDS = ("upgrade", "backup", "restore")
# move-cloud-cells-to-local-storage D3: the hourly backup of a cell on local
# storage, a hold that does not stop the cell.
SNAPSHOT_BACKUP = "snapshot-backup"


def row_hold_kind(kind: str | None) -> str | None:
    """The hold kind as the row records it. The C1 schema Substrate owns
    allows only upgrade, backup and restore, so the hourly hold is recorded
    as the backup it is; its StatefulSet annotation keeps the exact kind."""

    return "backup" if kind == SNAPSHOT_BACKUP else kind

# move-cloud-cells-to-local-storage D10: what one measured hourly backup found.
# GROWTH_NO_ROOM and GROWTH_AT_CAP each raise the storage-growth alert: the
# cell is past 80% use and cannot grow.
GROWTH_PLANNED = "grow"
GROWTH_NO_ROOM = "no-room"
GROWTH_AT_CAP = "at-cap"
GROWTH_NOT_NEEDED = "fits"

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
# move-cloud-cells-to-local-storage D4: a cell on a node confirmed stopped has
# no backup to relocate from, so it stays where it is for the operator.
RELOCATION_NO_BACKUP = "RELOCATION_NO_BACKUP"
# The row records a volume, but the cell has no claim. cellctl never creates a
# fresh one then: it would be empty, and a cell without the D5 guard would
# initialise a blank vault over the tenant's. The operator re-adopts the volume
# or marks it lost (docs/runbooks/cloud-node-loss.md).
VOLUME_MISSING = "VOLUME_MISSING"
# D4: a relocation step before the restore (the Retain patch or the claim
# delete) was refused. A refusal does not resolve itself, so the relocation
# ends for the operator, like RESTORE_FAILED, and no longer holds back others.
RELOCATION_REFUSED = "RELOCATION_REFUSED"
# move-cloud-cells-to-local-storage D5: cell-init's own value-free refusal,
# read from its termination message. Only these codes ever reach the row.
EMPTY_VOLUME_REFUSED = "CELL_INIT_EMPTY_VOLUME_REFUSED"
INIT_REFUSAL_CODES = frozenset({EMPTY_VOLUME_REFUSED})
# A cell failed by its init container stays observed, not re-applied, until
# its generation changes or its pod becomes Ready.
INIT_FAILURE_CODES = frozenset({INIT_DEADLINE_EXCEEDED}) | INIT_REFUSAL_CODES


@dataclass(frozen=True)
class CellRow:
    cell_id: str
    # C1's tenant_id is a uuid referencing exomem_tenants(id); asyncpg reads
    # it as uuid.UUID. cellctl never renders or writes it.
    tenant_id: uuid.UUID
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
    # move-cloud-cells-to-local-storage D10: the size cellctl grew the cell's
    # local volume to, or None while it has never grown.
    grown_storage_gib: int | None = None
    # Read only: a cell never backed up ages its backup alert from here.
    created_at: datetime | None = None

    @property
    def size_gib(self) -> int:
        """D10: the cell's size, the larger of what Substrate asked for and
        what cellctl grew it to. A local cell's claim and quota render at this,
        so a later pass never renders a smaller claim, and capacity charges it
        (D6). A claim on a Hetzner volume renders storage_gib."""

        return max(self.storage_gib, self.grown_storage_gib or 0)

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
    # D4: the operator's `exomem.io/volume-lost` mark on the namespace, naming
    # the recorded volume that no disk holds, so the cell is relocated.
    namespace_volume_lost: str | None = None

    pvc_bound: bool = False
    pvc_volume_id: str | None = None
    pvc_uid: str | None = None
    pv_claim_ref_uid: str | None = None
    pv_storage_class: str | None = None
    pvc_storage_class: str | None = None
    pvc_exists: bool = False
    pvc_terminating: bool = False
    pv_name: str | None = None
    pv_reclaim_policy: str | None = None
    # A local volume's node, from its PV's node affinity on the configured
    # topology key. None for a volume that is not node-local.
    pv_node: str | None = None
    # D4, observed this pass: the operator tainted that node out of service
    # by the node-removal rule and it is not Ready.
    pv_node_stop_confirmed: bool = False
    # D4, observed this pass: that node is not in the API.
    pv_node_absent: bool = False
    # D4, set by reconcile.py from the two above: the node is lost, so its
    # cells relocate. Stop-confirmed, or absent for NODE_ABSENCE_GRACE.
    pv_node_lost: bool = False

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
    # D3: an hourly backup's recorded outcome (its snapshot id, or "failed").
    # While set, the hold only removes its clone and snapshot.
    statefulset_backup_outcome: str | None = None
    # D4: a restore hold that relocates the cell off the volume with this id.
    statefulset_relocation_volume: str | None = None
    # D10: the size an hourly hold's backup planned to grow the cell to.
    statefulset_grow_storage_gib: int | None = None
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
    # D10: the backed-up filesystem's used and total bytes, as the finished
    # backup Job reported them. None when it reported none.
    backup_job_used_bytes: int | None = None
    backup_job_total_bytes: int | None = None

    # D3: the current hourly hold's VolumeSnapshot and its clone claim.
    snapshot_exists: bool = False
    snapshot_ready: bool = False
    # The instant the snapshot holds the volume as of: what its backup is worth.
    snapshot_created_at: datetime | None = None
    clone_exists: bool = False
    clone_bound: bool = False

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

    # D3, the hourly hold's steps. `backup_prune` makes its Job apply the
    # retention policy and prune; `backup_outcome` records the result on the
    # StatefulSet so the cleanup that follows never re-runs the backup.
    create_snapshot: bool = False
    create_clone: bool = False
    backup_prune: bool = False
    backup_outcome: str | None = None
    delete_snapshot_backup: bool = False

    # D4 relocation: the old volume's id while the hold runs, the PV to set
    # to Retain, and the claim delete that only a Retain PV allows.
    relocation_volume: str | None = None
    retain_volume: str | None = None
    delete_claim: bool = False

    # D10: the size this hourly hold grows the cell to once its clone is gone,
    # and what this pass's measured backup found (one of the GROWTH_ values).
    grow_storage_gib: int | None = None
    storage_growth: str | None = None

    row_updates: dict[str, object] = field(default_factory=dict)
    rollout_updates: dict[str, object] = field(default_factory=dict)
