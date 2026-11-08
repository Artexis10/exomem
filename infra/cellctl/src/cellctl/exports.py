"""C6 owner exports: decisions and S3 transport, scoped to one immutable cell.

An export runs in three steps of the cell image (add-cloud-media-store D8,
amendment of 2026-11-08). The pause copies the vault's present members into
the export's own staging claim under an `export` hold that stops the
runtime, the same way an import or a nightly backup holds the volume. The
hold ends at the pause report. The upload and the pack then run beside the
serving runtime: the pack writes the canonical ZIP into a spool on staging,
and the upload sends it to part URLs that cellctl presigned with the cell's
prefix-restricted key. cellctl checks the parts against the upload's report
before it completes the upload, keeps the object for the export TTL,
presigns short download links on request, and deletes every version at
expiry or supersede.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from botocore.exceptions import ClientError

from .capacity import GIB
from .decide import DEFAULT_RECONCILE_CONFIG, ReconcileConfig, usage_updates
from .imports import PART_BYTES, open_uploads, remove_object
from .state import CellRow, ClusterObservation, Decision

HOLD = "export"
# C6 fixes these failure codes; they are protocol tokens, not tenant vocabulary.
EXPORT_FAILED = "EXPORT_FAILED"
EXPORT_TOO_LARGE = "EXPORT_TOO_LARGE"
EXPORT_HOLD_EXPIRED = "EXPORT_HOLD_EXPIRED"
EXPORT_INTERRUPTED = "EXPORT_INTERRUPTED"
EXPORT_UPLOAD_FAILED = "EXPORT_UPLOAD_FAILED"
SOURCE_CHANGED_DURING_EXPORT = "SOURCE_CHANGED_DURING_EXPORT"
CASE_COLLISION = "CASE_COLLISION"
EXPORT_MEDIA_UNREACHABLE = "EXPORT_MEDIA_UNREACHABLE"
# The codes the cell's export Jobs themselves report; any other report is EXPORT_FAILED.
JOB_FAILURE_CODES = frozenset({
    EXPORT_FAILED, EXPORT_TOO_LARGE, EXPORT_UPLOAD_FAILED, SOURCE_CHANGED_DURING_EXPORT, CASE_COLLISION,
    EXPORT_MEDIA_UNREACHABLE,
})
# The cell command's own phases (`exomem cell-export --phase`), and the step
# that verifies the completed version. Each names one Job of the export.
PAUSE, PACK, UPLOAD, VERIFY = "pause", "pack", "upload", "verify"
# The step the row records once the pause reported: the upload and the pack run without a hold.
TRANSFER = "transfer"
# The diagnostics a failed export records in its packing report, cellctl's own
# identifiers that carry no vault content.
STAGING_NOT_RUNNING = "staging_not_running"
PAUSE_BELOW_COPY_FLOOR = "pause_below_copy_floor"
TRANSFER_DEADLINE = "transfer_deadline"
# C6: the part URLs cover the planned archive plus 2% for what the plan cannot count.
SIZE_MARGIN_PERCENT = 102
# One presigned URL is one Job argument; this keeps a Job object far inside the
# API server's request size limit. A larger archive gets larger parts instead.
MAX_PARTS = 1000
# D8: the pack holds at most two parts on staging, so a part stays at most 1 GiB,
# and the archive bound is MAX_PARTS of them, near 1 TiB.
MAX_PART_BYTES = GIB
ARCHIVE_BOUND = MAX_PARTS * MAX_PART_BYTES * 100 // SIZE_MARGIN_PERCENT
# D8: staging holds the copied members (at most the cell's used bytes plus 10%
# for filesystem overhead) and the pack's two-part spool.
STAGING_MARGIN_PERCENT = 110
STAGING_SPOOL_BYTES = 2 * MAX_PART_BYTES
_MIB = 1024 * 1024
# C6: CELLCTL_EXPORT_DEADLINE_MINUTES is the deadline for each started 10 GiB of
# the planned archive, the size of a default cell, so a larger archive gets the
# time that packing, uploading and verifying more bytes takes.
DEADLINE_STEP_BYTES = 10 * GIB
_HEX64 = re.compile(r"[0-9a-f]{64}")  # SHA-256 wire format; always fullmatch


@dataclass(frozen=True)
class ExportRow:
    export_id: str
    cell_id: str
    requested_state: str
    created_at: datetime
    link_requested_at: datetime | None = None
    observed_state: str | None = None
    error_code: str | None = None
    archive_bytes: int | None = None
    archive_sha256: str | None = None
    archive_version_id: str | None = None
    packing_report: str | None = None
    upload_id: str | None = None
    ready_at: datetime | None = None
    expires_at: datetime | None = None
    download_url: str | None = None
    download_url_expires_at: datetime | None = None
    link_issued_at: datetime | None = None
    updated_at: datetime | None = None
    # contain-cloud-controller-compromise D4: the grant Substrate signed with the request. cellctl only relays
    # it, so it stays out of the row's repr and any log line that prints the row.
    operation_grant: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if str(uuid.UUID(self.export_id)) != self.export_id:
            raise ValueError("invalid export UUID")

    @property
    def key(self) -> str:
        """The only object an export may name: derived from the row, inside the cell's own prefix."""
        return f"cells/{self.cell_id}/exports/{self.export_id}.zip"


