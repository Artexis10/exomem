"""A governed write never creates a folder that differs from a sibling folder only by separators.

Each case builds the pair on disk with invented names. A pair that differs by separators holds on
every volume, so no case here needs a case-sensitive one.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest

from exomem import audit as audit_module
from exomem import note as note_module
from exomem import path_variants, project_keys
from exomem import preserve as preserve_module
from exomem import vault as vault_module
from exomem.commands import op_maintain_memory
from exomem.create_directory import create_directory
from exomem.create_file import create_file
from exomem.move_file import MoveFileError, move_file

KB = "Knowledge Base"


def _page(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: note\n---\n\nBody.\n", encoding="utf-8")


def test_a_capture_scope_without_its_hyphen_lands_in_the_existing_folder_and_is_tagged_by_it(
    vault: Path,
) -> None:
    """Defect: scope `gardentools` beside `garden-tools` splits the collection, then tags it apart."""
    evidence = vault / KB / "Evidence"
    _page(evidence / "garden-tools" / "shared" / "old.md")

    result = preserve_module.preserve(
        vault, scope="gardentools", category="shared", filename="note.txt", content="trowel"
    )

    assert result.path.startswith(f"{KB}/Evidence/garden-tools/shared/")
    assert not (evidence / "gardentools").exists()
    tags = vault_module.parse_frontmatter((vault / result.sidecar_path).read_text())[0]["tags"]
    assert "garden-tools" in tags and "gardentools" not in tags


def test_a_capture_category_with_an_underscore_lands_in_the_existing_folder(vault: Path) -> None:
    """Defect: category `shared_docs` beside `shared-docs` opens a second category folder."""
    evidence = vault / KB / "Evidence"
    _page(evidence / "Garden" / "shared-docs" / "old.md")

    result = preserve_module.preserve(
        vault, scope="Garden", category="shared_docs", filename="note.txt", content="trowel"
    )

    assert result.path.startswith(f"{KB}/Evidence/Garden/shared-docs/")
    assert not (evidence / "Garden" / "shared_docs").exists()


def test_a_note_placement_folder_is_steered_into_its_look_alike(vault: Path) -> None:
    """Defect: a note whose medium differs from an existing folder by a hyphen files beside it."""
    _page(vault / KB / "Notes" / "Productions" / "Garden-Tools" / "old.md")

    draft = note_module.note(
        vault,
        content="## Observations\n\n- [finding] An invented outcome.\n",
        note_type="production-log",
        title="Launch Recap",
        medium="gardentools",
        validate_only=True,
        today=dt.date(2026, 5, 18),
    )

    assert draft.destination.startswith(f"{KB}/Notes/Productions/Garden-Tools/")


def test_a_directory_is_created_in_its_look_alike_not_beside_it(vault: Path) -> None:
    """Defect: create_directory makes `garden tools` beside `garden-tools`, so the leaf folder is not steered."""
    _page(vault / KB / "Notes" / "garden-tools" / "old.md")

    result = create_directory(vault, path=f"{KB}/Notes/garden tools")

    assert result.path == f"{KB}/Notes/garden-tools"
    assert not (vault / KB / "Notes" / "garden tools").exists()


def test_other_punctuation_keeps_two_folder_names_apart(vault: Path) -> None:
    """Defect: `C++` is read as `C` because only letters and digits are compared."""
    _page(vault / KB / "Notes" / "C" / "old.md")

    result = create_file(vault, path=f"{KB}/Notes/C++/intro.md", content="Intro.\n")

    assert result.path == f"{KB}/Notes/C++/intro.md"


def test_a_separator_between_two_numbers_keeps_numbered_folders_apart(vault: Path) -> None:
    """Defect: `Week 12` lands in `Week 1-2` and a date folder in another date, with no warning."""
    notes = vault / KB / "Notes"
    _page(notes / "Course" / "Week 1-2" / "old.md")
    _page(notes / "Journal" / "2026-1-12" / "old.md")

    week = create_file(vault, path=f"{KB}/Notes/Course/Week 12/notes.md", content="Notes.\n")
    day = create_file(vault, path=f"{KB}/Notes/Journal/2026-11-2/meeting.md", content="Meeting.\n")

    assert week.path == f"{KB}/Notes/Course/Week 12/notes.md"
    assert day.path == f"{KB}/Notes/Journal/2026-11-2/meeting.md"


def test_a_sources_folder_keeps_the_spelling_its_taxonomy_commits_to(vault: Path) -> None:
    """Defect: a stray `Field-Notes` takes a Source's `Field Notes` folder, and adoption then refuses the move."""
    sources = vault / KB / "Sources"
    for name in ("a", "b", "c"):
        _page(sources / "Field-Notes" / f"{name}.md")
    _page(sources / "Field Notes" / "d.md")

    assert vault_module.respell_new_path(vault, f"{KB}/Sources/Field Notes/new.md") == (
        f"{KB}/Sources/Field Notes/new.md"
    )
    assert vault_module.respell_new_path(vault, f"{KB}/Sources/Field Notes", leaf_is_folder=True) == (
        f"{KB}/Sources/Field Notes"
    )
    assert "Field" not in str(op_maintain_memory(vault, mode="path-variants"))


