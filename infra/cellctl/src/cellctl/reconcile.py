"""The D4 imperative shell: poll + LISTEN, observe, decide, apply, write back.

decide.py holds every decision; this module only does I/O. It is
deliberately thin — the state machine is the part worth testing in
isolation, and this glue is what 3.10's K3s integration test exercises.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import traceback
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
from kubernetes.client.rest import ApiException

from . import alerts, db
from .capacity import (
    CapacityConfig,
    CapacityObservation,
    LocalCapacityObservation,
    NodeCapacity,
    SharedWorkerPolicy,
    compute_local_capacity,
    compute_node_capacity,
    compute_shared_capacity,
)
from .decide import (
    DEFAULT_RECONCILE_CONFIG,
    ReconcileConfig,
    StorageRoom,
    can_relocate,
    decide,
    hourly_backup_due,
    nightly_backup_due,
    relocation_volume,
)
from .manifests import (
    BACKUP_JOB_NAME,
    CLONE_CLAIM_NAME,
    HOURLY_RETENTION_ARGS,
    RENDER_VERSION,
    RETENTION_ARGS,
    SNAPSHOT_NAME,
    STORAGE_CLASS,
    CellManifestSpec,
    ResourceSettings,
    check_artifact_broker_url,
    dedicated_placement,
    hold_job_name,
    namespace_name,
    render_backup_job,
    render_cell_manifests,
    render_clone_claim,
    render_restore_job,
    render_volume_snapshot,
)
from .rollout import (
    current_image,
    initial_image,
    owner_cell,
    parked_canary,
    select_upgrade_candidate,
)
from .secrets import derive_cell_bearer, unwrap_secret, wrap_secret
from .state import (
    BACKUP_FAILED,
    CANARY_PARKED,
    GROWTH_NOT_NEEDED,
    MANIFEST_IMMUTABLE,
    RELOCATION_REFUSED,
    RESTORE_FAILED,
    SNAPSHOT_BACKUP,
    TARGET_REJECTED,
    CellRow,
    ClusterObservation,
)
from .storage.interface import CELL_KEY_CAPABILITIES
from .storage.topolvm import LocalVolumeState, cell_data_absent
from .storage_config import DEFAULT_STORAGE, LocalStorage, StorageConfig

logger = logging.getLogger("cellctl")

# D4 amendment (2026-10-05): a pass follows the last one after the fast
# interval while anything is in transition, and after the idle interval once
# the fleet has settled. A NOTIFY wakes the loop at once either way.
FAST_POLL_INTERVAL_SECONDS = 5.0
IDLE_POLL_INTERVAL_SECONDS = 30.0
# D4 amendment: observed_at is written with an observed change, and on its
# own once the stored value is this old, so it reads as "last observed, at
# most about this stale" without a write per cell per pass.
OBSERVED_AT_REFRESH = timedelta(minutes=5)
RECONNECT_BACKOFF_INITIAL_SECONDS = 1.0
RECONNECT_BACKOFF_MAX_SECONDS = 30.0
DEFAULT_HEARTBEAT_PATH = "/tmp/cellctl-heartbeat"
DEFAULT_ADMISSION_POLICY_NAME = "exomem-cellctl-scope"
DEFAULT_ADMISSION_BINDING_NAME = "exomem-cellctl-scope"
# D4: the second policy, which admits a cellctl pod only where the cell's
# deny-all default-deny NetworkPolicy exists (its binding's paramRef).
DEFAULT_ISOLATION_POLICY_NAME = "exomem-cellctl-isolation"
DEFAULT_ISOLATION_BINDING_NAME = "exomem-cellctl-isolation"
ISOLATION_PARAM_NAME = "default-deny"
# move-cloud-cells-to-local-storage D4: with local storage configured, the
# third policy admits cellctl's delete of a cell's claim only while every PV
# claimed by it is Retain (its binding takes every PV as a param).
RETAINED_DELETE_POLICY_NAME = "exomem-cellctl-retained-delete"

# D4: an apply answered with a 4xx other than these is a refusal. These three
# are transient and retried at the normal cadence, like a 5xx or a timeout.
TRANSIENT_CLIENT_STATUSES = frozenset({408, 409, 429})


def _is_refusal(error: ApiException) -> bool:
    status = error.status or 0
    return 400 <= status < 500 and status not in TRANSIENT_CLIENT_STATUSES


def _apply_cell_manifests(
    cluster: ClusterGateway,
    cell_id: str,
    manifests: list[dict],
    *,
    hold_active: bool,
    statefulset_exists: bool,
) -> tuple[list[tuple[str, str]], bool]:
    """D4: each object is applied on its own, in render order, and a refusal
    of one does not stop the others, so the StatefulSet (which carries
    replicas and hold state) always gets its attempt. Returns the refused
    objects as (kind, name) and whether the StatefulSet was applied. A
    transient error still raises and aborts the row, as before.

    Two exceptions keep a healthy cell on the template it already runs.
    Outside an active hold, the StatefulSet is held back in a pass whose
    Secret was refused: a rotation's new template references a key only the
    refused Secret carries, and its pod would fail to start. A StatefulSet
    that does not exist yet is created only when every earlier object
    applied, so a new cell never starts without its NetworkPolicies. Inside
    a hold, including its exit, the StatefulSet is always applied."""

    refused: list[tuple[str, str]] = []
    statefulset_applied = False
    for manifest in manifests:
        kind, name = manifest["kind"], manifest["metadata"]["name"]
        if kind == "StatefulSet":
            secret_refused = any(refused_kind == "Secret" for refused_kind, _ in refused)
            if (secret_refused and not hold_active) or (refused and not statefulset_exists):
                logger.error("cellctl held back StatefulSet %s for cell %s: an earlier object was refused", name, cell_id)
                continue
        try:
            cluster.apply_all([manifest])
        except ApiException as error:
            if not _is_refusal(error):
                raise
            logger.error(
                "cellctl apply refused for cell %s: %s %s status=%s reason=%s", cell_id, kind, name, error.status, error.reason
            )
            refused.append((kind, name))
            continue
        if kind == "StatefulSet":
            statefulset_applied = True
    return refused, statefulset_applied


def _describe_error(error: BaseException) -> str:
    """D4: logs are content-private. An error is logged by class, plus status
    and reason for a Kubernetes ApiException, and the innermost frame. Never
    its str or a traceback that renders it: an ApiException's str carries the
    response headers and body, and a cell Secret's apply response carries
    that cell's credentials."""

    described = type(error).__name__
    if isinstance(error, ApiException):
        described += f" status={error.status} reason={error.reason}"
    frames = traceback.extract_tb(error.__traceback__)
    if frames:
        described += f" at {Path(frames[-1].filename).name}:{frames[-1].lineno} in {frames[-1].name}"
        # The innermost frame is usually inside the Kubernetes client, and the
        # innermost cellctl one is its request wrapper; the cellctl frames
        # around it name the call that failed.
        own = [frame for frame in frames if Path(frame.filename).parent == _PACKAGE_DIR][-3:]
        if own and own[-1] is not frames[-1]:
            described += " via " + " < ".join(
                f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}" for frame in reversed(own)
            )
    return described


_PACKAGE_DIR = Path(__file__).parent


# A lost or half-open database session. It fails every row alike, so it
# aborts the pass and reaches run_loop's reconnect instead of being logged
# once per row against a dead connection. TimeoutError is asyncpg's command
# timeout (and an OSError, which run_loop already treats as a lost session).
_ROW_SESSION_ERRORS = (asyncpg.InterfaceError, asyncpg.PostgresConnectionError, TimeoutError)

# D4: orphan namespaces are listed at most this often, and Hetzner's volume
# API is asked about a deleting row's volume at most this often.
ORPHAN_SCAN_INTERVAL = timedelta(minutes=10)
VOLUME_CHECK_INTERVAL = timedelta(minutes=1)
OBJECT_STORAGE_KEY_CHECK_INTERVAL = timedelta(minutes=10)
# D4: a refused row is retried on a backoff doubling from this floor to this
# ceiling, so a transient cause (a deploy-skew 403) clears by itself.
REFUSAL_BACKOFF_INITIAL = timedelta(minutes=2)
REFUSAL_BACKOFF_MAX = timedelta(hours=1)

# (row generation, applied render digest, image to render)
RefusalKey = tuple[int, str, str | None]


@dataclass(frozen=True)
class RefusalPark:
    """D4: the last refused apply of one row. Any change to the key (the
    row, the settings or the chart) unparks it; so does the backoff passing."""

    key: RefusalKey
    retry_at: datetime
    backoff: timedelta
    # The StatefulSet itself was refused or held back, so nothing that goes
    # through it (a backup hold, a digest re-apply) can start either.
    statefulset_blocked: bool
    # A backup or restore Job was refused; it waits out the backoff too, so
    # a hold's per-pass Job start never retries at loop speed.
    job_blocked: bool = False


@dataclass
class LoopMemory:
    """Process-local rate limits for the calls D4 bounds by time. Nothing
    here is needed for correctness: a restarted cellctl starts afresh and at
    worst makes each bounded call once more."""

    orphan_scan_at: datetime | None = None
    volume_checked_at: dict[str, datetime] = field(default_factory=dict)
    # A volume confirmed absent stays absent: its id is never reused.
    volumes_absent: set[str] = field(default_factory=set)
    refusals: dict[str, RefusalPark] = field(default_factory=dict)
    # B2 keys cannot change after creation, so a key listed with every
    # capability the backup needs is never listed again by this process.
    object_storage_keys_verified: set[str] = field(default_factory=set)
    object_storage_key_checked_at: dict[str, datetime] = field(default_factory=dict)
    # Task 2.8 and D10: each platform alert's state last delivered (absent:
    # nothing yet this process) and when a failed delivery may be tried again,
    # and the stale cells last logged.
    alert_delivered: dict[str, bool] = field(default_factory=dict)
    alert_retry_at: dict[str, datetime] = field(default_factory=dict)
    stale_backups: list[str] = field(default_factory=list)
    # D10: what each local cell's latest measured hourly backup found (a
    # GROWTH_ value). A restart forgets them, and the growth alert waits until
    # every serving local cell has been measured again.
    growth_verdicts: dict[str, str] = field(default_factory=dict)
    # D4: when this process first saw each node absent from the API, until it
    # is seen present again. A restart restarts every clock.
    node_absent_since: dict[str, datetime] = field(default_factory=dict)


