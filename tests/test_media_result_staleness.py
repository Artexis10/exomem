"""Retained media computation survives bookkeeping without replacing user evidence."""

from __future__ import annotations

import pytest

from exomem import deferred_index, extract, media_jobs, media_worker, preserve, provenance


def _pending_job(vault, *, durable: bool):
    result = preserve.preserve_bytes(
        vault, scope="Rental", category="audio", filename="retained.mp3", data=b"media"
    )
    job = media_jobs.MediaJob(
        binary_path=vault / result.path,
        sidecar_path=vault / result.sidecar_path,
        media_type="audio",
    )
    store = media_jobs.MediaJobStore(vault) if durable else None
    if store is not None:
        store.enqueue(job)
        job = store.claim_next()
        assert job is not None
    return job, store


def _backlink(sidecar, destination="compiled"):
    before = sidecar.read_text(encoding="utf-8")
    sidecar.write_text(
        preserve._set_frontmatter_field(before, "ingested_into", f"[{destination}]"), encoding="utf-8"
    )


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_backlinks_during_extraction_keep_computation_and_latest_metadata(
    vault, monkeypatch, durable, failed
):
    job, store = _pending_job(vault, durable=durable)
    original = job.sidecar_path.read_text(encoding="utf-8")
    calls = []

    def compute(*_args, **_kwargs):
        calls.append(True)
        _backlink(job.sidecar_path)
        if failed:
            raise extract.ExtractionUnavailable("decoder missing")
        return extract.ExtractResult(text="retained transcript", media_type="audio", engine="test")

    monkeypatch.setattr(extract, "extract_text", compute)
    worker = media_worker.MediaWorker(vault, execution_mode="process" if durable else "inline")
    worker._process(job)
    if store is not None:
        [retained] = store.pending_results()
        assert retained.sidecar_before_evidence_version == provenance.evidence_version(original)
        worker._drain_parent_results()
        assert store.pending_result_count() == 0
    content = job.sidecar_path.read_text(encoding="utf-8")
    assert "ingested_into: [compiled]" in content
    assert ("processing_state: blocked" if failed else "retained transcript") in content
    assert calls == [True]


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("edit", ["body", "metadata", "binary"])
def test_material_or_binary_edits_with_backlinks_never_publish_old_transcript(
    vault, monkeypatch, durable, edit
):
    job, store = _pending_job(vault, durable=durable)

    def compute(*_args, **_kwargs):
        _backlink(job.sidecar_path)
        if edit == "body":
            with job.sidecar_path.open("a", encoding="utf-8") as stream:
                stream.write("\nuser evidence\n")
        elif edit == "metadata":
            before = job.sidecar_path.read_text(encoding="utf-8")
            job.sidecar_path.write_text(before.replace("---\n", "---\nconfidentiality: restricted\n", 1))
        else:
            job.binary_path.write_bytes(b"replacement media")
        return extract.ExtractResult(text="obsolete transcript", media_type="audio", engine="test")

    monkeypatch.setattr(extract, "extract_text", compute)
    worker = media_worker.MediaWorker(vault, execution_mode="process" if durable else "inline")
    worker._process(job)
    if store is not None:
        worker._drain_parent_results()
        assert store.pending_result_count() == 0
        assert store.get(job.id).do_ocr
    content = job.sidecar_path.read_text(encoding="utf-8")
    assert "obsolete transcript" not in content
    assert "ingested_into: [compiled]" in content
    if edit == "body":
        assert "user evidence" in content
    elif edit == "metadata":
        assert "confidentiality: restricted" in content


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("edit", ["backlink", "body"])
def test_media_cas_race_revalidates_before_reusing_computation(vault, monkeypatch, durable, edit):
    job, store = _pending_job(vault, durable=durable)
    calls = []
    publications = []
    real_commit = preserve.commit_media_sidecar_writes

    def compute(*_args, **_kwargs):
        calls.append(True)
        return extract.ExtractResult(text="one computation", media_type="audio", engine="test")

    def race(root, writes, **kwargs):
        publications.append(True)
        if len(publications) == 1:
            _backlink(job.sidecar_path)
            if edit == "body":
                with job.sidecar_path.open("a", encoding="utf-8") as stream:
                    stream.write("\nlatest user evidence\n")
        return real_commit(root, writes, **kwargs)

    monkeypatch.setattr(extract, "extract_text", compute)
    monkeypatch.setattr(preserve, "commit_media_sidecar_writes", race)
    worker = media_worker.MediaWorker(vault, execution_mode="process" if durable else "inline")
    worker._process(job)
    if store is not None:
        worker._drain_parent_results()
        worker._drain_parent_results()
        assert store.pending_result_count() == 0
    content = job.sidecar_path.read_text(encoding="utf-8")
    assert ("one computation" in content) is (edit == "backlink")
    assert "ingested_into: [compiled]" in content
    assert calls == [True]
    assert len(publications) == (2 if edit == "backlink" else 1)
    if edit == "body":
        assert "latest user evidence" in content


