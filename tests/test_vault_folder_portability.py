"""A vault folder never holds two children that one portable filesystem would merge.

The temp volume is case-sensitive, so a split here is real and not an artefact of the host.
"""

from __future__ import annotations

import json
import os
import unicodedata
import uuid
from pathlib import Path

import pytest

from exomem import audit as audit_module
from exomem import cloud_export, project_keys, vault_portability
from exomem import preserve as preserve_module
from exomem import vault as vault_module
from exomem.vault import MISSING_CONTENT_HASH, PathGuardError, PlannedWrite

CELL = "abcdefghijklmnop"


def _page(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: note\n---\n\nBody.\n", encoding="utf-8")


def test_a_capture_scope_lands_in_the_existing_folder_spelling(vault: Path) -> None:
    """Defect: scope `food` beside an existing `Food` makes a second folder."""
    existing = vault / "Knowledge Base" / "Evidence" / "Food"
    _page(existing / "old.md")

    result = preserve_module.preserve(
        vault, scope="food", category="receipts", filename="note.txt", content="milk"
    )

    assert result.path.startswith("Knowledge Base/Evidence/Food/receipts/")
    assert not (vault / "Knowledge Base" / "Evidence" / "food").exists()


def test_the_folder_backstop_refuses_a_new_variant(vault: Path) -> None:
    """Defect: a write that bypasses re-spelling still adds a non-portable folder."""
    _page(vault / "Knowledge Base" / "Notes" / "Garden" / "a.md")
    target = vault / "Knowledge Base" / "Notes" / "garden" / "b.md"

    with pytest.raises(PathGuardError) as refused:
        vault_module.batch_atomic_write(
            [PlannedWrite(path=target, content="x", expected_hash=MISSING_CONTENT_HASH)],
            vault_root=vault,
        )

    assert refused.value.code == "PATH_VARIANT_EXISTS"
    assert "Garden" in refused.value.reason
    assert not target.parent.exists()


def test_an_existing_split_warns_and_does_not_block(vault: Path) -> None:
    """Defect: old drift blocks a capture, or is written to without a word."""
    notes = vault / "Knowledge Base" / "Notes"
    _page(notes / "Garden" / "a.md")
    _page(notes / "garden" / "b.md")
    target = notes / "garden" / "c.md"

    vault_module.batch_atomic_write(
        [PlannedWrite(path=target, content="x", expected_hash=MISSING_CONTENT_HASH)],
        vault_root=vault,
    )

    assert target.read_text() == "x"
    warnings = vault_module.folder_split_warnings(vault, target.parent)
    assert len(warnings) == 1 and "'Garden'" in warnings[0] and "path_variants" in warnings[0]


@pytest.mark.skipif(os.name == "nt", reason="the cell export Job runs only on Linux cells")
def test_the_export_reports_a_collision_code_and_a_count_without_paths(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect: the owner learns only EXPORT_FAILED and cannot tell what to fix."""
    notes = vault / "Knowledge Base" / "Notes"
    _page(notes / "Secret Garden" / "a.md")
    _page(notes / "secret garden" / "b.md")

    def enumerate_only(root: Path, *_args, **_kwargs):
        vault_portability._enumerate_source(root, vault_portability.PortabilityLimits())

    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    monkeypatch.setattr(cloud_export, "run_export", enumerate_only)
    log = tmp_path / "termination-log"
    status = cloud_export.main(
        ["--export-id", str(uuid.uuid4()), "--part-url", "https://unused.example/part",
         "--part-bytes", "65536", "--max-bytes", "1048576", "--vault", str(vault),
         "--termination-log", str(log)]
    )
    report = json.loads(log.read_text())

    assert status == 1 and report["error"] == "CASE_COLLISION" and report["groups"] == 1
    assert "arden" not in log.read_text()


def test_the_audit_finds_case_and_unicode_groups(vault: Path) -> None:
    """Defect: a split folder is invisible until an export fails."""
    notes = vault / "Knowledge Base" / "Notes"
    _page(notes / "Garden" / "a.md")
    _page(notes / "garden" / "b.md")
    composed = unicodedata.normalize("NFC", "Café")
    decomposed = unicodedata.normalize("NFD", "Café")
    _page(notes / "Cafés" / f"{composed}.md")
    _page(notes / "Cafés" / f"{decomposed}.md")
    _page(notes / "Tool Kit" / "a.md")
    _page(notes / "tool-kit" / "a.md")

    findings = [
        finding
        for finding in audit_module.audit(vault, categories=["path_variants"]).findings
        if finding.category == "path_variants"
    ]

    defects = [f for f in findings if f.meta == {"class": "defect"}]
    candidates = [f for f in findings if f.meta == {"class": "candidate"}]
    assert len(defects) == 2
    assert [f.path.rsplit("/", 1)[-1] for f in candidates] == ["Tool Kit"]


def test_project_key_registration_reuses_an_existing_folder_spelling(vault: Path) -> None:
    """Defect: a new key `orchard-notes` next to a hand-made `ORCHARD NOTES` folder splits it."""
    research = vault / "Knowledge Base" / "Notes" / "Research"
    _page(research / "ORCHARD NOTES" / "a.md")

    key, folder, was_new = project_keys.register_project_key(vault, "orchard-notes")

    assert (key, folder, was_new) == ("orchard-notes", "ORCHARD NOTES", True)
    assert not (research / "Orchard Notes").exists()
    with pytest.raises(ValueError, match="PATH_VARIANT_EXISTS"):
        project_keys.plan_project_keys(
            vault, ["orchard-two"], folders={"orchard-two": "orchard notes"}
        )
