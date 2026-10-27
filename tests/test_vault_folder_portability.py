"""A vault folder never holds two children that one portable filesystem would merge.

Each case builds a split, which only a case-sensitive volume can hold.
"""

from __future__ import annotations

import json
import sys
import unicodedata
import uuid
from pathlib import Path

import pytest

from exomem import audit as audit_module
from exomem import cloud_export, memory_refs, project_keys, vault_portability
from exomem import preserve as preserve_module
from exomem import vault as vault_module
from exomem.create_file import create_file
from exomem.move_file import MoveFileError, move_file
from exomem.vault import MISSING_CONTENT_HASH, PathGuardError, PlannedWrite

# Windows and default macOS volumes fold case, so they cannot hold the split these cases build.
pytestmark = pytest.mark.skipif(
    sys.platform in {"win32", "darwin"}, reason="needs a case-sensitive volume"
)

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


def test_an_explicit_create_path_lands_in_the_existing_folder_spelling(vault: Path) -> None:
    """Defect: create_file with `Notes/garden/...` beside `Notes/Garden/` makes a second folder."""
    notes = vault / "Knowledge Base" / "Notes"
    _page(notes / "Garden" / "a.md")

    result = create_file(vault, path="Knowledge Base/Notes/garden/plan.md", content="Plan.\n")

    assert result.path == "Knowledge Base/Notes/Garden/plan.md"
    assert not (notes / "garden").exists()


def test_a_move_destination_lands_in_the_existing_folder_spelling(vault: Path) -> None:
    """Defect: move_file into `Notes/garden/` beside `Notes/Garden/` makes a second folder."""
    notes = vault / "Knowledge Base" / "Notes"
    _page(notes / "Garden" / "a.md")
    _page(notes / "plan.md")

    result = move_file(
        vault,
        old_path="Knowledge Base/Notes/plan.md",
        new_path="Knowledge Base/Notes/garden/plan.md",
        update_wikilinks=False,
    )

    assert result.new_path == "Knowledge Base/Notes/Garden/plan.md"
    assert (notes / "Garden" / "plan.md").is_file()
    assert not (notes / "garden").exists()


def test_a_move_into_a_variant_of_its_own_folder_names_the_variant(vault: Path) -> None:
    """Defect: the re-spell lands on the source itself and refuses DEST_EXISTS."""
    notes = vault / "Knowledge Base" / "Notes"
    _page(notes / "food" / "trial.md")

    with pytest.raises(MoveFileError) as refused:
        move_file(
            vault,
            old_path="Knowledge Base/Notes/food/trial.md",
            new_path="Knowledge Base/Notes/Food/trial.md",
            update_wikilinks=False,
        )

    assert refused.value.code == "PATH_VARIANT_EXISTS"
    assert "'Knowledge Base/Notes/food'" in refused.value.reason
    assert (notes / "food" / "trial.md").is_file()


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


def test_deleting_one_side_of_a_split_keeps_the_other_page_reference(vault: Path) -> None:
    """Defect: a delete of `food/a.md` read as `Food/a.md` drops the surviving page's row."""
    notes = vault / "Knowledge Base" / "Notes"
    kept_id, gone_id = memory_refs.new_id(), memory_refs.new_id()
    (notes / "Food").mkdir(parents=True)
    (notes / "food").mkdir()
    (notes / "Food" / "a.md").write_text(f"---\ntype: note\nexomem_id: {kept_id}\n---\n\nKept.\n")
    (notes / "food" / "a.md").write_text(f"---\ntype: note\nexomem_id: {gone_id}\n---\n\nGone.\n")
    index = memory_refs.ReferenceIndex(vault)
    index.rebuild_all()
    (notes / "food" / "a.md").unlink()
    (notes / "food").rmdir()

    assert memory_refs.delete_after_remove(vault, ["Knowledge Base/Notes/food/a.md"])

    # `resolve` would fall back to a scan; drift reads the sidecar rows themselves.
    assert memory_refs.drift(vault) == []


def test_the_export_reports_a_collision_code_without_paths(
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

    assert status == 1 and report["error"] == "CASE_COLLISION"
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
