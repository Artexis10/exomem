"""A `root` vault files every new page flat in its area folder, gates compiled types by area, and owns its root.

Each test names the failure that only it catches. A `root` vault is the fixture's managed folder with
the layout record written beside its schema folder; a `nested` vault is the fixture as it stands.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from exomem import commands, layout, reclassify_source, semantic_contract
from exomem.governance.principal import library_scope
from exomem.vault_store import ownership
from tests.conftest import FIXTURE_VAULT, initialize_vault_state_offline

KB = "Knowledge Base"
UNIT = "## Observations\n\n- [finding] The runbook lists the restart order.\n"
ARTICLE = "Sources/Articles/2026-05-04-best-egcg-supplements.md"


def _write(vault: Path, rel: str, text: str = "") -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def root_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fixture's managed folder as the vault root, with a record that says `layout: root`."""
    dest = tmp_path / "rootvault"
    shutil.copytree(FIXTURE_VAULT / KB, dest)
    _write(dest, f"_Schema/{layout.RECORD_NAME}", "layout: root\n")
    _write(dest, ".exomem/schema/SKILL.md", "---\nname: exomem\n---\n")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(dest))
    initialize_vault_state_offline(dest, source="root layout fixture")
    return dest


def _remember(vault: Path, **kwargs: object) -> dict:
    with library_scope():
        return commands.op_remember(vault, content=UNIT, status="draft", **kwargs)


def _managed(vault: Path) -> layout.Managed:
    return layout.managed_of(vault)


def _finding(vault: Path, rel: str, *, page_type: str | None) -> str | None:
    front = f"---\ntype: {page_type}\n---\n" if page_type else "---\ntitle: A page\n---\n"
    state = semantic_contract.build_page_state(vault, rel, f"{front}\nbody\n")
    found = semantic_contract.compiled_structure_finding(state)
    return None if found is None else found.code


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #


def test_an_insight_lands_in_its_area_at_the_vault_root(root_vault: Path) -> None:
    """The writer joined the wrapper folder and `Notes/` in front of the area, so a root vault grew a second tree."""
    created = _remember(root_vault, title="Why churn rose", note_type="insight")

    assert created["path"] == "Insights/why-churn-rose.md"
    assert (root_vault / created["path"]).is_file()
    assert not (root_vault / KB).exists()


def test_a_project_partitioned_type_is_flat_in_a_root_vault(root_vault: Path) -> None:
    """A project folder in a root vault makes a project change move the file, and the area stops being flat."""
    created = _remember(root_vault, title="Pump notes", note_type="research-note", project="alpha")

    assert created["path"] == "Research/pump-notes.md"
    assert (root_vault / "_Schema/project-keys.yaml").is_file()
    assert not (root_vault / "Research/Alpha").exists()


def test_a_project_partitioned_type_stays_in_its_project_folder_in_a_nested_vault(vault: Path) -> None:
    """The flat rule must not leak into `nested`, which keeps a folder for each project."""
    created = _remember(vault, title="Pump notes", note_type="research-note", project="alpha")

    assert created["path"] == f"{KB}/Notes/Research/Alpha/pump-notes.md"


def test_an_overlay_type_lands_in_its_own_top_level_area(root_vault: Path) -> None:
    """The overlay was read from the wrapper's schema folder, so a root vault's own type was never found."""
    _write(
        root_vault,
        "_Schema/note-types.yaml",
        "schema_version: 1\nentries:\n  playbook:\n    label: Playbook\n    description: A playbook.\n"
        "    attributes: {role: compiled, folder: Playbooks}\n",
    )

    created = _remember(root_vault, title="Restart order", note_type="playbook")

    assert created["path"] == "Playbooks/restart-order.md"


def test_a_home_is_a_vault_root_folder_and_receives_the_page(root_vault: Path) -> None:
    """A home path joined to `Notes/` files the page where no reader of a root vault looks for it."""
    _write(
        root_vault,
        "_Schema/homes.yaml",
        "schema_version: 1\nentries:\n  equipment:\n    label: Equipment\n"
        "    description: Workshop notes.\n    attributes: {folder: Equipment, topics: [equipment]}\n",
    )

    created = _remember(root_vault, title="Pump pressure holds", note_type="insight", topics=["equipment"])

    assert created["path"] == "Equipment/pump-pressure-holds.md"


def test_a_domain_only_reclassification_moves_no_file_in_a_root_vault(root_vault: Path) -> None:
    """A domain folder under the kind makes a correction of frontmatter rename the file and rewrite its inbound links."""
    result = reclassify_source.reclassify(
        root_vault, path=ARTICLE, domain="travel", reason="the article is about travel, not health"
    )

    assert result.new_path == ARTICLE
    assert result.relocated is False
    assert (root_vault / ARTICLE).is_file()


# --------------------------------------------------------------------------- #
# The compiled-type write gate
# --------------------------------------------------------------------------- #


def test_the_gate_reads_a_top_level_area_as_a_compiled_destination(root_vault: Path) -> None:
    """A gate that knew only `Notes/<Area>` would let a page with no compiled type sit in an area of a root vault."""
    assert _finding(root_vault, "Insights/x.md", page_type="insight") is None
    assert _finding(root_vault, "Insights/x.md", page_type=None) == "COMPILED_TYPE_MISMATCH"


def test_the_gate_refuses_a_compiled_type_outside_every_compiled_folder(root_vault: Path) -> None:
    """A compiled page at the root, in a source area or in a collection layer is read as a source or a record."""
    for rel in ("x.md", "Sources/Articles/x.md", "Records/x.md"):
        assert _finding(root_vault, rel, page_type="insight") == "COMPILED_DESTINATION_MISMATCH", rel


