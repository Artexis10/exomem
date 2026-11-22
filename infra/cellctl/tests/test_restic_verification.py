"""Restart, replacement and priority consequences of the Cloud verifier."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import asyncpg

from cellctl import db, reconcile
from cellctl.main import build_restic_verification
from cellctl.manifests import (
    JOB_KIND_LABEL,
    JOB_KIND_RESTIC_VERIFY,
    RESTIC_VERIFICATION_ANNOTATION,
    namespace_name,
)
from cellctl.restic_verification import (
    VerificationConfig,
    VerificationReceipt,
    VerificationRuntime,
    VerificationTarget,
    sync_verifications,
)
from cellctl.state import ResticVerificationJob

from .test_reconcile import (
    FakeClusterGateway,
    FakeHetznerVolumeProvider,
    _cluster_config,
    _converged_cell,
    _observed_from_applied,
    _secrets_config,
    _seed_cell,
)

CELL = "aaaaaaaaaaaaaaaa"
OTHER = "bbbbbbbbbbbbbbbb"
NAMESPACE = namespace_name(CELL)
OTHER_NAMESPACE = namespace_name(OTHER)
REPOSITORY = "a" * 64
OTHER_REPOSITORY = "b" * 64
NOW = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = VerificationConfig(cell_ids=(CELL, OTHER), parts=2, interval=timedelta(minutes=2), retry=timedelta(minutes=1))


class FakeVerifier:
    def __init__(self) -> None:
        self.annotations: dict[str, str] = {}
        self.statefulset_uids = {NAMESPACE: "first-statefulset", OTHER_NAMESPACE: "other-statefulset"}
        self.jobs: dict[str, tuple[ResticVerificationJob, ...]] = {}
        self.operations: list[tuple] = []
        self.calls = 0
        self.observe_error: Exception | None = None
        self.runtime = VerificationRuntime()

    def observe_restic_verifications(self):
        self.calls += 1
        if self.observe_error:
            raise self.observe_error
        return self.jobs.copy()

    def receipt(self, namespace: str = NAMESPACE):
        return VerificationReceipt.parse(self.annotations.get(namespace))

    def patch_statefulset_annotations(self, namespace, patch, *, uid):
        if uid != self.statefulset_uids[namespace]:
            raise ValueError("metadata.uid is immutable")
        self.annotations[namespace] = patch[RESTIC_VERIFICATION_ANNOTATION]
        self.operations.append(("receipt", namespace, self.receipt(namespace).outcome))

    def delete_restic_verification_job(self, namespace, name, *, uid):
        self.operations.append(("delete", namespace, name, uid))

    def start(self, namespace, receipt):
        assert self.receipt(namespace) == receipt and not receipt.job_uid
        self.operations.append(("start", namespace, receipt.part))
        return "job-uid"

    async def sync(self, *, now=NOW, include=(), policy=POLICY, allow_start=True):
        repositories = {NAMESPACE: REPOSITORY, OTHER_NAMESPACE: OTHER_REPOSITORY}
        return await sync_verifications(
            self, policy, now=now, allow_start=allow_start, start=self.start, runtime=self.runtime,
            targets={
                namespace: VerificationTarget(
                    repository, namespace in include, self.annotations.get(namespace),
                    self.statefulset_uids[namespace], "fixture-image",
                )
                for namespace, repository in repositories.items()
            },
        )


def observed(receipt, *, outcome=None, uid="job-uid", active=False, deleting=False):
    return ResticVerificationJob(
        receipt.job_name, uid, receipt.statefulset_uid, receipt.invocation, active, deleting, outcome,
        pod_uid="pod-uid" if outcome == "success" else None,
        finished_at=NOW if outcome == "success" else None,
    )


def test_verifier_has_an_explicit_default_off_selection(monkeypatch):
    monkeypatch.delenv("CELLCTL_RESTIC_VERIFICATION_CELL_IDS", raising=False)
    assert build_restic_verification().cell_ids == ()
    monkeypatch.setenv("CELLCTL_RESTIC_VERIFICATION_CELL_IDS", '["aaaaaaaaaaaaaaaa"]')
    assert build_restic_verification().cell_ids == (CELL,)


async def test_one_fleet_flight_blocks_a_second_eligible_cell():
    cluster = FakeVerifier()
    await cluster.sync(include=(NAMESPACE, OTHER_NAMESPACE))
    first = cluster.receipt()
    assert first.job_uid == "job-uid"
    cluster.jobs[NAMESPACE] = (observed(first, active=True),)

    assert await cluster.sync(include=(NAMESPACE, OTHER_NAMESPACE))
    assert [operation for operation in cluster.operations if operation[0] == "start"] == [("start", NAMESPACE, 1)]
    assert cluster.receipt(OTHER_NAMESPACE) is None


async def test_restart_preserves_last_success_and_retries_the_failed_slice_before_wrapping():
    cluster = FakeVerifier()
    await cluster.sync(include=(NAMESPACE,))
    first = cluster.receipt()
    cluster.jobs[NAMESPACE] = (observed(first, outcome="success"),)
    await cluster.sync(include=(NAMESPACE,))
    assert cluster.operations[-2:] == [
        ("receipt", NAMESPACE, "success"), ("delete", NAMESPACE, first.job_name, "job-uid"),
    ]
    assert cluster.receipt().pod_uid == "pod-uid"
    await cluster.sync(now=NOW + POLICY.interval, include=(NAMESPACE,))
    second = cluster.receipt()
    assert second.part == 2 and second.last_success_at == NOW

    restarted = FakeVerifier()
    restarted.annotations = cluster.annotations.copy()
    restarted.jobs[NAMESPACE] = (observed(second, outcome="failed"),)
    failed_at = NOW + POLICY.interval + timedelta(seconds=10)
    await restarted.sync(now=failed_at, include=(NAMESPACE,))
    failed = restarted.receipt()
    assert (failed.outcome, failed.next_part, failed.last_success_part, failed.last_success_at) == ("failed", 2, 1, NOW)
    await restarted.sync(now=failed_at + POLICY.retry - timedelta(seconds=1), include=(NAMESPACE,))
    assert not any(operation[0] == "start" for operation in restarted.operations)

    await restarted.sync(now=failed_at + POLICY.retry, include=(NAMESPACE,))
    retry = restarted.receipt()
    assert retry.part == 2 and retry.attempt_id != second.attempt_id
    restarted.jobs[NAMESPACE] = (observed(retry, outcome="success"),)
    await restarted.sync(now=failed_at + POLICY.retry, include=(NAMESPACE,))
    assert restarted.receipt().next_part == 1


async def test_disabled_controller_still_observes_orphans_without_deleting_them():
    cluster = FakeVerifier()
    await cluster.sync(include=(NAMESPACE,))
    first = cluster.receipt()
    disabled = replace(POLICY, cell_ids=())

    restarted = FakeVerifier()
    restarted.jobs[NAMESPACE] = (observed(first, uid=None, active=True),)
    restarted.jobs[OTHER_NAMESPACE] = (observed(first, uid="foreign-job", outcome="success"),)
    assert await restarted.sync(policy=disabled)
    assert restarted.operations == []
    assert restarted.calls == 1

    restarted.jobs.clear()
    assert not await restarted.sync(policy=disabled)
    assert not await restarted.sync(policy=disabled)
    assert restarted.calls == 2


async def test_an_unavailable_fleet_read_starts_no_optional_work():
    cluster = FakeVerifier()
    cluster.observe_error = OSError("fixture API timeout")

    assert not await cluster.sync(include=(NAMESPACE, OTHER_NAMESPACE))
    assert cluster.operations == []


async def test_a_same_name_replacement_cannot_attest_to_a_fenced_receipt():
    cluster = FakeVerifier()
    await cluster.sync(include=(NAMESPACE,))
    attempt = cluster.receipt()
    cluster.jobs[NAMESPACE] = (observed(attempt, uid="wrong-uid", outcome="success"),)

    await cluster.sync(include=(NAMESPACE,))

    receipt = cluster.receipt()
    assert (receipt.outcome, receipt.job_uid, receipt.next_part, receipt.last_success_at) == ("unknown", "job-uid", 1, None)
    assert not any(operation[0] == "delete" for operation in cluster.operations)


async def test_an_uncertain_create_never_adopts_its_job_on_restart():
    class LostResponse(FakeVerifier):
        def start(self, namespace, receipt):
            self.jobs[namespace] = (observed(receipt, outcome="success"),)
            raise OSError("the API response was lost")

    original = LostResponse()
    await original.sync(include=(NAMESPACE,))

    restarted = FakeVerifier()
    restarted.annotations = original.annotations.copy()
    restarted.jobs = original.jobs.copy()
    await restarted.sync(include=(NAMESPACE,))

    receipt = restarted.receipt()
    assert (receipt.outcome, receipt.job_uid, receipt.next_part) == ("unknown", None, 1)
    assert not any(operation[0] == "delete" for operation in restarted.operations)


async def test_a_refused_cell_does_not_starve_the_next_due_cell():
    class Refusal(FakeVerifier):
        def patch_statefulset_annotations(self, namespace, patch, *, uid):
            if namespace == NAMESPACE:
                raise OSError("the cell refuses receipt writes")
            super().patch_statefulset_annotations(namespace, patch, uid=uid)

    cluster = Refusal()
    await cluster.sync(include=(NAMESPACE, OTHER_NAMESPACE))
    await cluster.sync(include=(NAMESPACE, OTHER_NAMESPACE))

    assert cluster.receipt() is None
    assert cluster.receipt(OTHER_NAMESPACE).job_uid == "job-uid"


async def test_a_refused_terminal_receipt_leaves_the_evidence_and_releases_the_slot():
    class Refusal(FakeVerifier):
        def patch_statefulset_annotations(self, namespace, patch, *, uid):
            if namespace == NAMESPACE and VerificationReceipt.parse(patch[RESTIC_VERIFICATION_ANNOTATION]).outcome == "success":
                raise OSError("the cell refuses terminal writes")
            super().patch_statefulset_annotations(namespace, patch, uid=uid)

    cluster = Refusal()
    await cluster.sync(include=(NAMESPACE, OTHER_NAMESPACE))
    first = cluster.receipt()
    cluster.jobs[NAMESPACE] = (observed(first, outcome="success"),)
    await cluster.sync(include=(NAMESPACE, OTHER_NAMESPACE))
    await cluster.sync(include=(NAMESPACE, OTHER_NAMESPACE))

    assert cluster.receipt() == first
    assert not any(operation[0] == "delete" for operation in cluster.operations)
    assert cluster.receipt(OTHER_NAMESPACE).job_uid == "job-uid"


async def test_copied_receipt_cannot_advance_the_replacement_statefulset():
    cluster = FakeVerifier()
    await cluster.sync(include=(NAMESPACE,))
    first = cluster.receipt()
    cluster.jobs[NAMESPACE] = (observed(first, outcome="success"),)
    cluster.statefulset_uids[NAMESPACE] = "replacement-statefulset"  # The replacement retains the old annotation.

    await cluster.sync(include=(NAMESPACE,))

    latest = cluster.receipt()
    assert latest.statefulset_uid == "replacement-statefulset" and latest.attempt_id != first.attempt_id
    assert latest.part == 1 and latest.last_success_at is None
    assert not any(operation[0] == "delete" for operation in cluster.operations)


async def test_statefulset_replaced_during_observation_never_receives_the_old_success():
    class ReplacingCluster(FakeVerifier):
        replace_on_observation = False

        def observe_restic_verifications(self):
            if self.replace_on_observation:
                self.statefulset_uids[NAMESPACE] = "replacement-statefulset"
                self.annotations.pop(NAMESPACE)
            return super().observe_restic_verifications()

    cluster = ReplacingCluster()
    await cluster.sync(include=(NAMESPACE,))
    attempt = cluster.receipt()
    cluster.jobs[NAMESPACE] = (observed(attempt, outcome="success"),)
    before_replacement = list(cluster.operations)
    cluster.replace_on_observation = True

    await cluster.sync(include=(NAMESPACE,))

    assert cluster.receipt() is None and cluster.operations == before_replacement


async def test_reconcile_does_not_wait_for_a_verifier_before_nightly_backup(cell_db):
    class Cluster(FakeClusterGateway):
        verifiers = ()

        def observe(self, cell_id, namespace):
            observation = super().observe(cell_id, namespace)
            annotations = self.applied.get((namespace, "StatefulSet", "cell"), {}).get("metadata", {}).get("annotations", {})
            return replace(
                observation, statefulset_restic_verification=annotations.get(RESTIC_VERIFICATION_ANNOTATION),
                statefulset_uid="fixture-statefulset" if observation.statefulset_exists else None,
            )

        def patch_statefulset_annotations(self, namespace, annotations, *, uid=None):
            if RESTIC_VERIFICATION_ANNOTATION in annotations:
                assert uid == "fixture-statefulset"
            super().patch_statefulset_annotations(namespace, annotations)

        def observe_restic_verifications(self):
            return {NAMESPACE: self.verifiers}

        def create_restic_verification_job(self, manifest):
            self.run_job(manifest)
            return "job-uid"

        def delete_restic_verification_job(self, namespace, name, *, uid):
            self.events.append(("delete_verifier", namespace, name, uid))

    await _seed_cell(cell_db, CELL, "backup-priority")
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = Cluster()
    noon = NOW + timedelta(hours=12)
    window = NOW + timedelta(days=1, hours=2)
    config = replace(_cluster_config(), restic_verification=VerificationConfig(cell_ids=(CELL,)))
    previous = "a" * 64
    try:
        await _converged_cell(connection, cluster, noon)
        await db.write_observed(connection, CELL, {"last_backup_at": NOW, "last_backup_snapshot": previous})
        await reconcile.reconcile_once(connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(), config, now=noon)
        launched, = [job for job in cluster.jobs if job["metadata"]["labels"].get(JOB_KIND_LABEL) == JOB_KIND_RESTIC_VERIFY]
        attempt = VerificationReceipt.parse(cluster.observe(CELL, NAMESPACE).statefulset_restic_verification)
        assert launched["metadata"]["name"] == attempt.job_name
        cluster.verifiers = (observed(attempt, active=True),)

        await reconcile.reconcile_once(connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(), config, now=window)
        assert ("delete_verifier", NAMESPACE, attempt.job_name, "job-uid") in cluster.events
        starting, = await db.select_all_rows(connection)
        assert starting.hold_kind == "backup"
        assert starting.last_backup_snapshot == previous and starting.last_backup_at == NOW
        assert cluster.applied[(NAMESPACE, "StatefulSet", "cell")]["spec"]["replicas"] == 0
        cluster.observations[CELL] = _observed_from_applied(cluster, CELL, pod_exists=False, pod_uses_volume=False, pod_ready=False)
        await reconcile.reconcile_once(
            connection, cluster, FakeHetznerVolumeProvider(), _secrets_config(), config, now=window + timedelta(minutes=1),
        )
        assert any(job["metadata"]["labels"].get(JOB_KIND_LABEL) == "backup" for job in cluster.jobs)
    finally:
        await connection.close()
