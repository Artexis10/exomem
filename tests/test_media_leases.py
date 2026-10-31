"""The lease layer of the media job ledger: a remote worker's claim, heartbeat, hand-back, expiry and result."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from exomem import media_jobs

SIDECAR_HASH = "a" * 64
REPLACED_DIGEST = "b" * 64
IDENTITY = {"stat": [1, 2, 3, 4, 5], "sha256": None}
EXTRACTION = {"text": "a transcript", "engine": "fake-asr"}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The ledger's time, which a test moves by changing `clock[0]`."""
    now = [time.time()]
    monkeypatch.setattr(media_jobs.time, "time", lambda: now[0])
    return now


def _snapshot(job: media_jobs.MediaJob, **changes) -> media_jobs.LeaseSnapshot:
    return media_jobs.LeaseSnapshot(sidecar_before_hash=SIDECAR_HASH, binary_identity=IDENTITY, **changes)


def _request(worker: str = "worker-1", *, max_leases: int = 1, lease_seconds: float = 60,
             deadline_seconds: float = 600, snapshot=_snapshot) -> media_jobs.LeaseRequest:
    return media_jobs.LeaseRequest(
        worker=worker, max_leases=max_leases, lease_seconds=lease_seconds,
        deadline_seconds=deadline_seconds, snapshot=snapshot,
    )


def _enqueue(vault: Path, name: str = "talk.mp3", media_type: str = "audio", **stages) -> int:
    job = media_jobs.MediaJob(vault / name, vault / f"{name}.md", media_type, **stages)
    return media_jobs.MediaJobStore(vault).enqueue(job)


def _evict(vault: Path, name: str = "talk.mp3") -> None:
    """Mark the original as evicted from the volume, so a claim has to hydrate it."""
    with sqlite3.connect(media_jobs.job_store_path(vault)) as connection:
        connection.execute(
            "INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read, evicted) "
            "VALUES (?, 'ref', ?, 'id', 0, 1)", (name, SIDECAR_HASH),
        )


