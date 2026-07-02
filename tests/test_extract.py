"""extract.py — media-type dispatch + soft-fail (engines themselves aren't run here)."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import extract


@pytest.mark.parametrize(
    "name,expected",
    [
        ("rec.mp3", "audio"),
        ("rec.WAV", "audio"),
        ("clip.mp4", "video"),
        ("clip.mov", "video"),
        ("shot.png", "image"),
        ("scan.JPG", "image"),
        ("doc.pdf", "pdf"),
        ("report.docx", "docx"),
        ("sheet.xlsx", "xlsx"),
        ("deck.pptx", "pptx"),
        ("page.HTML", "html"),
        ("notes.txt", "text"),
        ("mail.eml", "email"),
        ("cal.ics", "calendar"),
        ("archive.zip", None),
        ("noext", None),
    ],
)
def test_media_type_for(name: str, expected: str | None) -> None:
    assert extract.media_type_for(name) == expected


def test_is_extractable() -> None:
    assert extract.is_extractable("a.mp4") is True
    assert extract.is_extractable("a.docx") is True
    assert extract.is_extractable("a.zip") is False


def test_extraction_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    assert extract.extraction_enabled() is False
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    assert extract.extraction_enabled() is True


def test_prewarm_loads_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    called: list[bool] = []
    monkeypatch.setattr(extract, "_get_whisper", lambda: called.append(True))
    extract.prewarm()
    assert called == [True]  # warmed eagerly


def test_prewarm_soft_fails_when_engine_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)

    def unavailable():
        raise extract.ExtractionUnavailable("faster-whisper not installed")

    monkeypatch.setattr(extract, "_get_whisper", unavailable)
    extract.prewarm()  # must not raise — a lean box just stays lazy


def test_prewarm_skipped_when_extraction_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    called: list[bool] = []
    monkeypatch.setattr(extract, "_get_whisper", lambda: called.append(True))
    extract.prewarm()
    assert called == []  # disabled → never touches the model


def test_extract_text_routes_by_media_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "_transcribe", lambda p, mt: extract.ExtractResult("T", mt, "whisper"))
    monkeypatch.setattr(extract, "_ocr_image", lambda p: extract.ExtractResult("O", "image", "tesseract"))
    monkeypatch.setattr(extract, "_extract_pdf", lambda p: extract.ExtractResult("P", "pdf", "pymupdf"))
    monkeypatch.setattr(extract, "_extract_document", lambda p, mt: extract.ExtractResult("D", mt, "markitdown"))
    monkeypatch.setattr(extract, "_extract_textfile", lambda p: extract.ExtractResult("X", "text", "text"))
    monkeypatch.setattr(extract, "_extract_eml", lambda p: extract.ExtractResult("E", "email", "email"))
    monkeypatch.setattr(extract, "_extract_ics", lambda p: extract.ExtractResult("C", "calendar", "ics"))

    assert extract.extract_text("x.mp3").engine == "whisper"
    assert extract.extract_text("x.mp4").media_type == "video"
    assert extract.extract_text("x.png").engine == "tesseract"
    assert extract.extract_text("x.pdf").text == "P"
    assert extract.extract_text("x.docx").engine == "markitdown"
    assert extract.extract_text("x.xlsx").media_type == "xlsx"
    assert extract.extract_text("x.html").engine == "markitdown"
    assert extract.extract_text("x.txt").text == "X"
    assert extract.extract_text("x.eml").engine == "email"
    assert extract.extract_text("x.ics").media_type == "calendar"


def test_extract_text_unknown_type_raises() -> None:
    with pytest.raises(extract.ExtractionUnavailable):
        extract.extract_text("x.zip")


def test_extract_textfile_reads_utf8(tmp_path) -> None:
    f = tmp_path / "note.txt"
    f.write_text("plain text marker zylo", encoding="utf-8")
    r = extract._extract_textfile(f)
    assert r.media_type == "text" and r.engine == "text"
    assert "zylo" in r.text


def test_extract_eml_pulls_headers_and_body(tmp_path) -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "a@example.com"
    msg["Subject"] = "Quokka invoice 7731"
    msg.set_content("body marker narwhal")
    p = tmp_path / "m.eml"
    p.write_bytes(msg.as_bytes())
    r = extract._extract_eml(p)
    assert "Quokka invoice 7731" in r.text  # subject header
    assert "narwhal" in r.text              # body
    assert r.media_type == "email"


def test_extract_ics_pulls_vevent_fields(tmp_path) -> None:
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
        "SUMMARY:Appsignal Catchup 7731\r\n"
        "DTSTART:20260513T153000\r\n"
        "LOCATION:TLN-Roseni-3\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    f = tmp_path / "e.ics"
    f.write_text(ics, encoding="utf-8")
    r = extract._extract_ics(f)
    assert "Appsignal Catchup 7731" in r.text
    assert "TLN-Roseni-3" in r.text
    assert r.media_type == "calendar"


def test_extract_document_soft_fails_on_bad_input(tmp_path) -> None:
    # markitdown missing → ExtractionUnavailable; present but file missing → convert raises
    # → still ExtractionUnavailable (wrapped). Either way, never a hard crash.
    with pytest.raises(extract.ExtractionUnavailable):
        extract._extract_document(tmp_path / "does-not-exist.docx", "docx")


# ---------------- optional: ASR speaker diarization (EXOMEM_DIARIZE, default OFF) ----


class _FakeSeg:
    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


class _FakeWhisper:
    def __init__(self, segs: list) -> None:
        self._segs = segs

    def transcribe(self, path):  # faster-whisper returns (segments_generator, info)
        return iter(self._segs), None


def test_diarize_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DIARIZE", raising=False)
    assert extract._diarize_enabled() is False
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    assert extract._diarize_enabled() is True


def test_transcribe_plain_when_diarize_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DIARIZE", raising=False)
    segs = [_FakeSeg("hello there", 0.0, 1.0), _FakeSeg("general kenobi", 1.0, 2.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "hello there general kenobi"
    assert r.speakers is None
    assert r.engine == f"faster-whisper:{extract.WHISPER_MODEL}"


def test_transcribe_labels_speakers_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("hello there", 0.0, 1.0), _FakeSeg("general kenobi", 1.0, 2.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    # Stub the raw diarization → two turns mapped to distinct speakers (no real model).
    monkeypatch.setattr(
        extract, "_run_diarization",
        lambda p: [(0.0, 1.0, "SPEAKER_00"), (1.0, 2.0, "SPEAKER_01")],
    )
    r = extract._transcribe(Path("x.wav"), "audio")
    assert "[Speaker A]: hello there" in r.text
    assert "[Speaker B]: general kenobi" in r.text
    assert r.engine.endswith("+diarized")
    assert r.speakers is not None
    assert [t["speaker"] for t in r.speakers] == ["Speaker A", "Speaker B"]
    assert r.speakers[0]["text"] == "hello there"


def test_transcribe_merges_consecutive_same_speaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("part one", 0.0, 1.0), _FakeSeg("part two", 1.0, 2.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    monkeypatch.setattr(
        extract, "_run_diarization",
        lambda p: [(0.0, 2.0, "SPEAKER_00")],  # one speaker the whole time
    )
    r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "[Speaker A]: part one part two"
    assert len(r.speakers) == 1


def test_transcribe_soft_fails_to_plain_when_diarization_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("solo line", 0.0, 1.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))

    # Sidecar venv not provisioned → real _run_diarization runs its locate-then-spawn path,
    # finds no sidecar interpreter, and soft-fails to the plain transcript.
    monkeypatch.setattr(extract, "_diarizer_sidecar_python", lambda: None)
    r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "solo line"
    assert r.speakers is None
    assert r.engine == f"faster-whisper:{extract.WHISPER_MODEL}"


# ---------------- optional: vision captioning (EXOMEM_VISION_CAPTION, default OFF) ----


def test_vision_caption_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_VISION_CAPTION", raising=False)
    assert extract._vision_caption_enabled() is False
    monkeypatch.setenv("EXOMEM_VISION_CAPTION", "1")
    assert extract._vision_caption_enabled() is True


def test_maybe_caption_ocr_only_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_VISION_CAPTION", raising=False)
    called: list = []
    monkeypatch.setattr(extract, "_caption_image", lambda p: called.append(p) or "nope")
    text, engine = extract._maybe_caption("ocr body", Path("x.png"))
    assert text == "ocr body"
    assert engine == "tesseract"
    assert called == []  # flag off → captioner is never invoked


def test_maybe_caption_prepends_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_VISION_CAPTION", "1")
    monkeypatch.setattr(extract, "_caption_image", lambda p: "a cat sitting on a mat")
    text, engine = extract._maybe_caption("INVOICE 7731", Path("x.png"))
    assert text == "a cat sitting on a mat\n\nINVOICE 7731"
    assert engine.startswith("tesseract+")


def test_maybe_caption_empty_ocr_returns_caption_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_VISION_CAPTION", "1")
    monkeypatch.setattr(extract, "_caption_image", lambda p: "a beach at sunset")
    text, engine = extract._maybe_caption("", Path("x.png"))
    assert text == "a beach at sunset"
    assert engine.startswith("tesseract+")


def test_maybe_caption_soft_fails_to_ocr_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_VISION_CAPTION", "1")
    monkeypatch.setattr(extract, "_CAPTIONER", None)

    def _no_dep():
        raise ImportError("transformers not installed")

    # Real _caption_image runs; its model loader raises ImportError → soft-fail.
    monkeypatch.setattr(extract, "_load_captioner", _no_dep)
    text, engine = extract._maybe_caption("ocr body", Path("x.png"))
    assert text == "ocr body"
    assert engine == "tesseract"
