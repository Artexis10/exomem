"""C6 owner exports: decisions and S3 transport, scoped to one immutable cell.

An export packs the cell's vault into the canonical ZIP under an `export`
hold that stops the runtime, the same way an import or a nightly backup
holds the volume. The cell's own Job uploads the ZIP to part URLs that
cellctl presigned with the cell's prefix-restricted key; cellctl checks the
parts against the Job's report before it completes the upload, keeps the
object for the export TTL, presigns short download links on request, and
deletes every version at expiry or supersede.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
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
# The codes the cell's export Job itself reports; any other report is EXPORT_FAILED.
JOB_FAILURE_CODES = frozenset({EXPORT_FAILED, EXPORT_TOO_LARGE, EXPORT_UPLOAD_FAILED, SOURCE_CHANGED_DURING_EXPORT})
# C6: the part URLs cover the cell's whole size plus 2% for the ZIP's own headers.
SIZE_MARGIN_PERCENT = 102
# One presigned URL is one Job argument; this keeps a Job object far inside the
# API server's request size limit. A larger cell gets larger parts instead.
MAX_PARTS = 1000
_MIB = 1024 * 1024
# C6: CELLCTL_EXPORT_DEADLINE_MINUTES is the deadline for each started 10 GiB of
# an export's cap, the size of a default cell, so a larger cell gets the time
# that packing and uploading more bytes takes.
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

    @property
    def present(self) -> bool:
        return self.exists or self.active or self.pods_present


def job_name(export_id: str, *, verify: bool = False) -> str:
    return f"export-{uuid.UUID(export_id).hex}" + ("-verify" if verify else "")


def verification_secret_name(export_id: str) -> str:
    return job_name(export_id, verify=True) + "-get"


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


def report_usage(report: dict, observed_at: datetime) -> dict[str, object]:
    """The C1 usage columns an export Job's statvfs report carries, or none."""

    used, total = _count(report.get("used"), minimum=0), _count(report.get("total"), minimum=1)
    return usage_updates(used, total, observed_at)


@dataclass(frozen=True)
class ExportSize:
    max_bytes: int
    part_bytes: int
    parts: int


def export_size(row: CellRow) -> ExportSize:
    """C6: an export may pack everything the cell can hold, its size or its
    measured filesystem when that is larger, so only one file above the
    per-file limit makes a vault too large. The parts are 64 MiB, or larger in
    whole MiB, so that at most MAX_PARTS cover it with the ZIP margin."""

    max_bytes = max(row.size_gib * GIB, row.filesystem_bytes or 0)
    covered = -(-max_bytes * SIZE_MARGIN_PERCENT // 100)
    part_bytes = max(PART_BYTES, -(-covered // (MAX_PARTS * _MIB)) * _MIB)
    return ExportSize(max_bytes=max_bytes, part_bytes=part_bytes, parts=-(-covered // part_bytes))


def export_deadline(row: CellRow, config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG) -> timedelta:
    """The export hold's deadline, which the Job's activeDeadlineSeconds and the
    part URLs' expiry share: the configured deadline per started 10 GiB of cap."""

    return config.export_deadline * -(-export_size(row).max_bytes // DEADLINE_STEP_BYTES)


def decide_export(row: CellRow, item: ExportRow, observation: ClusterObservation, job: ExportJob, *,
                  now: datetime, config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG,
                  volume_unavailable: bool = False) -> Decision | None:
    """One pass of the export being packed, or of the newest one while its hold
    is still on. Returns None when the export leaves the cell to routine
    reconciliation this pass. `volume_unavailable` says the cell's volume is due
    to relocate or move, so decide() owns it this pass. reconcile.py never asks
    while a cell that has had a volume has no claim."""

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

    def hold(**fields: object) -> Decision:
        return Decision(apply_manifests=bool(image), replicas=0, image=image,
                        read_only=row.desired_state == "read_only", hold_kind=HOLD, hold_started_at=start,
                        row_updates={"hold_kind": HOLD, "hold_started_at": start,
                                     "ready": False, "observed_state": "stopping"}, **fields)

    def release() -> Decision:
        desired = 0 if row.desired_state == "stopped" else 1
        return Decision(apply_manifests=bool(image), replicas=desired, image=image,
                        read_only=row.desired_state == "read_only", hold_kind=None, hold_started_at=None,
                        row_updates={"hold_kind": None, "hold_started_at": None, "ready": False,
                                     "observed_state": "stopping"})

    def end(terminal: str, code: str | None) -> Decision:
        # The outcome is recorded before the upload is removed; a removal that
        # fails is retried from the row's upload_id and never holds the cell.
        updates = {"observed_state": terminal, "error_code": code}
        if not held:
            return Decision(export_transport="remove", export_updates=updates, stop_export_job=job.present)
        return hold(export_transport="remove", export_updates=updates, stop_export_job=job.present)

    if row.desired_state == "deleted":
        # Namespace and verified prefix deletion supersede the export; only its row and upload remain.
        if item.observed_state in (None, "packing"):
            return Decision(export_transport="remove",
                            export_updates={"observed_state": "failed", "error_code": EXPORT_INTERRUPTED})
        return None
    if item.observed_state not in (None, "packing"):
        if not held:
            return None
        if job.present:
            # The Job and its pod drain before the runtime may take the volume back.
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
        return hold(export_transport="create", export_updates={"observed_state": "packing"})

    if job.succeeded:
        if item.archive_version_id is not None:
            report = job.report
            if (report.get("verified") is not True or report.get("bytes") != item.archive_bytes
                    or report.get("sha256") != item.archive_sha256
                    or report.get("version") != item.archive_version_id):
                return end("failed", EXPORT_FAILED)
            return hold(export_updates={"observed_state": "ready", "error_code": None,
                        "ready_at": now, "expires_at": now + config.export_ttl, "upload_id": None})
        report = accepted_report(job.report)
        if report is None:
            return end("failed", EXPORT_FAILED)
        decision = hold(export_transport="complete", export_updates={
            "observed_state": "packing", "error_code": None, "archive_bytes": report["bytes"],
            "archive_sha256": report["sha256"], "packing_report": json.dumps(report),
        })
        decision.row_updates.update(report_usage(job.report, job.finished_at or now))
        return decision
    if held and now - start >= export_deadline(row, config):
        return end("failed", EXPORT_HOLD_EXPIRED)
    if job.failed:
        reported = job.report.get("error")
        decision = end("failed", reported if reported in JOB_FAILURE_CODES else EXPORT_FAILED)
        # A refused export still measured the volume, and C1 keeps the measurement.
        decision.row_updates.update(report_usage(job.report, job.finished_at or now))
        return decision
    decision = hold()
    if annotation == HOLD and not observation.pod_uses_volume and not job.present:
        decision.run_export_job = True
    return decision


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
    """The maintained S3 SDK with the cell's prefix-restricted credential.
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
