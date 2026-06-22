"""preserve tool tests — Evidence/<scope>/<category>/ artifact capture."""

from __future__ import annotations

import base64
import datetime as dt
from pathlib import Path

import pytest

from kb_mcp import preserve as preserve_module


TODAY = dt.date(2026, 5, 25)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_preserve_text_artifact_writes_file(vault: Path) -> None:
    result = preserve_module.preserve(
        vault,
        scope="Yolo",
        category="letters",
        filename="2026-05-25-warning-letter.txt",
        content="Dear Mr. Kivi, please cease and desist.",
        today=TODAY,
    )
    written = vault / result.path
    assert written.exists()
    assert "cease and desist" in _read(written)
    assert result.sidecar_path is None  # no description supplied


def test_preserve_binary_artifact_decodes_base64(vault: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\nfakepng"
    result = preserve_module.preserve(
        vault,
        scope="Mother Cancer",
        category="scans",
        filename="2026-04-15-mri.png",
        content_base64=base64.b64encode(payload).decode("ascii"),
        today=TODAY,
    )
    written = vault / result.path
    assert written.exists()
    assert written.read_bytes() == payload


def test_preserve_with_description_writes_sidecar(vault: Path) -> None:
    result = preserve_module.preserve(
        vault,
        scope="Yolo",
        category="court-docs",
        filename="2026-05-01-summons.pdf",
        content_base64=base64.b64encode(b"%PDF-fake").decode("ascii"),
        description="Civil summons received via courier.",
        today=TODAY,
    )
    assert result.sidecar_path == "Knowledge Base/Evidence/Yolo/court-docs/2026-05-01-summons.pdf.md"
    sidecar = vault / result.sidecar_path
    assert sidecar.exists()
    text = _read(sidecar)
    assert "Civil summons received via courier" in text
    assert "type: source" in text


def test_preserve_md_artifact_uses_notes_sidecar(vault: Path) -> None:
    """Regression for the .md.md double-extension bug fixed in 35f07db.

    When the artifact filename already ends in .md, the sidecar must NOT
    become `<stem>.md.md` — use `<stem>-notes.md` instead.
    """
    result = preserve_module.preserve(
        vault,
        scope="Smoke",
        category="cases",
        filename="2026-05-25-md-artifact.md",
        content="raw markdown content",
        description="Why this exists",
        today=TODAY,
    )
    assert result.sidecar_path is not None
    assert result.sidecar_path.endswith("-notes.md")
    assert not result.sidecar_path.endswith(".md.md")
    assert (vault / result.sidecar_path).exists()


def test_preserve_pdf_artifact_uses_filename_md_sidecar(vault: Path) -> None:
    """Non-.md artifacts keep the original `<filename>.md` sidecar pattern."""
    result = preserve_module.preserve(
        vault,
        scope="Mother Cancer",
        category="labs",
        filename="2026-04-15-pathology.pdf",
        content_base64=base64.b64encode(b"%PDF-x").decode("ascii"),
        description="Path report from April clinic visit.",
        today=TODAY,
    )
    assert result.sidecar_path.endswith("2026-04-15-pathology.pdf.md")


def test_preserve_refuses_when_artifact_exists(vault: Path) -> None:
    """Evidence is append-only per SKILL rule 2."""
    preserve_module.preserve(
        vault,
        scope="Yolo",
        category="letters",
        filename="dupe.txt",
        content="first",
        today=TODAY,
    )
    with pytest.raises(preserve_module.PreserveError) as exc:
        preserve_module.preserve(
            vault,
            scope="Yolo",
            category="letters",
            filename="dupe.txt",
            content="second",
            today=TODAY,
        )
    assert exc.value.code == "ARTIFACT_EXISTS"


def test_preserve_refuses_both_content_modes(vault: Path) -> None:
    with pytest.raises(preserve_module.PreserveError) as exc:
        preserve_module.preserve(
            vault,
            scope="x",
            category="y",
            filename="z.txt",
            content="text",
            content_base64=base64.b64encode(b"bytes").decode("ascii"),
            today=TODAY,
        )
    assert exc.value.code == "INVALID_PRESERVE"


def test_preserve_refuses_neither_content_mode(vault: Path) -> None:
    with pytest.raises(preserve_module.PreserveError) as exc:
        preserve_module.preserve(
            vault,
            scope="x",
            category="y",
            filename="z.txt",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_PRESERVE"


def test_preserve_refuses_oversized_base64(vault: Path) -> None:
    """5MB decoded cap."""
    big = base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode("ascii")
    with pytest.raises(preserve_module.PreserveError) as exc:
        preserve_module.preserve(
            vault,
            scope="x",
            category="y",
            filename="big.bin",
            content_base64=big,
            today=TODAY,
        )
    assert exc.value.code == "TOO_LARGE"


def test_preserve_auto_creates_scope_and_category_dirs(vault: Path) -> None:
    """Evidence/<scope>/<category>/ folders materialize on first write."""
    folder = vault / "Knowledge Base" / "Evidence" / "NewScope" / "NewCat"
    assert not folder.exists()
    preserve_module.preserve(
        vault,
        scope="NewScope",
        category="NewCat",
        filename="first.txt",
        content="hello",
        today=TODAY,
    )
    assert folder.is_dir()


def test_preserve_with_text_writes_searchable_sidecar(vault: Path) -> None:
    """The OCR companion: extracted `text` lands in the sidecar and is findable."""
    from kb_mcp import find as find_module

    result = preserve_module.preserve(
        vault,
        scope="Yolo",
        category="photos",
        filename="2026-05-25-kitchen.jpg",
        content_base64=base64.b64encode(b"\xff\xd8\xff-fakejpeg").decode("ascii"),
        text="Photo shows a cockroach infestation under the sink, water damage on the cabinet.",
        today=TODAY,
    )
    assert result.sidecar_path == "Knowledge Base/Evidence/Yolo/photos/2026-05-25-kitchen.jpg.md"
    sidecar = vault / result.sidecar_path
    body = _read(sidecar)
    assert "## Extracted text" in body
    assert "cockroach infestation" in body
    # The binary itself isn't embeddable, but its text twin is keyword-findable.
    find_module.clear_cache()
    hits = find_module.find(vault, query="cockroach infestation", mode="keyword")
    assert any("2026-05-25-kitchen.jpg.md" in h.path for h in hits), [h.path for h in hits]


def test_preserve_text_without_description_still_writes_sidecar(vault: Path) -> None:
    """`text` alone (no description) is enough to trigger the sidecar."""
    result = preserve_module.preserve(
        vault,
        scope="Yolo",
        category="docs",
        filename="2026-05-25-letter.pdf",
        content_base64=base64.b64encode(b"%PDF-fake").decode("ascii"),
        text="Full body of the scanned letter, transcribed.",
        today=TODAY,
    )
    assert result.sidecar_path is not None
    body = _read(vault / result.sidecar_path)
    assert "## Extracted text" in body
    assert "## Description" not in body  # none supplied
    assert "transcribed" in body


def test_preserve_description_and_text_render_both_sections(vault: Path) -> None:
    result = preserve_module.preserve(
        vault,
        scope="Yolo",
        category="docs",
        filename="2026-05-25-both.pdf",
        content_base64=base64.b64encode(b"%PDF-fake").decode("ascii"),
        description="Civil summons.",
        text="IN THE DISTRICT COURT ... full transcribed body ...",
        today=TODAY,
    )
    body = _read(vault / result.sidecar_path)
    assert "## Description" in body
    assert "Civil summons." in body
    assert "## Extracted text" in body
    assert "DISTRICT COURT" in body


def test_preserve_appends_to_log(vault: Path) -> None:
    log_file = vault / "Knowledge Base" / "log.md"
    preserve_module.preserve(
        vault,
        scope="Yolo",
        category="logged",
        filename="logged.txt",
        content="x",
        description="Why preserved",
        today=TODAY,
    )
    text = _read(log_file)
    assert "## [2026-05-25] preserve | Evidence/Yolo/logged/logged.txt" in text
    assert "Why preserved" in text
