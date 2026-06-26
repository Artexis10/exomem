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
    monkeypatch.setenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", "1")
    assert extract.extraction_enabled() is False
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    assert extract.extraction_enabled() is True


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
