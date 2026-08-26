from __future__ import annotations

from pathlib import Path

from exomem import attention as attention_module
from exomem import audit as audit_module
from exomem import source_closure


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _derived(root: Path, rel: str, *sources: str) -> Path:
    source_lines = ["sources:", *(f'  - "[[{value}]]"' for value in sources)]
    return _write(
        root,
        rel,
        "\n".join(
            (
                "---",
                "type: insight",
                "exomem_id: 11111111-1111-4111-8111-111111111111",
                "status: active",
                *source_lines,
                "---",
                "",
                "# Legacy derivative",
                "",
            )
        ),
    )


def _source(root: Path, rel: str) -> Path:
    return _write(
        root,
        rel,
        "---\ntype: source\ningested_into: []\n---\n\n# Captured original\n",
    )


def test_explicit_audit_reports_bounded_unresolved_source_debt(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/legacy-derivative.md"
    missing = tuple(
        f"Knowledge Base/Sources/Other/missing-{index}"
        for index in range(source_closure.PUBLIC_UNRESOLVED_LIMIT + 2)
    )
    _derived(vault, rel, *missing)

    report = audit_module.audit(vault, categories=["unresolved_source_citation"])

    assert report.summary == {"unresolved_source_citation": 1}
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.path == rel
    assert finding.category == "unresolved_source_citation"
    assert finding.meta is not None
    assert finding.meta["unresolved_sources"] == list(
        missing[: source_closure.PUBLIC_UNRESOLVED_LIMIT]
    )
    assert finding.meta["unresolved_source_count"] == len(missing)
    assert finding.meta["unresolved_sources_truncated"] is True
    assert "capture" in (finding.proposed_fix or "").lower()
    assert "remove" in (finding.proposed_fix or "").lower()


def test_all_category_audit_includes_debt_without_generic_link_duplicate(
    vault: Path,
) -> None:
    rel = "Knowledge Base/Notes/Insights/legacy-derivative.md"
    missing = "Knowledge Base/Sources/Other/missing-original"
    _derived(vault, rel, missing)

    report = audit_module.audit(vault)

    same_page = [finding for finding in report.findings if finding.path == rel]
    assert sum(finding.category == "unresolved_source_citation" for finding in same_page) == 1
    assert not any(
        finding.category in {"broken_wikilink", "forward_reference"} and missing in finding.detail
        for finding in same_page
    )


def test_audit_is_read_only_and_clears_only_from_current_corpus_truth(
    vault: Path,
) -> None:
    rel = "Knowledge Base/Notes/Insights/legacy-derivative.md"
    missing = "Knowledge Base/Sources/Other/original"
    derived = _derived(vault, rel, missing)
    before = derived.read_bytes()

    first = audit_module.audit(vault, categories=["unresolved_source_citation"])
    _source(vault, "Knowledge Base/Sources/Other/similar-but-unrelated.md")
    second = audit_module.audit(vault, categories=["unresolved_source_citation"])

    assert derived.read_bytes() == before
    assert first.findings[0].meta["finding_id"] == second.findings[0].meta["finding_id"]

    _source(vault, missing + ".md")
    cleared = audit_module.audit(vault, categories=["unresolved_source_citation"])
    assert cleared.findings == []


def test_withheld_and_missing_sources_have_the_same_audit_projection(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/legacy-derivative.md"
    supplied = "Knowledge Base/Sources/Private/claimed-original"
    _derived(vault, rel, supplied)
    _source(vault, supplied + ".md")
    _write(
        vault,
        "Knowledge Base/_access.yaml",
        "excluded:\n  - Sources/Private\n",
    )

    withheld = audit_module.audit(
        vault,
        categories=["unresolved_source_citation"],
    ).findings[0]

    (vault / (supplied + ".md")).unlink()
    missing = audit_module.audit(
        vault,
        categories=["unresolved_source_citation"],
    ).findings[0]

    assert withheld.detail == missing.detail
    assert withheld.proposed_fix == missing.proposed_fix
    assert withheld.meta == missing.meta


def test_unresolved_source_debt_is_not_default_attention(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/legacy-derivative.md"
    _derived(vault, rel, "Knowledge Base/Sources/Other/missing-original")

    report = attention_module.attention(vault, record_surfacing=False)

    assert all(
        "unresolved_source_citation" not in item.categories
        for item in report.items
        if item.path == rel
    )
    assert "unresolved_source_citation" not in attention_module.DEFAULT_ATTENTION_CATEGORIES
