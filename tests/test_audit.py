"""audit tool tests — findings must carry the affected page path, and
parent-vault wikilinks must resolve (SKILL.md rule 1 allows them)."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

from exomem import attention as attention_module
from exomem import audit as audit_module
from exomem import commands, index_paths
from exomem import entity_types as entity_types_module
from exomem import review_state as review_state_module
from exomem.governance.principal import library_scope


def test_a_guest_is_refused_the_audit_before_and_after_a_raw_source_lands(vault: Path) -> None:
    """The audit's link-existence walks and index checks read every file, so a
    guest is refused it on a vault with no policy too; the owner's audit
    counts a protected source."""
    from exomem import commands
    from exomem.governance import principal

    guest = principal.RequestPrincipal(
        "principal:" + "ab" * 32, surface="mcp", issuer_family="mcp-oauth:synthetic",
    )
    owner = principal.owner_principal(surface="library")
    with principal.request_scope(owner):
        before = commands.op_audit(vault, categories=["unprocessed_source"])
    with principal.request_scope(guest):
        refused = commands.op_audit(vault, categories=["unprocessed_source"])
    hidden = vault / "Knowledge Base/Sources/__exomem_raw_v1__private.md"
    hidden.write_text("---\ntype: source\ncreated: 2020-01-01\ningested_into: []\n---\nPrivate source\n", encoding="utf-8")
    with principal.request_scope(guest):
        assert commands.op_audit(vault, categories=["unprocessed_source"]) == refused
    with principal.request_scope(owner):
        after = commands.op_audit(vault, categories=["unprocessed_source"])
    assert refused == {"available": False, "reason": "audience_restricted"}
    assert after["summary"]["unprocessed_source"] == before["summary"].get("unprocessed_source", 0) + 1


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


def _save_proposal(
    vault: Path,
    proposal: dict,
    *,
    expected_hash: str | None = None,
) -> None:
    with library_scope():
        result = commands.op_schema_memory(
            vault,
            operation="save-entity-types",
            proposal=proposal,
            why="Register the synthetic type offered by attention.",
            expected_hash=expected_hash,
        )
    assert result["valid"] is True


def test_duplicated_blank_sidecar_requires_source_reextraction(vault: Path) -> None:
    sidecar = vault / "Knowledge Base" / "Evidence" / "Case" / "repeated.pdf.md"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    block = "# Repeated document\n\n## Body\n\nExact content."
    sidecar.write_text(
        "---\ntype: source\nmedia_type: pdf\n---\n"
        "# Evidence: repeated.pdf\n\nPreserved under `Evidence/Case/`.\n\n"
        "## Extracted text\n\n## Preserved notes\n\n"
        + "\n\n".join([block] * 3)
        + "\n",
        encoding="utf-8",
    )

    [finding] = audit_module.audit(vault, categories=["duplicated_sidecar"]).findings

    assert finding.severity == "error"
    assert "source re-extraction required" in finding.detail
    assert "retry media processing" in (finding.proposed_fix or "")


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
        "page_count": 1,
    }


def test_unregistered_type_finding_guides_to_the_full_proposal(
    tmp_path: Path,
) -> None:
    _save_proposal(
        tmp_path,
        {
            "schema_version": 1,
            "entity_types": {
                "venue": {
                    "folder": "Venues",
                    "label": "Venue",
                    "aliases": [],
                    "capture_guidance": "A stable synthetic venue identity.",
                }
            },
        },
    )
    _write_entity(
        tmp_path,
        folder="Places",
        name="Aster Hall",
        entity_type="place",
    )

    finding = audit_module.audit(
        tmp_path, categories=["entity_type_unregistered"]
    ).findings[0]
    meta = finding.meta

    assert "meta.proposal" in finding.proposed_fix
    assert "expected_hash" in finding.proposed_fix
    assert "capture_guidance" not in meta["proposed_entry"]
    _save_proposal(
        tmp_path,
        meta["proposal"],
        expected_hash=meta["expected_hash"],
    )
    assert set(entity_types_module.load_entity_types(tmp_path).extensions) == {
        "place",
        "venue",
    }


def test_stale_offer_is_refused_after_an_interleaving_registration(
    tmp_path: Path,
) -> None:
    _write_entity(
        tmp_path,
        folder="Halls",
        name="Aster Hall",
        entity_type="hall",
    )
    offer = audit_module.audit(
        tmp_path, categories=["entity_type_unregistered"]
    ).findings[0].meta
    _save_proposal(
        tmp_path,
        {
            "schema_version": 1,
            "entity_types": {
                "gadget": {
                    "folder": "Gadgets",
                    "label": "Gadget",
                    "aliases": [],
                    "capture_guidance": "A stable synthetic gadget identity.",
                }
            },
        },
    )

    with pytest.raises(ValueError, match="STALE_ENTITY_TYPE_REGISTRY"):
        _save_proposal(
            tmp_path,
            offer["proposal"],
            expected_hash=offer["expected_hash"],
        )

    assert set(entity_types_module.load_entity_types(tmp_path).extensions) == {
        "gadget"
    }


def test_proposal_is_emitted_once_per_distinct_type_and_folder(
    tmp_path: Path,
) -> None:
    _save_proposal(
        tmp_path,
        {
            "schema_version": 1,
            "entity_types": {
                f"kind-{index:02d}": {
                    "folder": f"Kind{index:02d}s",
                    "label": f"Kind {index:02d}",
                    "aliases": [],
                    "capture_guidance": "A stable synthetic identity.",
                }
                for index in range(40)
            },
        },
    )
    for index in range(30):
        _write_entity(
            tmp_path,
            folder="Places",
            name=f"Aster Hall {index:02d}",
            entity_type="place",
        )

    findings = audit_module.audit(
        tmp_path, categories=["entity_type_unregistered"]
    ).findings
    carriers = [finding for finding in findings if "proposal" in finding.meta]
    references = [
        finding for finding in findings if "proposal_carrier" in finding.meta
    ]

    assert len(carriers) == 1
    assert len(references) == 29
    assert all(
        finding.meta["proposal_carrier"] == carriers[0].path
        for finding in references
    )
    carrier_bytes = len(json.dumps(carriers[0].meta, sort_keys=True))
    total_bytes = sum(
        len(json.dumps(finding.meta, sort_keys=True)) for finding in findings
    )
    assert total_bytes < 4 * carrier_bytes


def test_one_unregistered_id_across_folders_yields_one_proposal(
    tmp_path: Path,
) -> None:
    for folder, names in (
        ("Rooms", ("Aster", "Beryl")),
        ("Halls", ("Cedar", "Dahlia", "Elm")),
    ):
        for name in names:
            _write_entity(
                tmp_path,
                folder=folder,
                name=name,
                entity_type="place",
            )

    findings = audit_module.audit(
        tmp_path, categories=["entity_type_unregistered"]
    ).findings
    carriers = [finding for finding in findings if "proposal" in finding.meta]

    assert len(carriers) == 2
    assert {finding.meta["proposed_entry"]["folder"] for finding in findings} == {
        "Halls"
    }
    assert len(
        {json.dumps(finding.meta["proposal"], sort_keys=True) for finding in carriers}
    ) == 1


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

    offer = before.findings[0].meta
    _save_proposal(tmp_path, offer["proposal"])
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
    offer = before.findings[0].meta
    proposed_entry = offer["proposed_entry"]
    assert proposed_entry["id"] == "mentor"
    assert proposed_entry["folder"] != "People"
    assert proposed_entry["parent"] == "person"

    _save_proposal(tmp_path, offer["proposal"])
    after = audit_module.audit(tmp_path, categories=["entity_type_unregistered"])

    assert after.findings == []


def test_proposed_entry_avoids_folders_owned_by_existing_extensions(
    tmp_path: Path,
) -> None:
    _save_proposal(
        tmp_path,
        {
            "schema_version": 1,
            "entity_types": {
                "venue": {
                    "folder": "Places",
                    "label": "Venue",
                    "aliases": [],
                    "capture_guidance": "A stable synthetic venue identity.",
                }
            },
        },
    )
    _write_entity(
        tmp_path,
        folder="Places",
        name="Aster Hall",
        entity_type="place",
    )
    _write_entity(
        tmp_path,
        folder="Places",
        name="Beryl Hall",
        entity_type="venue",
    )

    before = audit_module.audit(tmp_path, categories=["entity_type_unregistered"])

    assert len(before.findings) == 1
    offer = before.findings[0].meta
    assert offer["proposed_entry"]["folder"] != "Places"
    assert offer["proposed_entry"]["page_count"] == 1
    current_hash = entity_types_module.load_entity_types(tmp_path).extension_hash
    _save_proposal(
        tmp_path,
        offer["proposal"],
        expected_hash=current_hash,
    )

    registry = entity_types_module.load_entity_types(tmp_path)
    assert set(registry.extensions) == {"place", "venue"}
    after = audit_module.audit(tmp_path, categories=["entity_type_unregistered"])
    assert after.findings == []


def test_proposed_entry_is_null_with_reason_when_no_clean_entry_exists(
    tmp_path: Path,
) -> None:
    _save_proposal(
        tmp_path,
        {
            "schema_version": 1,
            "entity_types": {
                "venue": {
                    "folder": "Venues",
                    "label": "Venue",
                    "aliases": ["places"],
                    "capture_guidance": "A stable synthetic venue identity.",
                }
            },
        },
    )
    for name in ("Aster", "Beryl", "Cedar"):
        _write_entity(tmp_path, folder="Places", name=name, entity_type="concept")

    report = audit_module.audit(tmp_path, categories=["entity_type_unregistered"])

    assert len(report.findings) == 1
    assert report.findings[0].meta["proposed_entry"] is None
    assert report.findings[0].meta["reason"]


def test_proposed_entry_validation_is_memoized_per_type_and_folder(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in ("Aster Hall", "Beryl Hall"):
        _write_entity(
            tmp_path,
            folder="Places",
            name=name,
            entity_type="place",
        )
    calls = 0
    original = entity_types_module.validate_proposal

    def counted_validate(proposal: dict) -> list[dict[str, str]]:
        nonlocal calls
        calls += 1
        return original(proposal)

    monkeypatch.setattr(entity_types_module, "validate_proposal", counted_validate)

    report = audit_module.audit(tmp_path, categories=["entity_type_unregistered"])

    assert len(report.findings) == 2
    assert calls == 1


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
    sidecar = index_paths.sidecar_path(vault)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
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


def test_a_link_inside_an_origin_carrier_does_not_adopt_an_orphan_entity(tmp_path: Path) -> None:
    """A carrier is recorded data, not a use of the entity it names."""
    entity = _write_entity(tmp_path, folder="Places", name="Aster Hall", entity_type="place")
    note = tmp_path / "Knowledge Base" / "Notes" / "Insights" / "carrier-only.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\ntype: insight\nstatus: active\n---\n\n"
        '<!-- exomem-origin:v1 {"reason":"Met at [[Entities/Places/Aster Hall]]."} -->\n\n'
        "No prose link.\n",
        encoding="utf-8",
    )

    findings = audit_module.audit(tmp_path, categories=["orphan_entity"]).findings

    assert [finding.path for finding in findings] == [entity.relative_to(tmp_path).as_posix()]


# ---------------------------------------------------------------- duplicate_content and duplicate_source_url


def _duplicate_audit(vault: Path, *categories: str) -> dict:
    with library_scope():
        return commands.op_audit(vault, categories=list(categories))


def _three_photos(vault: Path) -> list[str]:
    from duplicate_fixtures import evidence_filing

    data = b"\x89PNG photo of the notice"
    # Filed in this order, so the oldest filing is the one whose folder sorts last.
    return [
        evidence_filing(vault, "Case", "letters", "notice.png", data, filed=dt.date(2026, 1, 1)),
        evidence_filing(vault, "Case", "exhibits", "exhibit-a.png", data, filed=dt.date(2026, 2, 1)),
        evidence_filing(vault, "Case", "annexes", "annex-a.png", data, filed=dt.date(2026, 3, 1)),
    ]


def test_three_identical_photos_give_one_finding_with_two_fold_steps(vault: Path) -> None:
    """One group, the oldest filing by capture date as canonical, and one step per copy."""
    pages = _three_photos(vault)

    result = _duplicate_audit(vault, "duplicate_content")

    [finding] = result["findings"]
    assert finding["category"] == "duplicate_content" and finding["path"] == pages[0]
    assert finding["meta"]["suggested_canonical"] == pages[0]
    assert [step["args"]["copy"]["path"] for step in finding["meta"]["steps"]] == pages[1:]
    assert {step["args"]["canonical"]["path"] for step in finding["meta"]["steps"]} == {pages[0]}
    assert [family["category"] for family in finding["meta"]["members"][0]["families"]] == ["letters"]
    assert finding["meta"]["bytes_freed"] == 2 * len(b"\x89PNG photo of the notice")
    assert result["metadata"]["duplicates"]["content"] == {"Evidence": {"groups": 1, "copies": 2, "verified": "re-hashed"}}
    assert result["metadata"]["coverage"]["duplicate_content"] == "complete"


def test_a_stale_index_entry_gives_no_finding(vault: Path) -> None:
    """The index only proposes a group: when one original no longer hashes to its page, the audit stays silent."""
    pages = _three_photos(vault)
    assert len(_duplicate_audit(vault, "duplicate_content")["findings"]) == 1

    (vault / pages[1].removesuffix(".md")).write_bytes(b"\x89PNG a different picture")
    (vault / pages[2].removesuffix(".md")).write_bytes(b"\x89PNG another different picture")

    after = _duplicate_audit(vault, "duplicate_content")
    assert after["findings"] == []
    assert after["metadata"]["duplicates"]["stale_index_groups"] == 1


def test_a_copy_cited_by_a_source_becomes_the_suggested_canonical_page_and_its_step_passes_proposal(vault: Path) -> None:
    """A Source cannot be repointed, so the page it cites must stay. Suggesting the oldest filing would send the reader into a refusal."""
    from duplicate_fixtures import evidence_filing, source_filing

    from exomem import curation

    data = b"\x89PNG photo of the notice"
    oldest = evidence_filing(vault, "Case", "letters", "notice.png", data, filed=dt.date(2026, 1, 1))
    cited = evidence_filing(vault, "Case", "exhibits", "exhibit-a.png", data, filed=dt.date(2026, 2, 1))
    source_filing(vault, "citer", f"See [[{cited.removesuffix('.md')}]] for the photo.")

    [finding] = _duplicate_audit(vault, "duplicate_content")["findings"]

    assert (finding["path"], finding["meta"]["suggested_canonical"]) == (cited, cited)
    assert finding["meta"]["canonical_because"] == "cited by a page the fold cannot rewrite"
    [step] = finding["meta"]["steps"]
    assert (step["args"]["canonical"]["path"], step["args"]["copy"]["path"]) == (cited, oldest)
    with library_scope():
        curation.propose(vault, {"version": 1, "title": "Fold the cited photo", "steps": [step]})


def test_two_copies_that_pages_the_fold_cannot_rewrite_cite_give_a_finding_with_no_step_and_a_reason(vault: Path) -> None:
    """Only one page can stay. With two pinned pages every fold would be refused, so the audit offers none and says why."""
    from duplicate_fixtures import source_filing

    pages = _three_photos(vault)
    source_filing(vault, "citer-one", f"See [[{pages[0].removesuffix('.md')}]].")
    source_filing(vault, "citer-two", f"See [[{pages[2].removesuffix('.md')}]].")

    [finding] = _duplicate_audit(vault, "duplicate_content")["findings"]

    assert finding["meta"]["steps"] == []
    assert pages[0] in finding["meta"]["no_fold"] and pages[2] in finding["meta"]["no_fold"]
    assert pages[1] not in finding["meta"]["no_fold"]
    assert finding["proposed_fix"].startswith("No fold is offered")


def test_one_url_with_different_text_gives_one_finding_and_no_fold(vault: Path) -> None:
    """Two captures of one address that differ are not duplicates: the audit names both and offers no step."""
    from duplicate_fixtures import source_filing

    first = source_filing(vault, "capture-one", "The page said one thing.", url="https://Example.org/page#top", captured="2026-01-01")
    second = source_filing(vault, "capture-two", "The page said another thing.", url="https://example.org/page", captured="2026-02-01")

    result = _duplicate_audit(vault, "duplicate_content", "duplicate_source_url")

    [finding] = result["findings"]
    assert finding["category"] == "duplicate_source_url"
    assert finding["paths"] == [first, second]
    assert finding["meta"]["no_fold"] and "steps" not in finding["meta"]
    assert result["metadata"]["duplicates"]["source_url"] == {"groups": 1, "sources": 2, "verified": "re-hashed"}


@pytest.mark.parametrize("setup", ["across both lanes", "raw-protected"])
def test_a_group_that_must_not_fold_is_reported_with_its_reason_and_no_step(vault: Path, owner_scope, setup: str) -> None:  # noqa: ANN001
    """The audit still counts the group, and says why no step is offered."""
    from duplicate_fixtures import evidence_filing, source_filing

    data = b"\x89PNG photo of the notice"
    protected = setup == "raw-protected"
    evidence_filing(vault, "Case", "letters", "notice.png", data, protected=protected)
    if protected:
        evidence_filing(vault, "Case", "exhibits", "exhibit-a.png", data, protected=True)
    else:
        source_filing(vault, "same-bytes", "A captured passage.", original=data)

    [finding] = _duplicate_audit(vault, "duplicate_content")["findings"]

    assert finding["meta"]["steps"] == []
    assert finding["meta"]["no_fold"]
    assert ("both lanes" in finding["meta"]["no_fold"]) == (setup == "across both lanes")


def _external_caller_who_cannot_read_sources(vault: Path):
    """A new request scope for an external caller who may read every page of a governed vault except the Sources lane."""
    from test_governance_egress import EXTERNAL, write_rule, write_scope

    from exomem.governance import egress, membership, policy
    from exomem.governance.principal import RequestPrincipal, request_scope

    write_scope(vault, paths="Sources/**", name="Sources")
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    return request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp"))


def _restricted_duplicate_answer(vault: Path) -> tuple[list[dict], dict]:
    from exomem import duplicate_audit

    findings, report, _ = duplicate_audit.check(vault, {"duplicate_content"})
    return [finding.as_dict() for finding in findings], report


def test_a_page_the_caller_cannot_read_changes_nothing_in_a_restricted_audit(vault: Path) -> None:
    """A withheld Source that cites a copy must not show: the restricted answer equals the answer of a vault without it."""
    from duplicate_fixtures import source_filing

    pages = _three_photos(vault)
    with _external_caller_who_cannot_read_sources(vault):
        without_the_source = _restricted_duplicate_answer(vault)
    with library_scope():
        source_filing(vault, "citer", f"See [[{pages[2].removesuffix('.md')}]] for the photo.")
        owner = _duplicate_audit(vault, "duplicate_content")["findings"][0]["meta"]
    with _external_caller_who_cannot_read_sources(vault):
        with_the_source = _restricted_duplicate_answer(vault)

    assert with_the_source == without_the_source
    [finding] = with_the_source[0]
    assert [member["path"] for member in finding["meta"]["members"]] == pages
    assert finding["meta"]["steps"] == [] and "whole-namespace authority" in finding["meta"]["no_fold"]
    assert owner["suggested_canonical"] == pages[2], "the owner does see the citation, so it is the withheld Source that the restricted answer hides"


def test_a_restricted_audit_counts_only_the_copies_the_caller_can_read(vault: Path) -> None:
    """One readable copy beside a withheld one is no group at all for that caller."""
    from duplicate_fixtures import source_filing

    data = b"the notice text"
    source_filing(vault, "kept-readable", "Opening line.", original=data)
    withheld = source_filing(vault, "kept-withheld", "Opening line.", original=data)
    with library_scope():
        assert len(_duplicate_audit(vault, "duplicate_content")["findings"]) == 1
    with _external_caller_who_cannot_read_sources(vault):
        findings, report = _restricted_duplicate_answer(vault)

    assert findings == [] and withheld
    assert report["content"] == {} and report["stale_index_groups"] == 0


def test_a_text_source_and_a_source_with_an_original_hold_identical_bytes_but_never_fold(vault: Path) -> None:
    """The fold refuses to merge the two forms, so the audit lists the group and offers no step."""
    from duplicate_fixtures import source_filing

    text = "The exact notice text."
    as_text = source_filing(vault, "as-text", text, captured="2026-01-01")
    as_file = source_filing(vault, "as-file", "A caption.", original=text.encode(), captured="2026-02-01")

    [finding] = _duplicate_audit(vault, "duplicate_content")["findings"]

    assert sorted(finding["paths"]) == sorted([as_text, as_file])
    assert finding["meta"]["steps"] == []
    assert "identical bytes" in finding["meta"]["no_fold"]


def test_an_evidence_page_outside_the_family_folders_gives_a_finding_with_no_step_and_a_reason(vault: Path) -> None:
    """A fold of two Evidence pages needs both in a family folder: the audit must not offer a step that proposal refuses."""
    from exomem import lexstore

    pages = _three_photos(vault)
    family, loose = pages[2].rpartition("/")[0], "Knowledge Base/Evidence/Case"
    for name in (pages[2], pages[2].removesuffix(".md")):
        (vault / name).rename(vault / loose / name.rpartition("/")[2])
    moved = vault / loose / pages[2].rpartition("/")[2]
    moved.write_text(moved.read_text(encoding="utf-8").replace(family, loose), encoding="utf-8")
    lexstore.clear_stores()  # the audit reconciles the index with the pages it finds

    [finding] = _duplicate_audit(vault, "duplicate_content")["findings"]

    assert finding["meta"]["steps"] == []
    assert "family folders" in finding["meta"]["no_fold"]


def test_a_row_lost_from_the_index_is_reconciled_before_the_audit_counts(vault: Path) -> None:
    """The index only proposes groups, so a lost row must never read as a complete answer with a short count."""
    import sqlite3
    from contextlib import closing

    from exomem import lexstore

    pages = _three_photos(vault)
    with closing(sqlite3.connect(lexstore.lexical_path(vault))) as conn:
        conn.execute("DELETE FROM pages WHERE path = ?", (pages[2],))
        conn.commit()
    lexstore.clear_stores()  # a restarted process opens the catalogue with the row gone

    result = _duplicate_audit(vault, "duplicate_content")

    assert result["metadata"]["duplicates"]["index"] == "complete"
    assert result["metadata"]["duplicates"]["content"] == {"Evidence": {"groups": 1, "copies": 2, "verified": "re-hashed"}}


def test_a_protected_group_without_a_matching_binding_is_unverifiable_not_stale(vault: Path) -> None:
    """The owner can read the pages but nothing proves their originals, which is not the index being out of date."""
    from duplicate_fixtures import evidence_filing

    data = b"\x89PNG photo of the notice"
    with library_scope():
        evidence_filing(vault, "Case", "letters", "notice.png", data, protected=True)
        damaged = evidence_filing(vault, "Case", "exhibits", "exhibit-a.png", data, protected=True)
    text = (vault / damaged).read_text(encoding="utf-8")
    digest = text.split("artifact_sha256: ", 1)[1][:64]
    # The protection block comes first in the page: only its digest stops matching the page's payload.
    (vault / damaged).write_text(text.replace(digest, "0" * 64, 1), encoding="utf-8")

    duplicates = _duplicate_audit(vault, "duplicate_content")["metadata"]["duplicates"]

    assert duplicates["unverifiable_groups"] == 1 and duplicates["stale_index_groups"] == 0
    assert "raw-protection binding" in duplicates["unverifiable_because"]


def test_an_index_that_cannot_answer_reads_as_unchecked_never_as_zero(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import lexstore

    _three_photos(vault)
    monkeypatch.setattr(lexstore, "duplicate_groups", lambda _root: None)

    result = _duplicate_audit(vault, "duplicate_content")

    assert result["findings"] == []
    assert result["metadata"]["duplicates"] == {"index": "unchecked"}
    assert result["metadata"]["coverage"]["duplicate_content"] == "unknown"
