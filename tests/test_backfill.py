"""backfill-media — sidecar + OCR + CLIP for pre-existing Evidence files (engines stubbed)."""

from __future__ import annotations

import numpy as np
import pytest

from kb_mcp import backfill, embeddings, extract, preserve

REL = "Knowledge Base/Evidence/Old/photos/legacy.jpg"


def _drop_image(vault, rel=REL, data=b"\xff\xd8\xffOLD"):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _quiet(*a, **k):
    pass


def test_ensure_media_sidecar_creates_stub(vault) -> None:
    img = _drop_image(vault)
    sidecar, created = preserve.ensure_media_sidecar(vault, img)
    assert created and sidecar.exists()
    body = sidecar.read_text("utf-8")
    assert "media_type: image" in body
    assert f"evidence_file: {REL}" in body
    assert "extracted_by: none" in body
    # idempotent
    sidecar2, created2 = preserve.ensure_media_sidecar(vault, img)
    assert sidecar2 == sidecar and created2 is False


def test_backfill_dry_run_writes_nothing(vault) -> None:
    img = _drop_image(vault)
    stats = backfill.backfill_media(vault, dry_run=True, log_fn=_quiet)
    assert stats.sidecars_created == 1
    assert not img.with_name(img.name + ".md").exists()  # nothing actually written