# D4: a Node gone from the API counts as lost only once this process has seen
# it absent this long. `kubectl delete node` on a healthy agent re-registers
# it within seconds, and that must neither relocate its cells nor complete a
# deletion proof. Five minutes is several fast passes, and small against the
# 4 h fleet RTO.
NODE_ABSENCE_GRACE = timedelta(minutes=5)


def _note_node_presence(memory: LoopMemory, now: datetime, *, absent: set[str], present: set[str]) -> None:
    for node in present:
        memory.node_absent_since.pop(node, None)
    for node in absent:
        memory.node_absent_since.setdefault(node, now)


def _node_gone(memory: LoopMemory, node: str, now: datetime) -> bool:
    since = memory.node_absent_since.get(node)
    return since is not None and now - since >= NODE_ABSENCE_GRACE


def _with_lost_nodes(
    observations: dict[str, ClusterObservation], memory: LoopMemory, now: datetime
) -> dict[str, ClusterObservation]:
    """D4: a cell's node is lost once the operator confirmed its stop, or
    once it has been absent from the API for NODE_ABSENCE_GRACE."""

    _note_node_presence(
        memory, now,
        absent={o.pv_node for o in observations.values() if o.pv_node is not None and o.pv_node_absent},
        present={o.pv_node for o in observations.values() if o.pv_node is not None and not o.pv_node_absent},
    )
    return {
        cell_id: dataclass_replace(observation, pv_node_lost=observation.pv_node is not None and (
            observation.pv_node_stop_confirmed
            or (observation.pv_node_absent and _node_gone(memory, observation.pv_node, now))))
        for cell_id, observation in observations.items()
    }


def _record_refusal(
    memory: LoopMemory,
    cell_id: str,
    key: RefusalKey,
    now: datetime,
    *,
    statefulset_blocked: bool,
    job_blocked: bool = False,
) -> None:
    """The backoff doubles only on a real retry of the same key. A pass
    inside the window (a hold's own pass) keeps it; a new key starts over."""

    previous = memory.refusals.get(cell_id)
    if previous is not None and previous.key == key and now < previous.retry_at:
        memory.refusals[cell_id] = dataclass_replace(
            previous,
            statefulset_blocked=statefulset_blocked,
            job_blocked=job_blocked or previous.job_blocked,
        )
        return
    if previous is not None and previous.key == key:
        backoff = min(previous.backoff * 2, REFUSAL_BACKOFF_MAX)
    else:
        backoff = REFUSAL_BACKOFF_INITIAL
    memory.refusals[cell_id] = RefusalPark(
        key=key,
        retry_at=now + backoff,
        backoff=backoff,
        statefulset_blocked=statefulset_blocked,
        job_blocked=job_blocked,
    )


def _run_cell_job(cluster: ClusterGateway, cell_id: str, manifest: dict, refused: list[tuple[str, str]]) -> None:
    """D4: a Job apply refusal is recorded like any other object's, so the
    pass's row updates still land. A transient error still raises."""

    try:
        cluster.run_job(manifest)
    except ApiException as error:
        if not _is_refusal(error):
            raise
        name = manifest["metadata"]["name"]
        logger.error(
            "cellctl apply refused for cell %s: Job %s status=%s reason=%s", cell_id, name, error.status, error.reason
        )
        refused.append(("Job", name))


def _image_to_render(row: CellRow, observation: ClusterObservation, rollout, cell_image: str | None) -> str | None:
    """The image a routine pass renders (decide._routine_converge)."""

    if observation.statefulset_exists:
        return current_image(row, observation)
    return initial_image(row, rollout, cell_image)[0]


@dataclass(frozen=True)
class SecretsConfig:
    cell_token_key_current: bytes
    cell_token_key_previous: bytes | None
    cell_token_key_version: int
    cell_token_key_previous_version: int | None
    backup_master_keys: dict[int, bytes]  # version -> key
    backup_master_key_current_version: int


@dataclass(frozen=True)
class ClusterConfig:
    object_storage_bucket: str
    # D8 amendment: restic reaches B2 through its S3-compatible endpoint
    # (e.g. "https://s3.us-west-002.backblazeb2.com" in production, a local
    # MinIO URL in the 3.10 test).
    object_storage_endpoint: str = ""
    resources: ResourceSettings = ResourceSettings()
    model_env: dict[str, str] | None = None
    capacity: CapacityConfig = CapacityConfig()
    job_egress_except: tuple[str, ...] = ()
    admission_policy_name: str = DEFAULT_ADMISSION_POLICY_NAME
    admission_binding_name: str = DEFAULT_ADMISSION_BINDING_NAME
    isolation_policy_name: str = DEFAULT_ISOLATION_POLICY_NAME
    isolation_binding_name: str = DEFAULT_ISOLATION_BINDING_NAME
    artifact_broker_url: str = ""
    artifact_broker_cell_ids: tuple[str, ...] = ()
    dedicated_cell_ids: tuple[str, ...] = ()
    # cloud-multimodal-processing D7: engine name -> the cells it is switched on in.
    # A selection, not a chart-wide value, so an engine can canary in one cell.
    media_engine_cell_ids: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    shared_worker: SharedWorkerPolicy | None = None
    storage: StorageConfig = DEFAULT_STORAGE
    # Task 2.8: (namespace, name, key) of the platform's alert-delivery
    # Secret. None leaves the backup-age alert to the log.
    alert_delivery_secret: tuple[str, str, str] | None = None

    def __post_init__(self) -> None:
        cell_ids = self.dedicated_cell_ids
        if (
            not isinstance(cell_ids, tuple)
            or len(cell_ids) > 1024
            or any(not isinstance(cell_id, str) or not re.fullmatch(r"[a-z2-7]{16}", cell_id) for cell_id in cell_ids)
            or len(set(cell_ids)) != len(cell_ids)
        ):
            raise ValueError("dedicated placement requires at most 1024 unique base32 cell IDs")
        if self.shared_worker and set(self.dedicated_cell_ids) & set(self.shared_worker.cell_ids):
            raise ValueError("dedicated and shared cell selections overlap")
        check_artifact_broker_url(self.artifact_broker_url)
        cell_ids = self.artifact_broker_cell_ids
        if (
            not isinstance(cell_ids, tuple)
            or len(cell_ids) > 1024
            or any(not isinstance(cell_id, str) or not re.fullmatch(r"[a-z2-7]{16}", cell_id) for cell_id in cell_ids)
            or len(set(cell_ids)) != len(cell_ids)
            or (cell_ids and not self.artifact_broker_url)
        ):
            raise ValueError("artifact broker activation requires a literal endpoint and unique base32 cell IDs")
        for engine, cell_ids in self.media_engine_cell_ids.items():
            if (
                not isinstance(engine, str)
                # Engine names are a value of the comma-separated cell variable.
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", engine)
                or not isinstance(cell_ids, tuple)
                or len(cell_ids) > 1024
                or any(not isinstance(cell_id, str) or not re.fullmatch(r"[a-z2-7]{16}", cell_id) for cell_id in cell_ids)
                or len(set(cell_ids)) != len(cell_ids)
            ):
                raise ValueError("a media engine selection maps lower-case engine names to unique base32 cell IDs")

    def workload_for_cell(self, cell_id: str) -> tuple[ResourceSettings, dict]:
        if cell_id in self.dedicated_cell_ids:
            return self.resources, dedicated_placement(cell_id)
        policy = self.shared_worker
        if policy and policy.selects(cell_id):
            return policy.resources, {
                "nodeSelector": {"exomem.io/shared-profile": policy.profile,
                                 policy.topology_key: policy.topology_value},
                "tolerations": [{"key": "exomem.io/shared-profile", "operator": "Equal",
                                 "value": policy.profile, "effect": "NoSchedule"}],
            }
        return self.resources, {}

    def artifact_broker_for_cell(self, cell_id: str) -> str:
        return self.artifact_broker_url if cell_id in self.artifact_broker_cell_ids else ""

    def media_engines_for_cell(self, cell_id: str) -> str:
        """The value of the cell's media engine switch; "" when no engine selects it."""
        return ",".join(sorted(engine for engine, ids in self.media_engine_cell_ids.items() if cell_id in ids))


class ClusterGateway:
    """The subset of ClusterClient + storage that reconcile.py needs, kept
    as a Protocol-shaped class so tests can supply a fake."""

    def observe_cells(
        self, cells: dict[str, str]
    ) -> dict[str, ClusterObservation | Exception]: ...  # pragma: no cover
    def apply_all(self, manifests: list[dict]) -> None: ...  # pragma: no cover
    def delete_namespace(self, name: str) -> None: ...  # pragma: no cover
    def delete_job(self, namespace: str, name: str) -> None: ...  # pragma: no cover
    def delete_snapshot_backup(self, namespace: str, snapshot: str, claim: str) -> None: ...  # pragma: no cover
    def retain_volume(self, name: str) -> None: ...  # pragma: no cover
    def delete_claim(self, namespace: str) -> None: ...  # pragma: no cover
    def run_job(self, manifest: dict) -> None: ...  # pragma: no cover
    def namespace_absent(self, name: str) -> bool: ...  # pragma: no cover
    def pv_absent_for_namespace(self, namespace: str) -> bool: ...  # pragma: no cover
    def local_volume_state(self, local: LocalStorage) -> LocalVolumeState: ...  # pragma: no cover
    def admission_policy_present(
        self, policy_name: str, binding_name: str, *, param_name: str | None = None, param_selects_all: bool = False
    ) -> bool: ...  # pragma: no cover
    def list_cell_namespaces(self) -> dict[str, str]: ...  # pragma: no cover
    def capacity_inputs(
        self, *, csi_driver: str, shared_policy: SharedWorkerPolicy | None = None
    ) -> CapacityObservation: ...  # pragma: no cover
    def local_capacity_inputs(self, local: LocalStorage) -> LocalCapacityObservation: ...  # pragma: no cover
    def read_secret_value(self, namespace: str, name: str, key: str) -> str: ...  # pragma: no cover


