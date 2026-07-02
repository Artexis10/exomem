"""Phase 3 audit improvements: code-block skipping, title fallback,
frontmatter compliance check."""

from __future__ import annotations

from pathlib import Path

from exomem import audit as audit_module


def _seed(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_audit_skips_wikilinks_inside_fenced_code(vault: Path) -> None:
    """`[[:space:]]` and similar regex/bash snippets must not flag broken."""
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8")
        + "\n\n```bash\n"
        + "grep '[[:space:]]' file.txt\n"
        + "[[Should/Not/Be/Flagged]]\n"
        + "```\n",
        encoding="utf-8",
    )

    report = audit_module.audit(vault, categories=["broken_wikilink"])
    bad = [
        f for f in report.findings
        if ":space:" in f.detail or "Should/Not/Be/Flagged" in f.detail
    ]
    assert not bad, [f.as_dict() for f in bad]


def test_audit_skips_wikilinks_inside_inline_code(vault: Path) -> None:
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8")
        + "\n\nUse `[[:digit:]]` for digits.\n",
        encoding="utf-8",
    )
    report = audit_module.audit(vault, categories=["broken_wikilink"])
    bad = [f for f in report.findings if ":digit:" in f.detail]
    assert not bad


def test_audit_resolves_bare_link_via_frontmatter_title(vault: Path) -> None:
    """A bare wikilink like `[[North-Led Content Manual]]` should resolve
    against a date-prefixed source file whose title matches — the real-vault
    case that produced 19 spurious broken_wikilink findings."""
    # Source file with date-prefixed slug + frontmatter title.
    _seed(
        vault / "Knowledge Base" / "Sources" / "Articles"
        / "2026-05-15-tu-test-manual.md",
        '---\ntype: source\nsource_type: article\n'
        'captured: 2026-05-15\ntitle: "Test Content Manual"\n'
        "tags: []\ningested_into: []\n---\n\n"
        "# Source: Test Content Manual\n\n## Capture\n\nbody\n",
    )
    # Compiled note that references the source by title.
    _seed(
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "title-fallback-test.md",
        "---\ntype: insight\nstatus: active\ncreated: 2026-05-15\n"
        "updated: 2026-05-15\ntags: []\n---\n\n"
        "# Title fallback test\n\nSee [[Test Content Manual]] for context.\n",
    )

    report = audit_module.audit(vault, categories=["broken_wikilink"])
    bad = [f for f in report.findings if "Test Content Manual" in f.detail]
    assert not bad, [f.as_dict() for f in bad]


def test_audit_ignores_anchors_in_target(vault: Path) -> None:
    """Wikilinks with `#anchor` should resolve against the file path,
    not the full string including the anchor."""
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8")
        + "\n\nSee [[Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation#mechanism]].\n",
        encoding="utf-8",
    )
    report = audit_module.audit(vault, categories=["broken_wikilink"])
    bad = [f for f in report.findings if "mechanism" in f.detail]
    assert not bad, [f.as_dict() for f in bad]


def test_audit_frontmatter_compliance_flags_missing_required_field(vault: Path) -> None:
    """A pattern page without `created:` should be flagged."""
    _seed(
        vault / "Knowledge Base" / "Notes" / "Patterns" / "incomplete-pattern.md",
        "---\ntype: pattern\nstatus: active\nupdated: 2026-05-15\n"
        "tags: []\n---\n\n# Incomplete pattern\n\nBody.\n",
    )
    report = audit_module.audit(vault, categories=["frontmatter_compliance"])
    matches = [
        f for f in report.findings
        if "incomplete-pattern" in f.path and "created" in f.detail
    ]
    assert matches, [f.as_dict() for f in report.findings]


def test_audit_frontmatter_compliance_flags_tenant_on_unexpected_project(vault: Path) -> None:
    """A `tenant:` set on a research-note whose project isn't the expected one
    should be flagged by the frontmatter_compliance check."""
    _seed(
        vault / "Knowledge Base" / "Notes" / "Research" / "Project Alpha" / "wrong-tenant.md",
        "---\ntype: research-note\nproject: project-alpha\ntenant: acme\n"
        "status: active\ncreated: 2026-05-15\nupdated: 2026-05-15\n"
        "tags: []\n---\n\n# Wrong tenant\n\nBody.\n",
    )
    report = audit_module.audit(vault, categories=["frontmatter_compliance"])
    matches = [
        f for f in report.findings
        if "wrong-tenant" in f.path and "tenant" in f.detail.lower()
    ]
    assert matches, [f.as_dict() for f in report.findings]


def test_audit_frontmatter_compliance_flags_singular_project_on_pattern(vault: Path) -> None:
    """Pattern using `project:` (singular) instead of `projects:` is flagged."""
    _seed(
        vault / "Knowledge Base" / "Notes" / "Patterns" / "singular-project-pattern.md",
        "---\ntype: pattern\nproject: project-alpha\nstatus: active\n"
        "created: 2026-05-15\nupdated: 2026-05-15\ntags: []\n---\n\n"
        "# Singular project\n\nBody.\n",
    )
    report = audit_module.audit(vault, categories=["frontmatter_compliance"])
    matches = [
        f for f in report.findings
        if "singular-project-pattern" in f.path and "projects" in f.detail
    ]
    assert matches, [f.as_dict() for f in report.findings]
