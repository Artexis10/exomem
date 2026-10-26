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


def _derivative_page(vault: Path, slug: str, *, status: str, cites: str | None) -> None:
    """A compiled note written straight to disk, so the test controls status and claim exactly."""
    sources = f"sources:\n  - '[[{cites}]]'\n" if cites else ""
    target = vault / "Knowledge Base" / "Notes" / "Insights" / f"{slug}.md"
    target.write_text(
        f"---\ntitle: {slug}\ntype: insight\nstatus: {status}\n{sources}---\n\n# {slug}\n\nA claim.\n",
        encoding="utf-8",
    )


def _name_derivatives(vault: Path, rel: str, *slugs: str) -> None:
    source = vault / rel
    entries = "".join(f"  - '[[Notes/Insights/{slug}]]'\n" for slug in slugs)
    source.write_text(
        source.read_text(encoding="utf-8").replace("ingested_into: []\n", f"ingested_into:\n{entries}"),
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


SOURCE = "Knowledge Base/Sources/Articles/imported-document.md"
CITED = "Sources/Articles/imported-document"


def _finding(vault: Path):
    report = audit_module.audit(vault, categories=["unprocessed_source"])
    return next((item for item in report.findings if item.path == SOURCE), None)


def test_a_live_derivative_that_cites_the_source_completes_it(vault: Path) -> None:
    _source(vault, SOURCE)
    _derivative_page(vault, "compiled-claim", status="active", cites=CITED)
    _name_derivatives(vault, SOURCE, "compiled-claim")

    assert _finding(vault) is None


def test_each_failing_derivative_reports_its_own_reason(vault: Path) -> None:
    _source(vault, SOURCE)
    _derivative_page(vault, "draft-claim", status="draft", cites=CITED)
    _derivative_page(vault, "archived-claim", status="archived", cites=CITED)
    _derivative_page(vault, "unclaimed-note", status="active", cites=None)
    _name_derivatives(
        vault, SOURCE, "draft-claim", "archived-claim", "unclaimed-note", "never-written"
    )

    finding = _finding(vault)

    assert {row["reference"]: row["reason"] for row in finding.meta["derivatives"]} == {
        "[[Notes/Insights/draft-claim]]": "pending",
        "[[Notes/Insights/archived-claim]]": "retired",
        "[[Notes/Insights/unclaimed-note]]": "claim_unresolved",
        "[[Notes/Insights/never-written]]": "missing",
    }


def test_a_dismissed_source_returns_when_a_derivative_changes_state(vault: Path) -> None:
    from exomem import commands

    def queue() -> list[dict]:
        items = commands.op_attention(vault, categories=["unprocessed_source"])["items"]
        return [item for item in items if item["path"] == SOURCE]

    _source(vault, SOURCE)
    _derivative_page(vault, "draft-claim", status="draft", cites=CITED)
    _name_derivatives(vault, SOURCE, "draft-claim")
    (item,) = queue()
    commands.op_triage_memory(vault, ref=item["ref"], action="dismiss", why="not now")
    assert queue() == []

    # The only derivative is now retired: a different state, so a different finding.
    _derivative_page(vault, "draft-claim", status="archived", cites=CITED)

    assert len(queue()) == 1