def _active_hold(row: CellRow, observation: ClusterObservation) -> str | None:
    # D4: annotations are the truth once the StatefulSet can be observed.
    return observation.statefulset_hold_kind if observation.statefulset_exists else row.hold_kind


def _restore_blocks_upgrades(
    row: CellRow, observation: ClusterObservation, rollout, now: datetime, config: ReconcileConfig
) -> bool:
    """D6: a restore hold blocks every new upgrade attempt. One older than
    the restore bound (so it has recorded RESTORE_FAILED) stops blocking
    once the owner resumes the rollout: the hold itself stays, fail-closed,
    but a single broken restore never stalls the fleet's releases
    indefinitely. Keyed on the hold's age, since a refusal inside the hold
    replaces the row's error code with MANIFEST_IMMUTABLE."""

    if _active_hold(row, observation) != "restore":
        return False
    hold_started_at = observation.statefulset_hold_started_at or row.hold_started_at
    overdue = hold_started_at is not None and now - hold_started_at > config.restore_bound
    return rollout.paused or not overdue


def _hetzner_volume_absent(volume_id: str, volume_provider, now: datetime, memory: LoopMemory) -> bool:
    """D4 API budget: at most one Hetzner call a minute per deleting volume.
    A check skipped for the budget is not a confirmation of absence."""

    if volume_id in memory.volumes_absent:
        return True
    checked_at = memory.volume_checked_at.get(volume_id)
    if checked_at is not None and now - checked_at < VOLUME_CHECK_INTERVAL:
        return False
    memory.volume_checked_at[volume_id] = now
    if volume_provider.get_volume(volume_id) is None:
        memory.volumes_absent.add(volume_id)
        return True
    return False


def _with_recent_absences(state: LocalVolumeState, now: datetime, memory: LoopMemory) -> LocalVolumeState:
    """D4: a node absent from the API for less than NODE_ABSENCE_GRACE still
    counts as present, so its volumes still block a deletion proof."""

    named = {node for _, node, _ in state.claimed_pvs} | {node for _, node in state.snapshot_contents}
    named |= {volume.node for volume in state.volumes}
    named -= {None, ""}
    _note_node_presence(memory, now, absent=named - state.present_nodes, present=set(state.present_nodes))
    recent = {node for node in named - state.present_nodes if not _node_gone(memory, node, now)}
    return dataclass_replace(state, present_nodes=state.present_nodes | recent)


def _augment_deletion_observation(
    observation: ClusterObservation,
    row: CellRow,
    cluster: ClusterGateway,
    object_storage,
    volume_provider,
    now: datetime | None = None,
    memory: LoopMemory | None = None,
    storage: StorageConfig = DEFAULT_STORAGE,
) -> ClusterObservation:
    """D10's absence proofs. The K8s Namespace's disappearance already shows
    up as `namespace_exists` on the base observation; the PV, the Hetzner
    volume, the backup objects and the B2 key need their own checks.

    D10 amendment: PV absence is confirmed by listing every PersistentVolume
    (cluster-scoped; cellctl already holds `get`/`list` on them) and checking
    that none has a `claimRef` in this cell's namespace, in addition to (not
    instead of) the existing Hetzner-volume-by-id check.
    """

    namespace_absent_confirmed = not observation.namespace_exists
    namespace = namespace_name(row.cell_id)
    # Probe in deletion order: an unavailable later service must not block
    # an earlier cleanup action. Unreached proofs stay false (fail closed).
    if storage.local is not None:
        # move-cloud-cells-to-local-storage 2.5: with local storage configured,
        # the PVs claimed from the namespace, the cell's logical volume, its
        # snapshots and their clones must all be gone, unless they sit on a
        # node confirmed destroyed. The Hetzner check below still applies.
        no_pv_claims_namespace = namespace_absent_confirmed and cell_data_absent(
            _with_recent_absences(cluster.local_volume_state(storage.local), now or datetime.now(UTC),
                                  memory or LoopMemory()),
            namespace=namespace, volume_id=row.volume_id,
        )
    else:
        no_pv_claims_namespace = namespace_absent_confirmed and cluster.pv_absent_for_namespace(namespace)
    # Hetzner is asked only once the namespace and every PV claim are gone,
    # since the volume cannot be released before that.
    hetzner_volume_absent = row.volume_id is None or (
        namespace_absent_confirmed
        and no_pv_claims_namespace
        and _hetzner_volume_absent(row.volume_id, volume_provider, now or datetime.now(UTC), memory or LoopMemory())
    )
    pv_absent_confirmed = no_pv_claims_namespace and hetzner_volume_absent

    prefix = f"cells/{row.cell_id}/"
    backup_objects_absent_confirmed = pv_absent_confirmed and len(object_storage.list_object_versions(prefix)) == 0

    b2_key_absent_confirmed = backup_objects_absent_confirmed and (
        row.b2_key_id is None or object_storage.key_absent(row.b2_key_id)
    )

    return dataclass_replace(
        observation,
        namespace_absent_confirmed=namespace_absent_confirmed,
        pv_absent_confirmed=pv_absent_confirmed,
        backup_objects_absent_confirmed=backup_objects_absent_confirmed,
        b2_key_absent_confirmed=b2_key_absent_confirmed,
    )


async def _resolve_secret_material(
    connection: asyncpg.Connection, row: CellRow, secrets_config: SecretsConfig
) -> tuple[dict[str, object], int]:
    """Ensures the write-once wrapped keys exist, then returns the plaintext
    material this pass's Secret render needs, and the backup key version in
    use. Never persists plaintext."""

    bearer_current = derive_cell_bearer(secrets_config.cell_token_key_current, row.cell_id)
    bearer_previous = (
        derive_cell_bearer(secrets_config.cell_token_key_previous, row.cell_id)
        if secrets_config.cell_token_key_previous
        else None
    )

    backup_key_wrapped = row.backup_key_wrapped
    backup_key_version = row.backup_key_version
    if backup_key_wrapped is None:
        from .secrets import generate_backup_data_key

        candidate = generate_backup_data_key()
        version = secrets_config.backup_master_key_current_version
        wrapped = wrap_secret(
            secrets_config.backup_master_keys[version],
            candidate,
            cell_id=row.cell_id,
            column="backup_key_wrapped",
            key_version=version,
        )
        # H5: written as one group so a crash between the two columns can
        # never leave a half-written row that wedges every later pass.
        won = await db.try_write_once_group(
            connection,
            row.cell_id,
            {"backup_key_wrapped": wrapped, "backup_key_version": version},
            first_column="backup_key_wrapped",
        )
        if won:
            backup_key_wrapped = wrapped
            backup_key_version = version
        else:
            refreshed = await db.select_all_rows(connection)
            current = next(r for r in refreshed if r.cell_id == row.cell_id)
            backup_key_wrapped = current.backup_key_wrapped
            backup_key_version = current.backup_key_version

    backup_password_plaintext = unwrap_secret(
        secrets_config.backup_master_keys[backup_key_version],
        backup_key_wrapped,
        cell_id=row.cell_id,
        column="backup_key_wrapped",
        key_version=backup_key_version,
    ).hex()

    material = {
        "bearer_current": bearer_current,
        "bearer_previous": bearer_previous,
        "backup_password": backup_password_plaintext,
    }
    return material, backup_key_version


async def _resolve_object_storage_key(
    connection: asyncpg.Connection,
    row: CellRow,
    secrets_config: SecretsConfig,
    object_storage,
    *,
    memory: LoopMemory | None = None,
    now: datetime | None = None,
    replace_allowed: bool = True,
) -> tuple[str, str, int]:
    """D7: create the per-cell B2 key once, deleting the loser on a race.
    Outside a hold, a stored key that B2 lists without a capability the
    backup needs is replaced. Returns the key id, its secret and the
    wrapping key version."""

    if row.b2_key_id is not None and row.b2_key_wrapped is not None:
        memory = memory if memory is not None else LoopMemory()
        now = now or datetime.now(UTC)
        if replace_allowed and _object_storage_key_lacks_capability(row, object_storage, memory, now):
            replaced = await _replace_object_storage_key(connection, row, secrets_config, object_storage)
            if replaced is not None:
                return replaced
        secret = unwrap_secret(
            secrets_config.backup_master_keys[row.b2_key_version],
            row.b2_key_wrapped,
            cell_id=row.cell_id,
            column="b2_key_wrapped",
            key_version=row.b2_key_version,
        ).decode("utf-8")
        return row.b2_key_id, secret, row.b2_key_version

    created = object_storage.create_prefix_key(row.cell_id)
    version = secrets_config.backup_master_key_current_version
    wrapped = wrap_secret(
        secrets_config.backup_master_keys[version],
        created.key_secret.encode("utf-8"),
        cell_id=row.cell_id,
        column="b2_key_wrapped",
        key_version=version,
    )
    # H5: id, wrapped secret and version all written together.
    won = await db.try_write_once_group(
        connection,
        row.cell_id,
        {"b2_key_id": created.key_id, "b2_key_wrapped": wrapped, "b2_key_version": version},
        first_column="b2_key_id",
    )
    if not won:
        object_storage.delete_key(created.key_id)
        refreshed = await db.select_all_rows(connection)
        current = next(r for r in refreshed if r.cell_id == row.cell_id)
        secret = unwrap_secret(
            secrets_config.backup_master_keys[current.b2_key_version],
            current.b2_key_wrapped,
            cell_id=row.cell_id,
            column="b2_key_wrapped",
            key_version=current.b2_key_version,
        ).decode("utf-8")
        return current.b2_key_id, secret, current.b2_key_version

    return created.key_id, created.key_secret, version