def test_a_leftover_notes_folder_keeps_its_meaning_in_a_root_vault(root_vault: Path) -> None:
    """A relayout leaves `Notes/<X>` folders behind. A registered area must still read as itself and any other folder be left alone."""
    assert _finding(root_vault, "Notes/Insights/x.md", page_type=None) == "COMPILED_TYPE_MISMATCH"
    assert _finding(root_vault, "Notes/Scratch/x.md", page_type=None) is None
    assert _finding(root_vault, "Notes/Scratch/x.md", page_type="insight") is None


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


def test_a_root_vault_owns_its_top_level_folders_and_not_its_staging_or_dot_names(root_vault: Path) -> None:
    """The store looked for a wrapper folder, so it owned nothing in a root vault and everything stayed a read-only input."""
    _write(root_vault, "Archive/old.md", "x")
    _write(root_vault, f"_Schema/{layout.RECORD_NAME}", "layout: root\nunmanaged: [Archive]\n")
    managed = _managed(root_vault)

    assert ownership.classify("Insights/x.md", managed) is ownership.Ownership.PAGE
    assert ownership.classify("Sources/Articles/scan.pdf", managed) is ownership.Ownership.BINARY
    assert ownership.classify("_Imports/abc/x.md", managed) is ownership.Ownership.NONE
    assert ownership.classify(".obsidian/app.json", managed) is ownership.Ownership.NONE
    assert ownership.classify("Archive/old.md", managed) is ownership.Ownership.NONE
    assert ownership.classify("Records/ledger/row.md", managed) is ownership.Ownership.NONE


def test_the_ownership_of_a_nested_vault_is_the_same_with_and_without_its_layout(vault: Path) -> None:
    """Passing the layout must not change `nested`, which the store's callers still ask without it."""
    managed = _managed(vault)

    for rel in (f"{KB}/Notes/Insights/x.md", "Insights/x.md", f"{KB}/.obsidian/x.json", f"{KB}/Records/a/b.md"):
        assert ownership.classify(rel, managed) is ownership.classify(rel)


def test_a_folder_is_owned_in_a_root_vault_by_the_same_rules(root_vault: Path) -> None:
    """`owns_folder` decides whether a delete or move runs as a store write, so it must agree with `classify`."""
    managed = _managed(root_vault)

    assert ownership.owns_folder("", managed)
    assert ownership.owns_folder("Insights", managed)
    assert not ownership.owns_folder("_Imports/abc", managed)
    assert not ownership.owns_folder(".obsidian", managed)
    assert not ownership.owns_folder("Records", managed)


def test_the_walk_of_a_root_vault_yields_its_pages_and_skips_staging(root_vault: Path) -> None:
    """A walk that started under the wrapper name found nothing in a root vault, and migration then imported an empty vault."""
    _write(root_vault, "_Imports/abc/staged.md", "x")
    _write(root_vault, ".obsidian/workspace.json", "{}")

    walked = {owned.rel for owned in ownership.iter_owned(root_vault, managed=_managed(root_vault))}

    assert "Sources/Articles/2026-05-04-best-egcg-supplements.md" in walked
    assert "_Imports/abc/staged.md" not in walked
    assert ".obsidian/workspace.json" not in walked


# --------------------------------------------------------------------------- #
# A shared namespace for areas, homes and group folders
# --------------------------------------------------------------------------- #


def _save(vault: Path, subject: str, key: str, attributes: dict) -> dict:
    row = {"label": key.title(), "description": f"A {key}.", "attributes": attributes}
    with library_scope():
        inspected = commands.op_schema_memory(vault, subject=subject, operation="inspect")
        return commands.op_schema_memory(
            vault,
            subject=subject,
            operation="save",
            proposal={"upsert": {key: row}},
            expected_hash=inspected["content_hash"],
            why="the owner keeps these notes in their own folder",
        )


def test_a_compiled_type_cannot_take_a_group_folder_in_a_root_vault(root_vault: Path) -> None:
    """Two roles in a single folder make the gate read an entity page or a record row as a compiled note."""
    for folder in ("Entities", "Records"):
        saved = _save(root_vault, "note-types", "catalogue", {"role": "compiled", "folder": folder})

        assert saved["valid"] is False, folder
        refusal = next(item for item in saved["findings"] if item["code"] == "NOTE_TYPE_AREA_COLLIDES")
        assert "attributes.folder" in refusal["detail"]
    assert not (root_vault / "_Schema/note-types.yaml").exists()


def test_a_home_cannot_take_a_group_folder_in_a_root_vault(root_vault: Path) -> None:
    """A home on a group folder files compiled pages among entity pages."""
    saved = _save(root_vault, "homes", "stock", {"folder": "Entities", "topics": ["stock"]})

    assert saved["valid"] is False
    assert any(item["code"] == "HOME_AREA_COLLIDES" for item in saved["findings"])


def test_the_same_compiled_type_and_home_save_in_a_nested_vault(vault: Path) -> None:
    """Below `Notes/` the folder is free, so the refusal must not fire in `nested`."""
    type_saved = _save(vault, "note-types", "catalogue", {"role": "compiled", "folder": "Entities"})
    home_saved = _save(vault, "homes", "stock", {"folder": "Entities", "topics": ["stock"]})

    assert type_saved.get("valid") is not False
    assert home_saved.get("valid") is not False
