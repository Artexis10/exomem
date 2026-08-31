"""preserve tool tests — Evidence/<scope>/<category>/ artifact capture."""

from __future__ import annotations

import base64
import datetime as dt
import io
from pathlib import Path

import pytest

from exomem import preserve as preserve_module
from exomem import vault as vault_module
from exomem.governance import companions

TODAY = dt.date(2026, 5, 25)
_ARTIFACT_SENTINEL = "<!-- exomem:sidecar-artifact -->"
_PRESERVED_NOTES_SENTINEL = "<!-- exomem:sidecar-preserved-notes -->"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_preserve_text_artifact_writes_file(vault: Path) -> None:
    """The artifact, and the page that makes it addressable.

    This used to assert `sidecar_path is None` when no description was
    supplied. That was true and it was the defect: an artifact with no page has
    no identifier, no `ingested_into`, and no corpus presence, because only
    `.md` is indexed — so citing it reported the source as missing. The
    property worth pinning is that the page describes the artifact rather than
    being an empty stub.
    """
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

    assert result.sidecar_path == (
        "Knowledge Base/Evidence/Yolo/letters/2026-05-25-warning-letter.txt.md"
    )
    page = _read(vault / result.sidecar_path)
    assert "type: source" in page
    assert "ingested_into: []" in page
    assert "original_filename: 2026-05-25-warning-letter.txt" in page
    assert f"binary_sha256: {result.hash}" in page
    assert f"binary_size: {result.size}" in page
    # Not an empty page: `schema.validate_source` treats an empty body as an
    # invalid source, and a page that says nothing about the artifact is worse
    # than useless next to it.
    assert "## Artifact" in page
    assert _ARTIFACT_SENTINEL in page
    # The artifact's own bytes are pointed at, never copied into the page.
    assert "cease and desist" not in page
    descriptor = vault_module.parse_frontmatter(page, strict=True)[0][
        "governance_companion"
    ]
    assert descriptor == {
        "version": 1,
        "state": "classified",
        "artifact_class": "media",
        "artifact_path": result.path,
        "artifact_sha256": result.hash,
        "artifact_size": result.size,
        "media_type": "text",
        "original_filename": "2026-05-25-warning-letter.txt",
        "semantics": {
            "projects": [],
            "tags": [],
            "types": [],
            "classes": [],
        },
    }
    assert companions.classify(vault, result.path).projects == ()


def test_cap_extracted_text_passthrough_under_limit() -> None:
    assert preserve_module._cap_extracted_text("short content") == "short content"


def test_cap_extracted_text_truncates_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preserve_module, "_MAX_EXTRACT_BYTES", 1000)
    out = preserve_module._cap_extracted_text("A" * 5000)
    assert len(out.encode("utf-8")) < 5000           # genuinely shrunk
    assert out.startswith("A" * 200)                 # kept the (most-relevant) start
    assert "truncated" in out and "/download" in out  # marker + pointer to the binary


