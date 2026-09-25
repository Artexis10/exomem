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
import traceback
from dataclasses import asdict, dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
from kubernetes.client.rest import ApiException

from . import db
from .capacity import CapacityConfig, compute_node_capacity
from .decide import (
    DEFAULT_RECONCILE_CONFIG,
    ReconcileConfig,
    decide,
    nightly_backup_due,
)
from .manifests import (
    BACKUP_JOB_NAME,
    RENDER_VERSION,
    CellManifestSpec,
    ResourceSettings,
    hold_job_name,
    namespace_name,
    render_backup_job,
    render_cell_manifests,
    render_restore_job,
)
from .rollout import current_image, initial_image, parked_canary, select_upgrade_candidate
from .secrets import derive_cell_bearer, unwrap_secret, wrap_secret
from .state import CANARY_PARKED, MANIFEST_IMMUTABLE, RESTORE_FAILED, TARGET_REJECTED, CellRow, ClusterObservation

logger = logging.getLogger("cellctl")

POLL_INTERVAL_SECONDS = 5.0
RECONNECT_BACKOFF_INITIAL_SECONDS = 1.0
RECONNECT_BACKOFF_MAX_SECONDS = 30.0
DEFAULT_HEARTBEAT_PATH = "/tmp/cellctl-heartbeat"
DEFAULT_ADMISSION_POLICY_NAME = "exomem-cellctl-scope"
DEFAULT_ADMISSION_BINDING_NAME = "exomem-cellctl-scope"

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
    return described


# A lost or half-open database session. It fails every row alike, so it
# aborts the pass and reaches run_loop's reconnect instead of being logged
# once per row against a dead connection. TimeoutError is asyncpg's command
# timeout (and an OSError, which run_loop already treats as a lost session).
_ROW_SESSION_ERRORS = (asyncpg.InterfaceError, asyncpg.PostgresConnectionError, TimeoutError)

# D4: orphan namespaces are listed at most this often, and Hetzner's volume
# API is asked about a deleting row's volume at most this often.
ORPHAN_SCAN_INTERVAL = timedelta(minutes=10)
VOLUME_CHECK_INTERVAL = timedelta(minutes=1)
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


class ClusterGateway:
    """The subset of ClusterClient + storage that reconcile.py needs, kept
    as a Protocol-shaped class so tests can supply a fake."""

    def observe(self, cell_id: str, namespace: str) -> ClusterObservation: ...  # pragma: no cover
    def apply_all(self, manifests: list[dict]) -> None: ...  # pragma: no cover
    def delete_namespace(self, name: str) -> None: ...  # pragma: no cover
    def delete_job(self, namespace: str, name: str) -> None: ...  # pragma: no cover
    def run_job(self, manifest: dict) -> None: ...  # pragma: no cover
    def namespace_absent(self, name: str) -> bool: ...  # pragma: no cover
    def pv_absent_for_namespace(self, namespace: str) -> bool: ...  # pragma: no cover
    def admission_policy_present(self, policy_name: str, binding_name: str) -> bool: ...  # pragma: no cover
    def list_cell_namespaces(self) -> dict[str, str]: ...  # pragma: no cover
    def capacity_inputs(
        self, *, csi_driver: str
    ) -> tuple[dict[str, int | None], dict[str, int], dict[str, int]]: ...  # pragma: no cover


def _active_hold(row: CellRow, observation: ClusterObservation) -> str | None:
    # D4: annotations are the truth once the StatefulSet can be observed.
    return observation.statefulset_hold_kind if observation.statefulset_exists else row.hold_kind


def _restore_blocks_upgrades(row: CellRow, observation: ClusterObservation, rollout) -> bool:
    """D6: a restore hold blocks every new upgrade attempt. One that has
    already failed (RESTORE_FAILED) stops blocking once the owner resumes
    the rollout: the hold itself stays, fail-closed, but a single broken
    restore never stalls the fleet's releases indefinitely."""

    if _active_hold(row, observation) != "restore":
        return False
    return rollout.paused or row.last_error_code != RESTORE_FAILED


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