def test_backfill_creates_sidecar_ocr_clip(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    img = _drop_image(vault)
    monkeypatch.setattr(
        extract, "extract_text",
        lambda p, media_type=None: extract.ExtractResult(text="legacy invoice acme", media_type="image", engine="tesseract"),
    )
    monkeypatch.setattr(embeddings, "embed_image", lambda p: np.ones(embeddings.CLIP_DIM, dtype=np.float32))

    stats = backfill.backfill_media(vault, log_fn=_quiet)
    assert (stats.sidecars_created, stats.extracted, stats.clip_indexed) == (1, 1, 1)
    body = img.with_name(img.name + ".md").read_text("utf-8")
    assert "legacy invoice acme" in body
    assert "extracted_by: tesseract" in body
    assert embeddings.ClipIndex(vault).has(REL)

    # idempotent: a second pass does nothing
    stats2 = backfill.backfill_media(vault, log_fn=_quiet)
    assert (stats2.sidecars_created, stats2.extracted, stats2.clip_indexed) == (0, 0, 0)
    assert stats2.skipped >= 1


FINANCE_REL = "Knowledge Base/Finance/invoices/inv.jpg"


def test_backfill_covers_non_evidence_kb_subtree(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # A binary that lives OUTSIDE Evidence/ (e.g. an invoice in Finance/) must
    # still be picked up — coverage is the whole KB, not just Evidence/.
    p = vault / FINANCE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xff\xd8\xffINV")
    monkeypatch.setattr(
        extract, "extract_text",
        lambda p, media_type=None: extract.ExtractResult(
            text="ugreen nexode 100w charger", media_type="image", engine="tesseract"
        ),
    )
    monkeypatch.setattr(embeddings, "embed_image", lambda p: np.ones(embeddings.CLIP_DIM, dtype=np.float32))

    stats = backfill.backfill_media(vault, log_fn=_quiet)
    assert stats.sidecars_created == 1
    assert stats.extracted == 1
    sidecar = p.with_name(p.name + ".md")
    assert sidecar.exists()
    body = sidecar.read_text("utf-8")
    assert "ugreen nexode 100w charger" in body
    assert f"evidence_file: {FINANCE_REL}" in body


def test_backfill_skips_schema_and_index_dirs(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cruft/config dirs must be pruned so the walk never indexes them.
    skipped = vault / "Knowledge Base" / "_Schema" / "_attachments" / "logo.png"
    skipped.parent.mkdir(parents=True, exist_ok=True)
    skipped.write_bytes(b"\xff\xd8\xffLOGO")
    stats = backfill.backfill_media(vault, dry_run=True, log_fn=_quiet)
    assert stats.sidecars_created == 0


def test_backfill_no_ocr_skips_extraction(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _drop_image(vault)
    monkeypatch.setattr(embeddings, "embed_image", lambda p: np.ones(embeddings.CLIP_DIM, dtype=np.float32))
    monkeypatch.setattr(
        extract, "extract_text",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("OCR ran under --no-ocr")),
    )
    stats = backfill.backfill_media(vault, do_ocr=False, log_fn=_quiet)
    assert (stats.sidecars_created, stats.extracted, stats.clip_indexed) == (1, 0, 1)


# ---------------- --rediarize: re-extract pre-diarization A/V sidecars ----------------

AUDIO_REL = "Knowledge Base/Evidence/Old/recordings/standup.wav"


def _drop_audio_with_plain_transcript(vault, rel=AUDIO_REL, engine="faster-whisper:large-v3"):
    """A pre-diarization state: audio binary + sidecar with a completed plain-ASR engine."""
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"RIFFfakewav")
    sidecar, _ = preserve.ensure_media_sidecar(vault, p)
    preserve.update_sidecar_extraction(vault, sidecar, text="plain transcript", engine=engine)
    return p, sidecar


def _diarized_result(*a, **k):
    return extract.ExtractResult(
        text="[Hugo]: hello there\n\n[Speaker B]: hi",
        media_type="audio",
        engine="faster-whisper:large-v3+diarized",
        speakers=[
            {"speaker": "Hugo", "start": 0.0, "end": 1.5, "text": "hello there"},
            {"speaker": "Speaker B", "start": 1.5, "end": 2.0, "text": "hi"},
        ],
    )


def test_rediarize_reextracts_plain_asr_sidecar(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DIARIZE", "1")
    _, sidecar = _drop_audio_with_plain_transcript(vault)
    monkeypatch.setattr(extract, "extract_text", _diarized_result)
    stats = backfill.backfill_media(vault, do_clip=False, rediarize=True, log_fn=_quiet)
    assert stats.rediarized == 1
    assert stats.extracted == 0  # counted separately from first-time extraction
    body = sidecar.read_text("utf-8")
    assert "extracted_by: faster-whisper:large-v3+diarized" in body
    assert "speakers: [Hugo, Speaker B]" in body  # the speakers= kwarg fix
    assert "[Hugo]: hello there" in body


def test_rediarize_second_run_is_noop(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DIARIZE", "1")
    _drop_audio_with_plain_transcript(vault)
    monkeypatch.setattr(extract, "extract_text", _diarized_result)
    backfill.backfill_media(vault, do_clip=False, rediarize=True, log_fn=_quiet)
    monkeypatch.setattr(
        extract, "extract_text",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-extracted a +diarized sidecar")),
    )
    stats2 = backfill.backfill_media(vault, do_clip=False, rediarize=True, log_fn=_quiet)
    assert stats2.rediarized == 0
    assert stats2.skipped >= 1


def test_needs_rediarize_classification(vault) -> None:
    p = vault / "Knowledge Base/Evidence/x/rec.wav"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"RIFF")
    sidecar, _ = preserve.ensure_media_sidecar(vault, p)  # extracted_by: none
    assert backfill._needs_rediarize(sidecar, "audio") is False  # not extracted yet → OCR path owns it
    preserve.update_sidecar_extraction(vault, sidecar, text="t", engine="faster-whisper:large-v3")
    assert backfill._needs_rediarize(sidecar, "audio") is True   # completed plain ASR
    assert backfill._needs_rediarize(sidecar, "video") is True
    assert backfill._needs_rediarize(sidecar, "image") is False  # never non-A/V
    assert backfill._needs_rediarize(sidecar, None) is False
    preserve.update_sidecar_extraction(vault, sidecar, text="t", engine="faster-whisper:large-v3+diarized")
    assert backfill._needs_rediarize(sidecar, "audio") is False  # done-marker
    preserve.update_sidecar_extraction(vault, sidecar, text="t", engine="failed: RuntimeError")
    assert backfill._needs_rediarize(sidecar, "audio") is False  # failed → normal retry path owns it
    preserve.update_sidecar_extraction(vault, sidecar, text="t", engine="no-audio")
    assert backfill._needs_rediarize(sidecar, "video") is False  # silent video: nothing to diarize
    assert backfill._needs_rediarize(sidecar.with_name("missing.md"), "audio") is False


def test_rediarize_guard_when_diarize_disabled(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DIARIZE", raising=False)
    _, sidecar = _drop_audio_with_plain_transcript(vault)
    before = sidecar.read_text("utf-8")
    monkeypatch.setattr(
        extract, "extract_text",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not extract when the guard trips")),
    )
    messages: list[str] = []
    stats = backfill.backfill_media(vault, do_clip=False, rediarize=True, log_fn=messages.append)
    assert stats.rediarized == 0
    assert sidecar.read_text("utf-8") == before
    assert any("KB_MCP_DIARIZE" in m for m in messages)


def test_rediarize_soft_fail_leaves_sidecar_and_stops(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # extract_text soft-fails diarization by contract: the result comes back WITHOUT
    # +diarized. The sidecar must keep its exact bytes and the pass must circuit-break.
    monkeypatch.setenv("KB_MCP_DIARIZE", "1")
    _, s1 = _drop_audio_with_plain_transcript(vault, rel="Knowledge Base/Evidence/r/a.wav")
    _, s2 = _drop_audio_with_plain_transcript(vault, rel="Knowledge Base/Evidence/r/b.wav")
    calls: list = []

    def _plain(p, media_type=None):
        calls.append(p)
        return extract.ExtractResult(
            text="plain again", media_type="audio", engine="faster-whisper:large-v3"
        )

    monkeypatch.setattr(extract, "extract_text", _plain)
    before1, before2 = s1.read_text("utf-8"), s2.read_text("utf-8")
    stats = backfill.backfill_media(vault, do_clip=False, rediarize=True, log_fn=_quiet)
    assert stats.rediarized == 0
    assert len(calls) == 1  # circuit-breaker: stop after the first soft-fail
    assert s1.read_text("utf-8") == before1
    assert s2.read_text("utf-8") == before2


def test_rediarize_does_not_rerun_clip(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DIARIZE", "1")
    rel = "Knowledge Base/Evidence/v/talk.mp4"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"FAKEVIDEO")
    sidecar, _ = preserve.ensure_media_sidecar(vault, p)
    preserve.update_sidecar_extraction(vault, sidecar, text="plain", engine="faster-whisper:large-v3")
    embeddings.ClipIndex(vault).upsert_frames(
        rel, [(0.0, np.ones(embeddings.CLIP_DIM, dtype=np.float32))], p.stat().st_mtime
    )
    monkeypatch.setattr(extract, "extract_text", _diarized_result)
    monkeypatch.setattr(
        embeddings, "embed_video_frames",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("CLIP re-ran for an indexed video")),
    )
    stats = backfill.backfill_media(vault, rediarize=True, log_fn=_quiet)
    assert stats.rediarized == 1
    assert stats.clip_indexed == 0


def test_rediarize_dry_run_counts_without_writing(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DIARIZE", "1")
    _, sidecar = _drop_audio_with_plain_transcript(vault)
    before = sidecar.read_text("utf-8")
    monkeypatch.setattr(
        extract, "extract_text",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run must not extract")),
    )
    stats = backfill.backfill_media(vault, do_clip=False, rediarize=True, dry_run=True, log_fn=_quiet)
    assert stats.rediarized == 1
    assert sidecar.read_text("utf-8") == before