def _object_storage_key_lacks_capability(row: CellRow, object_storage, memory: LoopMemory, now: datetime) -> bool:
    """True only when B2's key listing shows the stored key without a
    capability in CELL_KEY_CAPABILITIES. A key that is not listed, or a
    listing that fails, keeps the stored key: B2 is an external API that
    may lag, and a key that really is wrong still fails its backup
    visibly as BACKUP_FAILED. A verified key is not listed again; any
    other outcome is re-checked at most every OBJECT_STORAGE_KEY_CHECK_INTERVAL."""

    key_id = row.b2_key_id
    if key_id in memory.object_storage_keys_verified:
        return False
    checked_at = memory.object_storage_key_checked_at.get(key_id)
    if checked_at is not None and now - checked_at < OBJECT_STORAGE_KEY_CHECK_INTERVAL:
        return False
    memory.object_storage_key_checked_at[key_id] = now
    try:
        capabilities = object_storage.key_capabilities(key_id)
    except Exception as error:  # noqa: BLE001 -- any listing failure keeps the stored key
        logger.warning(
            "cellctl could not list object-storage key %s for cell %s; keeping it: %s",
            key_id,
            row.cell_id,
            _describe_error(error),
        )
        return False
    if capabilities is None:
        logger.warning("cellctl: object-storage key %s for cell %s is not in B2's key listing; keeping it", key_id, row.cell_id)
        return False
    if capabilities >= set(CELL_KEY_CAPABILITIES):
        memory.object_storage_keys_verified.add(key_id)
        return False
    return True


async def _replace_object_storage_key(
    connection: asyncpg.Connection,
    row: CellRow,
    secrets_config: SecretsConfig,
    object_storage,
) -> tuple[str, str, int] | None:
    """Swap a fresh key in place of the stored one, then delete the old one.
    On a lost swap the fresh key is deleted and the stored one used. If B2
    cannot create the fresh key, returns None and the caller keeps the
    stored one, so a B2 outage never blocks an apply.

    Residual: a crash between the swap and deleting the old key, or a
    failed delete, leaves the old key in B2. Its secret is no longer stored
    anywhere once the cell's Secret is re-rendered."""

    try:
        created = object_storage.create_prefix_key(row.cell_id)
    except Exception as error:  # noqa: BLE001 -- keep the stored key; retried after the check interval
        logger.error(
            "cellctl could not create a replacement object-storage key for cell %s; keeping %s: %s",
            row.cell_id,
            row.b2_key_id,
            _describe_error(error),
        )
        return None
    version = secrets_config.backup_master_key_current_version
    wrapped = wrap_secret(
        secrets_config.backup_master_keys[version],
        created.key_secret.encode("utf-8"),
        cell_id=row.cell_id,
        column="b2_key_wrapped",
        key_version=version,
    )
    won = await db.try_replace_group(
        connection,
        row.cell_id,
        {"b2_key_id": created.key_id, "b2_key_wrapped": wrapped, "b2_key_version": version},
        match_column="b2_key_id",
        expected=row.b2_key_id,
    )
    if not won:
        refreshed = await db.select_all_rows(connection)
        current = next(r for r in refreshed if r.cell_id == row.cell_id)
        if created.key_id != current.b2_key_id:
            _delete_object_storage_key(object_storage, created.key_id, row.cell_id)
        secret = unwrap_secret(
            secrets_config.backup_master_keys[current.b2_key_version],
            current.b2_key_wrapped,
            cell_id=row.cell_id,
            column="b2_key_wrapped",
            key_version=current.b2_key_version,
        ).decode("utf-8")
        return current.b2_key_id, secret, current.b2_key_version

    logger.warning("cellctl replaced object-storage key %s for cell %s with %s", row.b2_key_id, row.cell_id, created.key_id)
    if created.key_id != row.b2_key_id:
        _delete_object_storage_key(object_storage, row.b2_key_id, row.cell_id)
    return created.key_id, created.key_secret, version


def _delete_object_storage_key(object_storage, key_id: str, cell_id: str) -> None:
    try:
        object_storage.delete_key(key_id)
    except Exception as error:  # noqa: BLE001 -- the row no longer names this key; report it
        logger.error("cellctl could not delete object-storage key %s for cell %s: %s", key_id, cell_id, _describe_error(error))


def _compute_render_digest(
    row: CellRow, cluster_config: ClusterConfig, secrets_config: SecretsConfig, storage_class: str = STORAGE_CLASS
) -> str:
    """D4: a SHA-256 over the non-secret render inputs -- the renderer
    version, chart-level cell settings, the set of cell_token_key versions
    in play, and the row's storage and key versions. Never a secret value
    itself. storage_gib bumps no generation, so it must be here: it renders
    into the PVC and the quota, and it is part of the refusal park key."""

    resources, placement = cluster_config.workload_for_cell(row.cell_id)
    material = {
        "render_version": RENDER_VERSION,
        "resources": asdict(resources),
        "model_env": cluster_config.model_env or {},
        "job_egress_except": sorted(cluster_config.job_egress_except),
        "cell_token_key_version": secrets_config.cell_token_key_version,
        "cell_token_key_previous_version": secrets_config.cell_token_key_previous_version,
        "storage_gib": row.storage_gib,
        "backup_key_version": row.backup_key_version,
        "b2_key_version": row.b2_key_version,
    }
    if row.cell_id in cluster_config.dedicated_cell_ids:
        material["dedicated_node"] = True
    elif placement:
        material["placement"] = placement
    endpoint = cluster_config.artifact_broker_for_cell(row.cell_id)
    if endpoint:
        material["artifact_broker_url"] = endpoint
    engines = cluster_config.media_engines_for_cell(row.cell_id)
    if engines:
        # Only a selected cell's digest moves, so a canary restarts one cell.
        material["media_engines"] = engines
    if cluster_config.storage.is_local(storage_class):
        # Only a node-local cell's render depends on its class, so a cell on
        # a Hetzner volume keeps the digest it had before local storage.
        material["storage_class"] = storage_class
    blob = json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _claim_class(cluster_config: ClusterConfig, observation: ClusterObservation, *, relocating: bool = False) -> str:
    storage = cluster_config.storage
    if (relocating or observation.statefulset_relocation_volume) and storage.local is not None:
        # D4: a relocated cell's new claim is local, whatever the domain.
        return storage.local.class_name
    return storage.claim_class(observation.pvc_storage_class)


def _select_backup_candidates(
    rows: list[CellRow],
    observations: dict[str, ClusterObservation],
    now: datetime,
    config: ReconcileConfig,
    statefulset_blocked: frozenset[str] = frozenset(),
    *,
    unobserved: list[CellRow] | tuple[CellRow, ...] = (),
    storage: StorageConfig = DEFAULT_STORAGE,
) -> set[str]:
    """D8: at most `backup_concurrency` backup holds run at once. Due cells
    go oldest `last_backup_at` first, never-backed-up cells first of all.
    D4: a refused row is still backed up, unless its own StatefulSet was
    refused and that refusal's backoff has not passed. A backup hold whose
    own StatefulSet is refused cannot exit until it applies, so it does not
    occupy a slot the rest of the fleet needs.

    move-cloud-cells-to-local-storage D3: a cell on local storage is backed
    up hourly instead, with up to the configured number at once on each
    node; a cell on any other class keeps the nightly window and its slot."""

    def local(row: CellRow) -> bool:
        return storage.is_local(observations[row.cell_id].pv_storage_class)

    def eligible(row: CellRow) -> bool:
        return (
            row.observed_state in ("running", "read_only")
            and row.cell_id not in statefulset_blocked
            and _active_hold(row, observations[row.cell_id]) is None
        )

    def oldest_first(row: CellRow) -> tuple:
        return (row.last_backup_at is not None, row.last_backup_at or datetime.min.replace(tzinfo=UTC), row.cell_id)

    chosen: set[str] = set()
    if storage.local is not None:
        running: dict[str | None, int] = {}
        for row in rows:
            if _active_hold(row, observations[row.cell_id]) == SNAPSHOT_BACKUP:
                node = observations[row.cell_id].pv_node
                running[node] = running.get(node, 0) + 1
        for row in sorted((r for r in rows if local(r) and eligible(r)), key=oldest_first):
            node = observations[row.cell_id].pv_node
            if hourly_backup_due(row, observations[row.cell_id], now, config) and (
                running.get(node, 0) < storage.local.backup_concurrency_per_node
            ):
                running[node] = running.get(node, 0) + 1
                chosen.add(row.cell_id)
        rows = [row for row in rows if not local(row)]

    active = sum(
        1
        for row in rows
        if _active_hold(row, observations[row.cell_id]) == "backup" and row.cell_id not in statefulset_blocked
    )
    # A row that could not be observed this pass counts by its hold column.
    active += sum(1 for row in unobserved if row.hold_kind == "backup")
    slots = config.backup_concurrency - active
    if slots <= 0:
        return chosen
    due = [row for row in rows if eligible(row) and nightly_backup_due(row, observations[row.cell_id], now, config)]
    due.sort(key=lambda row: (row.last_backup_at is not None, row.last_backup_at or datetime.min.replace(tzinfo=UTC)))
    return chosen | {row.cell_id for row in due[:slots]}


def _select_relocation_candidates(
    rows: list[CellRow], observations: dict[str, ClusterObservation], storage: StorageConfig, config: ReconcileConfig
) -> set[str]:
    """D4: the relocations to start this pass. The owner's cell relocates
    alone, and the rest wait until its restore has ended; then up to
    `relocation_concurrency` run at once, by rollout priority. A cell with no
    backup to restore from is never a candidate. A relocation whose restore
    failed no longer counts as in flight: it waits for the operator, and the
    rest of the fleet does not wait with it."""

    if not rows:
        return set()

    def in_flight(row: CellRow) -> bool:
        observation = observations[row.cell_id]
        return (
            _active_hold(row, observation) == "restore"
            and observation.statefulset_relocation_volume is not None
            and row.last_error_code not in (RESTORE_FAILED, RELOCATION_REFUSED)
        )

    due = sorted(
        (row for row in rows if relocation_volume(row, observations[row.cell_id], storage) and can_relocate(row)),
        key=lambda row: (row.rollout_priority, row.cell_id),
    )
    owner = owner_cell(rows)
    if owner in due:
        return {owner.cell_id}
    if in_flight(owner):
        return set()
    room = config.relocation_concurrency - sum(1 for row in rows if in_flight(row))
    return {row.cell_id for row in due[: max(0, room)]}