def _augment_deletion_observation(
    observation: ClusterObservation,
    row: CellRow,
    cluster: ClusterGateway,
    object_storage,
    volume_provider,
    now: datetime | None = None,
    memory: LoopMemory | None = None,
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
    no_pv_claims_namespace = cluster.pv_absent_for_namespace(namespace)
    # Hetzner is asked only once the namespace and every PV claim are gone,
    # since the volume cannot be released before that.
    hetzner_volume_absent = row.volume_id is None or (
        namespace_absent_confirmed
        and no_pv_claims_namespace
        and _hetzner_volume_absent(row.volume_id, volume_provider, now or datetime.now(UTC), memory or LoopMemory())
    )
    pv_absent_confirmed = no_pv_claims_namespace and hetzner_volume_absent

    prefix = f"cells/{row.cell_id}/"
    backup_objects_absent_confirmed = len(object_storage.list_object_versions(prefix)) == 0

    b2_key_absent_confirmed = row.b2_key_id is None or object_storage.key_absent(row.b2_key_id)

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
) -> tuple[str, str, int]:
    """D7: create the per-cell B2 key once, deleting the loser on a race.
    Returns the key id, its secret and the wrapping key version."""

    if row.b2_key_id is not None and row.b2_key_wrapped is not None:
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


def _compute_render_digest(row: CellRow, cluster_config: ClusterConfig, secrets_config: SecretsConfig) -> str:
    """D4: a SHA-256 over the non-secret render inputs -- the renderer
    version, chart-level cell settings, the set of cell_token_key versions
    in play, and the row's storage and key versions. Never a secret value
    itself. storage_gib bumps no generation, so it must be here: it renders
    into the PVC and the quota, and it is part of the refusal park key."""

    material = {
        "render_version": RENDER_VERSION,
        "resources": asdict(cluster_config.resources),
        "model_env": cluster_config.model_env or {},
        "job_egress_except": sorted(cluster_config.job_egress_except),
        "cell_token_key_version": secrets_config.cell_token_key_version,
        "cell_token_key_previous_version": secrets_config.cell_token_key_previous_version,
        "storage_gib": row.storage_gib,
        "backup_key_version": row.backup_key_version,
        "b2_key_version": row.b2_key_version,
    }
    blob = json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _select_backup_candidates(
    rows: list[CellRow],
    observations: dict[str, ClusterObservation],
    now: datetime,
    config: ReconcileConfig,
    statefulset_blocked: frozenset[str] = frozenset(),
) -> set[str]:
    """D8: at most `backup_concurrency` backup holds run at once. Due cells
    go oldest `last_backup_at` first, never-backed-up cells first of all.
    D4: a refused row is still backed up, unless its own StatefulSet was
    refused and that refusal's backoff has not passed. A backup hold whose
    own StatefulSet is refused cannot exit until it applies, so it does not
    occupy a slot the rest of the fleet needs."""

    active = sum(
        1
        for row in rows
        if _active_hold(row, observations[row.cell_id]) == "backup" and row.cell_id not in statefulset_blocked
    )
    slots = config.backup_concurrency - active
    if slots <= 0:
        return set()
    due = [
        row
        for row in rows
        if row.observed_state in ("running", "read_only")
        and row.cell_id not in statefulset_blocked
        and _active_hold(row, observations[row.cell_id]) is None
        and nightly_backup_due(row, observations[row.cell_id], now, config)
    ]
    due.sort(key=lambda row: (row.last_backup_at is not None, row.last_backup_at or datetime.min.replace(tzinfo=UTC)))
    return {row.cell_id for row in due[:slots]}


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
) -> None:
    now = now or datetime.now(UTC)
    memory = memory if memory is not None else LoopMemory()

    # D4 self-check: without cellctl's own admission confinement in place,
    # its ClusterRole is close to cluster-admin. Do nothing this pass rather
    # than act unconfined.
    if not cluster.admission_policy_present(
        cluster_config.admission_policy_name, cluster_config.admission_binding_name
    ):
        logger.error(
            "cellctl admission policy/binding missing or not a Deny binding of that policy (%s/%s); skipping this pass",
            cluster_config.admission_policy_name,
            cluster_config.admission_binding_name,
        )
        return

    rows = await db.select_all_rows(connection)
    rollout = await db.read_rollout(connection)
    cell_image = await db.read_cell_image(connection)
    _log_orphan_namespaces(cluster, rows, now, memory)
    # D4 API budget: a row observed as deleted is not observed again.
    rows = [row for row in rows if not (row.desired_state == "deleted" and row.observed_state == "deleted")]

    # H4: one row's observe() must not take every other row down with it.
    observations: dict[str, ClusterObservation] = {}
    for row in rows:
        try:
            observations[row.cell_id] = cluster.observe(row.cell_id, namespace_name(row.cell_id))
        except Exception as error:  # noqa: BLE001
            logger.error("cellctl observe failed for cell %s: %s", row.cell_id, _describe_error(error))
    # A non-deleted row that could not be observed may be the owner's canary
    # or hold an upgrade, restore or backup slot. Deciding fleet-wide without
    # it could pick a tenant as canary or start a second upgrade, so this
    # pass starts nothing fleet-wide; per-row work still runs.
    fleet_unobserved = any(row.cell_id not in observations and row.desired_state != "deleted" for row in rows)
    rows = [row for row in rows if row.cell_id in observations]

    non_deleted_rows = [row for row in rows if row.desired_state != "deleted"]
    # H1/H9: an upgrade or a restore hold blocks new candidates; a deleted
    # row never counts, whatever its stale annotations or row state say.
    any_upgrading = any(_active_hold(row, observations[row.cell_id]) == "upgrade" for row in non_deleted_rows)
    any_restoring = any(_restore_blocks_upgrades(row, observations[row.cell_id], rollout) for row in non_deleted_rows)
    render_digests = {row.cell_id: _compute_render_digest(row, cluster_config, secrets_config) for row in rows}

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
    backup_candidates: set[str] = set()
    render_digest_candidate: str | None = None
    if fleet_unobserved:
        logger.warning("cellctl: a cell could not be observed; no upgrade, backup or re-render starts this pass")
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
        backup_candidates = _select_backup_candidates(non_deleted_rows, observations, now, config, statefulset_blocked)
        render_digest_candidate = _select_render_digest_candidate(
            non_deleted_rows, observations, render_digests, now, config, parked, statefulset_blocked
        )

    for row in rows:
        try:
            await _reconcile_row(
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
                is_render_digest_candidate=row.cell_id == render_digest_candidate,
                refusal_parked=row.cell_id in parked,
                memory=memory,
            )
        except Exception as error:  # noqa: BLE001 - one bad row must not take the pass down
            if isinstance(error, _ROW_SESSION_ERRORS) or connection.is_closed():
                raise
            logger.error("cellctl reconcile failed for cell %s: %s", row.cell_id, _describe_error(error))

    # D4: capacity is published at the end of every pass, whatever the rows
    # did, from Kubernetes CSINode/VolumeAttachment state -- never a Hetzner
    # server id or a hardcoded chart constant.
    # Capacity has its own boundary too: a failed capacity read only leaves
    # the previous `exomem_cloud_capacity` rows in place, and must never turn
    # a pass whose rows all succeeded into a failed one.
    try:
        allocatable, attachments_used, non_cell_attachments = cluster.capacity_inputs(
            csi_driver=cluster_config.capacity.csi_driver
        )
    except Exception as error:  # noqa: BLE001 - capacity must not take the pass down
        logger.error("cellctl: capacity read failed; keeping the last published capacity: %s", _describe_error(error))
        return
    present_nodes = sorted(set(allocatable) | set(attachments_used))
    for node in present_nodes:
        capacity = compute_node_capacity(
            allocatable=allocatable.get(node),
            attachments_used=attachments_used.get(node, 0),
            non_cell_attachments=non_cell_attachments.get(node, 0),
            config=cluster_config.capacity,
        )
        if not capacity.limit_known:
            logger.warning("cellctl: node %s has no known attachments limit; publishing 0 cell_slots", node)
        await db.write_capacity(
            connection,
            node=node,
            cell_slots=capacity.cell_slots,
            attachments_used=capacity.attachments_used,
            observed_at=now,
        )
    await db.zero_absent_capacity(connection, present_nodes=present_nodes, observed_at=now)


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
) -> None:
    already_served = row.observed_state in ("running", "read_only")
    backup_due = already_served and start_backup
    if (
        not row.is_dirty(refusal_parked=refusal_parked)
        and _active_hold(row, observation) is None
        and not backup_due
        and not start_upgrade
        and not is_render_digest_candidate
    ):
        # D4: observed_at is written on every observation, including one
        # that finds nothing to do.
        await db.write_observed(connection, row.cell_id, {"observed_at": now})
        return

    if row.desired_state == "deleted":
        observation = _augment_deletion_observation(
            observation, row, cluster, object_storage, volume_provider, now, memory
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
    )

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

    refused: list[tuple[str, str]] = []
    applied_cleanly = False
    if decision.apply_manifests and decision.image:
        secret_material, backup_key_version = await _resolve_secret_material(connection, row, secrets_config)
        key_id, key_secret, b2_key_version = await _resolve_object_storage_key(
            connection, row, secrets_config, object_storage
        )
        # D4: the digest covers the row's key versions. A new cell's first
        # pass creates them, so the applied digest is computed from the
        # versions this pass resolved; hashing the row as read would change
        # the digest on the next pass and restart the pod for nothing.
        render_digest = _compute_render_digest(
            dataclass_replace(row, backup_key_version=backup_key_version, b2_key_version=b2_key_version),
            cluster_config,
            secrets_config,
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
        spec = CellManifestSpec(
            cell_id=row.cell_id,
            image=decision.image,
            replicas=decision.replicas,
            read_only=decision.read_only,
            storage_gib=row.storage_gib,
            resources=cluster_config.resources,
            model_env=cluster_config.model_env or {},
            hold_kind=decision.hold_kind,
            hold_started_at=decision.hold_started_at.isoformat() if decision.hold_started_at else None,
            previous_image=decision.previous_image,
            pre_upgrade_snapshot=decision.pre_upgrade_snapshot,
            target_applied_at=decision.target_applied_at.isoformat() if decision.target_applied_at else None,
            restored_snapshot=decision.restored_snapshot,
            backup_retry_after=retry_after.isoformat() if retry_after else None,
            backup_retry_minutes=retry_minutes,
            render_digest=render_digest,
            render_digest_applied_at=digest_applied_at.isoformat() if digest_applied_at else None,
            row_generation=row.generation,
            job_egress_except=cluster_config.job_egress_except,
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
            return
        # D4: parked in memory on (generation, applied digest, image), and
        # retried on the backoff; success resets it.
        refusal_key = (row.generation, render_digest, spec.image)
        if not statefulset_applied:
            # Nothing this decision describes landed, so only the refusal is
            # recorded. observed_generation is not written, since nothing
            # converged. A refused hold start is retried on the backoff too.
            _record_refusal(memory, row.cell_id, refusal_key, now, statefulset_blocked=True)
            await db.write_observed(connection, row.cell_id, {"last_error_code": MANIFEST_IMMUTABLE, "observed_at": now})
            return
        park = memory.refusals.get(row.cell_id)
        jobs_parked = park is not None and park.job_blocked and park.key == refusal_key and now < park.retry_at
        job_refused: list[tuple[str, str]] = []
        if jobs_parked and (decision.run_backup_job or decision.run_restore_job_snapshot):
            # A Job refused for this key waits out the backoff.
            job_refused.append(("Job", "parked"))
        else:
            if decision.run_backup_job:
                _run_cell_job(
                    cluster,
                    row.cell_id,
                    render_backup_job(
                        spec,
                        bucket_name=cluster_config.object_storage_bucket,
                        endpoint=cluster_config.object_storage_endpoint,
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

    row_updates: dict[str, object] = {**decision.row_updates, "observed_at": now}
    if refused:
        # D4: the StatefulSet landed but another object was refused. The
        # refusal is what the row records, and nothing converged.
        row_updates["last_error_code"] = MANIFEST_IMMUTABLE
        row_updates.pop("observed_generation", None)
    elif applied_cleanly and row.last_error_code == MANIFEST_IMMUTABLE and "last_error_code" not in row_updates:
        row_updates["last_error_code"] = None
    await db.write_observed(connection, row.cell_id, row_updates)
    if decision.rollout_updates and not pause_first:
        await db.write_rollout(connection, decision.rollout_updates)


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
    """D4: poll every 5 seconds, plus LISTEN on a direct session.

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
            try:
                await reconcile_once(
                    connection,
                    cluster,
                    object_storage,
                    volume_provider,
                    secrets_config,
                    cluster_config,
                    config=config,
                    memory=memory,
                )
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
                await asyncio.wait_for(woken.wait(), timeout=POLL_INTERVAL_SECONDS)
            except TimeoutError:
                pass
    finally:
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()
