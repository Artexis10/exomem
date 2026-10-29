"""Explicit reprocess of a completed extraction, through the real job runtime.

Only the speech engine is replaced: a fake `extract_text` records the language it is
given and returns text that depends on it. Sidecars, jobs, the child/parent handoff and
the atomic commit are real.
"""

import os

import pytest

from exomem import (
    extract,
    media_jobs,
    media_processing,
    media_worker,
    preserve,
    prior_extraction,
)
from exomem.asr_facts import AsrFacts
from exomem.vault import parse_frontmatter

WRONG = "[0:01] wrong language text"
RIGHT = "[0:01] tere maailm"


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.delenv(extract.ASR_LANGUAGE_ENV, raising=False)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)


class _Engine:
    """A speech engine that mis-detects with no language and reads correctly with one."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.languages: list[str | None] = []
        self.fail_with: Exception | None = None
        self.during_run = lambda: None
        monkeypatch.setattr(extract, "extract_text", self)

    def __call__(self, path, *, media_type=None, vault_root=None, timestamps=False, language=None):
        self.languages.append(language)
        self.during_run()
        if self.fail_with is not None:
            raise self.fail_with
        if media_type == "pdf":
            return extract.ExtractResult(text="[page 1]\nnew words", media_type="pdf", engine="pymupdf")
        if language is None:
            facts = AsrFacts(
                language="en", language_probability=0.35, language_source="detected",
                speech_seconds=40.0, duration_seconds=61.5, policy="fw-policy-1",
            )
            return extract.ExtractResult(WRONG, "audio", "faster-whisper:test+timed", asr=facts)
        facts = AsrFacts(
            language=language, language_source="requested", language_requested=language,
            speech_seconds=40.0, duration_seconds=61.5, policy="fw-policy-1",
        )
        return extract.ExtractResult(f"{RIGHT} ({language})", "audio", "faster-whisper:test+timed", asr=facts)


def _run_to_idle(vault, worker) -> None:
    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
    worker._drain_parent_results()


def _completed(vault, filename: str):
    """A recording that the automatic pipeline has already transcribed."""
    stub = preserve.preserve_bytes(
        vault, scope="Rental", category="audio", filename=filename, data=b"FAKEBYTES"
    )
    binary, sidecar = vault / stub.path, vault / stub.sidecar_path
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    media_processing.reconcile_media(vault, binary, explicit=False)
    _run_to_idle(vault, worker)
    assert media_processing.has_completed_transcript(
        sidecar.read_text(encoding="utf-8"), media_type="audio"
    )
    return binary, sidecar, worker


def _frontmatter(path) -> dict:
    return parse_frontmatter(path.read_text(encoding="utf-8"))[0]


def test_reprocess_replaces_a_completed_transcript_and_keeps_the_old_one(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(monkeypatch)
    binary, sidecar, worker = _completed(vault, "rec.mp3")
    first = sidecar.read_text(encoding="utf-8")
    assert _frontmatter(sidecar)["asr_language_source"] == "detected"

    # The automatic pipeline leaves a valid completed transcript alone.
    media_processing.reconcile_media(vault, binary, explicit=False)
    _run_to_idle(vault, worker)
    assert engine.languages == [None]
    assert sidecar.read_text(encoding="utf-8") == first

    queued = media_processing.reprocess_media(vault, binary, language="et")
    assert queued.requeued == 1
    _run_to_idle(vault, worker)

    assert engine.languages == [None, "et"]
    now = sidecar.read_text(encoding="utf-8")
    assert RIGHT in now and WRONG not in now
    facts = _frontmatter(sidecar)
    assert (facts["asr_language"], facts["asr_language_source"]) == ("et", "requested")
    assert facts["asr_language_requested"] == "et"
    assert facts["asr_language_probability"] is None
    assert facts["asr_duration_seconds"] == 61.5 and facts["asr_policy"] == "fw-policy-1"
    assert media_jobs.status(vault)["jobs"] == []

    (link,) = prior_extraction.links_of(now)
    prior = vault / f"{link}.md"
    assert prior.parent == sidecar.parent
    kept = prior.read_text(encoding="utf-8")
    assert WRONG in kept and "faster-whisper:test+timed" in kept
    marker = _frontmatter(prior)
    assert marker["status"] == "superseded"
    assert marker["superseded_by"] == [f"[[{prior_extraction.link_to(vault, sidecar)}]]"]
    # It is a page about the old extraction, never an item to process.
    assert "media_type" not in marker and "evidence_file" not in marker

    # Nothing about the finished reprocess queues work again.
    media_processing.reconcile_media(vault, binary, explicit=False)
    media_processing.retry_media(vault, binary)
    worker.scan_pending()
    assert media_jobs.MediaJobStore(vault).counts()["pending"] == 0
    assert sidecar.read_text(encoding="utf-8") == now


def test_a_language_hint_is_pinned_until_a_later_request_replaces_it(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(monkeypatch)
    binary, sidecar, worker = _completed(vault, "rec.mp3")

    for requested in ("et", None, "fi"):
        media_processing.reprocess_media(vault, binary, language=requested)
        _run_to_idle(vault, worker)

    assert engine.languages == [None, "et", "et", "fi"]
    assert _frontmatter(sidecar)["asr_language_requested"] == "fi"
    assert len(prior_extraction.links_of(sidecar.read_text(encoding="utf-8"))) == 3


def test_an_unsupported_language_fails_the_job_and_leaves_the_transcript_alone(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(monkeypatch)
    binary, sidecar, worker = _completed(vault, "rec.mp3")
    before = sidecar.read_bytes()
    engine.fail_with = extract.UnsupportedLanguage("xx", ["en", "et"])

    media_processing.reprocess_media(vault, binary, language="xx")
    _run_to_idle(vault, worker)

    assert sidecar.read_bytes() == before
    (job,) = media_jobs.status(vault)["jobs"]
    assert job["state"] == media_jobs.FAILED
    assert "xx" in job["error"] and "language" in job["next_action"]
    assert not list(sidecar.parent.glob("*.prior-*.md"))


def test_a_transcript_edited_during_the_run_is_not_replaced(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(monkeypatch)
    binary, sidecar, worker = _completed(vault, "rec.mp3")

    def edit_the_transcript() -> None:
        sidecar.write_text(
            sidecar.read_text(encoding="utf-8").replace("wrong language text", "corrected by hand"),
            encoding="utf-8",
        )

    engine.during_run = edit_the_transcript
    media_processing.reprocess_media(vault, binary, language="et")
    _run_to_idle(vault, worker)

    now = sidecar.read_text(encoding="utf-8")
    assert "corrected by hand" in now and RIGHT not in now
    (job,) = media_jobs.status(vault)["jobs"]
    assert job["state"] == media_jobs.FAILED and "transcript changed" in job["error"]
    assert not list(sidecar.parent.glob("*.prior-*.md"))


def test_a_completed_pdf_is_reprocessed_but_takes_no_language(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(monkeypatch)
    stub = preserve.preserve_bytes(
        vault, scope="Rental", category="documents", filename="case.pdf", data=b"%PDF-1.4 fake"
    )
    binary, sidecar = vault / stub.path, vault / stub.sidecar_path
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    media_processing.reconcile_media(vault, binary, explicit=False)
    _run_to_idle(vault, worker)

    with pytest.raises(media_processing.MediaProcessingError) as refused:
        media_processing.reprocess_media(vault, binary, language="et")
    assert refused.value.code == "LANGUAGE_NOT_APPLICABLE"

    media_processing.reprocess_media(vault, binary)
    _run_to_idle(vault, worker)

    assert len(prior_extraction.links_of(sidecar.read_text(encoding="utf-8"))) == 1
    assert engine.languages == [None, None]


def test_a_job_store_from_before_reprocessing_opens_and_takes_a_request(vault) -> None:
    store = media_jobs.MediaJobStore(vault)
    with store._connect() as conn:  # the schema an earlier release created
        conn.execute("ALTER TABLE jobs DROP COLUMN reprocess")
        conn.execute("ALTER TABLE jobs DROP COLUMN language")
    stub = preserve.preserve_bytes(
        vault, scope="Rental", category="audio", filename="rec.mp3", data=b"FAKEBYTES"
    )
    job = media_jobs.MediaJob(
        binary_path=vault / stub.path, sidecar_path=vault / stub.sidecar_path, media_type="audio"
    )

    upgraded = media_jobs.MediaJobStore(vault)

    assert upgraded.request_reprocess(job, "et")
    claimed = upgraded.claim_next()
    assert claimed is not None and (claimed.reprocess, claimed.language) == (True, "et")
