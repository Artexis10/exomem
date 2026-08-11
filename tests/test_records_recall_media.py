"""Automatic media work must not make raw Records part of ordinary recall."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest

from exomem import backfill, embeddings, extract, media_jobs, media_processing, media_worker
from exomem.clip_index import CLIP_DIM, ClipIndex


def _drop_media(vault: Path, rel: str, data: bytes = b"\xff\xd8\xffmedia") -> Path:
    binary = vault / rel
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(data)
    return binary


def _pending_sidecar(binary: Path, rel: str) -> Path:
    sidecar = binary.with_name(binary.name + ".md")
    sidecar.write_text(
        "media_type: image\n"
        f"evidence_file: {rel}\n"
        "extracted_by: pending\n",
        encoding="utf-8",
    )
    return sidecar


class _MutationManager:
    @contextmanager
    def mutation_guard(self, *_args: object, **_kwargs: object):
        yield


@pytest.fixture
def mutation_manager(monkeypatch: pytest.MonkeyPatch) -> _MutationManager:
    manager = _MutationManager()
    monkeypatch.setattr(media_worker, "get_manager", lambda: manager)
    return manager


def test_worker_discovery_never_opens_or_queues_raw_record_sidecars(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_rel = "Knowledge Base/Records/Health/raw.jpg"
    admitted_rel = "Knowledge Base/Evidence/Photos/admitted.jpg"
    raw = _drop_media(vault, raw_rel)
    admitted = _drop_media(vault, admitted_rel)
    raw_sidecar = _pending_sidecar(raw, raw_rel)
    _pending_sidecar(admitted, admitted_rel)
    original_read_text = Path.read_text

    def deny_raw_sidecar(self: Path, *args: object, **kwargs: object) -> str:
        if self == raw_sidecar:
            raise AssertionError("raw Records sidecar must not be opened")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_raw_sidecar)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    assert worker._scan_pending_ocr() == 1
    job = worker._q.get_nowait()
    worker._q.task_done()
    assert job.binary_path == admitted
    assert job.sidecar_path == admitted.with_name(admitted.name + ".md")


def test_worker_purges_raw_clip_rows_without_clip_or_sidecar_reads(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_rel = "Knowledge Base/Records/Health/raw.jpg"
    raw = _drop_media(vault, raw_rel)
    raw_sidecar = _pending_sidecar(raw, raw_rel)
    clip = ClipIndex(vault)
    clip.upsert(raw_rel, np.ones(CLIP_DIM, dtype=np.float32), 1.0)
    original_read_text = Path.read_text

    def deny_raw_sidecar(self: Path, *args: object, **kwargs: object) -> str:
        if self == raw_sidecar:
            raise AssertionError("raw Records sidecar must not be opened")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setattr(Path, "read_text", deny_raw_sidecar)
    monkeypatch.setattr(
        embeddings,
        "embed_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("CLIP model ran")),
    )
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    assert worker._scan_unindexed_images() == 0
    assert not clip.has(raw_rel)


def test_backfill_projects_out_raw_records_before_ocr_or_clip_work(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_rels = [f"Knowledge Base/Records/Health/raw-{number}.jpg" for number in range(8)]
    for rel in raw_rels:
        _drop_media(vault, rel)
    admitted_rel = "Knowledge Base/ZAllowed/admitted.jpg"
    admitted = _drop_media(vault, admitted_rel)
    clip = ClipIndex(vault)
    clip.upsert(raw_rels[0], np.ones(CLIP_DIM, dtype=np.float32), 1.0)
    embedded: list[Path] = []

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("OCR ran")),
    )
    monkeypatch.setattr(
        embeddings,
        "embed_image",
        lambda path: embedded.append(path) or np.ones(CLIP_DIM, dtype=np.float32),
    )

    stats = backfill.backfill_media(vault, do_ocr=False, log_fn=lambda *_args: None)

    assert stats.scanned == 1
    assert stats.sidecars_created == 1
    assert stats.clip_indexed == 1
    assert embedded == [admitted]
    assert not (vault / raw_rels[0]).with_name("raw-0.jpg.md").exists()
    assert not clip.has(raw_rels[0])


def test_backfill_dry_run_preserves_raw_record_clip_rows_and_reports_cleanup(
    vault: Path,
) -> None:
    raw_rel = "Knowledge Base/Records/Health/raw.jpg"
    _drop_media(vault, raw_rel)
    clip = ClipIndex(vault)
    clip.upsert(raw_rel, np.ones(CLIP_DIM, dtype=np.float32), 1.0)
    messages: list[str] = []

    backfill.backfill_media(
        vault, do_ocr=False, do_clip=False, dry_run=True, log_fn=messages.append
    )

    assert clip.has(raw_rel)
    assert any("would purge CLIP rows for 1 suppressed media file" in message for message in messages)


def test_backfill_dry_run_preserves_clip_rows_when_policy_flips_in_loop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/Evidence/Photos/flip.jpg"
    _drop_media(vault, rel)
    clip = ClipIndex(vault)
    clip.upsert(rel, np.ones(CLIP_DIM, dtype=np.float32), 1.0)
    checks = 0
    messages: list[str] = []

    def suppress_after_discovery(*_args: object) -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    monkeypatch.setattr(
        backfill.recall_policy, "is_structured_only_path", suppress_after_discovery
    )

    backfill.backfill_media(
        vault, do_ocr=False, do_clip=False, dry_run=True, log_fn=messages.append
    )

    assert clip.has(rel)
    assert any("flip.jpg -> purge CLIP" in message for message in messages)


def test_bounded_automatic_reconciliation_skips_raw_record_volume(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(8):
        _drop_media(vault, f"Knowledge Base/Records/Health/raw-{number}.m4a", b"raw")
    admitted = _drop_media(vault, "Knowledge Base/ZAllowed/admitted.m4a", b"admitted")

    def deny_raw_provenance(_vault: Path, binary: Path, *_args: object) -> object:
        if "Records" in binary.parts:
            raise AssertionError("automatic discovery opened a raw Records binary")
        return original_read_provenance(_vault, binary, *_args)

    original_read_provenance = media_processing._read_provenance
    monkeypatch.setattr(media_processing, "_read_provenance", deny_raw_provenance)

    assert media_processing.reconcile_all_media(vault, limit=1) == 1
    assert admitted.with_name(admitted.name + ".md").exists()
    assert media_jobs.MediaJobStore(vault, create=False).counts()["pending"] == 1
    assert not (vault / "Knowledge Base/Records/Health/raw-0.m4a.md").exists()


def test_explicit_structured_record_media_reconciliation_remains_available(vault: Path) -> None:
    raw = _drop_media(vault, "Knowledge Base/Records/Health/inspection.m4a", b"raw")

    result = media_processing.reconcile_media(vault, raw, explicit=True)

    assert result is not None
    assert result.state == media_jobs.PENDING
    assert result.sidecar_path.exists()


def test_persisted_raw_record_job_is_retired_before_sidecar_or_binary_reads(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_rel = "Knowledge Base/Records/Health/persisted.jpg"
    raw = _drop_media(vault, raw_rel)
    sidecar = _pending_sidecar(raw, raw_rel)
    store = media_jobs.MediaJobStore(vault)
    job_id = store.enqueue(
        media_jobs.MediaJob(binary_path=raw, sidecar_path=sidecar, media_type="image")
    )
    claimed = store.claim_next()
    assert claimed is not None
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

    def deny_raw_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == sidecar:
            raise AssertionError("persisted raw job opened its sidecar")
        return original_read_text(self, *args, **kwargs)

    def deny_raw_read_bytes(self: Path, *args: object, **kwargs: object) -> bytes:
        if self == raw:
            raise AssertionError("persisted raw job opened its binary")
        return original_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_raw_read_text)
    monkeypatch.setattr(Path, "read_bytes", deny_raw_read_bytes)
    worker = media_worker.MediaWorker(vault, execution_mode="process")

    assert worker._process(claimed).state == "complete"
    assert worker._store is not None
    assert worker._store.get(job_id) is None


def test_extraction_commit_refuses_policy_flip_after_model_work(
    vault: Path, monkeypatch: pytest.MonkeyPatch, mutation_manager: _MutationManager
) -> None:
    binary = _drop_media(vault, "Knowledge Base/Evidence/Photos/flip.jpg")
    sidecar = _pending_sidecar(binary, "Knowledge Base/Evidence/Photos/flip.jpg")
    admitted = True

    monkeypatch.setattr(
        media_worker.recall_policy,
        "is_recall_candidate",
        lambda *_args: admitted,
    )

    def extract_then_suppress(*_args: object, **_kwargs: object) -> extract.ExtractResult:
        nonlocal admitted
        admitted = False
        return extract.ExtractResult(text="model text", media_type="image", engine="tesseract")

    monkeypatch.setattr(extract, "extract_text", extract_then_suppress)
    monkeypatch.setattr(
        media_worker.preserve,
        "update_sidecar_extraction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("raw sidecar write")),
    )
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    assert worker._process(
        media_worker._Job(binary_path=binary, sidecar_path=sidecar, media_type="image")
    ).state == "stale"


def test_failure_commit_refuses_policy_flip_after_model_work(
    vault: Path, monkeypatch: pytest.MonkeyPatch, mutation_manager: _MutationManager
) -> None:
    binary = _drop_media(vault, "Knowledge Base/Evidence/Photos/failure-flip.jpg")
    sidecar = _pending_sidecar(binary, "Knowledge Base/Evidence/Photos/failure-flip.jpg")
    admitted = True

    monkeypatch.setattr(
        media_worker.recall_policy,
        "is_recall_candidate",
        lambda *_args: admitted,
    )

    def fail_then_suppress(*_args: object, **_kwargs: object) -> None:
        nonlocal admitted
        admitted = False
        raise extract.ExtractionUnavailable("OCR unavailable")

    monkeypatch.setattr(extract, "extract_text", fail_then_suppress)
    monkeypatch.setattr(
        media_worker.preserve,
        "update_sidecar_processing_failure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("raw failure write")),
    )
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    assert worker._process(
        media_worker._Job(binary_path=binary, sidecar_path=sidecar, media_type="image")
    ).state == "stale"


def test_clip_and_reembed_refuse_policy_flip_before_publication(
    vault: Path, monkeypatch: pytest.MonkeyPatch, mutation_manager: _MutationManager
) -> None:
    binary = _drop_media(vault, "Knowledge Base/Evidence/Photos/clip-flip.jpg")
    sidecar = _pending_sidecar(binary, "Knowledge Base/Evidence/Photos/clip-flip.jpg")
    admitted = True

    monkeypatch.setattr(
        media_worker.recall_policy,
        "is_recall_candidate",
        lambda *_args: admitted,
    )

    def embed_then_suppress(*_args: object, **_kwargs: object) -> np.ndarray:
        nonlocal admitted
        admitted = False
        return np.ones(CLIP_DIM, dtype=np.float32)

    monkeypatch.setattr(embeddings, "embed_image", embed_then_suppress)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    monkeypatch.setattr(
        worker._clip_index,
        "upsert",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("raw CLIP publish")),
    )

    worker._process(
        media_worker._Job(
            binary_path=binary, sidecar_path=sidecar, media_type="image", do_ocr=False, do_clip=True
        )
    )

    admitted = True

    @contextmanager
    def flip_in_guard(*_args: object, **_kwargs: object):
        nonlocal admitted
        admitted = False
        yield

    monkeypatch.setattr(mutation_manager, "mutation_guard", flip_in_guard)
    monkeypatch.setattr(
        media_worker.index_sync,
        "upsert_after_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("raw re-embed publish")),
    )
    worker._process(
        media_worker._Job(
            binary_path=binary,
            sidecar_path=sidecar,
            media_type="image",
            do_ocr=False,
            do_reembed=True,
        )
    )


def test_backfill_refuses_policy_flip_before_clip_publication(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drop_media(vault, "Knowledge Base/Evidence/Photos/backfill-flip.jpg")
    suppressed = False

    monkeypatch.setattr(
        backfill.recall_policy,
        "is_structured_only_path",
        lambda *_args: suppressed,
    )

    published: list[object] = []

    class ClipStore:
        def purge_markdown_paths_if_present(self, _paths: list[str]) -> int:
            return 0

        def has(self, _path: str) -> bool:
            return False

        def upsert(self, *_args: object, **_kwargs: object) -> None:
            published.append(_args)

    def embed_then_suppress(*_args: object, **_kwargs: object) -> np.ndarray:
        nonlocal suppressed
        suppressed = True
        return np.ones(CLIP_DIM, dtype=np.float32)

    monkeypatch.setattr(embeddings, "get_clip_index", lambda *_args: ClipStore())
    monkeypatch.setattr(embeddings, "embed_image", embed_then_suppress)

    stats = backfill.backfill_media(vault, do_ocr=False, log_fn=lambda *_args: None)

    assert stats.clip_indexed == 0
    assert published == []
