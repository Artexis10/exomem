"""`missing_sources` — an opt-in measurement of provenance-free compiled notes.

Deliberately a dedicated informational category rather than a required-frontmatter
entry: the required-field table is `warn` severity and is iterated by the repair
pass to backfill inferable values. Provenance must never be inferred, so it stays
out of that table — which the last test here pins.
"""

from __future__ import annotations

from pathlib import Path

from exomem import audit as audit_module

_FOLDER_BY_TYPE = {
    "insight": "Knowledge Base/Notes/Insights",
    "research-note": "Knowledge Base/Notes/Research/personal",
    "failure": "Knowledge Base/Notes/Failures",
    "pattern": "Knowledge Base/Notes/Patterns",
    "experiment": "Knowledge Base/Notes/Experiments/health",
    "production-log": "Knowledge Base/Notes/Productions/video",
}


def _write(
    vault: Path,
    name: str,
    *,
    page_type: str = "insight",
    sources: str = "[]",
    status: str = "active",
    tags: str = "[]",
) -> str:
    rel = f"{_FOLDER_BY_TYPE[page_type]}/{name}.md"
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = "project: personal\n" if page_type == "research-note" else ""
    path.write_text(
        f"---\ntype: {page_type}\nstatus: {status}\ncreated: 2026-07-10\n"
        f"updated: 2026-07-10\nsources: {sources}\ntags: {tags}\n{extra}---\n"
        f"\n## Finding\n\nA durable conclusion.\n",
        encoding="utf-8",
    )
    return rel


def _findings(vault: Path):
    return audit_module.audit(vault, categories=["missing_sources"]).findings


def test_provenance_free_note_is_flagged_at_info_severity(tmp_path: Path) -> None:
    rel = _write(tmp_path, "no-provenance")

    findings = _findings(tmp_path)

    assert [finding.path for finding in findings] == [rel]
    assert findings[0].severity == "info"
    assert findings[0].meta["signal_version"]
    assert "honest empty list" in findings[0].proposed_fix


def test_cited_provenance_clears_the_finding(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "cited",
        sources='["[[Knowledge Base/Sources/Articles/2026-05-04-example]]"]',
    )

    assert _findings(tmp_path) == []


def test_every_required_type_is_flagged(tmp_path: Path) -> None:
    expected = set()
    for page_type in ("research-note", "insight", "failure", "pattern"):
        expected.add(_write(tmp_path, f"bare-{page_type}", page_type=page_type))

    assert {finding.path for finding in _findings(tmp_path)} == expected


def test_types_without_a_provenance_requirement_are_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "an-experiment", page_type="experiment")
    _write(tmp_path, "a-production-log", page_type="production-log", status="published")

    assert _findings(tmp_path) == []


def test_inactive_hub_and_snapshot_material_is_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "a-draft", status="draft")
    _write(tmp_path, "superseded-note", status="superseded")
    _write(tmp_path, "archived-note", status="archived")
    _write(tmp_path, "retrieval-architecture")
    _write(tmp_path, "tagged-hub", tags="[hub]")
    _write(tmp_path, "tagged-snapshot", tags="[snapshot]")

    assert _findings(tmp_path) == []


def test_category_is_optional_and_absent_from_the_default_sweep(tmp_path: Path) -> None:
    _write(tmp_path, "no-provenance")

    assert "missing_sources" in audit_module.OPTIONAL_CATEGORIES
    assert "missing_sources" not in audit_module.ALL_CATEGORIES

    default_categories = {
        finding.category for finding in audit_module.audit(tmp_path).findings
    }
    assert "missing_sources" not in default_categories


def test_repair_pass_can_never_fabricate_provenance(tmp_path: Path) -> None:
    """`sources` must stay out of the table the repair pass backfills from."""
    for required in audit_module._REQUIRED_FIELDS_BY_TYPE.values():
        assert "sources" not in required
