"""audit tool tests — findings must carry the affected page path, and
parent-vault wikilinks must resolve (SKILL.md rule 1 allows them)."""

from __future__ import annotations

from pathlib import Path

from exomem import audit as audit_module


def test_audit_findings_have_non_empty_path(vault: Path) -> None:
    """Regression: every finding must carry the path of the file it concerns.

    Previously _parse_page set rel_path="" and relied on find() to fill it.
    audit called _parse_page directly, so every finding's `path` was empty —
    making the report un-triagable.
    """
    # Plant a broken wikilink in an existing fixture file.
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    original = insight.read_text(encoding="utf-8")
    insight.write_text(
        original + "\n\nDangling: [[Knowledge Base/Notes/Insights/does-not-exist]]\n",
        encoding="utf-8",
    )

    report = audit_module.audit(vault, categories=["broken_wikilink"])
    assert report.findings, "expected at least one broken_wikilink finding"
    for f in report.findings:
        assert f.path, f"finding has empty path: {f.as_dict()}"
        assert f.path.startswith("Knowledge Base/"), f.path


def test_audit_does_not_flag_parent_vault_wikilinks(vault: Path, tmp_path: Path) -> None:
    """Wikilinks to curated sibling folders outside Knowledge Base/ (read-only
    material, e.g. a `Reference/` tree) are legitimate per SKILL.md rule 1 and
    must not be flagged.
    """
    # Create a parent-vault page outside Knowledge Base/.
    (vault / "Library").mkdir()
    (vault / "Library" / "AI Systems & Architecture.md").write_text(
        "# Reference page\n", encoding="utf-8"
    )

    # Link to it from a compiled note.
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8")
        + "\n\nSee [[Library/AI Systems & Architecture]] "
        + "and [[AI Systems & Architecture]] (bare name).\n",
        encoding="utf-8",
    )

    report = audit_module.audit(vault, categories=["broken_wikilink"])
    bad = [f for f in report.findings if "AI Systems" in f.detail]
    assert not bad, [f.as_dict() for f in bad]


def test_audit_resolves_explicit_extension_attachment_links(vault: Path) -> None:
    """A wikilink with an explicit non-.md extension pointing at a file that
    exists on disk is a valid Obsidian attachment link and must not be flagged.

    Regression: the resolution set was built from .md files only (and skipped
    `_attachments/`), so `[[.../foo.pdf]]` always false-positived even when the
    PDF was present. Mirrors Obsidian, which resolves `[[foo.pdf]]` to the file.
    """
    att_dir = vault / "Knowledge Base" / "Sources" / "Articles" / "_attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    (att_dir / "egcg-supplements.pdf").write_bytes(b"%PDF-1.4 fake\n")

    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8")
        + "\n\nReference: "
        + "[[Knowledge Base/Sources/Articles/_attachments/egcg-supplements.pdf]]\n",
        encoding="utf-8",
    )

    report = audit_module.audit(vault, categories=["broken_wikilink"])
    bad = [f for f in report.findings if "egcg-supplements.pdf" in f.detail]
    assert not bad, [f.as_dict() for f in bad]


def test_audit_flags_missing_attachment_with_explicit_extension(vault: Path) -> None:
    """The attachment fallback resolves only files that exist — an explicit-
    extension link to an absent file is still a genuine broken link."""
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8")
        + "\n\n[[Knowledge Base/Sources/Articles/_attachments/missing.pdf]]\n",
        encoding="utf-8",
    )

    report = audit_module.audit(vault, categories=["broken_wikilink"])
    bad = [f for f in report.findings if "missing.pdf" in f.detail]
    assert bad, "expected the missing .pdf link to stay flagged"