@dataclass(frozen=True)
class ExportJob:
    exists: bool = False
    active: bool = False
    succeeded: bool = False
    failed: bool = False
    report: dict = field(default_factory=dict)
    pods_present: bool = False
    finished_at: datetime | None = None
    # When the Job was created and when its container first ran: the pause's own deadline counts from them.
    created_at: datetime | None = None
    running_at: datetime | None = None

    @property
    def present(self) -> bool:
        return self.exists or self.active or self.pods_present


def job_name(export_id: str, step: str) -> str:
    return f"export-{uuid.UUID(export_id).hex}-{step}"


# The single Job an export ran before the three steps. cellctl still removes it.
def legacy_job_name(export_id: str) -> str:
    return f"export-{uuid.UUID(export_id).hex}"


def staging_claim_name(export_id: str) -> str:
    return f"export-staging-{uuid.UUID(export_id).hex}"


def verification_secret_name(export_id: str) -> str:
    return job_name(export_id, VERIFY) + "-get"


def export_state(item: ExportRow) -> dict:
    """The step the export's packing report records, cellctl's own JSON; {} before the pause starts."""

    try:
        value = json.loads(item.packing_report or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def etag_digest(etags: list[str]) -> str:
    """C6's part report: SHA-256 over one "<part number> <ETag>" line per part,
    in part order, each ETag without its surrounding quotes. The cell's
    `exomem cell-export` computes the same digest from its upload responses."""

    lines = "".join(f"{number} {etag.strip(chr(34))}\n" for number, etag in enumerate(etags, 1))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _count(value: object, *, minimum: int) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= minimum else None


def accepted_report(report: dict) -> dict | None:
    """The export Job's success report, or None when any field is missing or malformed."""

    sha256, digest = report.get("sha256"), report.get("etag_digest")
    size, parts = _count(report.get("bytes"), minimum=1), _count(report.get("parts"), minimum=1)
    if (size is None or parts is None or parts > MAX_PARTS or not isinstance(sha256, str)
            or not _HEX64.fullmatch(sha256) or not isinstance(digest, str) or not _HEX64.fullmatch(digest)):
        return None
    return {"bytes": size, "sha256": sha256, "parts": parts, "etag_digest": digest}


def accepted_pause_report(report: dict) -> dict | None:
    """The pause's numbers-only plan report, or None when any count is missing or malformed."""

    keys = ("members", "member_bytes", "evicted", "evicted_bytes", "unreadable_companions")
    counts = {key: _count(report.get(key), minimum=0) for key in keys}
    planned = _count(report.get("planned_bytes"), minimum=1)
    if report.get("phase") != PAUSE or planned is None or planned > ARCHIVE_BOUND or None in counts.values():
        return None
    return {**counts, "planned_bytes": planned}


def report_usage(report: dict, observed_at: datetime) -> dict[str, object]:
    """The C1 usage columns an export Job's statvfs report carries, or none."""

    used, total = _count(report.get("used"), minimum=0), _count(report.get("total"), minimum=1)
    return usage_updates(used, total, observed_at)


@dataclass(frozen=True)
class ExportSize:
    max_bytes: int
    part_bytes: int
    parts: int


def export_size(planned_bytes: int) -> ExportSize:
    """D8: the archive's limits follow the pause's plan, not the cell size. The
    pack may write the planned bytes, and the parts are 64 MiB, or larger in
    whole MiB, so that at most MAX_PARTS cover them with the margin. The pause
    refuses a plan above ARCHIVE_BOUND, so a part stays within MAX_PART_BYTES."""

    covered = -(-planned_bytes * SIZE_MARGIN_PERCENT // 100)
    part_bytes = max(PART_BYTES, -(-covered // (MAX_PARTS * _MIB)) * _MIB)
    return ExportSize(max_bytes=planned_bytes, part_bytes=part_bytes, parts=-(-covered // part_bytes))


def export_deadline(planned_bytes: int, config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG) -> timedelta:
    """The deadline after the pause, which the pack, the upload, the verification
    and the part URLs' expiry share: the configured deadline per started 10 GiB
    of the planned archive."""

    return config.export_deadline * max(1, -(-planned_bytes // DEADLINE_STEP_BYTES))


def _copied_bytes(row: CellRow) -> int:
    # The pause copies at most what the filesystem holds; a cell never measured counts its whole size.
    return row.used_bytes if row.used_bytes is not None else max(row.size_gib * GIB, row.filesystem_bytes or 0)


def pause_copy_time(row: CellRow, config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG) -> timedelta:
    """D8: how long the pause may run once its pod runs: one start allowance for
    its listing and its plan, then the used bytes at the configured floor rate."""

    rate = config.export_pause_copy_floor_mib * _MIB
    return config.export_pause_start + timedelta(seconds=-(-_copied_bytes(row) // rate))


def pause_window(row: CellRow, config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG) -> timedelta:
    """The export hold's whole length, which the pause Job's deadline shares: the
    runtime stops and the pause pod runs, each within the start allowance, then
    the pause copies at the floor rate."""

    return 2 * config.export_pause_start + pause_copy_time(row, config)


def staging_gib(used_bytes: int | None, storage_gib: int) -> int:
    """D8: the staging claim holds the copied members and the spool, in whole
    GiB. It never exceeds staging_bound_gib, which the cell quota admits."""

    bound = staging_bound_gib(storage_gib)
    if used_bytes is None:
        return bound
    return min(bound, -(-(used_bytes * STAGING_MARGIN_PERCENT // 100 + STAGING_SPOOL_BYTES) // GIB))


def staging_bound_gib(storage_gib: int) -> int:
    """The largest staging claim of a cell of this size: a full volume plus the spool."""

    return -(-(storage_gib * GIB * STAGING_MARGIN_PERCENT // 100 + STAGING_SPOOL_BYTES) // GIB)


def decide_export(row: CellRow, item: ExportRow, observation: ClusterObservation, job: ExportJob, *,
                  now: datetime, config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG,
                  volume_unavailable: bool = False, slot_free: bool = True) -> Decision | None:
    """One pass of the export's pause, or of the newest export while its hold
    is still on. `job` is the pause Job. Returns None when the export leaves
    the cell to routine reconciliation this pass, which it does for good once
    the pause reported (transfer_step continues it). `volume_unavailable` says
    the cell's volume is due to relocate or move, so decide() owns it this
    pass. `slot_free` says its node has room for one more export (D8).
    reconcile.py never asks while a cell that has had a volume has no claim."""

    if item.cell_id != row.cell_id:
        raise ValueError("export cell mismatch")
    annotation = observation.statefulset_hold_kind if observation.statefulset_exists else None
    if row.hold_kind not in (None, HOLD) or annotation not in (None, HOLD):
        # Another hold owns the volume. Waiting prevents stopping a cell mid-backup or
        # mid-upgrade; it costs the owner the length of that hold.
        return None
    held = HOLD in (annotation, row.hold_kind)
    image = observation.statefulset_image or row.observed_image
    # C1 owns the deadline; an older hold belongs to a predecessor.
    start = observation.statefulset_hold_started_at if annotation == HOLD else None
    start = start or (row.hold_started_at if row.hold_kind == HOLD else None)
    if start is None or start < item.created_at:
        start = now
    state = export_state(item)

    def hold(**fields: object) -> Decision:
        return Decision(apply_manifests=bool(image), replicas=0, image=image,
                        read_only=row.desired_state == "read_only", hold_kind=HOLD, hold_started_at=start,
                        row_updates={"hold_kind": HOLD, "hold_started_at": start,
                                     "ready": False, "observed_state": "stopping"}, **fields)

    def release(**fields: object) -> Decision:
        desired = 0 if row.desired_state == "stopped" else 1
        return Decision(apply_manifests=bool(image), replicas=desired, image=image,
                        read_only=row.desired_state == "read_only", hold_kind=None, hold_started_at=None,
                        row_updates={"hold_kind": None, "hold_started_at": None, "ready": False,
                                     "observed_state": "stopping"}, **fields)

    def end(terminal: str, code: str | None, diagnostic: str | None = None) -> Decision:
        # The outcome is recorded before the upload and the staging claim are
        # removed; a removal that fails is retried from the row's upload_id and
        # never holds the cell.
        updates: dict[str, object] = {"observed_state": terminal, "error_code": code}
        if diagnostic is not None:
            updates["packing_report"] = json.dumps({**state, "diagnostic": diagnostic})
        if not held:
            return Decision(export_transport="remove", export_updates=updates, stop_export_job=job.present)
        return hold(export_transport="remove", export_updates=updates, stop_export_job=job.present)

    if row.desired_state == "deleted":
        # Namespace and verified prefix deletion supersede the export; only its row and upload remain.
        if item.observed_state in (None, "packing"):
            return Decision(export_transport="remove",
                            export_updates={"observed_state": "failed", "error_code": EXPORT_INTERRUPTED})
        return None
    if item.observed_state not in (None, "packing") or state.get("step") == TRANSFER:
        if not held:
            return None
        if job.present:
            # The pause Job and its pod drain before the runtime may take the volume back.
            return hold(stop_export_job=True)
        return release()
    if item.requested_state == "superseded":
        return end("expired", None)
    if item.observed_state is None:
        if image is None:
            # A cell that never served has no vault to pack.
            return Decision(export_updates={"observed_state": "failed", "error_code": EXPORT_FAILED})
        if volume_unavailable:
            # D4: decide() starts a due relocation or move before anything else.
            # The export waits for the volume the cell serves from, and a
            # predecessor's leftover hold is released, not kept.
            return release() if held else None
        if not slot_free:
            # D8: the node's export slot is taken; the hold starts once it frees, so the vault never waits paused.
            return release() if held else None
        # D8: the pack and the upload run the image the pause runs.
        return hold(export_transport="create", export_updates={
            "observed_state": "packing", "packing_report": json.dumps({"step": PAUSE, "image": image})})

    if job.succeeded:
        report = accepted_pause_report(job.report)
        if report is None:
            return end("failed", EXPORT_FAILED)
        transfer = {"step": TRANSFER, "image": state.get("image") or image, "paused_at": now.isoformat(), **report}
        # The hold ends at the pause report, once the pause pod has left the volume.
        decision = hold(stop_export_job=True, export_updates={"packing_report": json.dumps(transfer)})
        decision.row_updates.update(report_usage(job.report, job.finished_at or now))
        return decision
    if held and now - start >= pause_window(row, config):
        return end("failed", EXPORT_HOLD_EXPIRED)
    if job.failed:
        reported = job.report.get("error")
        decision = end("failed", reported if reported in JOB_FAILURE_CODES else EXPORT_FAILED)
        # A refused export still measured the volume, and C1 keeps the measurement.
        decision.row_updates.update(report_usage(job.report, job.finished_at or now))
        return decision
    if job.created_at is not None:
        # D8: the short pause. The staging claim binds and attaches before the pod runs.
        if job.running_at is None and now - job.created_at >= config.export_pause_start:
            return end("failed", EXPORT_FAILED, STAGING_NOT_RUNNING)
        if job.running_at is not None and now - job.running_at >= pause_copy_time(row, config):
            return end("failed", EXPORT_FAILED, PAUSE_BELOW_COPY_FLOOR)
    decision = hold()
    if annotation == HOLD and not observation.pod_uses_volume and not job.present:
        decision.run_export_job = True
    return decision


@dataclass
class TransferStep:
    """What one pass does to an export after its pause: run one step Job, complete
    or remove the upload, record the outcome, and remove Jobs and staging."""

    export_updates: dict = field(default_factory=dict)
    transport: str | None = None
    run: str | None = None
    stop: tuple[str, ...] = ()
    remove_staging: bool = False


class ExportSlots:
    """D8: how many exports each node runs at once, counted from the exports that
    hold a slot when the pass starts. A node not known counts as free: a cell's
    node is an eventually consistent observation, and an export must not wait on it."""

    def __init__(self, limit: int, busy_nodes: Iterable[str | None]):
        self.limit = limit
        self.busy = Counter(node for node in busy_nodes if node is not None)

    def free(self, node: str | None) -> bool:
        return node is None or self.busy[node] < self.limit

    def take(self, node: str | None) -> None:
        if node is not None:
            self.busy[node] += 1


def transfer_deadline(item: ExportRow, config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG) -> datetime | None:
    """When the pack, the upload and the verification must have finished, or None before the pause reported."""

    state = export_state(item)
    if state.get("step") != TRANSFER:
        return None
    return datetime.fromisoformat(state["paused_at"]) + export_deadline(state["planned_bytes"], config)


def transfer_step(row: CellRow, item: ExportRow, jobs: dict[str, ExportJob], *, now: datetime,
                  config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG) -> TransferStep | None:
    """One pass of an export whose pause reported, beside the serving runtime.
    The upload Job starts first, so on a Hetzner volume the pack can join its
    node. cellctl completes the upload from the upload's report, then verifies
    the completed version. Returns None when nothing changes this pass."""

    state = export_state(item)
    if item.observed_state != "packing" or state.get("step") != TRANSFER or row.desired_state == "deleted":
        return None
    upload, pack, verify = (jobs.get(step, ExportJob()) for step in (UPLOAD, PACK, VERIFY))

    def end(terminal: str, code: str | None, diagnostic: str | None = None) -> TransferStep:
        # Removing the export removes its Jobs and its staging claim too.
        updates: dict[str, object] = {"observed_state": terminal, "error_code": code}
        if diagnostic is not None:
            updates["packing_report"] = json.dumps({**state, "diagnostic": diagnostic})
        return TransferStep(export_updates=updates, transport="remove")

    if item.requested_state == "superseded":
        return end("expired", None)
    if item.archive_version_id is not None:
        if verify.succeeded:
            report = verify.report
            if (report.get("verified") is not True or report.get("bytes") != item.archive_bytes
                    or report.get("sha256") != item.archive_sha256
                    or report.get("version") != item.archive_version_id):
                return end("failed", EXPORT_FAILED)
            return TransferStep(stop=(VERIFY,), export_updates={
                "observed_state": "ready", "error_code": None, "ready_at": now,
                "expires_at": now + config.export_ttl, "upload_id": None})
        if verify.failed:
            return end("failed", EXPORT_FAILED)
    elif upload.succeeded:
        report = accepted_report(upload.report)
        if report is None:
            return end("failed", EXPORT_FAILED)
        # The upload read the pack's end record, so the spool and staging are done with.
        return TransferStep(transport="complete", stop=(UPLOAD, PACK), remove_staging=True, export_updates={
            "error_code": None, "archive_bytes": report["bytes"], "archive_sha256": report["sha256"],
            "packing_report": json.dumps({**state, **report})})
    elif upload.failed or pack.failed:
        # The upload passes on the pack's failure code; a pack that died without one fails the upload too.
        reported = [job.report.get("error") for job in (pack, upload) if job.failed]
        return end("failed", next((code for code in reported if code in JOB_FAILURE_CODES), EXPORT_FAILED))
    if now >= transfer_deadline(item, config):
        return end("failed", EXPORT_FAILED, TRANSFER_DEADLINE)
    if item.archive_version_id is not None:
        return None if verify.present else TransferStep(run=VERIFY)
    if not upload.present:
        return TransferStep(run=UPLOAD)
    if not pack.present:
        return TransferStep(run=PACK)
    return None


def settle_step(row: CellRow, item: ExportRow, *, now: datetime,
                config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG) -> tuple[str, dict] | None:
    """What one pass does to an export no hold is needed for: remove a ready
    one once it expired, was superseded or its cell is deleted, answer a link
    request on a ready one, and retry an ended one's pending removal."""

    if item.observed_state == "ready":
        if item.requested_state == "superseded" or now >= item.expires_at or row.desired_state == "deleted":
            return "remove", {"observed_state": "expired", "download_url": None, "download_url_expires_at": None}
        if item.link_requested_at is not None and (
                item.link_issued_at is None or item.link_issued_at < item.link_requested_at):
            return "link", {"download_url_expires_at": now + config.export_link_ttl}
        return None
    if item.observed_state in ("failed", "expired") and (item.upload_id is not None or item.archive_version_id is not None):
        return "remove", {}
    return None


class ExportStorage:
    """The maintained S3 SDK with cellctl's staging-bucket credential.
    Every call names the row's own key and nothing else."""

    def __init__(self, client, bucket: str):
        self.client, self.bucket = client, bucket

    def create(self, item: ExportRow) -> str:
        # Recover an upload created before cellctl crashed ahead of its C6 write.
        existing = open_uploads(self.client, self.bucket, item.key)
        if existing:
            return existing[0]
        return self.client.create_multipart_upload(
            Bucket=self.bucket, Key=item.key, ContentType="application/zip", ServerSideEncryption="AES256",
        )["UploadId"]

    def part_urls(self, item: ExportRow, count: int, *, expires_in: int) -> list[str]:
        # No signed length: the Job's last part is shorter, and its report fixes every size.
        return [self.client.generate_presigned_url("upload_part", Params={
            "Bucket": self.bucket, "Key": item.key, "UploadId": item.upload_id, "PartNumber": number,
        }, ExpiresIn=expires_in, HttpMethod="PUT") for number in range(1, count + 1)]

    def complete(self, item: ExportRow, report: dict) -> str | None:
        """Complete the upload only when its parts are exactly the ones the Job
        reported. Returns the completed object's version id. Raises ValueError
        when the upload does not match, so a substituted part never completes."""

        try:
            parts = [part for page in self.client.get_paginator("list_parts").paginate(
                Bucket=self.bucket, Key=item.key, UploadId=item.upload_id) for part in page.get("Parts", [])]
        except ClientError as error:
            # A missing upload cannot be re-verified, so it is never accepted: a forged
            # completion is refused, and a controller crash between completion and its
            # C6 write costs the owner one new export.
            if error.response["Error"]["Code"] == "NoSuchUpload":
                raise ValueError("export upload is gone") from None
            raise
        if [part["PartNumber"] for part in parts] != list(range(1, report["parts"] + 1)):
            raise ValueError("export part count mismatch")
        if etag_digest([part["ETag"] for part in parts]) != report["etag_digest"]:
            raise ValueError("export part digest mismatch")
        if sum(part["Size"] for part in parts) != report["bytes"]:
            raise ValueError("export part size mismatch")
        try:
            completed = self.client.complete_multipart_upload(
                Bucket=self.bucket, Key=item.key, UploadId=item.upload_id,
                MultipartUpload={"Parts": [{"PartNumber": part["PartNumber"], "ETag": part["ETag"]} for part in parts]})
        except ClientError as error:
            # S3 fixes these multipart integrity errors; network and service failures retry normally.
            if error.response["Error"]["Code"] in ("InvalidPart", "InvalidPartOrder", "EntityTooSmall", "NoSuchUpload"):
                raise ValueError("export completion refused") from None
            raise
        version = completed.get("VersionId")
        # Immutable versions bind remote verification to this packing report.
        if not version:
            raise ValueError("export object has no immutable version")
        head = self.client.head_object(Bucket=self.bucket, Key=item.key, **({"VersionId": version} if version else {}))
        if head["ContentLength"] != report["bytes"]:
            raise ValueError("export size mismatch")
        return version

    def link(self, item: ExportRow, *, expires_in: int) -> str:
        """A download link for the verified version only, as an attachment."""

        day = item.ready_at.strftime("%Y-%m-%d") if item.ready_at else "export"
        params = {"Bucket": self.bucket, "Key": item.key,
                  "ResponseContentDisposition": f'attachment; filename="exomem-export-{day}.zip"'}
        if item.archive_version_id:
            params["VersionId"] = item.archive_version_id
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)

    def remove(self, item: ExportRow) -> None:
        remove_object(self.client, self.bucket, item.key)