def _select_render_digest_candidate(
    rows: list[CellRow],
    observations: dict[str, ClusterObservation],
    digests: dict[str, str],
    now: datetime,
    config: ReconcileConfig,
    parked: frozenset[str] = frozenset(),
    statefulset_blocked: frozenset[str] = frozenset(),
) -> str | None:
    """D4: a row dirty only because its render digest changed is re-applied
    one cell at a time. The next one waits only while a cell is in flight:
    it carries the current digest, is not Ready on its update revision, and
    was re-applied less than `render_digest_in_flight` ago. A cell that is
    not Ready for any other reason never blocks the fleet, and a row whose
    StatefulSet was refused for its current key is never a candidate."""

    def serving(row: CellRow) -> bool:
        return row.desired_state in ("running", "read_only") and _active_hold(row, observations[row.cell_id]) is None

    def in_flight(row: CellRow) -> bool:
        observation = observations[row.cell_id]
        applied_at = observation.statefulset_render_digest_applied_at
        return (
            observation.statefulset_render_digest == digests[row.cell_id]
            and not observation.pod_ready
            and applied_at is not None
            and now - applied_at < config.render_digest_in_flight
        )

    if any(serving(row) and in_flight(row) for row in rows):
        return None
    candidates = sorted(
        (
            row
            for row in rows
            if serving(row)
            and not row.is_dirty(refusal_parked=row.cell_id in parked)
            and row.cell_id not in statefulset_blocked
            and observations[row.cell_id].statefulset_render_digest != digests[row.cell_id]
        ),
        key=lambda row: row.cell_id,
    )
    return candidates[0].cell_id if candidates else None


def _log_orphan_namespaces(cluster: ClusterGateway, rows: list[CellRow], now: datetime, memory: LoopMemory) -> None:
    """D4: a namespace labelled exomem.io/cloud-cell with no row is logged by
    cell id, never deleted, and listed at most every ORPHAN_SCAN_INTERVAL."""

    if memory.orphan_scan_at is not None and now - memory.orphan_scan_at < ORPHAN_SCAN_INTERVAL:
        return
    memory.orphan_scan_at = now
    try:
        labelled = cluster.list_cell_namespaces()
    except Exception as error:  # noqa: BLE001 - a failed listing must not fail the pass
        logger.error("cellctl: listing cell namespaces for the orphan check failed: %s", _describe_error(error))
        return
    known = {row.cell_id for row in rows}
    for namespace, cell_id in sorted(labelled.items()):
        if cell_id not in known:
            logger.warning("cellctl: orphan namespace %s is labelled for cell %s, which has no row; left in place", namespace, cell_id)


async def reconcile_once(
    connection: asyncpg.Connection,
    cluster: ClusterGateway,
    object_storage,
    volume_provider,
    secrets_config: SecretsConfig,
    cluster_config: ClusterConfig,
    *,
    config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG,
    now: datetime | None = None,
    memory: LoopMemory | None = None,
) -> bool:
    """One pass. Returns whether the next one is due at the fast cadence:
    True while any cell is in transition or this pass observed a change,
    False once the whole fleet has settled."""

    now = now or datetime.now(UTC)
    memory = memory if memory is not None else LoopMemory()

    # D4 self-check: without cellctl's own admission confinement in place,
    # its ClusterRole is close to cluster-admin. Do nothing this pass rather
    # than act unconfined.
    checks: list[tuple[str, str, dict]] = [
        (cluster_config.admission_policy_name, cluster_config.admission_binding_name, {}),
        (cluster_config.isolation_policy_name, cluster_config.isolation_binding_name,
         {"param_name": ISOLATION_PARAM_NAME}),
    ]
    if cluster_config.storage.local is not None:
        checks.append((RETAINED_DELETE_POLICY_NAME, RETAINED_DELETE_POLICY_NAME, {"param_selects_all": True}))
    for policy_name, binding_name, param in checks:
        if not cluster.admission_policy_present(policy_name, binding_name, **param):
            logger.error(
                "cellctl admission policy/binding missing or not a Deny binding of that policy (%s/%s); skipping this pass",
                policy_name,
                binding_name,
            )
            return True

    if cluster_config.shared_worker and cluster_config.shared_worker.mode == "selected":
        # Close the old domain before inventory/workloads. An earlier redemption
        # must commit before this barrier, so it is included in the inventory.
        await db.write_capacity_snapshot(connection, capacities={}, observed_at=now)
    rows = await db.select_all_rows(connection)
    committed = frozenset(row.cell_id for row in rows if not (
        row.desired_state == "deleted" and row.observed_state == "deleted"
        and row.observed_generation == row.generation))
    # D6: every non-deleted cell's size, observed this pass or not. A grown
    # cell's size is its grown one (D10).
    committed_sizes = {row.cell_id: row.size_gib for row in rows if row.cell_id in committed}
    rollout = await db.read_rollout(connection)
    cell_image = await db.read_cell_image(connection)
    _log_orphan_namespaces(cluster, rows, now, memory)
    # D4 API budget: only cleanup observed at the current generation retires a row.
    rows = [row for row in rows if row.cell_id in committed]

    # H4: one row's observe() must not take every other row down with it.
    # D4 API budget: one list per kind for the whole fleet, not reads per cell.
    observations: dict[str, ClusterObservation] = {}
    try:
        observed = cluster.observe_cells({row.cell_id: namespace_name(row.cell_id) for row in rows})
    except Exception as error:  # noqa: BLE001 - every row goes unobserved this pass
        logger.error("cellctl could not observe the fleet: %s", _describe_error(error))
        observed = {}
    for row in rows:
        result = observed.get(row.cell_id)
        if isinstance(result, ClusterObservation):
            observations[row.cell_id] = result
        elif result is not None:
            logger.error("cellctl observe failed for cell %s: %s", row.cell_id, _describe_error(result))
    # A non-deleted row that could not be observed may be the owner's canary
    # or hold an upgrade or restore. Deciding a rollout without it could pick
    # a tenant as canary or start a second upgrade, so this pass starts no
    # upgrade or digest re-apply. Backups go on: its row's own backup hold
    # still counts as an occupied slot.
    unobserved_rows = [row for row in rows if row.cell_id not in observations and row.desired_state != "deleted"]
    observations = _with_lost_nodes(observations, memory, now)
    await _backup_age_alert(cluster, cluster_config, rows, observations, now, memory)
    await _growth_alert(cluster, cluster_config, rows, observations, now, memory)
    storage_room = _storage_room(cluster, cluster_config.storage, observations, committed_sizes)
    fleet_unobserved = bool(unobserved_rows)
    rows = [row for row in rows if row.cell_id in observations]

    non_deleted_rows = [row for row in rows if row.desired_state != "deleted"]
    # H1/H9: an upgrade or a restore hold blocks new candidates; a deleted
    # row never counts, whatever its stale annotations or row state say.
    any_upgrading = any(_active_hold(row, observations[row.cell_id]) == "upgrade" for row in non_deleted_rows)
    any_restoring = any(
        _restore_blocks_upgrades(row, observations[row.cell_id], rollout, now, config) for row in non_deleted_rows
    )
    render_digests = {
        row.cell_id: _compute_render_digest(
            row, cluster_config, secrets_config, _claim_class(cluster_config, observations[row.cell_id])
        )
        for row in rows
    }

    # D4 refusal parking. A row is refused while its last refusal's key is
    # still its current (generation, render digest, image to render), and
    # parked until that refusal's backoff passes.
    memory.refusals = {cell_id: record for cell_id, record in memory.refusals.items() if cell_id in observations}
    refusal_records = {
        row.cell_id: record
        for row in rows
        if (record := memory.refusals.get(row.cell_id)) is not None
        and record.key
        == (
            row.generation,
            render_digests[row.cell_id],
            _image_to_render(row, observations[row.cell_id], rollout, cell_image),
        )
    }
    refused = frozenset(refusal_records)
    parked = frozenset(cell_id for cell_id, record in refusal_records.items() if now < record.retry_at)
    statefulset_blocked = frozenset(cell_id for cell_id in parked if refusal_records[cell_id].statefulset_blocked)

    upgrade_candidate: str | None = None
    # Like an upgrade, no relocation starts while a row is unobserved: it may
    # be the owner's, which must relocate first. Started ones continue.
    relocation_candidates = (
        set() if fleet_unobserved
        else _select_relocation_candidates(non_deleted_rows, observations, cluster_config.storage, config)
    )
    backup_candidates: set[str] = set()
    render_digest_candidate: str | None = None
    backup_candidates = _select_backup_candidates(
        non_deleted_rows, observations, now, config, statefulset_blocked, unobserved=unobserved_rows,
        storage=cluster_config.storage,
    )
    if fleet_unobserved:
        logger.error(
            "cellctl: %d cell(s) could not be observed; no upgrade, re-render or relocation starts this pass",
            len(unobserved_rows),
        )
    else:
        upgrade_candidate = select_upgrade_candidate(
            non_deleted_rows,
            observations,
            rollout,
            cell_image,
            now=now,
            any_cell_already_upgrading=any_upgrading or any_restoring,
            refused=refused,
        )
        await _publish_parked_canary(connection, rollout, non_deleted_rows, observations, cell_image, refused)
        render_digest_candidate = _select_render_digest_candidate(
            non_deleted_rows, observations, render_digests, now, config, parked, statefulset_blocked
        )

    # D4 amendment: a hold, a dirty row or a maintenance start keeps the fast
    # cadence. A settled failure (identity conflict, init deadline) does not:
    # it changes only when its pod or its row does, and one broken tenant must
    # not hold the whole fleet at the fast cadence.
    fast = (
        fleet_unobserved
        or upgrade_candidate is not None
        or bool(relocation_candidates)
        or bool(backup_candidates)
        or render_digest_candidate is not None
        or any(_in_transition(row, observations[row.cell_id], row.cell_id in parked) for row in rows)
    )
    for row in rows:
        try:
            fast |= await _reconcile_row(
                connection,
                cluster,
                object_storage,
                volume_provider,
                secrets_config,
                cluster_config,
                config,
                now,
                row,
                observations[row.cell_id],
                rollout,
                cell_image,
                render_digests[row.cell_id],
                start_upgrade=row.cell_id == upgrade_candidate,
                start_backup=row.cell_id in backup_candidates,
                start_relocation=row.cell_id in relocation_candidates,
                is_render_digest_candidate=row.cell_id == render_digest_candidate,
                refusal_parked=row.cell_id in parked,
                memory=memory,
                storage_room=storage_room,
            )
        except Exception as error:  # noqa: BLE001 - one bad row must not take the pass down
            if isinstance(error, _ROW_SESSION_ERRORS) or connection.is_closed():
                raise
            logger.error("cellctl reconcile failed for cell %s: %s", row.cell_id, _describe_error(error))
            fast = True

    # D4: capacity is published at the end of every pass, whatever the rows
    # did, from Kubernetes CSINode/VolumeAttachment state -- never a Hetzner
    # server id or a hardcoded chart constant.
    # Capacity has its own boundary too: a failed capacity read only leaves
    # the previous `exomem_cloud_capacity` rows in place, and must never turn
    # a pass whose rows all succeeded into a failed one.
    try:
        kwargs = {"csi_driver": cluster_config.capacity.csi_driver}
        if cluster_config.shared_worker:
            kwargs["shared_policy"] = cluster_config.shared_worker
        observation = cluster.capacity_inputs(**kwargs)
        # D7: once local storage is the domain, only its pools publish slots.
        storage = cluster_config.storage
        local_slots = (
            compute_local_capacity(
                cluster.local_capacity_inputs(storage.local), local=storage.local, cell_sizes=committed_sizes
            )
            if storage.domain_is_local
            else None
        )
    except Exception as error:  # noqa: BLE001 - capacity must not take the pass down
        logger.error("cellctl: capacity read failed; keeping the last published capacity: %s", _describe_error(error))
        return True
    if cluster_config.shared_worker:
        capacities = compute_shared_capacity(observation, policy=cluster_config.shared_worker,
                                             config=cluster_config.capacity,
                                             committed=committed - frozenset(cluster_config.dedicated_cell_ids),
                                             storage_slots=local_slots)
        if cluster_config.shared_worker.mode == "selected":
            capacities = {node: NodeCapacity(0, value.attachments_used, value.limit_known)
                          for node, value in capacities.items()}
        elif not any(value.cell_slots for value in capacities.values()):
            logger.warning("cellctl: shared capacity unavailable: require one Ready, schedulable, unpressured "
                           "profile/CSI-compatible worker with complete resource and volume observations")
    elif local_slots is not None:
        capacities = local_slots
    else:
        capacities = {}
        for node in sorted(set(observation.allocatable) | set(observation.attachments_used)):
            value = compute_node_capacity(
                allocatable=observation.allocatable.get(node), attachments_used=observation.attachments_used.get(node, 0),
                non_cell_attachments=observation.non_cell_attachments.get(node, 0), config=cluster_config.capacity)
            capacities[node] = (NodeCapacity(0, value.attachments_used, value.limit_known)
                                if node in observation.reserved_nodes else value)
            if not value.limit_known:
                logger.warning("cellctl: node %s has no known attachments limit; publishing 0 cell_slots", node)
    # D9: capacity is published on every pass, so admission's five-minute
    # freshness bound holds at the idle cadence too.
    await db.write_capacity_snapshot(connection, capacities=capacities, observed_at=now)
    return fast


