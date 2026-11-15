"""Media processing of an original whose bytes are a shared pool object, with no file at its logical path."""

from __future__ import annotations

from pathlib import Path

from test_shared_originals import _pooled_capture

from exomem import media_jobs, media_processing, media_worker
from exomem.vault import parse_frontmatter


def _drain(vault: Path) -> list[str]:
    store = media_jobs.MediaJobStore(vault)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    states = []
    while (job := store.claim_next()) is not None:
        outcome = worker._process(job)
        states.append(outcome.state)
        store.complete(job)
    return states


def test_a_pooled_original_is_reconciled_queued_and_extracted_from_its_pool_object(vault, tmp_path, monkeypatch):
    """Processing a shared filing must read its pooled bytes, not report the missing logical file as gone."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "minutes.txt", b"the minutes of the quarterly meeting")
    assert not (vault / added.artifact_path).exists()

    outcome = media_processing.reconcile_media(vault, added.artifact_path)

    assert outcome.state == "pending"
    assert _drain(vault) == [media_worker._COMPLETE]
    fields = parse_frontmatter((vault / (added.artifact_path + ".md")).read_text())[0]
    assert fields["extracted_by"] != "pending"
    assert "quarterly meeting" in (vault / (added.artifact_path + ".md")).read_text()
    assert not (vault / added.artifact_path).exists()


def test_a_routine_reconcile_keeps_the_rows_and_frames_of_a_pooled_video(vault, tmp_path, monkeypatch):
    """A pooled video has no file at its path, so its CLIP rows and scene frames must not read as orphaned."""
    from test_media_deletion_propagation import _FakeImg, _scene, _unit

    from exomem import embeddings, scene_frames
    from exomem import reconcile as reconcile_module

    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    added = _pooled_capture(vault, tmp_path, monkeypatch, "demo.mp4", b"\x00video")
    video = vault / added.artifact_path
    assert not video.exists()
    embeddings.ClipIndex(vault).upsert(added.artifact_path, _unit(2), 1.0)

    written = scene_frames.write_scene_frames(vault, video, [(_scene(5.0), _FakeImg())])
    assert written, "scene frames of a pooled video must be written, not skipped"

    report = reconcile_module.reconcile(vault)

    assert report.clip_orphans_removed == 0
    assert report.frame_orphans_removed == 0
    assert embeddings.ClipIndex(vault).has(added.artifact_path)
    assert list(scene_frames.frames_dir_for(video).glob("scene-*.jpg"))


def test_the_media_sweep_finds_a_pooled_original(vault, tmp_path, monkeypatch):
    """A bounded sweep walks files, so a pooled original needs its own discovery to be queued for processing."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "minutes.txt", b"the minutes of the quarterly meeting")
    seen: list[Path] = []

    media_processing.reconcile_all_media(vault, reconcile_one=seen.append)

    assert vault / added.artifact_path in seen


def test_a_failed_job_of_a_pooled_original_can_be_retried(vault, tmp_path, monkeypatch):
    """An explicit retry must find the pooled bytes the failed job named, not report the file as missing."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "minutes.txt", b"the minutes of the quarterly meeting")
    first = media_processing.reconcile_media(vault, added.artifact_path)
    store = media_jobs.MediaJobStore(vault)
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == first.job_id
    store.mark(claimed.id, media_jobs.FAILED, "corrupt container")

    retried = media_processing.retry_media(vault, added.artifact_path)

    assert retried.job_id == first.job_id
    assert retried.state == media_jobs.PENDING


def test_backfill_extracts_and_indexes_a_pooled_image_from_its_pool_object(vault, tmp_path, monkeypatch):
    """The bulk pass must give the engines the pooled bytes under the logical name, and index the logical path."""
    import numpy as np

    from exomem import backfill, embeddings, extract

    data = b"\xff\xd8\xffPOOLED"
    added = _pooled_capture(vault, tmp_path, monkeypatch, "legacy.jpg", data)
    assert not (vault / added.artifact_path).exists()
    decoded: list[tuple[str, bytes]] = []

    def fake_extract(path, media_type=None, vault_root=None):
        decoded.append((path.name, path.read_bytes()))
        return extract.ExtractResult(text="pooled invoice acme", media_type="image", engine="tesseract")

    def fake_embed(path):
        decoded.append((path.name, path.read_bytes()))
        return np.ones(embeddings.CLIP_DIM, dtype=np.float32)

    monkeypatch.setattr(extract, "extract_text", fake_extract)
    monkeypatch.setattr(embeddings, "embed_image", fake_embed)
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)

    stats = backfill.backfill_media(vault, log_fn=lambda *a, **k: None)

    assert (stats.extracted, stats.clip_indexed) == (1, 1)
    logical_name = Path(added.artifact_path).name
    assert decoded == [(logical_name, data), (logical_name, data)]
    assert "pooled invoice acme" in (vault / (added.artifact_path + ".md")).read_text()
    assert embeddings.ClipIndex(vault).has(added.artifact_path)


def test_reconcile_rebuilds_a_missing_part_from_a_pooled_original(vault, tmp_path, monkeypatch):
    """A part lost from a long extraction is rebuilt from the original, which here is a pool object."""
    from exomem import extraction_parts, preserve
    from exomem import reconcile as reconcile_module

    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.setattr(preserve, "_MAX_EXTRACT_BYTES", 4000)
    text = "\n\n".join(f"paragraph {number} " + "alpha beta gamma " * 20 for number in range(60)).strip()
    added = _pooled_capture(vault, tmp_path, monkeypatch, "notes.txt", text.encode("utf-8"))
    page = vault / (added.artifact_path + ".md")
    preserve.update_sidecar_extraction(vault, page, text=text, engine="text")
    extraction_parts.part_path(page, 3).unlink()

    report = reconcile_module.reconcile(vault).as_dict()

    assert (report["parts_rebuilt"], report["parts_missing"]) == (1, [])
    assert extraction_parts.whole_text(vault, page, page.read_text(encoding="utf-8")) == text
