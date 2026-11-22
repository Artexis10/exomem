"""The Cloud verifier's one recorded attempt and fleet scheduling."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from .errors import describe_error
from .manifests import (
    RESTIC_VERIFICATION_ANNOTATION,
    RESTIC_VERIFICATION_MAX_PARTS,
    cell_id_from_namespace,
    namespace_name,
    restic_verification_job_name,
)
from .state import ResticVerificationJob

logger = logging.getLogger(__name__)
Outcome = Literal["pending", "success", "failed", "busy", "unknown"]
# Closed fields of the version 1 annotation. All times are UTC observations.
_TIMES = ("started_at", "observed_at", "due_at", "last_success_at")


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
    total: int
    part: int
    attempt_id: str
    started_at: datetime
    outcome: Outcome = "pending"
    job_uid: str | None = None
    observed_at: datetime | None = None
    due_at: datetime | None = None
    last_success_part: int | None = None
    last_success_at: datetime | None = None
    cleanup_pending: bool = False

    def __post_init__(self) -> None:
        if type(self.part) is not int or type(self.total) is not int or not (
            1 <= self.part <= self.total <= RESTIC_VERIFICATION_MAX_PARTS
        ):
            raise ValueError("invalid verification partition")
        if not isinstance(self.repository, str) or len(self.repository) != 64 or len(bytes.fromhex(self.repository)) != 32:
            raise ValueError("invalid verification repository fingerprint")
        if not isinstance(self.attempt_id, str) or uuid.UUID(self.attempt_id).hex != self.attempt_id:
            raise ValueError("invalid verification attempt ID")
        if self.job_uid is not None and (not isinstance(self.job_uid, str) or not self.job_uid):
            raise ValueError("invalid verification Job UID")
        # These are the closed result states of the receipt protocol.
        if self.outcome not in ("pending", "success", "failed", "busy", "unknown") or type(self.cleanup_pending) is not bool:
            raise ValueError("invalid verification outcome")
        if self.started_at is None or (self.last_success_part is None) != (self.last_success_at is None):
            raise ValueError("invalid verification receipt times")
        if self.last_success_part is not None and (
            type(self.last_success_part) is not int or not 1 <= self.last_success_part <= self.total
        ):
            raise ValueError("invalid verified partition")
        if self.outcome == "pending":
            if self.due_at is not None or self.observed_at is not None or self.cleanup_pending:
                raise ValueError("invalid pending verification")
        elif self.due_at is None or self.observed_at is None or (self.cleanup_pending and self.job_uid is None):
            raise ValueError("incomplete terminal verification")
        if self.outcome == "success" and (
            self.job_uid is None or self.last_success_part != self.part or self.last_success_at != self.observed_at
        ):
            raise ValueError("unproven verification success")

    @classmethod
    def parse(cls, raw: str | None) -> VerificationReceipt | None:
        if not raw:
            return None
        try:
            fields = json.loads(raw)
            if not isinstance(fields, dict):
                return None
            version = fields.pop("version", None)
            if type(version) is not int or version != 1:
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
        return json.dumps({"version": 1, **fields}, separators=(",", ":"), sort_keys=True)

    @property
    def job_name(self) -> str:
        return restic_verification_job_name(self.attempt_id)

    @property
    def next_part(self) -> int:
        return self.part % self.total + 1 if self.outcome == "success" else self.part

    @property
    def needs_observation(self) -> bool:
        return self.outcome == "pending" or self.cleanup_pending

    @property
    def might_still_run(self) -> bool:
        """Only unfinished or interrupted attempts can own the lock during an observation outage."""
        return self.outcome == "pending" or (self.outcome == "unknown" and self.cleanup_pending)

    def matches(self, job: ResticVerificationJob) -> bool:
        return (
            job.uid is not None and job.name == self.job_name
            and (self.job_uid is None or self.job_uid == job.uid)
            and (job.repository, job.part, job.total) == (self.repository, self.part, self.total)
        )

    def settle(
        self, job: ResticVerificationJob | None, *, same_repository: bool, now: datetime, config: VerificationConfig,
    ) -> VerificationReceipt:
        exact = job is not None and self.matches(job)
        result = job.outcome if exact and same_repository and not job.active and not job.deleting else None
        outcome = result or "unknown"
        interval = config.interval if outcome == "success" else config.retry
        return replace(
            self, outcome=outcome, job_uid=job.uid if exact else self.job_uid, observed_at=now, due_at=now + interval,
            last_success_part=self.part if outcome == "success" else self.last_success_part,
            last_success_at=now if outcome == "success" else self.last_success_at,
            cleanup_pending=exact,
        )


@dataclass(frozen=True)
class VerificationTarget:
    repository: str
    eligible: bool
    annotation: str | None
    statefulset_uid: str | None


async def _write_receipt(cluster, namespace: str, target: VerificationTarget, receipt: VerificationReceipt) -> None:
    if not target.statefulset_uid:
        raise ValueError("the StatefulSet UID is unknown")
    await asyncio.to_thread(
        cluster.patch_statefulset_annotations, namespace, {RESTIC_VERIFICATION_ANNOTATION: receipt.annotation()},
        uid=target.statefulset_uid,
    )


async def sync_verifications(
    cluster, config: VerificationConfig, *, targets: dict[str, VerificationTarget], now: datetime,
    allow_start: bool, start,
) -> tuple[frozenset[str], bool]:
    """Settle the labelled flight, then start at most one oldest due cell.

    Return the namespaces whose repository must drain and whether to poll fast.
    The caller holds the controller's database advisory lock for this pass.
    """
    records = {namespace: VerificationReceipt.parse(target.annotation) for namespace, target in targets.items()}
    pending = {namespace for namespace, record in records.items() if record and record.needs_observation}
    if not config.cell_ids and not pending:
        return frozenset(), False
    try:
        observed = await asyncio.to_thread(cluster.observe_restic_verifications)
    except Exception as error:  # noqa: BLE001 -- optional verifier observation must not take down other work
        logger.warning("cellctl: restic verification observation is unknown: %s", describe_error(error))
        return frozenset(namespace for namespace, record in records.items() if record and record.might_still_run), bool(pending)

    # An orphan already at a terminal Pod phase neither owns the lock nor has a Job to delete.
    observed = {namespace: live for namespace, jobs in observed.items()
                if (live := tuple(job for job in jobs if job.uid or job.active or job.deleting))}
    blocked = {
        namespace for namespace, jobs in observed.items()
        if any(job.active or job.deleting for job in jobs)
    }
    for namespace in pending | observed.keys():
        jobs = observed.get(namespace, ())
        record = records.get(namespace)
        target = targets.get(namespace)
        current = next((job for job in jobs if record is not None and job.name == record.job_name), None)
        retain = None
        try:
            if record is not None and target is not None and namespace in pending:
                if record.cleanup_pending:
                    if current is None or current.uid not in (None, record.job_uid):
                        await _write_receipt(cluster, namespace, target, replace(record, cleanup_pending=False))
                else:
                    same_repository = record.repository == target.repository
                    if current and current.active and not current.deleting and target.eligible and (
                        same_repository and record.total == config.parts and record.matches(current)
                    ):
                        if record.job_uid is None:
                            await _write_receipt(cluster, namespace, target, replace(record, job_uid=current.uid))
                        retain = current
                    else:
                        record = record.settle(current, same_repository=same_repository, now=now, config=config)
                        await _write_receipt(cluster, namespace, target, record)
                        logger.info(
                            "cellctl: restic verification %s %s slice %d/%d at %s",
                            namespace, record.outcome, record.part, record.total, now.isoformat(),
                        )
            # Foreground cleanup follows the receipt write. Orphan Pods still occupy the slot.
            for job in jobs:
                if job is not retain and job.uid is not None and not job.deleting:
                    await asyncio.to_thread(
                        cluster.delete_restic_verification_job, namespace, job.name, uid=job.uid,
                    )
        except Exception as error:  # noqa: BLE001 -- preserve other namespaces and retry the current attempt
            logger.warning("cellctl: restic verification is unsettled for %s: %s", namespace, describe_error(error))

    if pending or observed or not allow_start:
        return frozenset(blocked), bool(pending or observed)

    candidates = []
    for cell_id in config.cell_ids:
        namespace = namespace_name(cell_id)
        target = targets.get(namespace)
        if not target or not target.eligible or not target.statefulset_uid:
            continue
        record = records.get(namespace)
        if record is not None and (record.repository != target.repository or record.total != config.parts):
            record = None
        due = record.due_at if record is not None else None
        if due is None or due <= now:
            candidates.append((due or datetime.min.replace(tzinfo=UTC), namespace, record, target))
    if not candidates:
        return frozenset(), False
    _, namespace, previous, target = min(candidates, key=lambda candidate: candidate[:2])
    receipt = VerificationReceipt(
        target.repository, config.parts, previous.next_part if previous is not None else 1, uuid.uuid4().hex, now,
        last_success_part=previous.last_success_part if previous is not None else None,
        last_success_at=previous.last_success_at if previous is not None else None,
    )
    try:
        await _write_receipt(cluster, namespace, target, receipt)
        await asyncio.to_thread(start, namespace, receipt)
    except Exception as error:  # noqa: BLE001 -- a later observation settles an uncertain create
        logger.warning("cellctl: restic verification could not start in %s: %s", namespace, describe_error(error))
    return frozenset({namespace}), True
