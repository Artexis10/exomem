"""C5 import decisions and S3 multipart transport, scoped to one immutable cell."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from botocore.exceptions import ClientError

from .capacity import GIB, pool_takes
from .state import CellRow, ClusterObservation, Decision
from .storage_config import DEFAULT_STORAGE

# C5 fixes these state tokens; they are not tenant vocabulary.
TERMINAL = frozenset({"done", "failed", "expired", "discarded"})
PHASES = ("prepare", "import", "cleanup", "discard")
UPLOAD_DEADLINE = timedelta(hours=24)
PART_BYTES = 64 * 1024 * 1024
MAX_PARTS = 10000
MAX_MEMBERS = 500_000
RESERVE_BYTES = 256 * 1024 * 1024
SPACE_FACTOR = 2


@dataclass(frozen=True)
class ImportLimits:
    max_storage_gib: int
    import_deadline_seconds: int
    import_growth_deadline_seconds: int
    max_members: int = MAX_MEMBERS
    reserve_bytes: int = RESERVE_BYTES
    space_factor: int = SPACE_FACTOR
    part_bytes: int = PART_BYTES
    max_parts: int = MAX_PARTS
    upload_window_seconds: int = int(UPLOAD_DEADLINE.total_seconds())

    @classmethod
    def for_cell(cls, row, observation, storage, config, room):
        maximum = max(storage.max_cell_gib, row.size_gib)
        if storage.is_local(storage.claim_class(observation.pvc_storage_class or observation.pv_storage_class)):
            maximum = row.size_gib
            if room is not None:
                for size in range(row.size_gib + 1, storage.max_cell_gib + 1):
                    free = room.free_bytes.get(observation.pv_node)
                    if pool_takes(storage.local, free_bytes=free, added_bytes=(size - row.size_gib) * GIB,
                                  largest_cell_gib=max(room.largest_cell_gib, size)):
                        maximum = size
        return cls(maximum, int(config.import_deadline.total_seconds()),
                   int(config.import_growth_deadline.total_seconds()))


@dataclass(frozen=True)
class ImportRow:
    import_id: str
    cell_id: str
    kind: str
    declared_bytes: int
    declared_files: int
    requested_state: str
    created_at: datetime
    ciphertext_sha256: str | None = None
    ciphertext_bytes: int | None = None
    part_etags: list[dict] | None = None
    recipient: str | None = None
    upload_id: str | None = None
    part_urls: list[dict] | None = None
    urls_expire_at: datetime | None = None
    observed_state: str | None = None
    error_code: str | None = None
    updated_at: datetime | None = None
    required_storage_gib: int | None = None
    staged_files: int | None = None
    staged_bytes: int | None = None

    def __post_init__(self):
        if str(uuid.UUID(self.import_id)) != self.import_id:
            raise ValueError("invalid import UUID")

    def request(self) -> dict:
        return {name: getattr(self, name) for name in (
            "import_id", "cell_id", "kind", "declared_bytes", "declared_files",
            "ciphertext_sha256", "ciphertext_bytes",
        )}

    @property
    def key(self) -> str:
        return f"cells/{self.cell_id}/imports/{self.import_id}.age"


@dataclass(frozen=True)
class ImportJob:
    exists: bool = False
    active: bool = False
    succeeded: bool = False
    failed: bool = False
    report: dict = field(default_factory=dict)
    pods_present: bool = False


def job_name(import_id: str, phase: str) -> str:
    return f"import-{uuid.UUID(import_id).hex}-{phase}"


def decide_import(row: CellRow, item: ImportRow, observation: ClusterObservation,
                  jobs: dict[str, ImportJob], *, now: datetime, config=None,
                  storage=DEFAULT_STORAGE, storage_room=None) -> Decision | None:
    """Separate work completion, volume release and eventual cleanup."""
    from .decide import DEFAULT_RECONCILE_CONFIG

    config = config or DEFAULT_RECONCILE_CONFIG
    active_hold = row.hold_kind or observation.statefulset_hold_kind
    if active_hold not in (None, "import"):
        return None
    if item.cell_id != row.cell_id:
        raise ValueError("import cell mismatch")
    image = observation.statefulset_image or row.observed_image
    remaining = [phase for phase, job in jobs.items() if job.exists or job.pods_present or job.active]
    # C1 owns the deadline; an older StatefulSet annotation belongs to the predecessor.
    start = row.hold_started_at if row.hold_kind == "import" else None
    if start is None or start < item.created_at:
        start = now

    def held():
        return Decision(apply_manifests=bool(image), replicas=0, image=image,
                        read_only=row.desired_state == "read_only", hold_kind="import", hold_started_at=start,
                        row_updates={"hold_kind": "import", "hold_started_at": start,
                                     "ready": False, "observed_state": "stopping"})

    def release():
        decision = held()
        decision.stop_import_jobs = remaining
        if remaining:
            decision.row_updates["last_error_code"] = "IMPORT_STOP_PENDING"
        else:
            decision.hold_kind = None
            decision.hold_started_at = None
            decision.replicas = 0 if row.desired_state in ("stopped", "deleted") else 1
            decision.row_updates.update(hold_kind=None, hold_started_at=None, last_error_code=None)
        return decision

    if row.desired_state == "deleted":
        # Namespace/PVC and verified prefix deletion supersede import cleanup.
        if remaining:
            decision = held()
            decision.stop_import_jobs = remaining
            return decision
        return None
    if item.observed_state in TERMINAL and (item.requested_state != "discard_requested" or item.observed_state == "discarded"):
        return release() if active_hold == "import" or remaining else None
    if image is None:
        return None  # Ordinary first provisioning must produce the cell image and claim.
    if item.observed_state in ("cleanup_stopping", "cleanup_discard_stopping"):
        decision = held()
        decision.stop_import_jobs = remaining
        if not remaining:
            decision.finalize_import = True
        return decision

    cleanup_phase = "discard" if item.requested_state in ("cancelled", "discard_requested") else "cleanup"
    cleanup_job = jobs.get(cleanup_phase, ImportJob())
    if item.observed_state == "cleanup_pending":
        updated = item.updated_at or item.created_at
        if active_hold == "import" or remaining:
            return release()  # A failed attempt must drain before another acquires the volume.
        if now - updated < config.import_cleanup_retry:
            return None
        start = now
        decision = held()
        decision.import_updates.update(observed_state="cleanup_running", part_urls=None, urls_expire_at=None)
        if not observation.pod_uses_volume:
            decision.import_action = cleanup_phase
        return decision

    imported = jobs.get("import", ImportJob())
    prepared = jobs.get("prepare", ImportJob())
    cleanup_started = cleanup_job.exists or cleanup_job.active or cleanup_job.succeeded or cleanup_job.failed
    running = item.observed_state == "cleanup_running"
    failure = item.error_code
    waiting_for_storage = False
    if item.recipient is None and item.required_storage_gib is not None:
        required = item.required_storage_gib
        waiting_for_storage = (
            (row.observed_storage_capacity_bytes or 0) < required * GIB
            or row.observed_storage_generation != row.generation or observation.pvc_resize_pending
        )
        if storage.is_local(observation.pv_storage_class):
            free = storage_room.free_bytes.get(observation.pv_node) if storage_room else None
            accepted = observation.pvc_request_bytes
            if free is None or accepted is None:
                waiting_for_storage = True
            elif not pool_takes(storage.local, free_bytes=free,
                                added_bytes=max(0, required * GIB - accepted),
                                largest_cell_gib=max(storage_room.largest_cell_gib, required)):
                failure = failure or "IMPORT_STORAGE_NO_ROOM"
        if waiting_for_storage and now - item.created_at >= config.import_growth_deadline:
            failure = failure or "IMPORT_STORAGE_UNAVAILABLE"
    if not running and not cleanup_started:
        if row.desired_state == "stopped" and not imported.succeeded:
            failure = failure or "IMPORT_INTERRUPTED"
        elif active_hold == "import" and now - start >= config.import_deadline:
            failure = failure or "IMPORT_HOLD_EXPIRED"
        elif now - item.created_at > UPLOAD_DEADLINE:
            failure = failure or "IMPORT_EXPIRED"
        if not failure and (imported.failed or prepared.failed):
            reported = (imported if imported.failed else prepared).report.get("error")
            # The trusted cell command emits fixed uppercase error tokens, never exception text.
            failure = reported if isinstance(reported, str) and re.fullmatch(r"[A-Z_]{1,64}", reported) else "IMPORT_FAILED"
    cancelled = item.requested_state in ("cancelled", "discard_requested")
    if running or cancelled or failure or imported.succeeded or cleanup_started:
        if not running:
            start = now
        decision = held()
        decision.import_updates.update(part_urls=None, urls_expire_at=None)
        if not running:
            decision.import_updates.update(observed_state="cleanup_running", error_code=failure)
        decision.stop_import_jobs = [phase for phase in remaining if phase != cleanup_phase and
                                     (jobs[phase].active or cancelled or failure)]
        if cleanup_job.failed or now - start >= config.import_cleanup_deadline:
            decision.import_updates["observed_state"] = "cleanup_pending"
            decision.stop_import_jobs = remaining
            return decision
        if decision.stop_import_jobs or observation.pod_uses_volume:
            return decision
        if cleanup_job.succeeded:
            decision.cleanup_import_transport = True
            # The succeeded Job phase fixes cleanup scope; finalization reads newer intent under lock.
            decision.import_updates.update(observed_state="cleanup_discard_stopping" if cleanup_phase == "discard" else "cleanup_stopping",
                                           recipient=None, upload_id=None)
            decision.stop_import_jobs = remaining
            return decision
        if not cleanup_job.active and not cleanup_job.exists:
            decision.import_action = cleanup_phase
        return decision

    if waiting_for_storage:
        return None  # Ordinary reconciliation keeps the runtime mounted and grows the claim.
    decision = held()
    if item.recipient is None:
        if prepared.succeeded:
            recipient = prepared.report.get("recipient")
            # age validates the recipient; this checks its fixed X25519 wire shape.
            if not isinstance(recipient, str) or not recipient.startswith("age1") or len(recipient) != 62:
                decision.import_updates["error_code"] = "IMPORT_KEY_INVALID"
                return decision
            decision = release()
            decision.import_updates.update(recipient=recipient, observed_state="key_ready")
        elif not observation.pod_uses_volume and not prepared.active and not prepared.exists:
            decision.import_action = "prepare"
        return decision
    if item.requested_state == "uploaded":
        if not observation.pod_uses_volume and not imported.active and not imported.exists:
            decision.import_action = "import"
            decision.import_updates["observed_state"] = "importing"
        return decision
    # Uploading ciphertext requires no mounted volume or hold.
    decision = release()
    decision.apply_manifests = active_hold == "import" or bool(remaining)
    decision.prepare_import_upload = item.ciphertext_bytes is not None and item.upload_id is None
    if active_hold != "import" and not remaining:
        if not decision.prepare_import_upload:
            return None  # Ordinary reconciliation owns readiness while the owner uploads.
        decision.row_updates = {}
    return decision


class ImportStorage:
    """Use the maintained S3 SDK with the cell's prefix-restricted credential."""
    def __init__(self, client, bucket: str):
        self.client, self.bucket = client, bucket

    def prepare(self, item: ImportRow) -> tuple[str, list[dict]]:
        # Recover an upload created before cellctl crashed ahead of its C5 write.
        size = item.ciphertext_bytes
        if not size or size > MAX_PARTS * PART_BYTES:
            raise ValueError("ciphertext size exceeds multipart capacity")
        upload_id = (open_uploads(self.client, self.bucket, item.key) or [None])[0]
        if upload_id is None:
            upload_id = self.client.create_multipart_upload(Bucket=self.bucket, Key=item.key)["UploadId"]
        parts = []
        for number, offset in enumerate(range(0, size, PART_BYTES), 1):
            part_size = min(PART_BYTES, size - offset)
            url = self.client.generate_presigned_url("upload_part", Params={
                "Bucket": self.bucket, "Key": item.key, "UploadId": upload_id,
                "PartNumber": number, "ContentLength": part_size,
            }, ExpiresIn=86400, HttpMethod="PUT")
            parts.append({"part_number": number, "url": url, "part_size_bytes": part_size})
        return upload_id, parts

    def complete(self, item: ImportRow) -> str:
        expected = [part["part_number"] for part in item.part_urls or []]
        if not expected or [part["part_number"] for part in item.part_etags or []] != expected:
            raise ValueError("incomplete part report")
        try:
            self.client.complete_multipart_upload(Bucket=self.bucket, Key=item.key, UploadId=item.upload_id,
                MultipartUpload={"Parts": [{"PartNumber": part["part_number"], "ETag": part["etag"]}
                                           for part in item.part_etags]})
        except ClientError as error:
            code = error.response["Error"]["Code"]
            # S3 fixes these multipart integrity errors; network/service failures retry normally.
            if code in ("InvalidPart", "InvalidPartOrder", "EntityTooSmall"):
                raise ValueError("multipart completion refused") from None
            if code != "NoSuchUpload":
                raise
            # Completion may have committed before the controller's next row write.
        head = self.client.head_object(Bucket=self.bucket, Key=item.key)
        if head["ContentLength"] != item.ciphertext_bytes:
            raise ValueError("ciphertext size mismatch")
        params = {"Bucket": self.bucket, "Key": item.key}
        if head.get("VersionId"):
            params["VersionId"] = head["VersionId"]
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=7200)

    def cleanup(self, item: ImportRow) -> None:
        remove_object(self.client, self.bucket, item.key)


def open_uploads(client, bucket: str, key: str) -> list[str]:
    """The ids of every unfinished multipart upload of exactly `key`."""
    return [upload["UploadId"] for page in client.get_paginator("list_multipart_uploads").paginate(
        Bucket=bucket, Prefix=key) for upload in page.get("Uploads", []) if upload["Key"] == key]


def _versions(client, bucket: str, key: str) -> list[str]:
    return [version["VersionId"] for page in client.get_paginator("list_object_versions").paginate(
        Bucket=bucket, Prefix=key) for version in page.get("Versions", []) + page.get("DeleteMarkers", [])
        if version["Key"] == key]


def remove_object(client, bucket: str, key: str) -> None:
    """Abort every upload of `key` and delete every version of it, then prove both are gone."""
    for upload_id in open_uploads(client, bucket, key):
        client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
    for version_id in _versions(client, bucket, key):
        client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
    if open_uploads(client, bucket, key) or _versions(client, bucket, key):
        raise RuntimeError("object transport removal remains pending")
