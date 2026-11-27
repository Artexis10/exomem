"""An attach while the cell's own speech job runs: the job's later result never replaces the attach.

Only the speech engine is replaced; the sidecar, the job ledger, the child and parent handoff and
the atomic commit are real (the pattern of `test_media_reprocess`).
"""

from __future__ import annotations

import hashlib
import os

import pytest

from exomem import commands, extract, media_jobs, media_processing, media_worker, preserve, speech_attach
from exomem.vault import parse_frontmatter

CELL_TEXT = "[0:01] the cell's own transcript"
FAKE = b"FAKEBYTES"


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(speech_attach, "container_duration", lambda _path: 120.0)


def test_the_result_of_a_job_that_was_running_during_an_attach_is_not_published(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = preserve.preserve_bytes(vault, scope="Rental", category="audio", filename="rec.mp3", data=FAKE)
    binary, sidecar = vault / stub.path, vault / stub.sidecar_path
    relative = binary.relative_to(vault).as_posix()
    attached: list[dict] = []

    def attach_while_the_engine_runs(*_args, **_kwargs):
        attached.append(commands.op_process_media(
            vault, path=relative, operation="attach-speech", binary_sha256=hashlib.sha256(FAKE).hexdigest(),
            binary_size=len(FAKE), segments=[{"start": 0.0, "end": 4.0, "text": "the owner's own transcript"}],
            engine="the owner's engine",
        ))
        return extract.ExtractResult(CELL_TEXT, "audio", "faster-whisper:test+timed")

    monkeypatch.setattr(extract, "extract_text", attach_while_the_engine_runs)
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    media_processing.reconcile_media(vault, binary, explicit=False)

    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
    worker._drain_parent_results()

    now = sidecar.read_text(encoding="utf-8")
    assert attached[0]["outcome"] == "attached" and attached[0]["speech_job"] == "none"
    assert "the owner's own transcript" in now and CELL_TEXT not in now
    assert parse_frontmatter(now)[0]["extracted_by"] == "the owner's engine"
    assert not list(sidecar.parent.glob("*.prior-*.md"))
    assert media_jobs.MediaJobStore(vault).counts()["pending"] == 0


def test_a_queued_reprocess_goes_when_an_attach_replaces_the_transcript_it_would_replace(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | None] = []

    def cell_engine(*_args, language=None, **_kwargs):
        calls.append(language)
        return extract.ExtractResult(CELL_TEXT, "audio", "faster-whisper:test+timed")

    monkeypatch.setattr(extract, "extract_text", cell_engine)
    stub = preserve.preserve_bytes(vault, scope="Rental", category="audio", filename="rec.mp3", data=FAKE)
    binary, sidecar = vault / stub.path, vault / stub.sidecar_path
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    media_processing.reconcile_media(vault, binary, explicit=False)
    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
    worker._drain_parent_results()
    media_processing.reprocess_media(vault, binary, language="et")

    commands.op_process_media(
        vault, path=binary.relative_to(vault).as_posix(), operation="attach-speech",
        binary_sha256=hashlib.sha256(FAKE).hexdigest(), binary_size=len(FAKE),
        segments=[{"start": 0.0, "end": 4.0, "text": "the owner's own transcript"}], engine="the owner's engine",
    )
    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
    worker._drain_parent_results()

    assert calls == [None], "the queued reprocess did not run over the attached result"
    assert "the owner's own transcript" in sidecar.read_text(encoding="utf-8")
    assert media_jobs.MediaJobStore(vault).counts()["pending"] == 0
