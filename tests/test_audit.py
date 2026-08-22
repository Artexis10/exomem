"""audit tool tests — findings must carry the affected page path, and
parent-vault wikilinks must resolve (SKILL.md rule 1 allows them)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from exomem import attention as attention_module
from exomem import audit as audit_module
from exomem import commands
from exomem import review_state as review_state_module


def test_audit_and_reconcile_import_in_fresh_process() -> None:
    imported = subprocess.run(
        [sys.executable, "-c", "import exomem.audit; import exomem.reconcile"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert imported.returncode == 0, imported.stderr


def _write_entity(
    vault: Path,
    *,
    folder: str,
    name: str,
    entity_type: str,
) -> Path:
    path = vault / "Knowledge Base" / "Entities" / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: entity\ntitle: {name}\nentity_type: {entity_type}\n"
        "status: active\ncreated: 2026-08-22\nupdated: 2026-08-22\n---\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    return path


def _save_proposed_entry(vault: Path, entry: dict) -> None:
    type_id = entry["id"]
    definition = {
        key: value
        for key, value in entry.items()
        if key not in {"id", "page_count"}
    }
    result = commands.op_schema_memory(
        vault,
        operation="save-entity-types",
        proposal={
            "schema_version": 1,
            "entity_types": {type_id: definition},
        },
        why=f"Register the synthetic {type_id} type from attention.",
        expected_hash=None,
    )
    assert result["valid"] is True


def test_unregistered_entity_type_is_an_attention_finding_with_proposed_entry(
    tmp_path: Path,
) -> None:
    page = _write_entity(
        tmp_path,
        folder="Places",
        name="Aster Hall",
        entity_type="place",
    )

    report = attention_module.attention(
        tmp_path,
        categories=["entity_type_unregistered"],
    )

    assert len(report.items) == 1
    item = report.items[0]
    assert item.path == page.relative_to(tmp_path).as_posix()
    assert item.categories == ["entity_type_unregistered"]
    assert item.reasons[0]["meta"]["proposed_entry"] == {
        "id": "place",
        "folder": "Places",
        "label": "Place",
        "aliases": [],
        "capture_guidance": "A stable place identity with reusable context.",
        "parent": "concept",
        "page_count": 1,
    }


def test_unregistered_type_finding_resolves_when_the_type_is_registered(
    tmp_path: Path,
) -> None:
    _write_entity(
        tmp_path,
        folder="Places",
        name="Aster Hall",
        entity_type="place",
    )
    before = audit_module.audit(tmp_path, categories=["entity_type_unregistered"])
    assert len(before.findings) == 1

    proposed_entry = before.findings[0].meta["proposed_entry"]
    _save_proposed_entry(tmp_path, proposed_entry)
    after = audit_module.audit(tmp_path, categories=["entity_type_unregistered"])

    assert after.findings == []


def test_unregistered_type_under_a_core_folder_proposes_a_non_colliding_entry(
    tmp_path: Path,
) -> None:
    _write_entity(
        tmp_path,
        folder="People",
        name="Aster Mentor",
        entity_type="mentor",
    )
    before = audit_module.audit(tmp_path, categories=["entity_type_unregistered"])

    assert len(before.findings) == 1
    proposed_entry = before.findings[0].meta["proposed_entry"]
    assert proposed_entry["id"] == "mentor"
    assert proposed_entry["folder"] != "People"

    _save_proposed_entry(tmp_path, proposed_entry)
    after = audit_module.audit(tmp_path, categories=["entity_type_unregistered"])

    assert after.findings == []


def test_unregistered_type_finding_cannot_be_dismissed_to_silence(
    tmp_path: Path,
) -> None:
    _write_entity(
        tmp_path,
        folder="Places",
        name="Aster Hall",
        entity_type="place",
    )
    item = attention_module.attention(
        tmp_path,
        categories=["entity_type_unregistered"],
    ).items[0]
    review_state_module.ReviewStateStore(tmp_path).apply(
        item.item_id,
        item.fingerprint,
        action="dismiss",
        why="Leave the authored state unchanged.",
    )

    refreshed = attention_module.attention(
        tmp_path,
        categories=["entity_type_unregistered"],
        state="open",
    )

    assert len(refreshed.items) == 1
    assert refreshed.items[0].state == "open"


def test_three_pages_under_an_unregistered_folder_trigger_the_finding_two_do_not(
    tmp_path: Path,
) -> None:
    for name in ("Aster", "Beryl"):
        _write_entity(tmp_path, folder="Venues", name=name, entity_type="concept")

    two = audit_module.audit(tmp_path, categories=["entity_type_unregistered"])
    assert two.findings == []

    _write_entity(tmp_path, folder="Venues", name="Cedar", entity_type="concept")
    three = audit_module.audit(tmp_path, categories=["entity_type_unregistered"])

    assert len(three.findings) == 1
    assert three.findings[0].meta["proposed_entry"] == {
        "id": "venues",
        "folder": "Venues",
        "label": "Venues",
        "aliases": [],
        "capture_guidance": "A stable venues identity with reusable context.",
        "parent": "concept",
        "page_count": 3,
    }


def test_forward_reference_findings_have_non_empty_path(vault: Path) -> None:
    """Regression: every finding must carry the path of the file it concerns.

    Previously _parse_page set rel_path="" and relied on find() to fill it.
    audit called _parse_page directly, so every finding's `path` was empty —
    making the report un-triagable.
    """
    # Plant a forward reference in an existing fixture file.
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    original = insight.read_text(encoding="utf-8")
    insight.write_text(
        original + "\n\nDangling: [[Knowledge Base/Notes/Insights/does-not-exist]]\n",
        encoding="utf-8",
    )

    report = audit_module.audit(vault, categories=["forward_reference"])
    assert report.findings, "expected at least one forward_reference finding"
    for f in report.findings:
        assert f.path, f"finding has empty path: {f.as_dict()}"
        assert f.path.startswith("Knowledge Base/"), f.path


def test_missing_note_is_forward_reference_not_broken(vault: Path) -> None:
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8")
        + "\n\nPlanned: [[Knowledge Base/Notes/Patterns/future-pattern]]\n",
        encoding="utf-8",
    )

    report = audit_module.audit(
        vault, categories=["broken_wikilink", "forward_reference"]
    )
    hits = [f for f in report.findings if "future-pattern" in f.detail]

    assert len(hits) == 1, [f.as_dict() for f in hits]
    assert hits[0].category == "forward_reference"
    assert hits[0].severity == "info"


def test_forward_reference_clears_when_target_is_created(vault: Path) -> None:
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8")
        + "\n\nPlanned: [[Knowledge Base/Notes/Patterns/future-pattern]]\n",
        encoding="utf-8",
    )

    before = audit_module.audit(vault, categories=["forward_reference"])
    assert any("future-pattern" in f.detail for f in before.findings)

    target = vault / "Knowledge Base" / "Notes" / "Patterns" / "future-pattern.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Future pattern\n", encoding="utf-8")

    after = audit_module.audit(vault, categories=["forward_reference"])
    assert not any("future-pattern" in f.detail for f in after.findings)


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


def test_audit_keeps_duplicate_stem_ambiguity_broken(vault: Path) -> None:
    for folder in ("Patterns", "Research"):
        target = vault / "Knowledge Base" / "Notes" / folder / "shared-name.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {folder} target\n", encoding="utf-8")
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8") + "\n\n[[shared-name]]\n",
        encoding="utf-8",
    )

    report = audit_module.audit(
        vault, categories=["broken_wikilink", "forward_reference"]
    )
    hits = [finding for finding in report.findings if "shared-name" in finding.detail]

    assert len(hits) == 1, [finding.as_dict() for finding in hits]
    assert hits[0].category == "broken_wikilink"


def test_non_markdown_collision_probe_never_enumerates_outside_vault(
    vault: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    outside = tmp_path / "outside-target.eml"
    outside.write_text("not part of the vault", encoding="utf-8")
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8") + "\n\n[[../outside-target]]\n",
        encoding="utf-8",
    )
    original_iterdir = Path.iterdir
    vault_resolved = vault.resolve()

    def contained_iterdir(path: Path):
        path.resolve().relative_to(vault_resolved)
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", contained_iterdir)

    audit_module.audit(vault, categories=["broken_wikilink", "forward_reference"])


def test_forward_reference_in_append_only_file_is_info(vault: Path) -> None:
    """Forward references remain informational in append-only captured material."""
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

    report = audit_module.audit(vault, categories=["forward_reference"])
    hits = [f for f in report.findings if "immutable-link-src" in f.path]
    assert hits, "expected the source's forward reference to be surfaced"
    f = hits[0]
    assert f.category == "forward_reference", f.as_dict()
    assert f.severity == "info", f.as_dict()
    assert f.meta and f.meta.get("immutable") is True, f.as_dict()


def test_forward_reference_in_editable_file_is_info(vault: Path) -> None:
    """A missing Markdown page is informational in editable notes too."""
    insight = (
        vault / "Knowledge Base" / "Notes" / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
    insight.write_text(
        insight.read_text(encoding="utf-8")
        + "\n\n[[Knowledge Base/Notes/Insights/no-such-target-abc]]\n",
        encoding="utf-8",
    )

    report = audit_module.audit(vault, categories=["forward_reference"])
    hits = [f for f in report.findings if "no-such-target-abc" in f.detail]
    assert hits, "expected the forward reference to be surfaced"
    assert hits[0].category == "forward_reference", hits[0].as_dict()
    assert hits[0].severity == "info", hits[0].as_dict()
    assert not (hits[0].meta or {}).get("immutable"), hits[0].as_dict()


def test_embedding_drift_flags_never_embedded_file(vault: Path) -> None:
    """A file with NO sidecar row (out-of-band create) is flagged as drift —
    not just files whose existing row is mtime-stale. Regression for reconcile
    silently skipping never-embedded files."""
    import sqlite3

    kb = vault / "Knowledge Base"
    sidecar = kb / ".embeddings.sqlite"
    embedded = kb / "Notes" / "Insights" / "progressive-disclosure-without-mode-fragmentation.md"
    embedded_rel = (
        "Knowledge Base/Notes/Insights/"
        "progressive-disclosure-without-mode-fragmentation.md"
    )

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