def _storage_room(
    cluster: ClusterGateway,
    storage: StorageConfig,
    observations: dict[str, ClusterObservation],
    committed_sizes: dict[str, int],
) -> StorageRoom | None:
    """D10: the node pools, read only in a pass where an hourly backup has
    just reported its use, which is when a growth is planned. A failed read
    plans no growth: that backup's cell grows, if it must, after its next one."""

    local = storage.local
    if local is None or not any(
        observation.statefulset_hold_kind == SNAPSHOT_BACKUP
        and observation.statefulset_backup_outcome is None
        and observation.backup_job_used_bytes is not None
        for observation in observations.values()
    ):
        return None
    try:
        free_bytes = cluster.local_capacity_inputs(local).free_bytes
    except Exception as error:  # noqa: BLE001 - a growth waits; the backup still counts
        logger.error("cellctl: the node pools could not be read, so no cell grows this pass: %s", _describe_error(error))
        return None
    return StorageRoom(free_bytes=free_bytes, largest_cell_gib=max(committed_sizes.values(), default=0))


# A receiver that is down is asked again at most this often, not every pass.
ALERT_RETRY = timedelta(minutes=1)


async def _deliver_alert(
    cluster: ClusterGateway,
    cluster_config: ClusterConfig,
    memory: LoopMemory,
    alert: str,
    firing: bool,
    now: datetime,
) -> None:
    """Posts `alert`'s state when it differs from the one last delivered. An
    alert, never a gate: a failure is logged and retried a minute later, and
    changes nothing else the pass does. The POST runs on a worker thread, so a
    slow receiver never stalls the loop."""

    if cluster_config.alert_delivery_secret is None or memory.alert_delivered.get(alert) == firing:
        return
    retry_at = memory.alert_retry_at.get(alert)
    if retry_at is not None and now < retry_at:
        return
    try:
        webhook_url = cluster.read_secret_value(*cluster_config.alert_delivery_secret)
        await asyncio.to_thread(alerts.deliver, webhook_url, alert=alert, active=firing, observed_at=now)
    except Exception as error:  # noqa: BLE001 - an alert must not take the pass down
        memory.alert_retry_at[alert] = now + ALERT_RETRY
        logger.error("cellctl: %s alert delivery failed; retrying in a minute: %s", alert, _describe_error(error))
        return
    memory.alert_retry_at.pop(alert, None)
    memory.alert_delivered[alert] = firing


async def _growth_alert(
    cluster: ClusterGateway,
    cluster_config: ClusterConfig,
    rows: list[CellRow],
    observations: dict[str, ClusterObservation],
    now: datetime,
    memory: LoopMemory,
) -> None:
    """D10: one platform alert while a serving local cell past 80% use cannot
    grow: its node has no room, or it is at its cap. Nothing is sent while
    the answer is unknown (alerts.growth_blocked), nor where local storage is
    not configured: there a first "resolved" would email an operator about an
    alert that never fired."""

    if cluster_config.storage.local is None:
        return
    known = {row.cell_id for row in rows}
    memory.growth_verdicts = {cell: verdict for cell, verdict in memory.growth_verdicts.items() if cell in known}
    firing = alerts.growth_blocked(rows, observations, memory.growth_verdicts, storage=cluster_config.storage)
    if firing is not None:
        await _deliver_alert(cluster, cluster_config, memory, alerts.GROWTH_ALERT, firing, now)


async def _backup_age_alert(
    cluster: ClusterGateway,
    cluster_config: ClusterConfig,
    rows: list[CellRow],
    observations: dict[str, ClusterObservation],
    now: datetime,
    memory: LoopMemory,
) -> None:
    """Task 2.8: one platform alert while any serving cell's backup is older
    than its schedule allows."""

    stale = alerts.stale_backups(rows, observations, now, storage=cluster_config.storage)
    if stale != memory.stale_backups:
        if stale:
            logger.warning("cellctl: %d serving cell(s) have no backup within their schedule: %s",
                           len(stale), ", ".join(stale))
        else:
            logger.info("cellctl: every serving cell has a backup within its schedule")
        memory.stale_backups = stale
    await _deliver_alert(cluster, cluster_config, memory, alerts.BACKUP_ALERT, bool(stale), now)


def _in_transition(row: CellRow, observation: ClusterObservation, refusal_parked: bool) -> bool:
    if _active_hold(row, observation) is not None or row.hold_kind is not None:
        return True
    if row.observed_state == "failed" and row.generation == row.observed_generation:
        return False
    return row.is_dirty(refusal_parked=refusal_parked)


async def _write_observed(connection: asyncpg.Connection, row: CellRow, updates: dict[str, object], now: datetime) -> bool:
    """D4 amendment: observed_at is written with every observed change, and
    alone once the row's stored value is OBSERVED_AT_REFRESH old. Returns
    whether an observed column changed; a refresh alone is not a change and
    never makes the next pass fast."""

    if updates:
        await db.write_observed(connection, row.cell_id, {**updates, "observed_at": now})
        return True
    if row.observed_at is None or now - row.observed_at >= OBSERVED_AT_REFRESH:
        await db.write_observed(connection, row.cell_id, {"observed_at": now})
    return False


async def _publish_parked_canary(
    connection: asyncpg.Connection,
    rollout,
    rows: list[CellRow],
    observations: dict[str, ClusterObservation],
    cell_image: str | None,
    refused: frozenset[str],
) -> None:
    """D6: a rollout waiting on a refused owner cell says so on the rollout
    row, without pausing. Written before the rows run, and cleared only
    while it is still the error code, so it never overwrites a real pause."""

    canary = parked_canary(rows, observations, cell_image, refused)
    if canary is not None:
        # Only a real pause is never overwritten. A resumed rollout may still
        # carry the earlier error code or held_cell_id; neither hides this.
        if not rollout.paused and (rollout.error_code, rollout.held_cell_id) != (CANARY_PARKED, canary):
            logger.warning("cellctl: the rollout is waiting on canary cell %s, whose last apply was refused", canary)
            await db.write_rollout(
                connection,
                {"error_code": CANARY_PARKED, "held_cell_id": canary},
                only_if={"paused": False, "error_code": rollout.error_code, "held_cell_id": rollout.held_cell_id},
            )
    elif rollout.error_code == CANARY_PARKED:
        await db.write_rollout(
            connection,
            {"error_code": None, "held_cell_id": None},
            only_if={"paused": rollout.paused, "error_code": CANARY_PARKED, "held_cell_id": rollout.held_cell_id},
        )


