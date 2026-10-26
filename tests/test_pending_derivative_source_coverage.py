from __future__ import annotations

from pathlib import Path

from exomem import audit as audit_module
from exomem import note as note_module


def _source(vault: Path, rel: str) -> None:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: source\ningested_into: []\ncaptured: 2026-10-01\n---\n\n# Imported document\n",
        encoding="utf-8",
    )


def _unprocessed(vault: Path) -> set[str]:
    report = audit_module.audit(vault, categories=["unprocessed_source"])
    return {finding.path for finding in report.findings}


def test_a_draft_derivative_does_not_complete_its_source(vault: Path) -> None:
    rel = "Knowledge Base/Sources/Articles/imported-document.md"
    _source(vault, rel)
    assert rel in _unprocessed(vault)

    note_module.note(
        vault,
        content="## Claim\n\nNot yet compiled.\n",
        note_type="insight",
        title="Pending compilation",
        status="draft",
        sources=[rel.removesuffix(".md")],
    )

    assert rel in _unprocessed(vault)