def test_audit_flags_extensionless_link_even_if_nonmd_file_exists(vault: Path) -> None:
    """Extension-less wikilinks resolve only to .md notes, matching Obsidian:
    `[[Foo]]` is broken even if `Foo.eml` exists — the link must carry the
    extension to target the attachment. Guards against over-resolving."""
    ev = vault / "Knowledge Base" / "Evidence" / "Scope"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "Formal Warning.eml").write_text("raw email", encoding="utf-8")

    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8")
        + "\n\n[[Evidence/Scope/Formal Warning]]\n",
        encoding="utf-8",
    )

    report = audit_module.audit(vault, categories=["broken_wikilink"])
    bad = [f for f in report.findings if "Formal Warning" in f.detail]
    assert bad, "extension-less link to a .eml must stay flagged (Obsidian parity)"


def test_broken_link_in_append_only_file_is_info_not_warn(vault: Path) -> None:
    """Broken wikilinks inside append-only trees (Sources/, Evidence/) can't be
    repaired in place, so audit surfaces them at `info` severity + meta.immutable
    — keeping them out of the actionable (`warn`) set."""
    src = (
        vault / "Knowledge Base" / "Sources" / "Articles"
        / "2026-05-31-immutable-link-src.md"
    )
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "---\ntype: source\nstatus: active\ncreated: 2026-05-31\n"
        "updated: 2026-05-31\ningested_into: []\n---\n\n"
        "# Test source\n\nDangling: [[Knowledge Base/Notes/Insights/no-such-target-xyz]]\n",
        encoding="utf-8",
    )

    report = audit_module.audit(vault, categories=["broken_wikilink"])
    hits = [f for f in report.findings if "immutable-link-src" in f.path]
    assert hits, "expected the source's broken link to be flagged"
    f = hits[0]
    assert f.severity == "info", f.as_dict()
    assert f.meta and f.meta.get("immutable") is True, f.as_dict()


def test_broken_link_in_editable_file_stays_warn(vault: Path) -> None:
    """Guard: broken links in editable compiled notes remain actionable `warn`."""
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8")
        + "\n\n[[Knowledge Base/Notes/Insights/no-such-target-abc]]\n",
        encoding="utf-8",
    )

    report = audit_module.audit(vault, categories=["broken_wikilink"])
    hits = [f for f in report.findings if "no-such-target-abc" in f.detail]
    assert hits, "expected the broken link to be flagged"
    assert hits[0].severity == "warn", hits[0].as_dict()
    assert not (hits[0].meta or {}).get("immutable"), hits[0].as_dict()


def test_embedding_drift_flags_never_embedded_file(vault: Path) -> None:
    """A file with NO sidecar row (out-of-band create) is flagged as drift —
    not just files whose existing row is mtime-stale. Regression for reconcile
    silently skipping never-embedded files."""
    import sqlite3

    kb = vault / "Knowledge Base"
    sidecar = kb / ".embeddings.sqlite"
    embedded = kb / "Notes" / "Insights" / "progressive-disclosure-without-mode-fragmentation.md"
    embedded_rel = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"

    # Seed a sidecar with one already-embedded note; row mtime ahead of disk so
    # it is NOT mtime-stale.
    conn = sqlite3.connect(sidecar)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chunks (file_path TEXT NOT NULL, "
        "chunk_idx INTEGER NOT NULL, chunk_text TEXT NOT NULL, vector BLOB NOT NULL, "
        "file_mtime REAL NOT NULL, PRIMARY KEY (file_path, chunk_idx))"
    )
    conn.execute(
        "INSERT INTO chunks VALUES (?,?,?,?,?)",
        (embedded_rel, 0, "x", b"\x00", embedded.stat().st_mtime + 60),
    )
    conn.commit()
    conn.close()

    # A brand-new note written out-of-band — no sidecar row at all.
    new = kb / "Notes" / "Insights" / "brand-new-out-of-band.md"
    new.write_text(
        "---\ntype: insight\nstatus: active\n---\n\n# Brand new\n\nSome body to chunk.\n",
        encoding="utf-8",
    )

    findings = audit_module._check_embedding_drift(vault)
    flagged = {f.path for f in findings}
    assert "Knowledge Base/Notes/Insights/brand-new-out-of-band.md" in flagged, flagged
    assert embedded_rel not in flagged  # already embedded + fresh → not flagged
    assert all(f.category == "embedding_drift" for f in findings)