async def _reconcile_row(
    connection: asyncpg.Connection,
    cluster: ClusterGateway,
    object_storage,
    volume_provider,
    secrets_config: SecretsConfig,
    cluster_config: ClusterConfig,
    config: ReconcileConfig,
    now: datetime,
    row: CellRow,
    observation: ClusterObservation,
    rollout,
    cell_image: str | None,
    render_digest: str,
    *,
    start_upgrade: bool,
    start_backup: bool,
    is_render_digest_candidate: bool,
    refusal_parked: bool,
    memory: LoopMemory,
    start_relocation: bool = False,
    storage_room: StorageRoom | None = None,
) -> bool:
    """Returns whether the row's observation changed (and was written)."""

    already_served = row.observed_state in ("running", "read_only")
    backup_due = already_served and start_backup
    nothing_to_start = (
        _active_hold(row, observation) is None
        and not backup_due
        and not start_upgrade
        and not is_render_digest_candidate
        # D4: a converged cell whose volume is lost still has its relocation
        # to start.
        and relocation_volume(row, observation, cluster_config.storage) is None
    )
    # D4: ready is an observation, never a memory. A served, converged row
    # whose only change is its pod's readiness is observed, not re-applied:
    # ready follows the pod on the update revision both ways, observed_state
    # stays served (so it is still backed up), and nothing is written but
    # ready, and observed_at with it, when it changes.
    # A served cell is in place while its StatefulSet runs one replica over
    # its claim. Out of place is more than readiness: the node-loss runbook
    # stops a cell and retires its claim, and expects cellctl to start it
    # again, or to report its recorded volume missing, so such a row always
    # goes through decide().
    out_of_place = already_served and not (
        observation.statefulset_exists
        and observation.statefulset_replicas == 1
        and (observation.pvc_exists or observation.pvc_bound)
    )
    readiness_only = (
        nothing_to_start
        and already_served
        and not out_of_place
        and not refusal_parked
        and not dataclass_replace(row, ready=True).is_dirty()
        and observation.statefulset_image == row.observed_image
    )
    if readiness_only:
        changed = {"ready": observation.pod_ready} if row.ready != observation.pod_ready else {}
        return await _write_observed(connection, row, changed, now)
    # A parked refused row is not dirty, but it still goes through decide(),
    # which applies nothing while it is parked and observes it this pass.
    if (not row.is_dirty(refusal_parked=refusal_parked) and not refusal_parked and nothing_to_start
            and not out_of_place):
        # D4 amendment: an observation that finds nothing changed writes only
        # a due observed_at refresh.
        return await _write_observed(connection, row, {}, now)

    if row.desired_state == "deleted":
        observation = _augment_deletion_observation(
            observation, row, cluster, object_storage, volume_provider, now, memory, storage=cluster_config.storage
        )

    decision = decide(
        row,
        rollout,
        observation,
        now=now,
        cell_image=cell_image,
        start_upgrade=start_upgrade,
        start_backup=start_backup,
        render_digest=render_digest,
        config=config,
        refusal_parked=refusal_parked,
        storage=cluster_config.storage,
        start_relocation=start_relocation,
        storage_room=storage_room,
    )
    if decision.storage_growth is not None:
        _note_growth(memory, row, observation, decision)
    if decision.row_updates.get("last_error_code") == BACKUP_FAILED and observation.backup_job_failure_code:
        # D5: the failed Job's own code, value-free; the Job goes a pass later.
        logger.warning("cellctl: cell %s's backup failed: %s", row.cell_id, observation.backup_job_failure_code)

    # D6 step 4.1: a pause is written before the apply it accompanies, so a
    # crash between the two leaves the rollout paused and no other cell can
    # start the same image.
    pause_first = bool(decision.rollout_updates.get("paused"))
    if pause_first:
        await db.write_rollout(connection, decision.rollout_updates)

    if decision.delete_backup_job and observation.statefulset_hold_started_at is not None:
        # D4/D8: before the cell restarts. The Job's name derives from the
        # hold-started-at string it was rendered with (manifests.hold_job_name).
        cluster.delete_job(
            namespace_name(row.cell_id),
            hold_job_name(BACKUP_JOB_NAME, observation.statefulset_hold_started_at.isoformat()),
        )

    if decision.delete_snapshot_backup and observation.statefulset_hold_started_at is not None:
        # D3: whatever happens to the cell or its apply, the hold's clone and
        # snapshot go. Named from the hold-started string they were made with.
        started = observation.statefulset_hold_started_at.isoformat()
        cluster.delete_snapshot_backup(
            namespace_name(row.cell_id), hold_job_name(SNAPSHOT_NAME, started), hold_job_name(CLONE_CLAIM_NAME, started)
        )

    # D4 relocation: the old PV is set to Retain one pass, and its claim is
    # deleted only on a later pass that observes Retain (admission checks it
    # again). Neither depends on the StatefulSet's apply.
    if decision.retain_volume or decision.delete_claim:
        try:
            if decision.retain_volume:
                cluster.retain_volume(decision.retain_volume)
            if decision.delete_claim:
                cluster.delete_claim(namespace_name(row.cell_id))
        except ApiException as error:
            if not _is_refusal(error):
                raise
            logger.error("cellctl: relocation step refused for cell %s: status=%s reason=%s",
                         row.cell_id, error.status, error.reason)
            # The claim is still there, so the row keeps its volume identity.
            kept = {key: value for key, value in decision.row_updates.items() if key not in ("volume_id", "node")}
            decision.row_updates = {**kept, "last_error_code": RELOCATION_REFUSED, "ready": False}
        else:
            if row.last_error_code == RELOCATION_REFUSED:
                decision.row_updates = {**decision.row_updates, "last_error_code": None}

    grown = decision.row_updates.get("grown_storage_gib")
    if grown is not None:
        # D10: recorded before the larger claim is applied. A crash after the
        # apply then never leaves the claim larger than the row, which would
        # make every later pass render a smaller claim that Kubernetes refuses.
        await db.write_observed(connection, row.cell_id, {"grown_storage_gib": grown})
        row = dataclass_replace(row, grown_storage_gib=grown)
        logger.info("cellctl: cell %s grows to %d GiB", row.cell_id, grown)

    refused: list[tuple[str, str]] = []
    applied_cleanly = False
    if decision.apply_manifests and decision.image:
        secret_material, backup_key_version = await _resolve_secret_material(connection, row, secrets_config)
        # A hold's Jobs read the key from the Secret, so it is only checked
        # and replaced outside a hold, including the pass that starts one.
        key_id, key_secret, b2_key_version = await _resolve_object_storage_key(
            connection,
            row,
            secrets_config,
            object_storage,
            memory=memory,
            now=now,
            replace_allowed=_active_hold(row, observation) is None,
        )
        # D4: the digest covers the row's key versions. A new cell's first
        # pass creates them, so the applied digest is computed from the
        # versions this pass resolved; hashing the row as read would change
        # the digest on the next pass and restart the pod for nothing.
        storage_class = _claim_class(cluster_config, observation, relocating=decision.relocation_volume is not None)
        render_digest = _compute_render_digest(
            dataclass_replace(row, backup_key_version=backup_key_version, b2_key_version=b2_key_version),
            cluster_config,
            secrets_config,
            storage_class,
        )
        # D6/D8: the backup-retry-after backoff must survive a hold ending,
        # so unless this decision explicitly changes it, the currently
        # observed value is carried forward rather than dropped.
        # D4: a digest-only re-apply records when it landed; every other
        # apply carries the observed time forward, since server-side apply
        # drops an annotation its manager stops sending.
        digest_only_reapply = (
            is_render_digest_candidate and decision.hold_kind is None and _active_hold(row, observation) is None
        )
        digest_applied_at = now if digest_only_reapply else observation.statefulset_render_digest_applied_at
        if decision.clear_backup_retry_after:
            retry_after, retry_minutes = None, None
        elif decision.backup_retry_after is not None:
            retry_after, retry_minutes = decision.backup_retry_after, decision.backup_retry_minutes
        else:
            retry_after = observation.statefulset_backup_retry_after
            retry_minutes = observation.statefulset_backup_retry_minutes
        resources, placement = cluster_config.workload_for_cell(row.cell_id)
        local_volume = cluster_config.storage.is_local(storage_class)
        spec = CellManifestSpec(
            cell_id=row.cell_id,
            image=decision.image,
            replicas=decision.replicas,
            read_only=decision.read_only,
            # D10: only a local claim grows. A cell back on its retained
            # Hetzner volume (a D7 rollback) renders storage_gib, that
            # volume's own size, so its claim still binds and never grows it.
            storage_gib=row.size_gib if local_volume else row.storage_gib,
            resources=resources,
            model_env=cluster_config.model_env or {},
            placement=placement,
            storage_class=storage_class,
            local_volume=local_volume,
            # D5: a backup this decision records counts at once, so the hold's
            # own exit writes the key.
            backed_up=row.last_backup_at is not None or "last_backup_at" in decision.row_updates,
            hold_kind=decision.hold_kind,
            hold_started_at=decision.hold_started_at.isoformat() if decision.hold_started_at else None,
            previous_image=decision.previous_image,
            pre_upgrade_snapshot=decision.pre_upgrade_snapshot,
            target_applied_at=decision.target_applied_at.isoformat() if decision.target_applied_at else None,
            restored_snapshot=decision.restored_snapshot,
            backup_outcome=decision.backup_outcome,
            relocation_volume=decision.relocation_volume,
            grow_storage_gib=decision.grow_storage_gib,
            backup_retry_after=retry_after.isoformat() if retry_after else None,
            backup_retry_minutes=retry_minutes,
            render_digest=render_digest,
            render_digest_applied_at=digest_applied_at.isoformat() if digest_applied_at else None,
            row_generation=row.generation,
            job_egress_except=cluster_config.job_egress_except,
            artifact_broker_url=cluster_config.artifact_broker_for_cell(row.cell_id),
            media_engines=cluster_config.media_engines_for_cell(row.cell_id),
            b2_key_id=key_id,
            b2_key_secret=key_secret,
            **secret_material,
        )

        # D6 step 3: the first apply of a target image inside an upgrade
        # attempt. decide() is pure and never sees an apply's own result, so
        # a 4xx refusal here (the image-pin policy, for example) is handled
        # right here rather than fed back through another decide() call.
        is_first_target_apply = (
            decision.hold_kind == "upgrade"
            and observation.statefulset_target_applied_at is None
            and decision.target_applied_at is not None
        )
        hold_active = observation.statefulset_exists and observation.statefulset_hold_kind is not None
        refused, statefulset_applied = _apply_cell_manifests(
            cluster,
            row.cell_id,
            render_cell_manifests(spec),
            hold_active=hold_active,
            statefulset_exists=observation.statefulset_exists,
        )
        if is_first_target_apply and ("StatefulSet", spec.statefulset_name) in refused:
            desired_replicas = 0 if row.desired_state == "stopped" else 1
            reject_read_only = row.desired_state == "read_only"
            reject_spec = dataclass_replace(
                spec,
                image=decision.previous_image,
                replicas=desired_replicas,
                read_only=reject_read_only,
                hold_kind=None,
                hold_started_at=None,
                previous_image=None,
                pre_upgrade_snapshot=None,
                target_applied_at=None,
            )
            reject_refused, reject_applied = _apply_cell_manifests(
                cluster, row.cell_id, render_cell_manifests(reject_spec), hold_active=True, statefulset_exists=True
            )
            if reject_refused or not reject_applied:
                reject_key = (row.generation, render_digest, reject_spec.image)
                _record_refusal(memory, row.cell_id, reject_key, now, statefulset_blocked=not reject_applied)
            else:
                memory.refusals.pop(row.cell_id, None)
            # The pause stands whatever the rollback's own apply did: the
            # target is known-bad. The hold columns mirror the annotations,
            # so they clear only once the rollback's StatefulSet landed.
            rejected: dict[str, object] = {"last_error_code": TARGET_REJECTED, "observed_at": now}
            if reject_applied:
                rejected.update({"hold_kind": None, "hold_started_at": None})
            await db.write_observed(connection, row.cell_id, rejected)
            await db.write_rollout(
                connection,
                {"paused": True, "error_code": TARGET_REJECTED, "held_cell_id": row.cell_id},
            )
            return True
        # D4: parked in memory on (generation, applied digest, image), and
        # retried on the backoff; success resets it.
        refusal_key = (row.generation, render_digest, spec.image)
        if not statefulset_applied:
            # Nothing this decision describes landed, so only the refusal is
            # recorded. observed_generation is not written, since nothing
            # converged. A refused hold start is retried on the backoff too.
            _record_refusal(memory, row.cell_id, refusal_key, now, statefulset_blocked=True)
            changed = {"last_error_code": MANIFEST_IMMUTABLE} if row.last_error_code != MANIFEST_IMMUTABLE else {}
            return await _write_observed(connection, row, changed, now)
        park = memory.refusals.get(row.cell_id)
        jobs_parked = park is not None and park.job_blocked and park.key == refusal_key and now < park.retry_at
        job_refused: list[tuple[str, str]] = []
        steps = decision.run_backup_job or decision.run_restore_job_snapshot or decision.create_snapshot or decision.create_clone
        local = cluster_config.storage.local
        if jobs_parked and steps:
            # A Job refused for this key waits out the backoff.
            job_refused.append(("Job", "parked"))
        else:
            if decision.create_snapshot and local is not None:
                _run_cell_job(cluster, row.cell_id, render_volume_snapshot(spec, snapshot_class=local.snapshot_class),
                              job_refused)
            if decision.create_clone and local is not None:
                _run_cell_job(cluster, row.cell_id, render_clone_claim(spec, clone_class=local.clone_class), job_refused)
            if decision.run_backup_job:
                _run_cell_job(
                    cluster,
                    row.cell_id,
                    render_backup_job(
                        spec,
                        bucket_name=cluster_config.object_storage_bucket,
                        endpoint=cluster_config.object_storage_endpoint,
                        **_backup_source(decision, spec),
                    ),
                    job_refused,
                )
            if decision.run_restore_job_snapshot:
                _run_cell_job(
                    cluster,
                    row.cell_id,
                    render_restore_job(
                        spec,
                        bucket_name=cluster_config.object_storage_bucket,
                        endpoint=cluster_config.object_storage_endpoint,
                        snapshot_id=decision.run_restore_job_snapshot,
                    ),
                    job_refused,
                )
        refused = refused + job_refused
        if refused:
            _record_refusal(
                memory, row.cell_id, refusal_key, now, statefulset_blocked=False, job_blocked=bool(job_refused)
            )
        else:
            memory.refusals.pop(row.cell_id, None)
        applied_cleanly = not refused

    if decision.delete_namespace:
        cluster.delete_namespace(namespace_name(row.cell_id))
    if decision.delete_backup_objects:
        _delete_all_backup_objects(object_storage, row)
    if decision.delete_b2_key and row.b2_key_id:
        object_storage.delete_key(row.b2_key_id)

    row_updates: dict[str, object] = dict(decision.row_updates)
    if refused:
        # D4: the StatefulSet landed but another object was refused. The
        # refusal is what the row records, and nothing converged.
        row_updates["last_error_code"] = MANIFEST_IMMUTABLE
        row_updates.pop("observed_generation", None)
    elif applied_cleanly and row.last_error_code == MANIFEST_IMMUTABLE and "last_error_code" not in row_updates:
        row_updates["last_error_code"] = None
    # Bounded writes: a column whose value is unchanged is not written again.
    row_updates = {column: value for column, value in row_updates.items() if getattr(row, column) != value}
    changed = await _write_observed(connection, row, row_updates, now)
    if decision.rollout_updates and not pause_first:
        await db.write_rollout(connection, decision.rollout_updates)
    return changed or decision.apply_manifests