def test_a_leading_underscore_keeps_a_system_folder_apart(vault: Path) -> None:
    """Defect: a page for `Drafts` lands in the system folder `_Drafts` because their separators match."""
    _page(vault / KB / "Notes" / "_Drafts" / "old.md")

    result = create_file(vault, path=f"{KB}/Notes/Drafts/plan.md", content="Plan.\n")

    assert result.path == f"{KB}/Notes/Drafts/plan.md"


def test_a_file_name_is_not_steered_but_a_folder_of_the_same_name_is(vault: Path) -> None:
    """Defect: a file named `gardentools` is written as the folder `garden-tools`, so it replaces a folder."""
    notes = vault / KB / "Notes"
    _page(notes / "garden-tools" / "old.md")
    _page(notes / "garden-tools.md")

    created = create_file(vault, path=f"{KB}/Notes/gardentools.md", content="Other.\n")

    assert created.path == f"{KB}/Notes/gardentools.md"
    assert vault_module.respell_new_path(vault, f"{KB}/Notes/garden tools") == f"{KB}/Notes/garden tools"
    assert vault_module.respell_new_path(vault, f"{KB}/Notes/garden tools", leaf_is_folder=True) == (
        f"{KB}/Notes/garden-tools"
    )


def test_a_group_never_gains_a_third_spelling(vault: Path) -> None:
    """Defect: the write takes the first sibling by name, not the fuller one, so the smaller side keeps growing."""
    notes = vault / KB / "Notes"
    for name in ("a", "b", "c"):
        _page(notes / "garden_tools" / f"{name}.md")
    _page(notes / "Garden-Tools" / "d.md")

    result = create_file(vault, path=f"{KB}/Notes/gardentools/plan.md", content="Plan.\n")

    assert result.path == f"{KB}/Notes/garden_tools/plan.md"
    assert not (notes / "gardentools").exists()


def test_a_write_naming_the_smaller_side_of_a_separator_pair_lands_in_the_fuller_one_and_warns(
    vault: Path,
) -> None:
    """Defect: an existing pair keeps growing on both sides, with no word that it is a split."""
    notes = vault / KB / "Notes"
    for name in ("a", "b", "c"):
        _page(notes / "garden-tools" / f"{name}.md")
    _page(notes / "gardentools" / "d.md")

    result = create_file(vault, path=f"{KB}/Notes/gardentools/plan.md", content="Plan.\n")

    assert result.path == f"{KB}/Notes/garden-tools/plan.md"
    assert any("gardentools" in warning and "path-variants" in warning for warning in result.warnings)