def _row(vault: Path, job_id: int) -> sqlite3.Row:
    with sqlite3.connect(media_jobs.job_store_path(vault)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _expire(store: media_jobs.MediaJobStore, **budget) -> list[tuple[media_jobs.MediaJob, str]]:
    return store.expire_leases(stop_limit=budget.get("stop_limit", 3), context="{}")


def test_a_remote_claim_pins_the_job_and_records_the_lease_with_the_callers_snapshot(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault, language="et")

    lease = store.claim_lease(
        _request(snapshot=lambda job: _snapshot(job, language="et", replaces=REPLACED_DIGEST)),
        skip_media_types=(),
    )

    assert lease is not None and lease.job.id == job_id
    assert (lease.worker, lease.claim_revision, lease.job.state, lease.job.attempts) == ("worker-1", 1, "running", 1)
    assert (lease.expires_at, lease.deadline_at) == (clock[0] + 60, clock[0] + 600)
    assert (lease.snapshot.sidecar_before_hash, lease.snapshot.language, lease.snapshot.replaces) == (
        SIDECAR_HASH, "et", REPLACED_DIGEST)
    assert _row(vault, job_id)["pinned"] == 1
    assert store.get_lease(job_id) == lease


def test_a_snapshot_that_is_not_a_digest_is_refused_before_anything_is_claimed(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)

    def bad(job: media_jobs.MediaJob) -> media_jobs.LeaseSnapshot:
        return media_jobs.LeaseSnapshot(sidecar_before_hash="not-a-digest", binary_identity=IDENTITY)

    with pytest.raises(ValueError):
        store.claim_lease(_request(snapshot=bad), skip_media_types=())

    assert _row(vault, job_id)["state"] == "pending"


def test_a_refused_job_does_not_hold_back_the_jobs_behind_it(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    withheld = _enqueue(vault, "withheld.mp3")
    released = _enqueue(vault, "released.mp3")

    lease = store.claim_lease(
        _request(snapshot=lambda job: None if job.id == withheld else _snapshot(job)), skip_media_types=()
    )

    assert lease.job.id == released
    assert (_row(vault, withheld)["state"], _row(vault, withheld)["attempts"]) == ("pending", 0)
    assert store.get_lease(withheld) is None


def test_a_job_with_an_offload_stage_is_never_leased(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault, do_offload=True)

    assert store.claim_lease(_request(), skip_media_types=()) is None

    assert store.claim_next().id == job_id  # the local worker still takes it


def test_a_job_without_an_extraction_stage_is_never_leased(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    _enqueue(vault, do_ocr=False, do_clip=True)

    assert store.claim_lease(_request(), skip_media_types=()) is None


def test_a_remote_claim_skips_the_media_kinds_the_caller_names(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    _enqueue(vault, "film.mp4", "video")
    audio = _enqueue(vault, "talk.mp3")

    lease = store.claim_lease(_request(max_leases=2), skip_media_types=("video",))

    assert lease.job.id == audio
    assert store.claim_lease(_request(max_leases=2), skip_media_types=("video", "audio")) is None


def test_a_worker_holds_no_more_leases_than_its_limit(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    _enqueue(vault, "one.mp3")
    second = _enqueue(vault, "two.mp3")

    assert store.claim_lease(_request(max_leases=1), skip_media_types=()) is not None
    assert store.claim_lease(_request(max_leases=1), skip_media_types=()) is None

    assert _row(vault, second)["state"] == "pending"


def test_a_worker_whose_lease_expired_holds_no_slot_before_the_sweep_runs(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    _enqueue(vault, "one.mp3")
    second = _enqueue(vault, "two.mp3")
    store.claim_lease(_request(max_leases=1, lease_seconds=60), skip_media_types=())
    clock[0] += 61

    assert store.claim_lease(_request(max_leases=1), skip_media_types=()).job.id == second


def test_the_limit_counts_each_workers_leases_apart(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    _enqueue(vault, "one.mp3")
    second = _enqueue(vault, "two.mp3")
    assert store.claim_lease(_request("worker-1"), skip_media_types=()) is not None

    assert store.claim_lease(_request("worker-2"), skip_media_types=()).job.id == second


def test_a_remote_claim_stops_hydrating_at_the_cap_as_a_local_claim_does(vault: Path, monkeypatch) -> None:
    monkeypatch.setattr(media_jobs, "_max_hydrations", lambda _vault: 1)
    store = media_jobs.MediaJobStore(vault)
    _enqueue(vault, "evicted-1.mp3")
    _enqueue(vault, "evicted-2.mp3")
    resident = _enqueue(vault, "resident.mp3")
    _evict(vault, "evicted-1.mp3")
    _evict(vault, "evicted-2.mp3")
    request = _request(max_leases=3)

    claimed = [store.claim_lease(request, skip_media_types=()) for _ in range(3)]

    assert claimed[0].job.id == resident  # the original on the volume comes first
    assert claimed[1].job.binary_path.name == "evicted-1.mp3"
    assert claimed[2] is None  # one hydrated claim already holds the only slot


def test_a_heartbeat_extends_the_lease(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    lease = store.claim_lease(_request(lease_seconds=60), skip_media_types=())
    clock[0] += 40

    assert store.heartbeat_lease(job_id, lease.claim_revision, "worker-1", lease_seconds=60)

    assert store.get_lease(job_id).expires_at == clock[0] + 60


def test_a_heartbeat_for_another_worker_is_refused(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    lease = store.claim_lease(_request("worker-1"), skip_media_types=())
    clock[0] += 40

    assert not store.heartbeat_lease(job_id, lease.claim_revision, "worker-2", lease_seconds=60)

    assert store.get_lease(job_id).expires_at == lease.expires_at


def test_a_heartbeat_for_a_superseded_revision_is_refused(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    first = store.claim_lease(_request(), skip_media_types=())
    clock[0] += 61
    _expire(store)
    second = store.claim_lease(_request(), skip_media_types=())

    assert not store.heartbeat_lease(job_id, first.claim_revision, "worker-1", lease_seconds=60)

    assert (second.claim_revision, store.get_lease(job_id).expires_at) == (2, second.expires_at)


def test_a_heartbeat_after_the_lease_expired_does_not_revive_it(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    lease = store.claim_lease(_request(lease_seconds=60), skip_media_types=())
    clock[0] += 61

    assert not store.heartbeat_lease(job_id, lease.claim_revision, "worker-1", lease_seconds=60)


def test_heartbeats_stop_at_the_hard_deadline(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    start = clock[0]
    lease = store.claim_lease(_request(lease_seconds=60, deadline_seconds=100), skip_media_types=())

    clock[0] = start + 50
    assert store.heartbeat_lease(job_id, lease.claim_revision, "worker-1", lease_seconds=60)
    assert store.get_lease(job_id).expires_at == start + 100  # now + 60 would pass the deadline

    clock[0] = start + 99
    assert store.heartbeat_lease(job_id, lease.claim_revision, "worker-1", lease_seconds=60)
    assert store.get_lease(job_id).expires_at == start + 100

    clock[0] = start + 100
    assert not store.heartbeat_lease(job_id, lease.claim_revision, "worker-1", lease_seconds=60)


def test_an_expired_lease_returns_its_job_and_frees_its_pin_and_hydration_slot(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    _evict(vault)
    store.claim_lease(_request(lease_seconds=60), skip_media_types=())
    assert (_row(vault, job_id)["pinned"], _row(vault, job_id)["hydrated"]) == (1, 1)
    clock[0] += 61

    returned = _expire(store)

    assert [(job.id, verdict) for job, verdict in returned] == [(job_id, media_jobs.PENDING)]
    row = _row(vault, job_id)
    assert (row["state"], row["pinned"], row["hydrated"], row["attempts"], row["timeout_stops"]) == (
        "pending", 0, 0, 0, 1)
    assert store.get_lease(job_id) is None


def test_a_lease_that_has_not_expired_is_not_swept(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    store.claim_lease(_request(lease_seconds=60), skip_media_types=())
    clock[0] += 59

    assert _expire(store) == []

    assert _row(vault, job_id)["state"] == "running"


def test_a_job_that_keeps_losing_its_worker_ends_over_budget(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    verdicts = []
    for _ in range(2):
        assert store.claim_lease(_request(), skip_media_types=()) is not None
        clock[0] += 61
        verdicts += [verdict for _job, verdict in _expire(store, stop_limit=2)]

    assert verdicts == [media_jobs.PENDING, media_jobs.OVER_BUDGET]
    row = _row(vault, job_id)
    assert (row["state"], row["blocked_reason"]) == ("blocked", media_jobs.OVER_BUDGET)
    assert store.claim_lease(_request(), skip_media_types=()) is None


def test_a_hand_back_keeps_the_pin_frees_the_slot_and_waits_a_retry_time(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    _evict(vault)
    lease = store.claim_lease(_request(), skip_media_types=())

    assert store.release_lease(job_id, lease.claim_revision, "worker-1", "gpu busy")

    row = _row(vault, job_id)
    assert (row["state"], row["pinned"], row["hydrated"], row["returns"], row["attempts"]) == ("pending", 1, 0, 1, 0)
    assert (row["retry_at"], row["last_error"]) == (clock[0] + media_jobs.RETRY_FIRST_SECONDS, "gpu busy")
    assert store.get_lease(job_id) is None
    assert store.claim_lease(_request(), skip_media_types=()) is None  # it waits for its retry time
    clock[0] += media_jobs.RETRY_FIRST_SECONDS
    assert store.claim_lease(_request(), skip_media_types=()) is not None
    assert _row(vault, job_id)["hydrated"] == 0  # the original stayed on the volume, so nothing hydrated again


def test_a_hand_back_for_an_original_too_large_blocks_the_job(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    lease = store.claim_lease(_request(), skip_media_types=())

    assert store.release_lease(job_id, lease.claim_revision, "worker-1", media_jobs.HYDRATION_TOO_LARGE)

    row = _row(vault, job_id)
    assert (row["state"], row["blocked_reason"], row["pinned"]) == ("blocked", media_jobs.HYDRATION_TOO_LARGE, 0)
    assert store.get_lease(job_id) is None


def test_a_hand_back_from_another_worker_changes_nothing(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    lease = store.claim_lease(_request("worker-1"), skip_media_types=())

    assert not store.release_lease(job_id, lease.claim_revision, "worker-2", "gpu busy")

    assert (_row(vault, job_id)["state"], store.get_lease(job_id).worker) == ("running", "worker-1")


def test_a_result_is_recorded_against_the_snapshot_and_not_the_workers_claims(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault, reprocess=True)
    lease = store.claim_lease(
        _request(snapshot=lambda job: _snapshot(job, replaces=REPLACED_DIGEST)), skip_media_types=()
    )

    assert store.record_remote_result(
        job_id, lease.claim_revision, "worker-1", "extraction", {**EXTRACTION, "replaces": "c" * 64}
    )

    [result] = store.pending_results()
    assert (result.sidecar_before_hash, result.binary_identity) == (SIDECAR_HASH, IDENTITY)
    assert result.payload["replaces"] == REPLACED_DIGEST
    assert result.payload["text"] == "a transcript"


def test_a_worker_cannot_name_an_extraction_for_a_result_to_replace(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    lease = store.claim_lease(_request(), skip_media_types=())

    assert store.record_remote_result(
        job_id, lease.claim_revision, "worker-1", "extraction", {**EXTRACTION, "replaces": REPLACED_DIGEST}
    )

    [result] = store.pending_results()
    assert "replaces" not in result.payload


def test_a_result_keeps_the_ocr_generation_of_the_grant_when_a_newer_request_came_in(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    lease = store.claim_lease(_request(), skip_media_types=())
    _enqueue(vault)  # a newer request for the same file raises the job's OCR generation

    assert store.record_remote_result(job_id, lease.claim_revision, "worker-1", "extraction", EXTRACTION)

    [result] = store.pending_results()
    assert (result.ocr_generation, _row(vault, job_id)["ocr_generation"]) == (0, 1)


def test_a_result_from_another_worker_is_refused(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    lease = store.claim_lease(_request("worker-1"), skip_media_types=())

    assert not store.record_remote_result(job_id, lease.claim_revision, "worker-2", "extraction", EXTRACTION)

    assert store.pending_results() == []


def test_a_result_for_a_superseded_revision_is_refused(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    first = store.claim_lease(_request(), skip_media_types=())
    clock[0] += 61
    _expire(store)
    store.claim_lease(_request(), skip_media_types=())

    assert not store.record_remote_result(job_id, first.claim_revision, "worker-1", "extraction", EXTRACTION)

    assert store.pending_results() == []


def test_a_second_result_for_the_same_lease_is_refused(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    lease = store.claim_lease(_request(), skip_media_types=())
    assert store.record_remote_result(job_id, lease.claim_revision, "worker-1", "extraction", EXTRACTION)

    assert not store.record_remote_result(job_id, lease.claim_revision, "worker-1", "extraction", EXTRACTION)
    assert not store.release_lease(job_id, lease.claim_revision, "worker-1", "gpu busy")

    assert _row(vault, job_id)["state"] == "running"  # the parent publishes the result it holds


def test_a_lease_whose_result_is_in_is_neither_swept_nor_counted_against_the_limit(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault, "one.mp3")
    second = _enqueue(vault, "two.mp3")
    lease = store.claim_lease(_request(max_leases=1), skip_media_types=())
    assert store.record_remote_result(job_id, lease.claim_revision, "worker-1", "extraction", EXTRACTION)
    clock[0] += 61

    assert _expire(store) == []
    assert _row(vault, job_id)["state"] == "running"

    clock[0] = lease.granted_at  # inside the lease again: the worker is free for more work
    assert store.claim_lease(_request(max_leases=1), skip_media_types=()).job.id == second


def test_a_lease_ends_when_its_job_leaves_the_running_state(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    lease = store.claim_lease(_request(), skip_media_types=())

    assert store.mark(lease.job, media_jobs.FAILED, "the engine failed")

    assert store.get_lease(job_id) is None


def test_recovery_leaves_a_job_on_a_live_lease_and_returns_a_local_claim(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    leased = _enqueue(vault, "leased.mp3")
    local = _enqueue(vault, "local.mp3")
    assert store.claim_lease(_request(), skip_media_types=()).job.id == leased
    assert store.claim_next().id == local

    assert store.recover_interrupted() == 1

    assert (_row(vault, leased)["state"], _row(vault, local)["state"]) == ("running", "pending")
    assert store.get_lease(leased) is not None


def test_recovery_returns_a_job_whose_lease_expired_and_frees_its_pin(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    _evict(vault)
    store.claim_lease(_request(lease_seconds=60), skip_media_types=())
    clock[0] += 61

    assert store.recover_interrupted() == 1

    row = _row(vault, job_id)
    assert (row["state"], row["pinned"], row["hydrated"]) == ("pending", 0, 0)
    assert store.get_lease(job_id) is None


def test_the_local_watchdog_does_not_see_a_job_on_a_remote_lease(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    _enqueue(vault, "leased.mp3")
    local = _enqueue(vault, "local.mp3")
    store.claim_lease(_request(), skip_media_types=())
    store.claim_next()

    assert [job.id for job in store.running_jobs()] == [local]


def test_a_job_on_a_live_lease_is_no_work_for_a_local_child_until_the_lease_expires(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    _enqueue(vault)
    store.claim_lease(_request(lease_seconds=60), skip_media_types=())

    assert not store.needs_worker()

    clock[0] += 61
    assert store.needs_worker()  # the child's recovery returns the expired lease's job


# ---- the verdict of the grant: what the local claim leaves to remote workers ------------------------


@pytest.mark.parametrize("change", ["enqueue", "request_extraction"])
def test_the_mark_of_a_job_no_claim_can_take_clears_when_its_input_changes(vault: Path, change: str) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)
    assert store.claim_lease(_request(snapshot=lambda job: None), skip_media_types=()) is None
    assert _row(vault, job_id)["remote_eligible"] == 0  # the claim marked it: none can take it now
    assert store.claim_lease(_request(), skip_media_types=()) is None  # and the mark holds for the next claim

    if change == "enqueue":
        _enqueue(vault)
    else:
        job = media_jobs.MediaJob(vault / "talk.mp3", vault / "talk.mp3.md", "audio")
        assert store.request_extraction(job, None, None, reprocess=False) == job_id

    assert _row(vault, job_id)["remote_eligible"] is None
    assert store.claim_lease(_request(), skip_media_types=()).job.id == job_id  # judged afresh


def test_the_local_claim_leaves_a_routed_job_to_remote_workers_unless_no_claim_can_take_it(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    _enqueue(vault, "refused.mp3")
    _enqueue(vault, "granted.mp3")
    _enqueue(vault, "film.mp4", "video")  # not a routed kind
    assert store.claim_next(routed_media_types=("audio",)).binary_path.name == "film.mp4"
    store.recover_interrupted()
    assert store.claim_next(routed_media_types=("audio",)).binary_path.name == "film.mp4"  # unjudged audio waits

    store.judge_remote_eligibility(lambda job: job.binary_path.name != "refused.mp3", ["audio"])

    store.recover_interrupted()
    taken = [store.claim_next(routed_media_types=("audio",)).binary_path.name for _ in range(2)]
    assert taken == ["refused.mp3", "film.mp4"]  # the refused one first, by claim order; granted.mp3 stays
    assert store.claim_next(routed_media_types=("audio",)) is None


def test_a_routed_job_with_an_offload_stage_stays_with_the_local_claim(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault, do_offload=True)  # no remote worker can run it, so routing must not strand it

    assert store.claim_next(routed_media_types=("audio",)).id == job_id


def test_a_verdict_read_from_an_input_that_changed_meanwhile_is_not_recorded(vault: Path, clock) -> None:
    store = media_jobs.MediaJobStore(vault)
    job_id = _enqueue(vault)

    def grantable(job: media_jobs.MediaJob) -> bool:
        clock[0] += 1
        _enqueue(vault)  # the input changes while the grant is read
        return False

    assert store.judge_remote_eligibility(grantable, ["audio"]) == 0

    assert _row(vault, job_id)["remote_eligible"] is None