def _note_growth(memory: LoopMemory, row: CellRow, observation: ClusterObservation, decision) -> None:
    """D10: keeps the verdict for the growth alert, and logs the measurement,
    sizes only, never content."""

    memory.growth_verdicts[row.cell_id] = decision.storage_growth
    used, total = observation.backup_job_used_bytes, observation.backup_job_total_bytes
    level = logging.INFO if decision.storage_growth == GROWTH_NOT_NEEDED else logging.WARNING
    logger.log(level, "cellctl: cell %s's volume is %d of %d bytes used at %d GiB: %s%s", row.cell_id, used, total,
               row.size_gib, decision.storage_growth,
               f" to {decision.grow_storage_gib} GiB" if decision.grow_storage_gib is not None else "")


def _backup_source(decision, spec: CellManifestSpec) -> dict[str, object]:
    """D3: the hourly hold reads its clone and prunes only on its first run
    after 02:00 UTC; the stopped pre-upgrade backup of a local cell never
    prunes. Every backup of a Hetzner cell keeps its claim and the nightly
    prune, as before local storage."""

    if decision.hold_kind == SNAPSHOT_BACKUP:
        return {
            "claim_name": hold_job_name(CLONE_CLAIM_NAME, spec.hold_started_at),
            "retention": HOURLY_RETENTION_ARGS if decision.backup_prune else None,
        }
    if spec.local_volume and decision.hold_kind == "upgrade":
        return {"retention": None}
    return {"retention": RETENTION_ARGS}


def _delete_all_backup_objects(object_storage, row: CellRow) -> None:
    prefix = f"cells/{row.cell_id}/"
    for version in object_storage.list_object_versions(prefix):
        object_storage.delete_object_version(version)


def _touch_heartbeat(path: str) -> None:
    try:
        Path(path).touch()
    except OSError as error:
        logger.error("cellctl failed to touch heartbeat file %s: %s", path, _describe_error(error))


async def _sleep_or_stop(stop: asyncio.Event | None, seconds: float) -> None:
    if stop is None:
        await asyncio.sleep(seconds)
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def run_loop(
    dsn: str,
    cluster: ClusterGateway,
    object_storage,
    volume_provider,
    secrets_config: SecretsConfig,
    cluster_config: ClusterConfig,
    *,
    config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG,
    stop: asyncio.Event | None = None,
    heartbeat_path: str = DEFAULT_HEARTBEAT_PATH,
) -> None:
    """D4: poll, plus LISTEN on a direct session. The poll comes after
    FAST_POLL_INTERVAL_SECONDS while a pass reports a transition and after
    IDLE_POLL_INTERVAL_SECONDS once the fleet has settled (D4 amendment).

    The loop survives its dependencies: a lost database session is reopened
    with capped exponential backoff (1s up to 30s), re-taking the single-
    writer advisory lock and re-issuing LISTEN; every iteration, including
    ones spent reconnecting, touches a heartbeat file so a wedged loop (not
    a slow dependency) is what trips liveness.
    """

    connection: asyncpg.Connection | None = None
    woken = asyncio.Event()
    backoff = RECONNECT_BACKOFF_INITIAL_SECONDS
    memory = LoopMemory()
    try:
        while stop is None or not stop.is_set():
            _touch_heartbeat(heartbeat_path)

            if connection is None:
                try:
                    connection = await db.connect(dsn)
                    woken = asyncio.Event()
                    await connection.add_listener(db.NOTIFY_CHANNEL, lambda *_args, w=woken: w.set())
                    # A session the server closes wakes the loop too, so an
                    # idle wait does not delay the reconnect (D4: notifications
                    # sent while it is down are lost; the full pass after it
                    # is what picks their rows up).
                    connection.add_termination_listener(lambda *_args, w=woken: w.set())
                    if not await db.try_advisory_lock(connection):
                        raise RuntimeError("cellctl advisory lock is held by another writer")
                    backoff = RECONNECT_BACKOFF_INITIAL_SECONDS
                except Exception as error:  # noqa: BLE001
                    logger.error("cellctl failed to (re)connect to the database: %s", _describe_error(error))
                    if connection is not None:
                        with contextlib.suppress(Exception):
                            await connection.close()
                    connection = None
                    _touch_heartbeat(heartbeat_path)
                    await _sleep_or_stop(stop, backoff)
                    backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECONDS)
                    continue

            # D4: clear a pending notification before the pass, never after
            # it, so one that arrives during the pass triggers the next one.
            woken.clear()
            interval = FAST_POLL_INTERVAL_SECONDS
            try:
                fast = await reconcile_once(
                    connection,
                    cluster,
                    object_storage,
                    volume_provider,
                    secrets_config,
                    cluster_config,
                    config=config,
                    memory=memory,
                )
                if not fast:
                    interval = IDLE_POLL_INTERVAL_SECONDS
            except (asyncpg.InterfaceError, asyncpg.PostgresConnectionError, OSError) as error:
                # D4: a dropped session is reopened, not retried on the same
                # (now-dead) connection forever.
                logger.error("cellctl lost its database connection; reconnecting: %s", _describe_error(error))
                with contextlib.suppress(Exception):
                    await connection.close()
                connection = None
                continue
            except Exception as error:  # noqa: BLE001 - one bad pass must not kill the loop
                logger.error("cellctl reconcile pass failed: %s", _describe_error(error))

            try:
                await asyncio.wait_for(woken.wait(), timeout=interval)
            except TimeoutError:
                pass
    finally:
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()