@pytest.mark.parametrize("published", [False, True])
def test_parent_recovers_prepared_result_after_backlinks_without_replacing_completed_bytes(
    vault, monkeypatch, published
):
    job, store = _pending_job(vault, durable=True)
    monkeypatch.setattr(extract, "extract_text", lambda *_a, **_k: extract.ExtractResult(
        text="retained across restart", media_type="audio", engine="test"
    ))
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    worker._process(job)
    real_commit = preserve.commit_media_sidecar_writes
    real_complete = worker._complete_deferred_graph_completion
    if published:
        monkeypatch.setattr(worker, "_complete_deferred_graph_completion", lambda *_a, **_k: False)
    else:
        def fail_before_publish(*_args, **_kwargs):
            raise OSError("publication interrupted")
        monkeypatch.setattr(preserve, "commit_media_sidecar_writes", fail_before_publish)
    worker._drain_parent_results()
    [retained] = store.pending_results()
    assert retained.target_evidence_version is not None
    _backlink(job.sidecar_path)
    latest = job.sidecar_path.read_bytes()
    monkeypatch.setattr(preserve, "commit_media_sidecar_writes", real_commit)
    monkeypatch.setattr(worker, "_complete_deferred_graph_completion", real_complete)
    worker._drain_parent_results()
    assert store.pending_result_count() == 0
    content = job.sidecar_path.read_text(encoding="utf-8")
    assert "retained across restart" in content
    assert "ingested_into: [compiled]" in content
    assert deferred_index.snapshot_full(vault) == []
    if published:
        assert job.sidecar_path.read_bytes() == latest


def test_parent_does_not_call_changed_binary_a_foreign_completed_extraction(vault, monkeypatch):
    job, store = _pending_job(vault, durable=True)
    monkeypatch.setattr(extract, "extract_text", lambda *_a, **_k: extract.ExtractResult(
        text="old computation", media_type="audio", engine="test"
    ))
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    worker._process(job)
    before = job.sidecar_path.read_text(encoding="utf-8")
    job.sidecar_path.write_text(preserve.render_sidecar_extraction(
        before, text="foreign completed transcript", engine="manual"
    ))
    job.binary_path.write_bytes(b"new binary")
    latest = job.sidecar_path.read_bytes()
    worker._drain_parent_results()
    assert job.sidecar_path.read_bytes() == latest
    assert store.get(job.id).do_ocr
    assert store.get(job.id).state == media_jobs.FAILED


def test_invalid_yaml_never_matches_another_invalid_evidence_version(vault, monkeypatch):
    job, _store = _pending_job(vault, durable=False)
    job.sidecar_path.write_text("---\ninvalid: [\n---\n## Extracted text\n")

    def compute(*_args, **_kwargs):
        job.sidecar_path.write_text("---\ninvalid: {\n---\n## Extracted text\n")
        return extract.ExtractResult(text="unsafe result", media_type="audio", engine="test")

    monkeypatch.setattr(extract, "extract_text", compute)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    worker._process(job)
    assert "unsafe result" not in job.sidecar_path.read_text(encoding="utf-8")


def test_legacy_result_keeps_exact_byte_protection_after_backlinks(vault):
    job, store = _pending_job(vault, durable=True)
    before = job.sidecar_path.read_text(encoding="utf-8")
    identity = media_worker._binary_identity(job.binary_path)
    assert store.record_result(
        job, kind="extraction", sidecar_before_hash=media_worker.content_hash(before),
        binary_identity=media_worker._result_binary_identity(identity),
        payload={"text": "legacy computation", "engine": "test"},
    )
    _backlink(job.sidecar_path)
    latest = job.sidecar_path.read_bytes()
    media_worker.MediaWorker(vault, execution_mode="process")._drain_parent_results()
    assert job.sidecar_path.read_bytes() == latest
    assert store.get(job.id).state == media_jobs.FAILED
    assert store.get(job.id).do_ocr
