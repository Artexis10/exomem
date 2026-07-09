from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from exomem import media_jobs
from exomem.media_worker_child import _VaultLock


def _job(vault: Path, *, ocr: bool = True, clip: bool = False) -> media_jobs.MediaJob:
    binary = vault / "Knowledge Base" / "Evidence" / "item.mp4"
    sidecar = binary.with_name(binary.name + ".md")
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"x")
    sidecar.write_text("---\nmedia_type: video\n---\n", encoding="utf-8")
    return media_jobs.MediaJob(
        binary_path=binary,
        sidecar_path=sidecar,
        media_type="video",
        do_ocr=ocr,
        do_clip=clip,
    )


def test_status_does_not_create_store(vault: Path) -> None:
    path = media_jobs.job_store_path(vault)
    assert not path.exists()
    status = media_jobs.status(vault)
    assert status["counts"]["pending"] == 0
    assert not path.exists()


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