def test_a_page_moved_into_a_look_alike_of_its_own_folder_names_that_folder(vault: Path) -> None:
    """Defect: moving a page to `garden-tools/` opens a second folder beside its own `garden_tools/`."""
    notes = vault / KB / "Notes"
    _page(notes / "garden_tools" / "a.md")

    with pytest.raises(MoveFileError) as refused:
        move_file(
            vault,
            old_path=f"{KB}/Notes/garden_tools/a.md",
            new_path=f"{KB}/Notes/garden-tools/a.md",
            update_wikilinks=False,
        )

    assert refused.value.code == "PATH_VARIANT_EXISTS"
    assert (notes / "garden_tools" / "a.md").is_file()


def test_a_folder_at_the_top_of_the_vault_or_the_managed_root_keeps_its_spelling(vault: Path) -> None:
    """Defect: code reads these folders by exact spelling, so a steered write is never found again."""
    (vault / "Shared-Docs").mkdir()
    _page(vault / "Shared-Docs" / "old.md")
    _page(vault / KB / "side-notes" / "old.md")

    assert vault_module.respell_new_path(vault, "SharedDocs/plan.md") == "SharedDocs/plan.md"
    assert vault_module.respell_new_path(vault, f"{KB}/sidenotes/plan.md") == f"{KB}/sidenotes/plan.md"
    (vault / "Knowledge-Base").mkdir()
    _page(vault / KB / "No-tes" / "old.md")
    assert vault_module.respell_new_path(vault, "Knowledge-Base/plan.md") == "Knowledge-Base/plan.md"
    assert vault_module.respell_new_path(vault, f"{KB}/No-tes/plan.md") == f"{KB}/No-tes/plan.md"
    assert vault_module.folder_split_warnings(vault, vault / KB / "Notes") == []


def test_a_project_registry_spelling_keeps_its_spelling(vault: Path) -> None:
    """Defect: the registry names `Garden Tools`, yet a stray `Garden-Tools` takes its writes."""
    research = vault / KB / "Notes" / "Research"
    project_keys.register_project_key(vault, "garden-tools")
    assert project_keys.load_project_registry(vault).folder_for("garden-tools") == "Garden Tools"
    shutil.rmtree(research / "Garden Tools", ignore_errors=True)
    _page(research / "Garden-Tools" / "stray.md")

    assert vault_module.respell_new_path(vault, f"{KB}/Notes/Research/Garden Tools/plan.md") == (
        f"{KB}/Notes/Research/Garden Tools/plan.md"
    )


def test_a_capture_scope_named_as_a_project_folder_is_still_steered_outside_the_research_folder(
    vault: Path,
) -> None:
    """Defect: the project's spelling `Garden Tools` opens a second Evidence folder beside `garden-tools`."""
    project_keys.register_project_key(vault, "garden-tools")
    _page(vault / KB / "Evidence" / "garden-tools" / "shared" / "old.md")

    result = preserve_module.preserve(
        vault, scope="Garden Tools", category="shared", filename="note.txt", content="trowel"
    )

    assert result.path.startswith(f"{KB}/Evidence/garden-tools/shared/")
    assert not (vault / KB / "Evidence" / "Garden Tools").exists()


def test_a_folder_the_layout_declares_keeps_its_spelling(vault: Path) -> None:
    """Defect: a stray `Fail-ures` takes the writes of the declared `Failures` folder."""
    notes = vault / KB / "Notes"
    shutil.rmtree(notes / "Failures")
    _page(notes / "Fail-ures" / "stray.md")

    assert vault_module.respell_new_path(vault, f"{KB}/Notes/Failures/plan.md") == (
        f"{KB}/Notes/Failures/plan.md"
    )


