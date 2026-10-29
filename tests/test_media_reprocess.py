"""Explicit reprocess of a completed extraction, through the real job runtime.

Only the speech engine is replaced: a fake `extract_text` records the language it is
given and returns text that depends on it. Sidecars, jobs, the child/parent handoff and
the atomic commit are real.
"""

import os

import pytest

from exomem import (
    commands,
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
        self.multilingual: list[bool] = []
        self.fail_with: Exception | None = None
        self.during_run = lambda: None
        monkeypatch.setattr(extract, "extract_text", self)

    def __call__(
        self, path, *, media_type=None, vault_root=None, timestamps=False, language=None, multilingual=False
    ):
        self.languages.append(language)
        self.multilingual.append(multilingual)
        self.during_run()
        if self.fail_with is not None:
            raise self.fail_with
        if media_type == "pdf":
            return extract.ExtractResult(text="[page 1]\nnew words", media_type="pdf", engine="pymupdf")
        if multilingual:
            facts = AsrFacts(
                language="et", language_source="requested", language_mode="multilingual",
                speech_seconds=40.0, duration_seconds=61.5, policy="fw-policy-2",
            )
            return extract.ExtractResult(f"{RIGHT} (several)", "audio", "faster-whisper:test+timed", asr=facts)
        if language is None:
            facts = AsrFacts(
                language="en", language_probability=0.35, language_source="detected", language_mode="single",
                speech_seconds=40.0, duration_seconds=61.5, policy="fw-policy-1",
            )
            return extract.ExtractResult(WRONG, "audio", "faster-whisper:test+timed", asr=facts)
        facts = AsrFacts(
            language=language, language_source="requested", language_requested=language,
            language_mode="single", decoding="batched", speech_seconds=40.0, duration_seconds=61.5,
            policy="fw-policy-1",
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
    assert facts["asr_decoding"] == "batched"
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


def test_the_multilingual_mode_is_pinned_until_a_later_language_replaces_it(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(monkeypatch)
    binary, sidecar, worker = _completed(vault, "rec.mp3")

    media_processing.reprocess_media(vault, binary, multilingual=True)
    _run_to_idle(vault, worker)
    facts = _frontmatter(sidecar)
    assert (facts["asr_language_mode"], facts["asr_language_source"]) == ("multilingual", "requested")
    assert facts["asr_language"] == "et" and facts.get("asr_language_requested") is None

    media_processing.reprocess_media(vault, binary)  # no declaration: the pinned mode carries over
    _run_to_idle(vault, worker)
    assert engine.multilingual == [False, True, True]

    media_processing.reprocess_media(vault, binary, language="fi")
    _run_to_idle(vault, worker)
    facts = _frontmatter(sidecar)
    assert (engine.languages[-1], engine.multilingual[-1]) == ("fi", False)
    assert (facts["asr_language_mode"], facts["asr_language_requested"]) == ("single", "fi")


def test_a_recording_can_be_declared_multilingual_before_the_pipeline_reads_it(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(monkeypatch)
    stub = preserve.preserve_bytes(
        vault, scope="Rental", category="audio", filename="rec.mp3", data=b"FAKEBYTES"
    )
    binary, sidecar = vault / stub.path, vault / stub.sidecar_path
    worker = media_worker.MediaWorker(vault, execution_mode="process")

    media_processing.process_declared_media(vault, binary, multilingual=True)
    _run_to_idle(vault, worker)

    assert (engine.languages, engine.multilingual) == ([None], [True])
    assert _frontmatter(sidecar)["asr_language_mode"] == "multilingual"


def test_a_language_and_the_multilingual_mode_together_are_refused_before_any_work(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.cli_ops import OpError

    engine = _Engine(monkeypatch)
    binary, _sidecar, _worker = _completed(vault, "rec.mp3")
    relative = binary.relative_to(vault).as_posix()

    for operation in ("process", "reprocess"):
        with pytest.raises(OpError) as refused:
            commands.op_process_media(
                vault, path=relative, operation=operation, language="et", multilingual=True
            )
        assert refused.value.code == "LANGUAGE_MODE_CONFLICT"

    assert media_jobs.status(vault)["jobs"] == [] and engine.languages == [None]


def test_a_declaration_the_process_operation_cannot_apply_is_refused_not_ignored(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.cli_ops import OpError

    _Engine(monkeypatch)
    pdf = preserve.preserve_bytes(
        vault, scope="Rental", category="documents", filename="case.pdf", data=b"%PDF-1.4 fake"
    )
    binary, _sidecar, _worker = _completed(vault, "rec.mp3")

    with pytest.raises(OpError) as on_pdf:
        commands.op_process_media(vault, path=pdf.path, operation="process", multilingual=True)
    with pytest.raises(OpError) as on_complete:
        commands.op_process_media(
            vault, path=binary.relative_to(vault).as_posix(), operation="process", language="et"
        )

    assert on_pdf.value.code == "LANGUAGE_NOT_APPLICABLE"
    assert on_complete.value.code == "TRANSCRIPT_COMPLETE"
    assert media_jobs.status(vault)["jobs"] == []


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


def test_an_engine_that_cannot_detect_per_window_fails_the_multilingual_job_and_leaves_the_transcript_alone(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(monkeypatch)
    binary, sidecar, worker = _completed(vault, "rec.mp3")
    before = sidecar.read_bytes()
    engine.fail_with = extract.MultilingualUnsupported("onnx-asr")

    media_processing.reprocess_media(vault, binary, multilingual=True)
    _run_to_idle(vault, worker)

    assert sidecar.read_bytes() == before
    (job,) = media_jobs.status(vault)["jobs"]
    assert job["state"] == media_jobs.FAILED and job["retryable"] is False
    assert "onnx-asr" in job["error"] and "language" in job["next_action"]


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
    assert "unchanged" not in job["next_action"]  # the person's edit is the change
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

    assert upgraded.request_extraction(job, "et", "single", reprocess=True)
    claimed = upgraded.claim_next()
    assert claimed is not None and (claimed.reprocess, claimed.language) == (True, "et")


def test_a_job_store_from_before_the_language_mode_opens_and_takes_a_declaration(vault) -> None:
    store = media_jobs.MediaJobStore(vault)
    with store._connect() as conn:  # the schema of the release that added `language`
        conn.execute("ALTER TABLE jobs DROP COLUMN language_mode")
    stub = preserve.preserve_bytes(
        vault, scope="Rental", category="audio", filename="rec.mp3", data=b"FAKEBYTES"
    )
    job = media_jobs.MediaJob(
        binary_path=vault / stub.path, sidecar_path=vault / stub.sidecar_path, media_type="audio"
    )

    upgraded = media_jobs.MediaJobStore(vault)

    assert upgraded.request_extraction(job, None, "multilingual", reprocess=False)
    claimed = upgraded.claim_next()
    assert claimed is not None
    assert (claimed.reprocess, claimed.language, claimed.language_mode) == (False, None, "multilingual")


def test_a_prior_page_owes_no_compilation_and_a_deleted_page_number_is_not_reused(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Engine(monkeypatch)
    binary, sidecar, worker = _completed(vault, "rec.mp3")
    live_type = _frontmatter(sidecar)["type"]

    media_processing.reprocess_media(vault, binary, language="et")
    _run_to_idle(vault, worker)
    (first_link,) = prior_extraction.links_of(sidecar.read_text(encoding="utf-8"))
    first = vault / f"{first_link}.md"
    kept = _frontmatter(first)
    # The old text is a former extraction of the same source, so it carries the sidecar's own kind.
    assert kept["type"] == live_type and kept["source_type"] == _frontmatter(sidecar)["source_type"]

    flagged = commands.op_audit(vault, categories=["unprocessed_source"])["findings"]
    paths = {finding["path"] for finding in flagged}
    assert sidecar.relative_to(vault).as_posix() in paths  # a live Source still waits
    assert first.relative_to(vault).as_posix() not in paths

    first.unlink()  # the user deletes the page; its number stays taken
    media_processing.reprocess_media(vault, binary, language="fi")
    _run_to_idle(vault, worker)
    assert [link.rpartition(".prior-")[2] for link in prior_extraction.links_of(
        sidecar.read_text(encoding="utf-8")
    )] == ["1", "2"]
    assert not first.exists()


def test_a_blocked_reprocess_leaves_the_completed_sidecar_alone_and_is_not_retried_by_the_pass(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(monkeypatch)
    binary, sidecar, worker = _completed(vault, "rec.mp3")
    media_processing.reprocess_media(vault, binary, language="et")
    before = sidecar.read_bytes()

    try:
        media_processing.mark_processing_unavailable(
            vault, reason="MediaRuntimeUnavailable: no worker", next_action="start the worker"
        )
        assert sidecar.read_bytes() == before
        assert media_processing.has_completed_transcript(
            sidecar.read_text(encoding="utf-8"), media_type="audio"
        )
        (job,) = media_jobs.status(vault)["jobs"]
        assert job["state"] == media_jobs.BLOCKED and job["retryable"] is False
        assert "reprocess" in job["next_action"]

        # The completed transcript stays complete for reconcile, and the request is refused.
        media_processing.reconcile_media(vault, binary, explicit=False)
        assert sidecar.read_bytes() == before
        with pytest.raises(media_processing.MediaProcessingError) as refused:
            media_processing.reprocess_media(vault, binary, language="et")
        assert refused.value.code == "MEDIA_RUNTIME_UNAVAILABLE"
        # A reprocess row never holds a place in the automatic retry pass.
        assert media_jobs.MediaJobStore(vault).retryable_jobs() == []
    finally:
        media_processing.set_media_runtime_available(vault)

    media_processing.reprocess_media(vault, binary, language="et")
    _run_to_idle(vault, worker)
    assert engine.languages == [None, "et"]
    assert RIGHT in sidecar.read_text(encoding="utf-8")
    assert len(prior_extraction.links_of(sidecar.read_text(encoding="utf-8"))) == 1


def test_a_failed_reprocess_does_not_starve_the_retry_pass(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(monkeypatch)
    binary, sidecar, worker = _completed(vault, "rec.mp3")
    engine.fail_with = extract.UnsupportedLanguage("xx", ["en", "et"])
    media_processing.reprocess_media(vault, binary, language="xx")
    _run_to_idle(vault, worker)
    (job,) = media_jobs.status(vault)["jobs"]
    assert job["state"] == media_jobs.FAILED

    assert media_jobs.MediaJobStore(vault).retryable_jobs() == []
