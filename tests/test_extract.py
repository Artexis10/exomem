"""extract.py — media-type dispatch + soft-fail (engines themselves aren't run here)."""

from __future__ import annotations

import pytest

from kb_mcp import extract


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
        ("notes.txt", None),
        ("archive.zip", None),
        ("noext", None),
    ],
)
def test_media_type_for(name: str, expected: str | None) -> None:
    assert extract.media_type_for(name) == expected


def test_is_extractable() -> None:
    assert extract.is_extractable("a.mp4") is True
    assert extract.is_extractable("a.docx") is False


def test_extraction_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", "1")
    assert extract.extraction_enabled() is False
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    assert extract.extraction_enabled() is True


def test_prewarm_loads_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    called: list[bool] = []
    monkeypatch.setattr(extract, "_get_whisper", lambda: called.append(True))
    extract.prewarm()
    assert called == [True]  # warmed eagerly


def test_prewarm_soft_fails_when_engine_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)

    def unavailable():
        raise extract.ExtractionUnavailable("faster-whisper not installed")

    monkeypatch.setattr(extract, "_get_whisper", unavailable)
    extract.prewarm()  # must not raise — a lean box just stays lazy


def test_prewarm_skipped_when_extraction_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", "1")
    called: list[bool] = []
    monkeypatch.setattr(extract, "_get_whisper", lambda: called.append(True))
    extract.prewarm()
    assert called == []  # disabled → never touches the model


def test_extract_text_routes_by_media_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "_transcribe", lambda p, mt: extract.ExtractResult("T", mt, "whisper"))
    monkeypatch.setattr(extract, "_ocr_image", lambda p: extract.ExtractResult("O", "image", "tesseract"))
    monkeypatch.setattr(extract, "_extract_pdf", lambda p: extract.ExtractResult("P", "pdf", "pymupdf"))

    assert extract.extract_text("x.mp3").engine == "whisper"
    assert extract.extract_text("x.mp4").media_type == "video"
    assert extract.extract_text("x.png").engine == "tesseract"
    assert extract.extract_text("x.pdf").text == "P"


def test_extract_text_unknown_type_raises() -> None:
    with pytest.raises(extract.ExtractionUnavailable):
        extract.extract_text("x.txt")