def test_the_preview_merges_a_separator_pair_and_removes_the_emptied_folder(vault: Path) -> None:
    """Defect: a pair that differs only by a hyphen can be listed but never merged."""
    evidence = vault / KB / "Evidence"
    for name in ("a", "b", "c"):
        _page(evidence / "garden-tools" / f"{name}.md")
    _page(evidence / "gardentools" / "d.md")

    preview = op_maintain_memory(vault, mode="path-variants")

    assert [split["canonical"] for split in preview["splits"]] == ["garden-tools"]
    assert [(unit["from"], unit["to"]) for unit in preview["moves"]] == [
        (f"{KB}/Evidence/gardentools/d.md", f"{KB}/Evidence/garden-tools/d.md")
    ]
    applied = op_maintain_memory(
        vault, mode="path-variants", dry_run=False, plan_id=preview["plan_id"], why="merge the pair"
    )
    assert applied["outcome"] == "committed"
    assert applied["removed_folders"] == [f"{KB}/Evidence/gardentools"]
    assert (evidence / "garden-tools" / "d.md").is_file() and not (evidence / "gardentools").exists()
    assert op_maintain_memory(vault, mode="path-variants")["split_count"] == 0


def test_the_audit_reports_a_separator_pair_as_a_defect_that_the_preview_fixes(vault: Path) -> None:
    """Defect: the owner learns of the pair only as a candidate to judge, and no tool offers a merge."""
    _page(vault / KB / "Notes" / "Research" / "Tool Kit" / "a.md")
    _page(vault / KB / "Notes" / "Research" / "tool-kit" / "a.md")

    findings = [
        finding
        for finding in audit_module.audit(vault, categories=["path_variants"]).findings
        if finding.category == "path_variants"
    ]

    [defect] = [f for f in findings if f.meta == {"class": "defect"}]
    assert sorted(defect.paths) == [f"{KB}/Notes/Research/Tool Kit", f"{KB}/Notes/Research/tool-kit"]
    assert "path-variants" in defect.proposed_fix and "rename one of these by hand" not in defect.proposed_fix
    assert [f for f in findings if f.meta == {"class": "candidate"}] == []


def test_a_looser_look_alike_stays_a_candidate_and_is_not_merged(vault: Path) -> None:
    """Defect: `C` and `C++` are reported as a defect and offered for a merge."""
    _page(vault / KB / "Notes" / "Research" / "C" / "a.md")
    _page(vault / KB / "Notes" / "Research" / "C++" / "a.md")

    findings = [
        finding
        for finding in audit_module.audit(vault, categories=["path_variants"]).findings
        if finding.category == "path_variants"
    ]

    assert [f for f in findings if f.meta == {"class": "defect"}] == []
    assert [f.meta for f in findings] == [{"class": "candidate"}]
    assert path_variants.preview(vault)["split_count"] == 0


def test_two_pages_that_differ_only_by_a_hyphen_are_no_split(vault: Path) -> None:
    """Defect: `garden-tools.md` and `gardentools.md` are two pages, yet the merge calls them one name."""
    research = vault / KB / "Notes" / "Research"
    _page(research / "garden-tools.md")
    _page(research / "gardentools.md")

    findings = [
        finding
        for finding in audit_module.audit(vault, categories=["path_variants"]).findings
        if finding.category == "path_variants"
    ]

    assert [f for f in findings if f.meta == {"class": "defect"}] == []
    assert path_variants.preview(vault)["split_count"] == 0


def test_project_key_planning_refuses_a_folder_that_only_differs_by_separators(vault: Path) -> None:
    """Defect: registering `Garden Tools` beside `Garden-Tools/` splits the project's notes."""
    _page(vault / KB / "Notes" / "Research" / "Garden-Tools" / "a.md")

    with pytest.raises(ValueError, match="PATH_VARIANT_EXISTS"):
        project_keys.plan_project_keys(vault, ["garden-two"], folders={"garden-two": "Garden Tools"})
    key, folder, was_new = project_keys.register_project_key(vault, "garden-tools")

    assert (key, folder, was_new) == ("garden-tools", "Garden-Tools", True)