def test_capped_sidecar_keeps_corpus_small(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The worker/backfill write path must cap, so one oversized doc can't poison find.
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setattr(preserve_module, "_MAX_EXTRACT_BYTES", 500)
    res = preserve_module.preserve_bytes(
        vault, scope="Test", category="docs", filename="huge.docx", data=b"BINARY"
    )
    sidecar = vault / res.sidecar_path
    descriptor_before = vault_module.parse_frontmatter(
        sidecar.read_text(encoding="utf-8"), strict=True
    )[0]["governance_companion"]
    preserve_module.update_sidecar_extraction(vault, sidecar, text="Z" * 20000, engine="markitdown")
    body = _read(sidecar)
    assert "truncated" in body
    assert body.count("Z") < 20000           # capped, not the full 20k chars
    assert len(body.encode("utf-8")) < 5000  # sidecar stays small → corpus protected
    assert vault_module.parse_frontmatter(body, strict=True)[0][
        "governance_companion"
    ] == descriptor_before


def test_update_sidecar_extraction_replaces_internal_headings_before_preserved_notes(
    vault: Path,
) -> None:
    result = preserve_module.preserve_bytes(
        vault, scope="Test", category="docs", filename="report.docx", data=b"BINARY"
    )
    sidecar = vault / result.sidecar_path
    sidecar.write_text(
        _read(sidecar)
        + "# Old H1\n\n## Old H2\n\nOld extraction.\n\n"
        + f"{_PRESERVED_NOTES_SENTINEL}\n## Preserved notes\n\nKeep this authored note.\n",
        encoding="utf-8",
    )
    first = "# First H1\n\n## First H2\n\nFirst extraction."
    second = "# Second H1\n\n## Second H2\n\nSecond extraction."

    preserve_module.update_sidecar_extraction(vault, sidecar, text=first, engine="markitdown")
    preserve_module.update_sidecar_extraction(vault, sidecar, text=second, engine="markitdown")

    body = _read(sidecar)
    assert body.count("## Extracted text") == 1
    assert body.count(second) == 1
    assert "First extraction." not in body
    assert "Old extraction." not in body
    assert body.count("Keep this authored note.") == 1


def test_update_sidecar_extraction_replaces_internal_headings_through_eof(
    vault: Path,
) -> None:
    result = preserve_module.preserve_bytes(
        vault, scope="Test", category="docs", filename="report.docx", data=b"BINARY"
    )
    sidecar = vault / result.sidecar_path
    sidecar.write_text(
        _read(sidecar) + "# Old H1\n\n## Old H2\n\nOld extraction.\n",
        encoding="utf-8",
    )
    first = "# First H1\n\n## First H2\n\nFirst extraction."
    second = "# Second H1\n\n## Second H2\n\nSecond extraction."

    preserve_module.update_sidecar_extraction(vault, sidecar, text=first, engine="markitdown")
    preserve_module.update_sidecar_extraction(vault, sidecar, text=second, engine="markitdown")

    body = _read(sidecar)
    assert body.count("## Extracted text") == 1
    assert body.count(second) == 1
    assert "First extraction." not in body
    assert "Old extraction." not in body


def test_update_sidecar_extraction_keeps_renderer_artifact_after_internal_headings(
    vault: Path,
) -> None:
    result = preserve_module.preserve_bytes(
        vault, scope="Test", category="docs", filename="report.docx", data=b"BINARY"
    )
    sidecar = vault / result.sidecar_path
    artifact = (
        "## Artifact\n\n- Original filename: `report.docx`\n"
        f"- SHA-256: `{'a' * 64}`\n"
    )
    sidecar.write_text(
        _read(sidecar) + "# Old H1\n\n## Old H2\n\nOld extraction.\n\n" + artifact,
        encoding="utf-8",
    )
    extraction = "# New H1\n\n## New H2\n\nNew extraction."

    preserve_module.update_sidecar_extraction(vault, sidecar, text=extraction, engine="markitdown")
    preserve_module.update_sidecar_extraction(vault, sidecar, text=extraction, engine="markitdown")

    body = _read(sidecar)
    assert body.count("## Extracted text") == 1
    assert body.count(extraction) == 1
    assert "Old extraction." not in body
    assert body.count(artifact) == 1


@pytest.mark.parametrize(
    ("heading", "updates_extraction"),
    [("## Artifact", True), ("## Preserved notes", False)],
)
def test_update_sidecar_extraction_does_not_treat_document_titles_as_boundaries(
    vault: Path, heading: str, updates_extraction: bool
) -> None:
    result = preserve_module.preserve_bytes(
        vault, scope="Test", category="docs", filename="collision.docx", data=b"BINARY"
    )
    sidecar = vault / result.sidecar_path
    sidecar.write_text(
        _read(sidecar) + f"# Old H1\n\n{heading}\n\nLiteral document section.\n",
        encoding="utf-8",
    )
    first = "# First H1\n\n## First H2\n\nFirst extraction."
    second = "# Second H1\n\n## Second H2\n\nSecond extraction."

    preserve_module.update_sidecar_extraction(vault, sidecar, text=first, engine="markitdown")
    preserve_module.update_sidecar_extraction(vault, sidecar, text=second, engine="markitdown")

    body = _read(sidecar)
    if updates_extraction:
        assert body.count(second) == 1
        assert "First extraction." not in body
        assert "Literal document section." not in body
    else:
        assert second not in body
        assert body.count("Literal document section.") == 1


@pytest.mark.parametrize(
    ("sentinel", "heading", "owned"),
    [
        (_ARTIFACT_SENTINEL, "## Artifact", "- SHA-256: `abc123`"),
        (_PRESERVED_NOTES_SENTINEL, "## Preserved notes", "Keep this authored note."),
    ],
)
def test_update_sidecar_extraction_keeps_sentinel_owned_boundary_after_collision(
    vault: Path, sentinel: str, heading: str, owned: str
) -> None:
    result = preserve_module.preserve_bytes(
        vault, scope="Test", category="docs", filename="owned.docx", data=b"BINARY"
    )
    sidecar = vault / result.sidecar_path
    sidecar.write_text(
        _read(sidecar)
        + "# Old H1\n\n## Artifact\n\nLiteral document section.\n\n"
        + f"{sentinel}\n{heading}\n\n{owned}\n",
        encoding="utf-8",
    )
    extraction = "# New H1\n\n## New H2\n\nNew extraction."

    preserve_module.update_sidecar_extraction(vault, sidecar, text=extraction, engine="markitdown")
    preserve_module.update_sidecar_extraction(vault, sidecar, text=extraction, engine="markitdown")

    body = _read(sidecar)
    assert body.count(extraction) == 1
    assert "Literal document section." not in body
    assert body.count(sentinel) == 1
    assert body.count(owned) == 1


def test_update_sidecar_extraction_keeps_complete_legacy_artifact_block() -> None:
    artifact = (
        "## Artifact\n\n- Original filename: `report.docx`\n"
        f"- SHA-256: `{'0' * 64}`\n- Bytes: 6\n"
    )
    content = f"## Extracted text\n\nOld extraction.\n\n{artifact}"

    updated = preserve_module._set_extracted_text(content, "New extraction.")

    assert "Old extraction." not in updated
    assert updated.endswith(artifact)


def test_update_sidecar_extraction_upgrades_empty_legacy_preserved_notes() -> None:
    content = "## Extracted text\n\n\n## Preserved notes\n\nKeep this authored note.\n"

    updated = preserve_module._set_extracted_text(content, "New extraction.")

    assert _PRESERVED_NOTES_SENTINEL in updated
    assert updated.endswith("## Preserved notes\n\nKeep this authored note.\n")


def test_preserve_binary_artifact_decodes_base64(vault: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\nfakepng"
    result = preserve_module.preserve(
        vault,
        scope="Private Family Case",
        category="scans",
        filename="2026-04-15-mri.png",
        content_base64=base64.b64encode(payload).decode("ascii"),
        today=TODAY,
    )
    written = vault / result.path
    assert written.exists()
    assert written.read_bytes() == payload
    sidecar = vault / (result.sidecar_path or "")
    descriptor = vault_module.parse_frontmatter(
        sidecar.read_text(encoding="utf-8"), strict=True
    )[0]["governance_companion"]
    assert descriptor["artifact_class"] == "media"
    assert descriptor["media_type"] == "image"
    assert descriptor["original_filename"] == "2026-04-15-mri.png"
    assert descriptor["artifact_path"] == result.path
    assert descriptor["artifact_sha256"] == result.hash
    assert descriptor["artifact_size"] == result.size
    assert descriptor["semantics"] == {
        "projects": [],
        "tags": [],
        "types": [],
        "classes": [],
    }


def test_preserve_with_description_writes_sidecar(vault: Path) -> None:
    result = preserve_module.preserve(
        vault,
        scope="Project Alpha",
        category="court-docs",
        filename="2026-05-01-summons.pdf",
        content_base64=base64.b64encode(b"%PDF-fake").decode("ascii"),
        description="Civil summons received via courier.",
        today=TODAY,
    )
    assert (
        result.sidecar_path
        == "Knowledge Base/Evidence/Project Alpha/court-docs/2026-05-01-summons.pdf.md"
    )
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
        scope="Private Family Case",
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


def test_preserve_refuses_oversized_stream_without_canonical_residue(
    vault: Path,
) -> None:
    folder = vault / "Knowledge Base" / "Evidence" / "Private" / "files"

    with pytest.raises(preserve_module.PreserveError) as exc:
        preserve_module.preserve_stream(
            vault,
            scope="Private",
            category="files",
            filename="too-large.bin",
            stream=io.BytesIO(b"five!"),
            max_bytes=4,
            today=TODAY,
        )

    assert exc.value.code == "TOO_LARGE"
    assert not (folder / "too-large.bin").exists()
    assert not (folder / "too-large.bin.md").exists()


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


def test_preserve_refuses_preexisting_companion_without_writing_artifact(
    vault: Path,
) -> None:
    folder = vault / "Knowledge Base" / "Evidence" / "Private" / "files"
    folder.mkdir(parents=True)
    companion = folder / "collision.bin.md"
    companion.write_text("existing companion", encoding="utf-8")

    with pytest.raises(preserve_module.PreserveError) as exc:
        preserve_module.preserve_bytes(
            vault,
            scope="Private",
            category="files",
            filename="collision.bin",
            data=b"new artifact",
            today=TODAY,
        )

    assert exc.value.code == "COMPANION_EXISTS"
    assert not (folder / "collision.bin").exists()
    assert companion.read_text(encoding="utf-8") == "existing companion"


def test_preserve_with_text_writes_searchable_sidecar(vault: Path) -> None:
    """The OCR companion: extracted `text` lands in the sidecar and is findable."""
    from exomem import find as find_module

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
