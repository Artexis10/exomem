"""Repair of media sidecars that accumulated nested copies of themselves."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import sidecar_repair

FRONTMATTER = """---
type: source
media_type: pdf
evidence_file: Knowledge Base/Evidence/Case/report.pdf
extracted_by: pymupdf
processing_state: completed
---
"""

HEAD = """
# Evidence: report.pdf

Preserved under `Evidence/Case/`.
"""


def _sidecar(*levels: str) -> str:
    """Build a sidecar whose body nests `levels` under `## Preserved notes`."""
    body = ""
    for text in reversed(levels):
        block = f"{HEAD}\n## Extracted text\n\n{text}\n".lstrip("\n")
        body = block if not body else f"{block}\n## Preserved notes\n\n{body}"
    return FRONTMATTER + "\n" + body


def _sidecar_with_preserved_residual(extraction: str, residual: str) -> str:
    return (
        FRONTMATTER
        + "\n"
        + f"{HEAD}\n## Extracted text\n\n{extraction}\n\n"
        + f"## Preserved notes\n\n{residual}\n"
    )


def _empty_extraction_with_preserved_residual(residual: str) -> str:
    return (
        FRONTMATTER
        + "\n"
        + f"{HEAD}\n## Extracted text\n\n## Preserved notes\n\n{residual}\n"
    )


def test_clean_sidecar_is_untouched() -> None:
    content = _sidecar("The only extraction.")
    assert sidecar_repair.analyze(content, Path("report.pdf.md")) is None
    assert sidecar_repair.repair(content) == content


def test_identical_nested_copies_collapse_to_one() -> None:
    text = "The same extraction, four times over."
    content = _sidecar(text, text, text, text)

    damage = sidecar_repair.analyze(content, Path("report.pdf.md"))
    assert damage is not None
    assert damage.depth == 3
    assert damage.distinct_extractions == 1

    repaired = sidecar_repair.repair(content)
    assert repaired.count(sidecar_repair.PRESERVED_HEADING) == 0
    assert repaired.count(sidecar_repair.EXTRACTED_HEADING) == 1
    assert text in repaired
    assert len(repaired) < len(content)


def test_crlf_nested_copies_collapse_without_preserving_scaffolding() -> None:
    """Guarded byte reads must retain the old universal-newline semantics."""

    text = "The same extraction, twice over."
    content = _sidecar(text, text).replace("\n", "\r\n")

    damage = sidecar_repair.analyze(content, Path("report.pdf.md"))
    assert damage is not None

    repaired = sidecar_repair.repair(content)
    assert sidecar_repair.PRESERVED_HEADING not in repaired
    assert repaired.count(sidecar_repair.EXTRACTED_HEADING) == 1
    assert repaired.count(text) == 1
    assert sidecar_repair.analyze(repaired, Path("report.pdf.md")) is None


def test_crlf_frontmatter_bytes_are_preserved_during_repair() -> None:
    content = _sidecar("Extraction body.", "Extraction body.").replace("\n", "\r\n")
    frontmatter = content.split("\r\n---\r\n", 1)[0] + "\r\n---\r\n"

    repaired = sidecar_repair.repair(content)

    assert repaired.startswith(frontmatter)
    assert sidecar_repair.analyze(content, Path("report.pdf.md")) is not None
    assert sidecar_repair.repair_is_safe(content, repaired)
    assert sidecar_repair.analyze(_sidecar("clean").replace("\n", "\r\n"), Path("clean.md")) is None


def test_repair_is_idempotent() -> None:
    text = "Extraction body."
    once = sidecar_repair.repair(_sidecar(text, text, text))
    assert sidecar_repair.repair(once) == once
    assert sidecar_repair.analyze(once, Path("report.pdf.md")) is None


def test_longest_extraction_wins_when_the_top_level_was_blanked() -> None:
    """The buried copy can be the ONLY copy.

    A re-render wrote a fresh empty `## Extracted text` above the old body, so
    truncating at the first `## Preserved notes` — the obvious repair — would
    destroy the transcript outright.
    """
    buried = "The full transcript that only survives in a nested copy."
    content = _sidecar("", buried)

    damage = sidecar_repair.analyze(content, Path("report.pdf.md"))
    assert damage is not None
    assert damage.recovery_only is True
    assert damage.top_level_chars == 0

    repaired = sidecar_repair.repair(content)
    assert buried in repaired
    assert sidecar_repair.repair_is_safe(content, repaired)


def test_extraction_containing_h2_headings_survives() -> None:
    """markitdown emits `## <sheet name>` per spreadsheet tab.

    A reader that stops the extracted block at the next `## ` sees an empty block
    and silently drops the whole table.
    """
    table = "## On-Call Hours\n| # | Start | End |\n| --- | --- | --- |\n| 1 | a | b |"
    content = _sidecar(table, table)

    damage = sidecar_repair.analyze(content, Path("hours.xlsx.md"))
    assert damage is not None
    assert damage.recovered_chars == len(table)

    repaired = sidecar_repair.repair(content)
    assert table in repaired
    assert sidecar_repair.repair_is_safe(content, repaired)


def test_repeated_extraction_residual_collapses_without_preserved_notes() -> None:
    extraction = "# Report\n\n## Findings\n\nA result.\n\n### Detail\n\nMore detail."
    content = _sidecar_with_preserved_residual(extraction, "\n\n\n".join([extraction] * 720))

    repaired = sidecar_repair.repair(content)

    assert repaired.count(extraction) == 1
    assert sidecar_repair.PRESERVED_HEADING not in repaired
    assert sidecar_repair.repair_is_safe(content, repaired)
    assert sidecar_repair.repair(repaired) == repaired


def test_repeated_extraction_residual_with_prose_is_preserved() -> None:
    extraction = "# Report\n\n## Findings\n\nA result."
    prose = "The author confirmed this finding after the call."
    content = _sidecar_with_preserved_residual(extraction, f"{extraction}\n\n{prose}")

    repaired = sidecar_repair.repair(content)

    assert sidecar_repair.PRESERVED_HEADING in repaired
    assert repaired.count(extraction) == 2
    assert prose in repaired


def test_clean_sentinel_preserved_notes_are_not_repaired() -> None:
    content = (
        FRONTMATTER
        + "# Evidence: report.pdf\n\nPreserved under `Evidence/Case/`.\n\n"
        + "## Extracted text\n\nExtraction body.\n\n"
        + "<!-- exomem:sidecar-preserved-notes -->\n## Preserved notes\n\nAuthored note.\n"
    )

    assert sidecar_repair.repair(content) == content
    assert sidecar_repair.analyze(content, Path("sentinel.pdf.md")) is None


def test_sentinel_boundary_keeps_literal_preserved_notes_in_extraction() -> None:
    content = (
        FRONTMATTER
        + "# Evidence: report.pdf\n\nPreserved under `Evidence/Case/`.\n\n"
        + "## Extracted text\n\n# Document\n\n"
        + "<!-- exomem:sidecar-preserved-notes -->\n## Preserved notes\n\n"
        + "This is literal extracted content, not sidecar structure.\n\n"
        + "<!-- exomem:sidecar-preserved-notes -->\n"
        + "## Preserved notes\n\nAuthored note.\n"
    )

    assert sidecar_repair.repair(content) == content
    assert sidecar_repair.analyze(content, Path("collision.pdf.md")) is None


def test_empty_extraction_keeps_sentinel_marked_repeated_residual_without_candidate() -> None:
    block = "# Repeated document\n\n## Body\n\nExact content."
    content = _empty_extraction_with_preserved_residual("\n\n".join([block] * 3)).replace(
        "## Preserved notes",
        "<!-- exomem:sidecar-preserved-notes -->\n## Preserved notes",
    )

    repaired = sidecar_repair.repair(content)

    assert repaired == content
    damage = sidecar_repair.analyze(content, Path("sentinel.pdf.md"))
    assert damage is not None
    assert damage.recovered_chars == 0


@pytest.mark.parametrize(
    ("halves", "units"),
    [(2, 3), (2, 4), (2, 5), (3, 3), (3, 4), (4, 3)],
)
def test_empty_extraction_refuses_ambiguous_periodic_residual(
    halves: int, units: int
) -> None:
    half = "# Repeated half\n\n## Body\n\nExact content."
    unit = "\n\n".join([half] * halves)
    residual = "\n\n".join([unit] * units)
    content = _empty_extraction_with_preserved_residual(residual)

    repaired = sidecar_repair.repair(content)

    assert repaired == content
    damage = sidecar_repair.analyze(content, Path("periodic.pdf.md"))
    assert damage is not None
    assert damage.recovered_chars == 0
    assert sidecar_repair.repair_is_safe(content, repaired)


def test_empty_extraction_keeps_repeated_preserved_block_without_candidate() -> None:
    block = "# Product MVP Requirements\n\n## Overview\n\nA requirement.\n\n## Scope\n\nAnother requirement."
    content = _empty_extraction_with_preserved_residual("\n\n".join([block] * 100))

    repaired = sidecar_repair.repair(content)

    assert repaired == content
    damage = sidecar_repair.analyze(content, Path("requirements.xlsx.md"))
    assert damage is not None
    assert damage.recovered_chars == 0
    assert damage.source_reextraction_required is True
    assert sidecar_repair.repair_is_safe(content, repaired)


@pytest.mark.parametrize("copies", [2, 3])
def test_empty_extraction_keeps_residual_without_exact_three_copy_proof(copies: int) -> None:
    block = "# Repeated document\n\n## Body\n\nExact content."
    residual = "\n\n".join([block] * copies)
    if copies == 3:
        residual += "\n\nA genuine differing byte."
    content = _empty_extraction_with_preserved_residual(residual)

    repaired = sidecar_repair.repair(content)

    assert sidecar_repair.PRESERVED_HEADING in repaired
    assert residual in repaired


def test_hand_written_prose_is_kept_once() -> None:
    content = _sidecar("Extraction.", "Extraction.")
    content = content.replace(
        "## Preserved notes\n\n",
        "## Preserved notes\n\nKeep the original cassette label: Side B.\n\n",
        1,
    )

    repaired = sidecar_repair.repair(content)
    assert repaired.count("Keep the original cassette label: Side B.") == 1
    assert repaired.count(sidecar_repair.PRESERVED_HEADING) == 1


def test_frontmatter_is_preserved_verbatim() -> None:
    """A still-`pending` sidecar must stay queued for a real re-extraction."""
    content = _sidecar("Recovered.", "Recovered.").replace(
        "extracted_by: pymupdf", "extracted_by: pending"
    )
    repaired = sidecar_repair.repair(content)
    assert repaired.startswith(content.split("\n---\n")[0])
    assert "extracted_by: pending" in repaired
    assert "processing_state: completed" in repaired


def test_repair_is_safe_rejects_a_shorter_transcript() -> None:
    original = _sidecar("A much longer transcript than the replacement.")
    assert sidecar_repair.repair_is_safe(original, _sidecar("short")) is False
    assert sidecar_repair.repair_is_safe(original, original) is True


def test_iter_media_sidecars_finds_only_binary_sidecars(tmp_path: Path) -> None:
    evidence = tmp_path / "Knowledge Base" / "Evidence" / "Case"
    evidence.mkdir(parents=True)
    (evidence / "report.pdf.md").write_text("x", encoding="utf-8")
    (evidence / "index.md").write_text("x", encoding="utf-8")
    (evidence / "notes.md").write_text("x", encoding="utf-8")

    found = {p.name for p in sidecar_repair.iter_media_sidecars(tmp_path)}
    assert found == {"report.pdf.md"}
