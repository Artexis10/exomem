"""media_worker — the async extraction pipeline (extract engines stubbed; no GPU)."""

import os
import threading
import time

import numpy as np
import pytest

from exomem import embeddings, extract, media_worker, preserve, server_runtime
from exomem import find as find_module


def _preserve_media_stub(vault, filename="rec.mp3"):
    """Preserve a media binary with no text → a `pending` stub sidecar."""
    return preserve.preserve_bytes(
        vault, scope="Yolo", category="audio", filename=filename, data=b"FAKEBYTES"
    )


def test_preserve_media_writes_pending_stub(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault)
    assert result.sidecar_path is not None
    body = (vault / result.sidecar_path).read_text(encoding="utf-8")
    assert "media_type: audio" in body
    assert "evidence_file: " in body
    assert "extracted_by: pending" in body


def test_preserve_media_no_stub_when_extraction_disabled(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    result = _preserve_media_stub(vault, filename="rec2.mp3")
    assert result.sidecar_path is None  # nothing would fill it → don't write a stub


def test_worker_fills_pending_sidecar(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="call.mp3")
    sidecar = vault / result.sidecar_path
    monkeypatch.setattr(
        extract, "extract_text",
        lambda p, media_type=None, vault_root=None: extract.ExtractResult(
            text="discussion of the broken sink and water damage", media_type="audio", engine="faster-whisper:test"
        ),
    )
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio"))

    body = sidecar.read_text(encoding="utf-8")
    assert "water damage" in body
    assert "extracted_by: faster-whisper:test" in body
    assert "extracted_by: pending" not in body


def test_worker_writes_speaker_labels_and_field(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # Opt-in diarization output round-trips: labeled turns into the sidecar text AND
    # the distinct speaker labels into a `speakers:` frontmatter list.
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="meeting2.mp3")
    sidecar = vault / result.sidecar_path
    monkeypatch.setattr(
        extract, "extract_text",
        lambda p, media_type=None, vault_root=None: extract.ExtractResult(
            text="[Speaker A]: we shipped it\n[Speaker B]: nice work",
            media_type="audio",
            engine="faster-whisper:test+diarized",
            speakers=[
                {"speaker": "Speaker A", "start": 0.0, "end": 1.0, "text": "we shipped it"},
                {"speaker": "Speaker B", "start": 1.0, "end": 2.0, "text": "nice work"},
            ],
        ),
    )
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio"))

    body = sidecar.read_text(encoding="utf-8")
    assert "[Speaker A]: we shipped it" in body
    assert "[Speaker B]: nice work" in body
    assert "speakers: [Speaker A, Speaker B]" in body
    assert "extracted_by: faster-whisper:test+diarized" in body


def test_worker_marks_failed_on_extraction_error(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="bad.mp3")
    sidecar = vault / result.sidecar_path

    def boom(p, media_type=None, vault_root=None):
        raise RuntimeError("corrupt container")

    monkeypatch.setattr(extract, "extract_text", boom)
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio"))

    body = sidecar.read_text(encoding="utf-8")
    assert "extracted_by: failed:" in body
    assert "extracted_by: pending" not in body  # won't re-loop on restart scan


def test_start_prewarms_asr_off_the_request_path(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    warmed = threading.Event()
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: True)
    monkeypatch.setattr(extract, "prewarm", warmed.set)
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w.start()
    try:
        assert warmed.wait(timeout=5.0), "start() should warm ASR in a background thread"
    finally:
        w.stop()


def test_start_skips_asr_prewarm_when_policy_disables_it(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "prewarm", lambda: pytest.fail("prewarm should be skipped"))
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w.start()
    try:
        pass
    finally:
        w.stop()



def test_start_logs_diarization_readiness(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda v=None: calls.append(v))
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w.start()
    try:
        assert calls == [vault]
    finally:
        w.stop()


def test_run_extraction_passes_vault_root(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # Named attribution matches against the worker's vault profile store — the vault
    # must flow through extract_text, not be re-resolved from env.
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="vaulted.mp3")
    seen: dict = {}

    def _spy(p, media_type=None, vault_root=None):
        seen["vault_root"] = vault_root
        return extract.ExtractResult(text="t", media_type="audio", engine="faster-whisper:test")

    monkeypatch.setattr(extract, "extract_text", _spy)
    w = media_worker.MediaWorker(vault)
    w._process(media_worker._Job(
        binary_path=vault / result.path, sidecar_path=vault / result.sidecar_path,
        media_type="audio",
    ))
    assert seen["vault_root"] == vault


def test_worker_unavailable_leaves_pending(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="later.mp3")
    sidecar = vault / result.sidecar_path

    def unavailable(p, media_type=None, vault_root=None):
        raise extract.ExtractionUnavailable("engine not installed")

    monkeypatch.setattr(extract, "extract_text", unavailable)
    w = media_worker.MediaWorker(vault)
    w._process(media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio"))

    # Engine absent now → stays pending so a provisioned box retries on its restart scan.
    assert "extracted_by: pending" in sidecar.read_text(encoding="utf-8")


def test_scan_pending_reenqueues(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    _preserve_media_stub(vault, filename="one.mp3")
    _preserve_media_stub(vault, filename="two.wav")
    w = media_worker.MediaWorker(vault)
    assert w.scan_pending() == 2


def test_worker_clip_embeds_image(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="photos", filename="p.jpg", data=b"\xff\xd8\xff", text="beach",
    )
    monkeypatch.setattr(embeddings, "embed_image", lambda p: np.ones(embeddings.CLIP_DIM, dtype=np.float32))
    w = media_worker.MediaWorker(vault)
    w._process(media_worker._Job(
        binary_path=vault / res.path, sidecar_path=vault / res.sidecar_path,
        media_type="image", do_ocr=False, do_clip=True,
    ))
    assert embeddings.ClipIndex(vault).has(res.path)


def test_scan_unindexed_images_enqueues(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    preserve.preserve_bytes(vault, scope="Yolo", category="photos", filename="x.jpg", data=b"\xff\xd8\xff", text="t")
    preserve.preserve_bytes(vault, scope="Yolo", category="photos", filename="y.png", data=b"\x89PNG", text="t")
    w = media_worker.MediaWorker(vault)
    assert w._scan_unindexed_images() == 2  # both images queued for CLIP


def test_process_worker_drains_and_exits_after_idle(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="lifecycle.mp3")
    w = media_worker.MediaWorker(vault, execution_mode="process", idle_seconds=0.15)
    w.start()
    try:
        w.enqueue(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
            do_ocr=False,
        )
        w.join(timeout=10)
        deadline = time.monotonic() + 10
        from exomem import media_jobs

        while time.monotonic() < deadline and media_jobs.status(vault)["worker_active"]:
            time.sleep(0.02)
        assert media_jobs.status(vault)["worker_active"] is False
    finally:
        w.stop()


def test_child_marks_unavailable_engine_blocked(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    result = _preserve_media_stub(vault, filename="blocked.mp3")

    def _unavailable(*_args, **_kwargs):
        raise extract.ExtractionUnavailable("engine absent")

    monkeypatch.setattr(extract, "extract_text", _unavailable)
    from exomem import media_jobs

    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0
    assert store.counts()["blocked"] == 1


def test_media_runtime_failure_does_not_deny_core_service(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)

    class _BrokenWorker:
        def __init__(self, _vault):
            raise OSError("ledger unavailable")

    monkeypatch.setattr(media_worker, "MediaWorker", _BrokenWorker)
    assert server_runtime._start_media_worker(vault) is None


def test_supervisor_recovers_jobs_owned_by_crashed_child(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="crash.mp3")
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    assert worker._store is not None
    worker.enqueue(
        binary_path=vault / result.path,
        sidecar_path=vault / result.sidecar_path,
        media_type="audio",
    )
    assert worker._store.claim_next() is not None

    class _CrashedChild:
        pid = 2_147_483_646
        returncode = 1

        @staticmethod
        def poll():
            return 1

    worker._store.set_worker(_CrashedChild.pid, 30.0)
    worker._child = _CrashedChild()
    thread = threading.Thread(target=worker._supervise)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and worker._store.counts()["pending"] == 0:
            time.sleep(0.01)
        assert worker._store.counts()["pending"] == 1
        assert worker._store.counts()["running"] == 0
    finally:
        worker._stop_event.set()
        worker._wake.set()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_find_surfaces_media_fields(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    # Provide text so the sidecar is populated + keyword-findable; media frontmatter is set either way.
    preserve.preserve_bytes(
        vault, scope="Yolo", category="audio", filename="meeting.mp3", data=b"X",
        text="quarterly review of the water damage claim",
    )
    find_module.clear_cache()
    hits = find_module.find(vault, query="water damage claim", mode="keyword")
    media = [h for h in hits if "meeting.mp3.md" in h.path]
    assert media, [h.path for h in hits]
    d = media[0].as_dict()
    assert d["media_type"] == "audio"
    assert d["media_file"].endswith("meeting.mp3")
