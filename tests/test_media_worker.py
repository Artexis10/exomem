"""media_worker — the async extraction pipeline (extract engines stubbed; no GPU)."""

import hashlib
import os
import threading
import time
from contextlib import contextmanager

import numpy as np
import pytest
import yaml

from exomem import embeddings, extract, media_jobs, media_worker, preserve, server_runtime
from exomem import find as find_module


@pytest.fixture(autouse=True)
def _isolated_writer_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))


class _RecordingMutationManager:
    def __init__(self, vault) -> None:
        self.vault = vault
        self.depth = 0
        self.events: list[str] = []

    @contextmanager
    def mutation_guard(self, vault):
        assert vault == self.vault
        self.events.append("guard-enter")
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            self.events.append("guard-exit")


def _preserve_media_stub(vault, filename="rec.mp3"):
    """Preserve a media binary with no text → a `pending` stub sidecar."""
    return preserve.preserve_bytes(
        vault, scope="Yolo", category="audio", filename=filename, data=b"FAKEBYTES"
    )


def _parsed_frontmatter(path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    raw, _body = content.removeprefix("---\n").split("\n---\n", 1)
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


def test_preserve_media_writes_pending_stub(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault)
    assert result.sidecar_path is not None
    body = (vault / result.sidecar_path).read_text(encoding="utf-8")
    assert "media_type: audio" in body
    assert "evidence_file: " in body
    assert "extracted_by: pending" in body


def test_preserve_media_writes_actionable_stub_when_extraction_disabled(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    result = _preserve_media_stub(vault, filename="rec2.mp3")
    assert result.sidecar_path is not None
    body = (vault / result.sidecar_path).read_text(encoding="utf-8")
    assert "media_type: audio" in body
    assert "extracted_by: pending" in body


def test_worker_fills_pending_sidecar(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="call.mp3")
    sidecar = vault / result.sidecar_path
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda p, media_type=None, vault_root=None: extract.ExtractResult(
            text="discussion of the broken sink and water damage",
            media_type="audio",
            engine="faster-whisper:test",
        ),
    )
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(
        media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio")
    )

    body = sidecar.read_text(encoding="utf-8")
    assert "water damage" in body
    assert "extracted_by: faster-whisper:test" in body
    assert "extracted_by: pending" not in body


def test_extraction_compute_stays_outside_guard_and_sidecar_commit_is_inside(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="guarded.mp3")
    sidecar = vault / result.sidecar_path
    manager = _RecordingMutationManager(vault)
    original_update = preserve.update_sidecar_extraction

    def extract_outside(*_args, **_kwargs):
        assert manager.depth == 0
        manager.events.append("extract")
        return extract.ExtractResult(text="guarded transcript", media_type="audio", engine="test")

    def update_inside(*args, **kwargs):
        assert manager.depth > 0
        manager.events.append("sidecar-commit")
        return original_update(*args, **kwargs)

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(extract, "extract_text", extract_outside)
    monkeypatch.setattr(preserve, "update_sidecar_extraction", update_inside)

    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    worker._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert manager.events.index("extract") < manager.events.index("guard-enter")
    assert manager.events.index("guard-enter") < manager.events.index("sidecar-commit")
    assert manager.depth == 0


def test_sidecar_edit_during_extraction_makes_result_stale(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="edited-during-extraction.mp3")
    sidecar = vault / result.sidecar_path
    manager = _RecordingMutationManager(vault)

    def extract_after_user_edit(*_args, **_kwargs):
        sidecar.write_text(
            sidecar.read_text(encoding="utf-8") + "\nUSER CANONICAL EDIT\n",
            encoding="utf-8",
        )
        return extract.ExtractResult(
            text="STALE MACHINE TRANSCRIPT", media_type="audio", engine="test"
        )

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(extract, "extract_text", extract_after_user_edit)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    worker._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    body = sidecar.read_text(encoding="utf-8")
    assert "USER CANONICAL EDIT" in body
    assert "STALE MACHINE TRANSCRIPT" not in body
    assert "extracted_by: pending" in body


@pytest.mark.parametrize("extraction_fails", [False, True])
def test_parent_media_change_during_extraction_skips_stale_sidecar_commit(
    vault, monkeypatch: pytest.MonkeyPatch, extraction_fails: bool
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename=f"binary-stale-{extraction_fails}.mp3")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    manager = _RecordingMutationManager(vault)

    def extract_after_binary_change(*_args, **_kwargs):
        binary.write_bytes(b"replacement-media")
        if extraction_fails:
            raise RuntimeError("old media could not be decoded")
        return extract.ExtractResult(
            text="STALE BINARY TRANSCRIPT", media_type="audio", engine="test"
        )

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(extract, "extract_text", extract_after_binary_change)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    worker._process(
        media_worker._Job(
            binary_path=binary,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    body = sidecar.read_text(encoding="utf-8")
    assert binary.read_bytes() == b"replacement-media"
    assert "STALE BINARY TRANSCRIPT" not in body
    assert "extracted_by: failed:" not in body
    assert "extracted_by: pending" in body


def test_changed_media_identity_is_automatically_reconciled_without_retry(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_processing

    result = _preserve_media_stub(vault, filename="durable-stale.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    replacement = b"replacement identity"

    def _replace_during_asr(*_args, **_kwargs):
        binary.write_bytes(replacement)
        return extract.ExtractResult(
            text="[0:00] stale transcript",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(extract, "extract_text", _replace_during_asr)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0
    assert media_jobs.status(vault)["jobs"] == []
    assert "processing_state: pending" in sidecar.read_text(encoding="utf-8")

    reconciled = media_processing.reconcile_media(vault, binary)

    assert reconciled.state == media_jobs.PENDING
    assert reconciled.job_id is not None
    [pending] = media_jobs.status(vault)["jobs"]
    assert pending["state"] == media_jobs.PENDING
    frontmatter = _parsed_frontmatter(sidecar)
    assert frontmatter["binary_sha256"] == hashlib.sha256(replacement).hexdigest()


def test_pending_sidecar_edit_during_durable_asr_is_persisted_as_retryable_failure(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="durable-sidecar-stale.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path

    def _edit_pending_sidecar(*_args, **_kwargs):
        sidecar.write_text(
            sidecar.read_text(encoding="utf-8") + "\nUSER CANONICAL EDIT\n",
            encoding="utf-8",
        )
        return extract.ExtractResult(
            text="[0:00] stale transcript",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(extract, "extract_text", _edit_pending_sidecar)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    [failed] = media_jobs.status(vault)["jobs"]
    assert failed["state"] == media_jobs.FAILED
    assert failed["retryable"] is True
    assert failed["error"] == "stale extraction: sidecar content changed"
    assert failed["next_action"] == "review the sidecar changes, then retry media processing"
    assert "USER CANONICAL EDIT" in sidecar.read_text(encoding="utf-8")
    assert "stale transcript" not in sidecar.read_text(encoding="utf-8")


def test_combined_binary_and_sidecar_stale_remains_actionable(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="combined-stale.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path

    def _change_both(*_args, **_kwargs):
        binary.write_bytes(b"replacement media")
        sidecar.write_text(
            sidecar.read_text(encoding="utf-8") + "\nUSER CANONICAL EDIT\n",
            encoding="utf-8",
        )
        return extract.ExtractResult(
            text="[0:00] stale transcript",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(extract, "extract_text", _change_both)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    [failed] = media_jobs.status(vault)["jobs"]
    assert failed["state"] == media_jobs.FAILED
    assert failed["error"] == (
        "stale extraction: sidecar content changed, media identity changed"
    )
    assert failed["next_action"] == "review the sidecar changes, then retry media processing"


def test_transient_stable_commit_precondition_is_retried_without_repeating_asr(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="transient-commit.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    job = media_worker._Job(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    extraction_calls = 0
    commit_calls = 0
    real_commit = worker._commit_sidecar_extraction

    def _extract_once(*_args, **_kwargs):
        nonlocal extraction_calls
        extraction_calls += 1
        return extract.ExtractResult(
            text="[0:00] recovered transcript",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    def _transient_commit(*args, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            return False
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(extract, "extract_text", _extract_once)
    monkeypatch.setattr(worker, "_commit_sidecar_extraction", _transient_commit)

    outcome = worker._process(job)

    assert outcome.state == "complete"
    assert extraction_calls == 1
    assert commit_calls == 2
    assert "[0:00] recovered transcript" in sidecar.read_text(encoding="utf-8")


def test_clip_compute_stays_outside_guard_and_index_commit_is_inside(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    result = preserve.preserve_bytes(
        vault,
        scope="Yolo",
        category="photos",
        filename="guarded.jpg",
        data=b"\xff\xd8\xff",
        text="beach",
    )
    manager = _RecordingMutationManager(vault)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    def embed_outside(_path):
        assert manager.depth == 0
        manager.events.append("clip-embed")
        return np.ones(embeddings.CLIP_DIM, dtype=np.float32)

    def upsert_inside(*_args, **_kwargs):
        assert manager.depth > 0
        manager.events.append("clip-commit")

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(embeddings, "embed_image", embed_outside)
    monkeypatch.setattr(worker._clip_index, "upsert", upsert_inside)

    worker._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="image",
            do_ocr=False,
            do_clip=True,
        )
    )

    assert manager.events.index("clip-embed") < manager.events.index("guard-enter")
    assert manager.events.index("guard-enter") < manager.events.index("clip-commit")
    assert manager.depth == 0


def test_scene_artifacts_and_reembed_index_commit_use_guard(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    result = preserve.preserve_bytes(
        vault,
        scope="Yolo",
        category="video",
        filename="guarded.mp4",
        data=b"video",
        text="scene",
    )
    manager = _RecordingMutationManager(vault)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    frames = [(1.0, np.ones(embeddings.CLIP_DIM, dtype=np.float32))]
    pairs = [(object(), object())]

    def embed_scenes_outside(_path):
        assert manager.depth == 0
        manager.events.append("scene-embed")
        return frames, pairs

    def clip_commit_inside(*_args, **_kwargs):
        assert manager.depth > 0
        manager.events.append("clip-commit")

    def scene_commit_inside(*_args, **_kwargs):
        assert manager.depth > 0
        manager.events.append("scene-commit")
        return []

    def reembed_inside(*_args, **_kwargs):
        assert manager.depth > 0
        manager.events.append("reembed-commit")

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(media_worker.scene_frames, "scene_frames_enabled", lambda: True)
    monkeypatch.setattr(embeddings, "embed_video_scenes", embed_scenes_outside)
    monkeypatch.setattr(worker._clip_index, "upsert_frames", clip_commit_inside)
    monkeypatch.setattr(media_worker.scene_frames, "write_scene_frames", scene_commit_inside)
    monkeypatch.setattr(media_worker.index_sync, "upsert_after_write", reembed_inside)

    clip_job = media_worker._Job(
        binary_path=vault / result.path,
        sidecar_path=vault / result.sidecar_path,
        media_type="video",
        do_ocr=False,
        do_clip=True,
    )
    worker._process(clip_job)
    worker._process(
        media_worker._Job(
            binary_path=clip_job.binary_path,
            sidecar_path=clip_job.sidecar_path,
            media_type="video",
            do_ocr=False,
            do_clip=False,
            do_reembed=True,
        )
    )

    assert manager.events.index("scene-embed") < manager.events.index("guard-enter")
    assert "clip-commit" in manager.events
    assert "scene-commit" in manager.events
    assert "reembed-commit" in manager.events
    assert manager.depth == 0


def test_parent_media_change_during_clip_compute_skips_stale_index_and_scenes(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    result = preserve.preserve_bytes(
        vault,
        scope="Yolo",
        category="video",
        filename="changes-during-clip.mp4",
        data=b"original-video",
        text="scene",
    )
    binary = vault / result.path
    manager = _RecordingMutationManager(vault)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    def embed_then_replace(_path):
        assert manager.depth == 0
        binary.write_bytes(b"replacement-video-with-new-identity")
        return (
            [(1.0, np.ones(embeddings.CLIP_DIM, dtype=np.float32))],
            [(object(), object())],
        )

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(media_worker.scene_frames, "scene_frames_enabled", lambda: True)
    monkeypatch.setattr(embeddings, "embed_video_scenes", embed_then_replace)
    monkeypatch.setattr(
        worker._clip_index,
        "upsert_frames",
        lambda *_args, **_kwargs: pytest.fail("stale CLIP vectors were committed"),
    )
    monkeypatch.setattr(
        media_worker.scene_frames,
        "write_scene_frames",
        lambda *_args, **_kwargs: pytest.fail("stale scene frames were committed"),
    )

    worker._process(
        media_worker._Job(
            binary_path=binary,
            sidecar_path=vault / result.sidecar_path,
            media_type="video",
            do_ocr=False,
            do_clip=True,
        )
    )

    assert binary.read_bytes() == b"replacement-video-with-new-identity"
    assert manager.depth == 0


def test_media_commit_exception_releases_mutation_guard(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.writer_lease import LeaseConfig, LeaseManager

    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="commit-fails.mp3")
    manager = LeaseManager(LeaseConfig(state_dir=vault.parent / "exception-state"))
    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text="transcript", media_type="audio", engine="test"
        ),
    )

    def fail_commit(*_args, **_kwargs):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(preserve, "update_sidecar_extraction", fail_commit)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    with pytest.raises(RuntimeError, match="commit failed"):
        worker._process(
            media_worker._Job(
                binary_path=vault / result.path,
                sidecar_path=vault / result.sidecar_path,
                media_type="audio",
            )
        )

    with manager.mutation_guard(vault):
        pass


def test_worker_writes_speaker_labels_and_field(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # Opt-in diarization output round-trips: labeled turns into the sidecar text AND
    # the distinct speaker labels into a `speakers:` frontmatter list.
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="meeting2.mp3")
    sidecar = vault / result.sidecar_path
    monkeypatch.setattr(
        extract,
        "extract_text",
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
    w._process(
        media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio")
    )

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
    w._process(
        media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio")
    )

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
    w._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )
    assert seen["vault_root"] == vault


@pytest.mark.parametrize(
    ("filename", "media_type", "do_clip"),
    [("automatic.m4a", "audio", False), ("automatic.mp4", "video", True)],
)
def test_automatic_audio_and_video_jobs_explicitly_require_timestamps(
    vault,
    filename: str,
    media_type: str,
    do_clip: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    result = _preserve_media_stub(vault, filename=filename)
    calls: list[tuple[str, bool]] = []
    clip_calls: list[str] = []

    def _extract_spy(path, media_type=None, vault_root=None, *, timestamps=False):
        calls.append((media_type, timestamps))
        return extract.ExtractResult(
            text="[0:00] automatic transcript",
            media_type=media_type,
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "extract_text", _extract_spy)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    monkeypatch.setattr(worker, "_run_clip", lambda job: clip_calls.append(job.media_type))

    worker._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type=media_type,
            do_clip=do_clip,
        )
    )

    assert calls == [(media_type, True)]
    assert clip_calls == (["video"] if do_clip else [])
    assert "extracted_by: faster-whisper:test+timed" in (
        vault / result.sidecar_path
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("verification", "label"),
    [("anonymous", "Speaker A"), ("profile-matched", "Enrolled Speaker")],
)
def test_worker_persists_speaker_verification_metadata(
    vault,
    verification: str,
    label: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _preserve_media_stub(vault, filename=f"{verification}.m4a")
    extracted = extract.ExtractResult(
        text=f"[0:00] [{label}]: hello",
        media_type="audio",
        engine="faster-whisper:test+timed+diarized",
        speakers=[{"speaker": label, "start": 0.0, "end": 1.0, "text": "hello"}],
    )
    extracted.speaker_verification = verification
    monkeypatch.setattr(extract, "extract_text", lambda *_a, **_kw: extracted)

    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    worker._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    body = (vault / result.sidecar_path).read_text(encoding="utf-8")
    assert f"speaker_verification: {verification}" in body
    assert f"[{label}]: hello" in body


def test_worker_does_not_downgrade_human_verified_speaker_state(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="human-reviewed.m4a")
    sidecar = vault / result.sidecar_path
    sidecar.write_text(
        sidecar.read_text(encoding="utf-8").replace(
            "extracted_by: pending",
            "extracted_by: pending\nspeaker_verification: human-verified",
        ),
        encoding="utf-8",
    )
    extracted = extract.ExtractResult(
        text="[0:00] [Enrolled Speaker]: reviewed",
        media_type="audio",
        engine="faster-whisper:test+timed+diarized",
        speakers=[
            {
                "speaker": "Enrolled Speaker",
                "start": 0.0,
                "end": 1.0,
                "text": "reviewed",
            }
        ],
    )
    extracted.speaker_verification = "profile-matched"
    monkeypatch.setattr(extract, "extract_text", lambda *_a, **_kw: extracted)

    media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert "speaker_verification: human-verified" in sidecar.read_text(encoding="utf-8")


def test_worker_unavailable_leaves_pending(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="later.mp3")
    sidecar = vault / result.sidecar_path

    def unavailable(p, media_type=None, vault_root=None):
        raise extract.ExtractionUnavailable("engine not installed")

    monkeypatch.setattr(extract, "extract_text", unavailable)
    w = media_worker.MediaWorker(vault)
    w._process(
        media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio")
    )

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
        vault,
        scope="Yolo",
        category="photos",
        filename="p.jpg",
        data=b"\xff\xd8\xff",
        text="beach",
    )
    monkeypatch.setattr(
        embeddings, "embed_image", lambda p: np.ones(embeddings.CLIP_DIM, dtype=np.float32)
    )
    w = media_worker.MediaWorker(vault)
    w._process(
        media_worker._Job(
            binary_path=vault / res.path,
            sidecar_path=vault / res.sidecar_path,
            media_type="image",
            do_ocr=False,
            do_clip=True,
        )
    )
    assert embeddings.ClipIndex(vault).has(res.path)


def test_scan_unindexed_images_enqueues(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    preserve.preserve_bytes(
        vault, scope="Yolo", category="photos", filename="x.jpg", data=b"\xff\xd8\xff", text="t"
    )
    preserve.preserve_bytes(
        vault, scope="Yolo", category="photos", filename="y.png", data=b"\x89PNG", text="t"
    )
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


def test_child_retains_actionable_asr_dependency_failure(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    result = _preserve_media_stub(vault, filename="dependency-blocked.m4a")

    def _unavailable(*_args, **_kwargs):
        raise extract.ExtractionUnavailable("ASR backend: install the media extra")

    monkeypatch.setattr(extract, "extract_text", _unavailable)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    [status] = media_jobs.status(vault)["jobs"]
    assert status["state"] == media_jobs.BLOCKED
    assert status["attempts"] == 1
    assert status["retryable"] is True
    expected_error = "ExtractionUnavailable: ASR backend: install the media extra"
    assert status["error"] == expected_error
    assert status["next_action"] == "install the required media dependency, then retry"
    frontmatter = _parsed_frontmatter(vault / result.sidecar_path)
    assert frontmatter["processing_state"] == "blocked"
    assert frontmatter["processing_attempts"] == 1
    assert frontmatter["processing_error"] == expected_error
    assert frontmatter["processing_retryable"] is True
    assert (
        frontmatter["processing_next_action"]
        == "install the required media dependency, then retry"
    )
    assert frontmatter["evidence_file"] == result.path


def test_child_retains_actionable_corrupt_media_failure(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    result = _preserve_media_stub(vault, filename="corrupt.m4a")

    def _corrupt(*_args, **_kwargs):
        raise ValueError("invalid audio container: missing moov atom")

    monkeypatch.setattr(extract, "extract_text", _corrupt)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    [status] = media_jobs.status(vault)["jobs"]
    assert status["state"] == media_jobs.FAILED
    assert status["attempts"] == 1
    assert status["retryable"] is True
    expected_error = "ValueError: invalid audio container: missing moov atom"
    assert status["error"] == expected_error
    assert status["next_action"] == "repair or replace the media artifact, then retry"
    frontmatter = _parsed_frontmatter(vault / result.sidecar_path)
    assert frontmatter["processing_state"] == "failed"
    assert frontmatter["processing_attempts"] == 1
    assert frontmatter["processing_error"] == expected_error
    assert frontmatter["processing_retryable"] is True
    assert (
        frontmatter["processing_next_action"]
        == "repair or replace the media artifact, then retry"
    )
    assert frontmatter["evidence_file"] == result.path


def test_child_blocks_timestamp_renderer_failure_with_renderer_remediation(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    result = _preserve_media_stub(vault, filename="renderer-blocked.m4a")

    def _renderer_unavailable(*_args, **_kwargs):
        raise extract.TimestampRenderingUnavailable("timed renderer: unavailable")

    monkeypatch.setattr(extract, "extract_text", _renderer_unavailable)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    [status] = media_jobs.status(vault)["jobs"]
    assert status["state"] == media_jobs.BLOCKED
    assert status["error"] == (
        "TimestampRenderingUnavailable: timed renderer: unavailable"
    )
    assert status["next_action"] == "check the timestamp renderer, then retry"
    assert "repair or replace" not in status["next_action"]
    frontmatter = _parsed_frontmatter(vault / result.sidecar_path)
    assert frontmatter["processing_state"] == "blocked"
    assert frontmatter["processing_next_action"] == "check the timestamp renderer, then retry"


def test_success_refreshes_sidecar_before_completing_durable_job(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    result = _preserve_media_stub(vault, filename="indexed-success.m4a")
    sidecar = vault / result.sidecar_path
    events: list[str] = []
    original_write = preserve.batch_atomic_write
    original_complete = media_jobs.MediaJobStore.complete

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_a, **_kw: extract.ExtractResult(
            text="[0:00] indexed transcript",
            media_type="audio",
            engine="faster-whisper:test+timed",
        ),
    )

    def _write_and_refresh(*args, **kwargs):
        events.append("sidecar-write-and-index-refresh")
        return original_write(*args, **kwargs)

    def _complete_after_commit(store, job):
        body = sidecar.read_text(encoding="utf-8")
        assert "[0:00] indexed transcript" in body
        assert "extracted_by: faster-whisper:test+timed" in body
        events.append("durable-complete")
        return original_complete(store, job)

    monkeypatch.setattr(preserve, "batch_atomic_write", _write_and_refresh)
    monkeypatch.setattr(media_jobs.MediaJobStore, "complete", _complete_after_commit)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    assert events == ["sidecar-write-and-index-refresh", "durable-complete"]
    assert media_jobs.status(vault)["jobs"] == []


def test_claimed_job_skips_asr_when_sidecar_completed_before_worker_runs(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="claim-race.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=binary,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )
    claimed = store.claim_next()
    assert claimed is not None
    completed = sidecar.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: external-asr+timed"
    )
    completed += "\n[0:00] Transcript won the startup race.\n"
    sidecar.write_text(completed, encoding="utf-8")
    before_body = sidecar.read_text(encoding="utf-8").split("\n---\n", 1)[1]

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_a, **_kw: pytest.fail("ASR must not run for a completed transcript"),
    )
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    outcome = worker._process(claimed)

    assert outcome.state == "complete"
    after = sidecar.read_text(encoding="utf-8")
    assert after.split("\n---\n", 1)[1] == before_body
    assert "extracted_by: external-asr+timed" in after
    store.complete(claimed)
    assert media_jobs.status(vault)["jobs"] == []


def test_external_completed_transcript_written_during_asr_wins_final_commit_race(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="commit-race.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    job = media_worker._Job(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    external_bytes: bytes | None = None

    def external_completion(*_args, **_kwargs):
        nonlocal external_bytes
        completed = sidecar.read_text(encoding="utf-8").replace(
            "extracted_by: pending", "extracted_by: external-asr+timed"
        ).replace("processing_state: pending", "processing_state: completed")
        completed += "\n[0:00] External transcript must win.\n"
        sidecar.write_text(completed, encoding="utf-8")
        external_bytes = sidecar.read_bytes()
        return extract.ExtractResult(
            text="[0:00] Worker transcript must lose.",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "extract_text", external_completion)
    monkeypatch.setattr(media_worker, "_content_digest", lambda _path: "stable")

    outcome = worker._process(job)

    assert outcome.state == "stale"
    assert external_bytes is not None
    assert sidecar.read_bytes() == external_bytes
    assert "Worker transcript must lose" not in sidecar.read_text(encoding="utf-8")


def test_external_completed_transcript_survives_asr_failure_commit_race(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="failure-commit-race.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    job = media_worker._Job(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    external_bytes: bytes | None = None

    def external_completion_then_failure(*_args, **_kwargs):
        nonlocal external_bytes
        preserve.update_sidecar_extraction(
            vault,
            sidecar,
            text="[0:00] External transcript must survive ASR failure.",
            engine="external-asr+timed",
            speaker_verification="unavailable",
        )
        external_bytes = sidecar.read_bytes()
        raise ValueError("decoder failed after external completion")

    monkeypatch.setattr(extract, "extract_text", external_completion_then_failure)
    monkeypatch.setattr(media_worker, "_content_digest", lambda _path: "stable")

    outcome = worker._process(job)

    assert outcome.state == "stale"
    assert external_bytes is not None
    assert sidecar.read_bytes() == external_bytes
    content = sidecar.read_text(encoding="utf-8")
    assert "extracted_by: external-asr+timed" in content
    assert "processing_state: completed" in content
    assert "External transcript must survive ASR failure" in content
    assert "extracted_by: failed" not in content
    assert "decoder failed after external completion" not in content

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_a, **_kw: pytest.fail("completed transcript must not be reprocessed"),
    )
    assert worker._process(job).state == "complete"
    assert sidecar.read_bytes() == external_bytes


def test_transcript_index_refresh_failure_is_durable_and_retryable_without_asr(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import deferred_index, index_sync

    result = _preserve_media_stub(vault, filename="index-retry.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    calls = 0

    def transcribe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return extract.ExtractResult(
            text="[0:00] transcript survives index failure",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "extract_text", transcribe)
    monkeypatch.setattr(
        index_sync,
        "upsert_after_write",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("lexical index offline")),
    )
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    outcome = worker._process(
        media_worker._Job(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    )

    assert outcome.state == "complete"
    assert calls == 1
    assert "transcript survives index failure" in sidecar.read_text(encoding="utf-8")
    status = deferred_index.full_status(vault)
    assert status["count"] == 1
    assert status["next_action"] == "retry deferred index refresh"

    monkeypatch.setattr(index_sync, "upsert_after_write", lambda *_a, **_kw: True)
    assert index_sync.drain_deferred_work(vault) == 1
    assert deferred_index.full_status(vault)["count"] == 0
    assert calls == 1


def test_process_worker_restart_preserves_blocked_job_until_explicit_retry(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="restart-blocked.mp3")
    store = media_jobs.MediaJobStore(vault)
    job_id = store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == job_id
    error = "ExtractionUnavailable: install the ASR extra"
    store.mark(job_id, media_jobs.BLOCKED, error)
    monkeypatch.setattr(media_worker.MediaWorker, "_supervise", lambda _self: None)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)

    worker = media_worker.MediaWorker(vault, execution_mode="process")
    worker.start()
    try:
        [status] = media_jobs.status(vault)["jobs"]
        assert status["state"] == media_jobs.BLOCKED
        assert status["attempts"] == 1
        assert status["error"] == error
    finally:
        worker.stop()


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
        vault,
        scope="Yolo",
        category="audio",
        filename="meeting.mp3",
        data=b"X",
        text="quarterly review of the water damage claim",
    )
    find_module.clear_cache()
    hits = find_module.find(vault, query="water damage claim", mode="keyword")
    media = [h for h in hits if "meeting.mp3.md" in h.path]
    assert media, [h.path for h in hits]
    d = media[0].as_dict()
    assert d["media_type"] == "audio"
    assert d["media_file"].endswith("meeting.mp3")
