"""Tests for the audit_fix self-healing op.

audit_fix closes the lint-finds-but-doesn't-fix loop for safe categories.
These tests pin which categories auto-fix vs. which stay proposal-only.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from exomem import audit_fix as audit_fix_module


TODAY = dt.date(2026, 5, 28)


def _seed(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_audit_fix_canonicalizes_wikilinks_in_body(vault: Path) -> None:
    """KB-relative wikilinks in compiled material get rewritten to full form."""
    _seed(
        vault / "Knowledge Base" / "Notes" / "Insights" / "linker.md",
        "---\ntype: insight\nstatus: active\ncreated: 2026-05-28\n"
        "updated: 2026-05-28\ntags: []\n---\n"
        "# Linker\n\nSee [[Notes/Insights/progressive-disclosure-without-mode-fragmentation]].\n",
    )
    report = audit_fix_module.audit_fix(vault, today=TODAY)
    text = (
        vault / "Knowledge Base" / "Notes" / "Insights" / "linker.md"
    ).read_text(encoding="utf-8")
    assert (
        "[[Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation]]"
        in text
    )
    assert report.files_rewritten >= 1


def test_audit_fix_uses_kb_relative_links_for_nested_obsidian_root(vault: Path) -> None:
    (vault / "Knowledge Base" / ".obsidian").mkdir()
    target = (
        "Knowledge Base/Notes/Insights/"
        "progressive-disclosure-without-mode-fragmentation"
    )
    path = vault / "Knowledge Base" / "Notes" / "Insights" / "nested-root-linker.md"
    _seed(
        path,
        "---\ntype: insight\nstatus: active\ncreated: 2026-05-28\n"
        "updated: 2026-05-28\ntags: []\n"
        f'sources:\n  - "[[{target}]]"\n---\n'
        f"# Linker\n\nSee [[{target}]].\n",
    )

    audit_fix_module.audit_fix(vault, today=TODAY)

    text = path.read_text(encoding="utf-8")
    assert "[[Notes/Insights/progressive-disclosure-without-mode-fragmentation]]" in text
    assert "[[Knowledge Base/" not in text


def test_audit_fix_skips_sources_and_evidence(vault: Path) -> None:
    """Append-only trees are never touched even if they contain stale wikilinks."""
    # Plant a KB-relative wikilink in an existing source (which would normally
    # be rewritten if compiled material).
    src = vault / "Knowledge Base" / "Sources" / "Articles" / "2026-05-04-best-egcg-supplements.md"
    original = src.read_text(encoding="utf-8")
    src.write_text(
        original + "\nSee [[Notes/Insights/progressive-disclosure-without-mode-fragmentation]].\n",
        encoding="utf-8",
    )
    audit_fix_module.audit_fix(vault, today=TODAY)
    after = src.read_text(encoding="utf-8")
    # Stale form preserved — Sources are append-only.
    assert "[[Notes/Insights/progressive-disclosure-without-mode-fragmentation]]" in after


def test_audit_fix_backfills_production_log_created(vault: Path) -> None:
    """production-log missing `created` → use `started`."""
    _seed(
        vault / "Knowledge Base" / "Notes" / "Productions" / "Reels" / "2026-05-test.md",
        "---\ntype: production-log\nmedium: reels\nstatus: shipped\n"
        "started: 2026-05-15\nshipped: 2026-05-15\ntags: []\n---\n"
        "# Test\n\nBody.\n",
    )
    report = audit_fix_module.audit_fix(vault, today=TODAY)
    text = (
        vault / "Knowledge Base" / "Notes" / "Productions" / "Reels" / "2026-05-test.md"
    ).read_text(encoding="utf-8")
    assert "created: 2026-05-15" in text
    assert any(
        "created" in f.action and "2026-05-15" in f.action for f in report.fixed
    ), report.fixed


def test_audit_fix_backfills_research_note_status(vault: Path) -> None:
    """research-note missing `status:` → default 'active'."""
    _seed(
        vault / "Knowledge Base" / "Notes" / "Research" / "Q" / "no-status.md",
        "---\ntype: research-note\nproject: q\ncreated: 2026-05-28\n"
        "updated: 2026-05-28\ntags: []\n---\n# No status\n\nBody.\n",
    )
    audit_fix_module.audit_fix(vault, today=TODAY)
    text = (
        vault / "Knowledge Base" / "Notes" / "Research" / "Q" / "no-status.md"
    ).read_text(encoding="utf-8")
    assert "status: active" in text


def test_audit_fix_converts_singular_project_to_plural_on_patterns(vault: Path) -> None:
    """Pattern with `project: X` (singular) gets rewritten to `projects: [X]`."""
    _seed(
        vault / "Knowledge Base" / "Notes" / "Patterns" / "singular.md",
        "---\ntype: pattern\nproject: project-alpha\nstatus: active\n"
        "created: 2026-05-28\nupdated: 2026-05-28\ntags: []\n---\n"
        "# Singular project\n\nBody.\n",
    )
    audit_fix_module.audit_fix(vault, today=TODAY)
    text = (
        vault / "Knowledge Base" / "Notes" / "Patterns" / "singular.md"
    ).read_text(encoding="utf-8")
    assert "projects: [project-alpha]" in text
    assert "project: project-alpha" not in text  # singular removed


def test_audit_fix_computes_experiment_duration(vault: Path) -> None:
    """experiment missing `duration:` → compute from started + concluded."""
    _seed(
        vault / "Knowledge Base" / "Notes" / "Experiments" / "Food" / "2026-05-three-day.md",
        "---\ntype: experiment\ndomain: food\nstatus: active\n"
        "created: 2026-05-28\nupdated: 2026-05-28\n"
        "started: 2026-05-01\nconcluded: 2026-05-03\nn: 1\n"
        "tags: []\n---\n# Three day\n\nBody.\n",
    )
    audit_fix_module.audit_fix(vault, today=TODAY)
    text = (
        vault / "Knowledge Base" / "Notes" / "Experiments" / "Food" / "2026-05-three-day.md"
    ).read_text(encoding="utf-8")
    assert "duration:" in text
    assert "3 days" in text  # 2026-05-01 to 2026-05-03 inclusive


def test_audit_fix_is_idempotent_on_clean_vault(vault: Path) -> None:
    """Running audit_fix twice produces no second-pass changes."""
    audit_fix_module.audit_fix(vault, today=TODAY)
    second = audit_fix_module.audit_fix(vault, today=TODAY)
    # Second run shouldn't fix anything that the first run fixed.
    assert second.files_rewritten == 0
    # `fixed` counts can differ if first run made changes; on second run
    # the fixed list should be empty (everything's clean).
    assert len(second.fixed) == 0, second.fixed


def test_audit_fix_dry_run_makes_no_writes(vault: Path) -> None:
    """dry_run=True computes the report without touching disk."""
    _seed(
        vault / "Knowledge Base" / "Notes" / "Insights" / "would-change.md",
        "---\ntype: insight\nstatus: active\ncreated: 2026-05-28\n"
        "updated: 2026-05-28\ntags: []\n---\n"
        "# Would change\n\nSee [[Notes/Insights/progressive-disclosure-without-mode-fragmentation]].\n",
    )
    before = (
        vault / "Knowledge Base" / "Notes" / "Insights" / "would-change.md"
    ).read_text(encoding="utf-8")
    report = audit_fix_module.audit_fix(vault, dry_run=True, today=TODAY)
    after = (
        vault / "Knowledge Base" / "Notes" / "Insights" / "would-change.md"
    ).read_text(encoding="utf-8")
    assert before == after
    assert report.dry_run is True
    # Report still surfaces what WOULD change.
    assert report.files_rewritten >= 1


def test_audit_fix_proposes_orphan_entities_without_deleting(vault: Path) -> None:
    """orphan_entity is a propose-only category; never auto-deleted."""
    orphan_path = vault / "Knowledge Base" / "Entities" / "Concepts" / "Lonely.md"
    _seed(
        orphan_path,
        "---\ntype: entity\nentity_type: concept\nstatus: active\n"
        "created: 2026-05-28\nupdated: 2026-05-28\ntags: []\n---\n"
        "# Lonely\n\n## Summary\n\nNo inbound links.\n",
    )
    report = audit_fix_module.audit_fix(vault, today=TODAY)
    # File still exists.
    assert orphan_path.exists()
    # Finding surfaced in proposed list.
    assert any(
        p.category == "orphan_entity" and "Lonely" in p.path for p in report.proposed
    ), report.proposed


def test_audit_fix_report_summary_has_expected_keys(vault: Path) -> None:
    """Report summary should always include fixed + proposed totals."""
    report = audit_fix_module.audit_fix(vault, today=TODAY)
    assert "fixed" in report.summary
    assert "proposed" in report.summary
    assert isinstance(report.summary["fixed"], int)
    assert isinstance(report.summary["proposed"], int)
