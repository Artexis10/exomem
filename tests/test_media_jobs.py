from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from exomem import media_jobs
from exomem.media_worker_child import _VaultLock


def _cantopen() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("unable to open database file")
    error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
    return error


def _job(
    vault: Path,
    *,
    name: str = "item.mp4",
    ocr: bool = True,
    clip: bool = False,
) -> media_jobs.MediaJob:
    binary = vault / "Knowledge Base" / "Evidence" / name
    sidecar = binary.with_name(binary.name + ".md")
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"x")
    sidecar.write_text("---\nmedia_type: video\n---\n", encoding="utf-8")
    return media_jobs.MediaJob(
        binary_path=binary,
        sidecar_path=sidecar,
        media_type="audio" if binary.suffix == ".mp3" else "video",
        do_ocr=ocr,
        do_clip=clip,
    )


def test_status_does_not_create_store(vault: Path) -> None:
    path = media_jobs.job_store_path(vault)
    assert not path.exists()
    status = media_jobs.status(vault)
    assert status["counts"]["pending"] == 0
    assert not path.exists()


def test_readonly_connection_uses_percent_safe_sqlite_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault #% üll"
    store = media_jobs.MediaJobStore(vault)
    path = media_jobs.job_store_path(vault)
    real_connect = sqlite3.connect
    calls: list[tuple[object, dict[str, object]]] = []

    def capture_connect(database, *args, **kwargs):
        calls.append((database, kwargs.copy()))
        return real_connect(path)

    monkeypatch.setattr(media_jobs.sqlite3, "connect", capture_connect)

    conn = store._connect(readonly=True)
    conn.close()

    [(database, kwargs)] = calls
    assert database == f"{path.resolve().as_uri()}?mode=ro"
    assert kwargs["uri"] is True


