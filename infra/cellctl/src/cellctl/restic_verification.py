"""Durable attempt evidence and scheduling for optional read-only Cloud verification."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from .errors import describe_error
from .k8s_client import VERIFICATION_CENSUS_SECONDS
from .manifests import (
    RESTIC_VERIFICATION_ANNOTATION,
    RESTIC_VERIFICATION_MAX_PARTS,
    cell_id_from_namespace,
    namespace_name,
    restic_verification_command,
    restic_verification_invocation,
    restic_verification_job_name,
)
from .state import ResticVerificationJob

logger = logging.getLogger(__name__)
Outcome = Literal["pending", "success", "failed", "unknown"]
# These are the timestamp keys of the version 2 annotation.
_TIMES = ("started_at", "observed_at", "due_at", "last_success_at", "finished_at")
_IDLE_CENSUS_INTERVAL = timedelta(minutes=2)


@dataclass(frozen=True)
class VerificationConfig:
    cell_ids: tuple[str, ...] = ()
    parts: int = 24
    deadline_seconds: int = 300
    download_kib_per_second: int = 8192
    interval: timedelta = timedelta(days=1)
    retry: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if (
            len(self.cell_ids) > 1024
            or any(not isinstance(cell_id, str) or not cell_id_from_namespace(namespace_name(cell_id))
                   for cell_id in self.cell_ids)
            or len(set(self.cell_ids)) != len(self.cell_ids)
        ):
            raise ValueError("restic verification needs at most 1024 unique base32 cell IDs")
        if type(self.parts) is not int or not 1 <= self.parts <= RESTIC_VERIFICATION_MAX_PARTS:
            raise ValueError("restic verification partition count is invalid")
        if any(type(value) is not int or value <= 0 for value in (self.deadline_seconds, self.download_kib_per_second)):
            raise ValueError("restic verification needs positive whole time and rate limits")
        if min(self.interval, self.retry) <= timedelta(0):
            raise ValueError("restic verification intervals must be positive")


@dataclass(frozen=True)
class VerificationReceipt:
    repository: str
    statefulset_uid: str
    invocation: str
    total: int
    part: int
    attempt_id: str
    started_at: datetime
    outcome: Outcome = "pending"
    job_uid: str | None = None
    pod_uid: str | None = None
    finished_at: datetime | None = None
    observed_at: datetime | None = None
    due_at: datetime | None = None
    last_success_part: int | None = None
    last_success_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.part) is not int or type(self.total) is not int or not (
            1 <= self.part <= self.total <= RESTIC_VERIFICATION_MAX_PARTS
        ):
            raise ValueError("invalid verification partition")
        for value in (self.repository, self.invocation):
            if not isinstance(value, str) or len(value) != 64 or len(bytes.fromhex(value)) != 32:
                raise ValueError("invalid verification fingerprint")
        if not isinstance(self.attempt_id, str) or uuid.UUID(self.attempt_id).hex != self.attempt_id:
            raise ValueError("invalid verification attempt ID")
        if not self.statefulset_uid or any(
            value is not None and (not isinstance(value, str) or not value)
            for value in (self.statefulset_uid, self.job_uid, self.pod_uid)
        ):
            raise ValueError("invalid Kubernetes object identity")
        if self.started_at is None or (self.last_success_part is None) != (self.last_success_at is None):
            raise ValueError("invalid verification receipt times")
        if self.last_success_part is not None and (
            type(self.last_success_part) is not int or not 1 <= self.last_success_part <= self.total
        ):
            raise ValueError("invalid verified partition")
        if self.outcome == "pending":
            if self.due_at is not None or self.observed_at is not None:
                raise ValueError("invalid pending verification")
        elif self.outcome not in ("success", "failed", "unknown") or self.due_at is None or self.observed_at is None:
            raise ValueError("invalid terminal verification")
        if self.outcome == "success" and (
            not self.job_uid or not self.pod_uid or not self.finished_at
            or self.last_success_part != self.part or self.last_success_at != self.finished_at
        ):
            raise ValueError("unproven verification success")

    @classmethod
    def parse(cls, raw: str | None) -> VerificationReceipt | None:
        if not raw:
            return None
        try:
            fields = json.loads(raw)
            if not isinstance(fields, dict) or fields.pop("version", None) != 2:
                return None
            for key in _TIMES:
                if fields.get(key) is not None:
                    timestamp = datetime.fromisoformat(fields[key])
                    if timestamp.tzinfo is None:
                        return None
                    fields[key] = timestamp.astimezone(UTC)
            return cls(**fields)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    def annotation(self) -> str:
        fields = asdict(self)
        for key in _TIMES:
            if fields[key] is not None:
                fields[key] = fields[key].isoformat()
        return json.dumps({"version": 2, **fields}, separators=(",", ":"), sort_keys=True)

    @property
    def job_name(self) -> str:
        return restic_verification_job_name(self.attempt_id)

    @property
    def next_part(self) -> int:
        return self.part % self.total + 1 if self.outcome == "success" else self.part

    def owns(self, job: ResticVerificationJob) -> bool:
        return bool(self.job_uid and (job.name, job.uid, job.statefulset_uid)
                    == (self.job_name, self.job_uid, self.statefulset_uid))

    def settle(
        self, job: ResticVerificationJob | None, *, eligible: bool, now: datetime, config: VerificationConfig,
    ) -> VerificationReceipt:
        outcome: Outcome = "unknown"
        if (job is not None and eligible and self.owns(job) and not job.active and not job.deleting
                and job.invocation == self.invocation):
            if job.outcome == "failed":
                outcome = "failed"
            elif job.outcome == "success" and job.pod_uid and job.finished_at:
                outcome = "success"
        success = outcome == "success"
        return replace(
            self, outcome=outcome, observed_at=now, due_at=now + (config.interval if success else config.retry),
            pod_uid=job.pod_uid if success else None, finished_at=job.finished_at if success else None,
            last_success_part=self.part if success else self.last_success_part,
            last_success_at=job.finished_at if success else self.last_success_at,
        )


@dataclass(frozen=True)
class VerificationTarget:
    repository: str
    eligible: bool
    annotation: str | None
    statefulset_uid: str
    image: str | None


@dataclass
class VerificationRuntime:
    """Census pacing and per-cell parks. The durable receipt is the only result authority."""

    next_census_at: datetime | None = None
    census_retry_at: datetime | None = None
    refused_until: dict[str, datetime] = field(default_factory=dict)


async def _write_receipt(cluster, namespace: str, receipt: VerificationReceipt) -> None:
    await asyncio.to_thread(
        cluster.patch_statefulset_annotations, namespace, {RESTIC_VERIFICATION_ANNOTATION: receipt.annotation()},
        uid=receipt.statefulset_uid,
    )


async def sync_verifications(
    cluster, config: VerificationConfig, *, targets: dict[str, VerificationTarget], now: datetime,
    allow_start: bool, start, runtime: VerificationRuntime,
) -> bool:
    """Observe the single verifier flight after managed work and return its fast-poll hint."""
    if runtime.census_retry_at is not None and now < runtime.census_retry_at:
        return False
    runtime.refused_until = {name: until for name, until in runtime.refused_until.items() if name in targets and until > now}
    records = {}
    pending = []
    due = []
    selected = {namespace_name(cell_id) for cell_id in config.cell_ids}
    for namespace, target in targets.items():
        record = VerificationReceipt.parse(target.annotation)
        if record is not None and record.statefulset_uid != target.statefulset_uid:
            record = None
        records[namespace] = record
        if namespace in runtime.refused_until:
            continue
        if record is not None and record.outcome == "pending":
            pending.append(namespace)
        elif allow_start and namespace in selected and target.eligible and target.image:
            if record is not None and (record.repository != target.repository or record.total != config.parts):
                record = None
            due_at = record.due_at if record is not None else None
            if due_at is None or due_at <= now:
                due.append((due_at or datetime.min.replace(tzinfo=UTC), namespace, record, target))
    if not pending and not due and runtime.next_census_at is not None and now < runtime.next_census_at:
        return False

    try:
        observed = await asyncio.wait_for(asyncio.to_thread(cluster.observe_restic_verifications), VERIFICATION_CENSUS_SECONDS)
    except Exception as error:  # noqa: BLE001 -- optional reads cannot take down ordinary reconciliation
        runtime.census_retry_at = now + _IDLE_CENSUS_INTERVAL
        logger.warning("cellctl: restic verification observation is unknown: %s", describe_error(error))
        return False

    active = any(job.active for jobs in observed.values() for job in jobs)
    runtime.census_retry_at = None
    runtime.next_census_at = now if active else now + _IDLE_CENSUS_INTERVAL
    for namespace in sorted(pending):
        record = records[namespace]
        target = targets[namespace]
        job = next((item for item in observed.get(namespace, ()) if item.name == record.job_name), None)
        eligible = namespace in selected and target.eligible and record.repository == target.repository
        same_job = job is not None and record.owns(job)
        if same_job and job.active and not job.deleting and eligible and job.invocation == record.invocation:
            continue
        settled = record.settle(job, eligible=eligible, now=now, config=config)
        try:
            await _write_receipt(cluster, namespace, settled)
            logger.info(
                "cellctl: restic verification %s %s slice %d/%d at %s",
                namespace, settled.outcome, settled.part, settled.total, now.isoformat(),
            )
            if same_job and not job.deleting:
                await asyncio.to_thread(cluster.delete_restic_verification_job, namespace, job.name, uid=job.uid)
        except Exception as error:  # noqa: BLE001 -- abandon this pass and let healthy cells proceed
            runtime.refused_until[namespace] = now + config.retry
            logger.warning("cellctl: restic verification is unsettled for %s: %s", namespace, describe_error(error))
        return True  # Bound a pass to one receipt and one cleanup.

    if active or not due:
        return active
    _, namespace, previous, target = min(due, key=lambda candidate: candidate[:2])
    part = previous.next_part if previous is not None else 1
    invocation = restic_verification_invocation(
        target.image, restic_verification_command(part, config.parts, config.download_kib_per_second), target.repository,
    )
    receipt = VerificationReceipt(
        target.repository, target.statefulset_uid, invocation, config.parts, part, uuid.uuid4().hex, now,
        last_success_part=previous.last_success_part if previous is not None else None,
        last_success_at=previous.last_success_at if previous is not None else None,
    )
    try:
        await _write_receipt(cluster, namespace, receipt)
        job_uid = await asyncio.to_thread(start, namespace, receipt)
        await _write_receipt(cluster, namespace, replace(receipt, job_uid=job_uid))
    except Exception as error:  # noqa: BLE001 -- only a durably acknowledged UID can attest to a result
        runtime.refused_until[namespace] = now + config.retry
        logger.warning("cellctl: restic verification could not start in %s: %s", namespace, describe_error(error))
    return True