def test_status_diagnostic_snapshot_uses_only_stable_immutable_database(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_jobs.MediaJobStore(vault)
    path = media_jobs.job_store_path(vault)
    real_connect = sqlite3.connect
    calls: list[str] = []

    def record_connect(database, *args, **kwargs):
        uri = str(database)
        calls.append(uri)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(media_jobs.sqlite3, "connect", record_connect)

    snapshot = media_jobs.status(vault, diagnostic_snapshot=True)

    assert snapshot["healthy"] is True
    assert len(calls) == 1
    assert "mode=ro" in calls[0] and "immutable=1" in calls[0]
    assert not path.with_name(f"{path.name}-wal").exists()
    assert not path.with_name(f"{path.name}-shm").exists()


def test_normal_status_does_not_use_immutable_fallback(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_jobs.MediaJobStore(vault)
    real_connect = sqlite3.connect
    calls: list[str] = []

    def cantopen_readonly(database, *args, **kwargs):
        uri = str(database)
        calls.append(uri)
        if "mode=ro" in uri:
            raise _cantopen()
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(media_jobs.sqlite3, "connect", cantopen_readonly)

    snapshot = media_jobs.status(vault)

    assert snapshot["healthy"] is False
    assert len(calls) == 1
    assert "immutable=1" not in calls[0]


@pytest.mark.parametrize("live_suffix", ["-wal", "-shm"])
def test_diagnostic_snapshot_refuses_immutable_with_live_sqlite_sidecar(
    vault: Path, monkeypatch: pytest.MonkeyPatch, live_suffix: str
) -> None:
    media_jobs.MediaJobStore(vault)
    path = media_jobs.job_store_path(vault)
    companion = path.with_name(f"{path.name}{live_suffix}")
    companion.write_bytes(b"live")
    before = companion.read_bytes(), companion.stat()
    calls: list[str] = []

    def cantopen_readonly(database, *args, **kwargs):
        calls.append(str(database))
        raise _cantopen()

    monkeypatch.setattr(media_jobs.sqlite3, "connect", cantopen_readonly)

    snapshot = media_jobs.status(vault, diagnostic_snapshot=True)

    assert snapshot["healthy"] is False
    assert calls == []
    content, info = before
    after = companion.stat()
    assert companion.read_bytes() == content
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
    )


def test_diagnostic_snapshot_refuses_immutable_when_main_database_is_not_readable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_jobs.MediaJobStore(vault)
    path = media_jobs.job_store_path(vault)
    real_open = Path.open
    calls: list[str] = []

    def cantopen_readonly(database, *args, **kwargs):
        calls.append(str(database))
        raise _cantopen()

    def deny_main_database(self: Path, *args, **kwargs):
        if self == path and args and args[0] == "rb":
            raise PermissionError("database bytes denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(media_jobs.sqlite3, "connect", cantopen_readonly)
    monkeypatch.setattr(Path, "open", deny_main_database)

    snapshot = media_jobs.status(vault, diagnostic_snapshot=True)

    assert snapshot["healthy"] is False
    assert calls == []


def test_diagnostic_snapshot_refuses_identity_drift_after_immutable_queries(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_jobs.MediaJobStore(vault)
    path = media_jobs.job_store_path(vault)
    real_connect = sqlite3.connect

    class DriftingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

        def close(self) -> None:
            self.connection.close()
            with path.open("ab") as stream:
                stream.write(b"drift")

    calls: list[str] = []

    def immutable_then_drift(database, *args, **kwargs):
        calls.append(str(database))
        return DriftingConnection(real_connect(database, *args, **kwargs))

    monkeypatch.setattr(media_jobs.sqlite3, "connect", immutable_then_drift)

    snapshot = media_jobs.status(vault, diagnostic_snapshot=True)

    assert snapshot["healthy"] is False
    assert len(calls) == 1 and "immutable=1" in calls[0]


def test_diagnostic_snapshot_refuses_sidecar_created_during_immutable_query(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_jobs.MediaJobStore(vault)
    path = media_jobs.job_store_path(vault)
    wal = path.with_name(f"{path.name}-wal")
    real_connect = sqlite3.connect

    class SidecarCreatingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

        def execute(self, statement: str, *args, **kwargs):
            result = self.connection.execute(statement, *args, **kwargs)
            if statement.startswith("SELECT state, count"):
                wal.write_bytes(b"appeared during snapshot")
            return result

    calls: list[str] = []

    def immutable_then_create_sidecar(database, *args, **kwargs):
        calls.append(str(database))
        return SidecarCreatingConnection(real_connect(database, *args, **kwargs))

    monkeypatch.setattr(media_jobs.sqlite3, "connect", immutable_then_create_sidecar)

    snapshot = media_jobs.status(vault, diagnostic_snapshot=True)

    assert snapshot["healthy"] is False
    assert wal.exists()
    assert len(calls) == 1 and "immutable=1" in calls[0]


def test_pid_alive_handles_current_and_missing_processes() -> None:
    assert media_jobs.pid_alive(os.getpid()) is True
    assert media_jobs.pid_alive(2_147_483_647) is False


def test_enqueue_deduplicates_and_merges_stages(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    first = store.enqueue(_job(vault, ocr=True, clip=False))
    second = store.enqueue(_job(vault, ocr=False, clip=True))
    assert first == second

    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.do_ocr is True
    assert claimed.do_clip is True
    assert store.claim_next() is None


def test_has_binary_uses_exact_vault_relative_path(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job = _job(vault, name="exact.mp3")
    store.enqueue(job)
    sibling = job.binary_path.with_name("exact-copy.mp3")
    sibling.write_bytes(b"x")

    assert store.has_binary(job.binary_path) is True
    assert store.has_binary(sibling) is False


def test_has_binary_uses_binary_relative_index(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    conn = store._connect(readonly=True)
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM jobs WHERE binary_rel = ? LIMIT 1",
            ("Knowledge Base/Evidence/exact.mp3",),
        ).fetchall()
    finally:
        conn.close()

    assert any("jobs_binary_rel" in str(row[3]) for row in plan)


def test_discovery_cursor_is_durable_and_vault_relative(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    binary = _job(vault, name="cursor.mp3").binary_path

    store.set_discovery_cursor(binary)

    reopened = media_jobs.MediaJobStore(vault, create=False)
    assert reopened.discovery_cursor() == binary.relative_to(vault).as_posix()


def test_recover_and_retry_states(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(_job(vault))
    claimed = store.claim_next()
    assert claimed is not None and claimed.id is not None

    assert store.recover_interrupted() == 1
    claimed = store.claim_next()
    assert claimed is not None and claimed.id is not None
    store.mark(claimed.id, media_jobs.BLOCKED, "missing engine")
    assert store.counts()["blocked"] == 1
    assert store.retry() == 1
    assert store.counts()["pending"] == 1


def test_status_reports_actionable_per_path_failure_details(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    blocked = _job(vault, name="missing-engine.mp3")
    failed = _job(vault, name="corrupt-audio.mp3")
    store.enqueue(blocked)
    blocked_claim = store.claim_next()
    assert blocked_claim is not None and blocked_claim.id is not None
    store.mark(blocked_claim.id, media_jobs.BLOCKED, "ExtractionUnavailable: engine absent")
    store.enqueue(failed)
    failed_claim = store.claim_next()
    assert failed_claim is not None and failed_claim.id is not None
    store.mark(failed_claim.id, media_jobs.FAILED, "InvalidDataError: corrupt container")

    jobs = {job["path"]: job for job in media_jobs.status(vault)["jobs"]}

    assert jobs["Knowledge Base/Evidence/missing-engine.mp3"] == {
        "id": blocked_claim.id,
        "path": "Knowledge Base/Evidence/missing-engine.mp3",
        "sidecar_path": "Knowledge Base/Evidence/missing-engine.mp3.md",
        "media_type": "audio",
        "state": "blocked",
        "attempts": 1,
        "error": "ExtractionUnavailable: engine absent",
        "retryable": True,
        "next_action": "install the required media dependency, then retry",
    }
    assert jobs["Knowledge Base/Evidence/corrupt-audio.mp3"] == {
        "id": failed_claim.id,
        "path": "Knowledge Base/Evidence/corrupt-audio.mp3",
        "sidecar_path": "Knowledge Base/Evidence/corrupt-audio.mp3.md",
        "media_type": "audio",
        "state": "failed",
        "attempts": 1,
        "error": "InvalidDataError: corrupt container",
        "retryable": True,
        "next_action": "repair or replace the media artifact, then retry",
    }


@pytest.mark.parametrize("target_state", [media_jobs.BLOCKED, media_jobs.FAILED])
def test_targeted_retry_requeues_only_the_exact_terminal_job(
    vault: Path, target_state: str
) -> None:
    store = media_jobs.MediaJobStore(vault)
    blocked = _job(vault, name="blocked.mp3")
    failed = _job(vault, name="failed.mp3")
    blocked_id = store.enqueue(blocked)
    blocked_claim = store.claim_next()
    assert blocked_claim is not None and blocked_claim.id == blocked_id
    store.mark(blocked_id, media_jobs.BLOCKED, "engine absent")
    failed_id = store.enqueue(failed)
    failed_claim = store.claim_next()
    assert failed_claim is not None and failed_claim.id == failed_id
    store.mark(failed_id, media_jobs.FAILED, "corrupt container")

    target = blocked if target_state == media_jobs.BLOCKED else failed
    untouched = failed if target_state == media_jobs.BLOCKED else blocked
    assert store.retry(binary_path=target.binary_path, include_failed=True) == 1

    jobs = {job["path"]: job for job in media_jobs.status(vault)["jobs"]}
    target_status = jobs[target.binary_path.relative_to(vault).as_posix()]
    untouched_status = jobs[untouched.binary_path.relative_to(vault).as_posix()]
    assert target_status["id"] == (blocked_id if target is blocked else failed_id)
    assert target_status["state"] == media_jobs.PENDING
    assert target_status["attempts"] == 1
    assert target_status["error"] is None
    assert untouched_status["state"] == (
        media_jobs.FAILED if untouched is failed else media_jobs.BLOCKED
    )
    assert sum(store.counts().values()) == 2


def test_duplicate_enqueue_does_not_implicitly_retry_terminal_job(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    job = _job(vault, name="retained-failure.mp3")
    job_id = store.enqueue(job)
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == job_id
    store.mark(job_id, media_jobs.FAILED, "DecodeError: retained failure")

    assert store.enqueue(job) == job_id

    [retained] = media_jobs.status(vault)["jobs"]
    assert retained["state"] == media_jobs.FAILED
    assert retained["attempts"] == 1
    assert retained["error"] == "DecodeError: retained failure"
    assert store.claim_next() is None


def test_transient_coordination_failures_recover_without_consuming_attempts(
    vault: Path,
) -> None:
    store = media_jobs.MediaJobStore(vault)
    transient_errors = (
        "OpError: WRITER_LEASE_REQUIRED: replica is read-only; current writer is desktop",
        "OpError: MUTATION_BUSY: vault mutation boundary is busy",
        'OpError: {"code":"WRITER_COORDINATOR_UNAVAILABLE","message":"offline"}',
    )
    transient_ids: list[int] = []
    for index, error in enumerate(transient_errors):
        job_id = store.enqueue(_job(vault, name=f"transient-{index}.mp3"))
        claimed = store.claim_next()
        assert claimed is not None and claimed.id == job_id
        store.mark(job_id, media_jobs.FAILED, error)
        transient_ids.append(job_id)

    permanent_id = store.enqueue(_job(vault, name="corrupt.mp3"))
    permanent = store.claim_next()
    assert permanent is not None and permanent.id == permanent_id
    store.mark(permanent_id, media_jobs.FAILED, "ValueError: corrupt container")

    assert store.recover_transient_failures() == len(transient_ids)

    jobs = {job["id"]: job for job in media_jobs.status(vault)["jobs"]}
    for job_id in transient_ids:
        assert jobs[job_id]["state"] == media_jobs.PENDING
        assert jobs[job_id]["attempts"] == 0
        assert jobs[job_id]["error"] is None
    assert jobs[permanent_id]["state"] == media_jobs.FAILED
    assert jobs[permanent_id]["attempts"] == 1
    assert jobs[permanent_id]["error"] == "ValueError: corrupt container"


def test_live_worker_prevents_duplicate_recovery(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(_job(vault))
    assert store.claim_next() is not None
    store.set_worker(os.getpid(), 30.0)

    assert store.needs_worker() is False
    assert store.counts()["running"] == 1

    store.clear_worker(os.getpid())
    assert store.needs_worker() is True


def test_atomic_claim_allows_one_winner(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(_job(vault))

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: store.claim_next(), range(2)))

    assert sum(job is not None for job in claimed) == 1


def test_completion_preserves_new_stage_added_while_running(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(_job(vault, ocr=True, clip=False))
    claimed = store.claim_next()
    assert claimed is not None

    store.enqueue(_job(vault, ocr=False, clip=True))
    store.complete(claimed)

    followup = store.claim_next()
    assert followup is not None
    assert followup.do_ocr is False
    assert followup.do_clip is True


def test_vault_lock_allows_one_worker(vault: Path) -> None:
    first = _VaultLock(media_jobs.worker_lock_path(vault))
    second = _VaultLock(media_jobs.worker_lock_path(vault))
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert second.acquire() is True
    second.release()
