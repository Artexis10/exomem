"""In a `root` vault a title-derived page name is held once across the vault, and `nested` keeps its per-folder rule.

Each test runs a writer entry point against a vault that has the `root` layout, as `test_root_writers.py`
does. The names are what a person sees in the file list and what a bare link resolves by, so a test reads
the file names on disk, not the helper that chose them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from exomem import commands
from exomem.governance.principal import library_scope
from tests.conftest import FIXTURE_VAULT, bind_root_vault, initialize_vault_state_offline

KB = "Knowledge Base"
UNIT = "## Observations\n\n- [finding] The runbook lists the restart order.\n"
BYTE_CAP = 200


@pytest.fixture
def root_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dest = tmp_path / "rootvault"
    shutil.copytree(FIXTURE_VAULT / KB, dest, ignore=shutil.ignore_patterns("Notes"))
    shutil.move(dest / "_Schema", dest / ".exomem" / "schema")
    bind_root_vault(dest, monkeypatch)
    initialize_vault_state_offline(dest, source="root unique names fixture")
    return dest


def _write(vault: Path, title: str, note_type: str = "procedure", **kwargs: object) -> str:
    with library_scope():
        return commands.op_remember(vault, content=UNIT, status="draft", title=title, note_type=note_type, **kwargs)["path"]


def test_titles_that_differ_only_in_case_take_numbers_across_areas(root_vault: Path) -> None:
    """Two areas held `Churn cause` and `churn cause`, and a bare link to either name was ambiguous."""
    first = _write(root_vault, "Churn cause")
    second = _write(root_vault, "churn cause", note_type="insight")
    third = _write(root_vault, "Churn cause", note_type="insight")

    assert [first, second, third] == [
        "Procedures/Churn cause.md",
        "Insights/churn cause (2).md",
        "Insights/Churn cause (3).md",
    ]


def test_a_link_to_each_numbered_page_resolves_to_its_one_file(root_vault: Path) -> None:
    """The point of a vault-wide name: `[[Name]]` reaches a single file, whichever area holds it."""
    from exomem.vault import WikilinkResolver

    pages = [_write(root_vault, "Retry budget"), _write(root_vault, "retry budget", note_type="insight")]
    resolver = WikilinkResolver(root_vault)

    for page in pages:
        found = resolver.obsidian_match(Path(page).stem)
        assert found is not None and found.path == page


def _names(vault: Path, area: str) -> set[str]:
    return {path.name for path in (vault / area).glob("*.md")}


def test_every_character_a_name_removes_gives_the_same_name_so_each_page_is_numbered(root_vault: Path) -> None:
    """A title that differs from another only in a character the file name drops must not make a second `before after.md`."""
    removed = list('<>:"/\\|?*#[]^`')
    areas = ["procedure", "insight"]
    for index, character in enumerate(removed):
        _write(root_vault, f"before{character}after", note_type=areas[index % 2])

    names = sorted(_names(root_vault, "Procedures") | _names(root_vault, "Insights"))
    expected = ["before after.md", *(f"before after ({number}).md" for number in range(2, len(removed) + 1))]
    assert names == sorted(expected)


def test_a_leading_dot_is_dropped_and_the_name_is_still_held_once(root_vault: Path) -> None:
    """A name that starts with a dot is hidden by Obsidian, so it loses the dots; the plain title then collides."""
    assert _write(root_vault, "..draft plan") == "Procedures/draft plan.md"
    assert _write(root_vault, "draft plan", note_type="insight") == "Insights/draft plan (2).md"


def test_a_multibyte_title_at_the_byte_cap_shrinks_to_fit_its_number(root_vault: Path) -> None:
    """The number counts toward the 200-byte cap, so the title part is cut at a character, not the file name at a byte."""
    title = "é" * 150
    first = _write(root_vault, title)
    second = _write(root_vault, title, note_type="insight")

    assert first == "Procedures/" + "é" * 98 + ".md"
    assert second == "Insights/" + "é" * 96 + " (2).md"
    assert all(len(Path(page).name.encode()) <= BYTE_CAP for page in (first, second))


def test_an_explicit_slug_keeps_its_per_folder_rule(root_vault: Path) -> None:
    """A caller who pins a slug links to it by that name: the same slug is allowed in two areas and takes `-2` in the same folder."""
    pages = [
        _write(root_vault, "Churn cause", slug="churn-cause"),
        _write(root_vault, "Churn cause", note_type="insight", slug="churn-cause"),
        _write(root_vault, "Churn cause", slug="churn-cause"),
    ]

    assert pages == ["Procedures/churn-cause.md", "Insights/churn-cause.md", "Procedures/churn-cause-2.md"]


def test_a_nested_vault_keeps_the_per_folder_dash_number(tmp_path: Path) -> None:
    """Only a `root` vault counts a name across the vault: `nested` allows a name in each folder and numbers with `-2`."""
    nested = tmp_path / "nestedvault"
    shutil.copytree(FIXTURE_VAULT, nested)
    initialize_vault_state_offline(nested, source="nested names fixture")

    pages = [
        _write(nested, "Churn cause"),
        _write(nested, "Churn cause", note_type="insight"),
        _write(nested, "Churn cause"),
    ]

    leaves = [page.removeprefix(f"{KB}/Notes/") for page in pages]
    assert leaves == ["Procedures/Churn cause.md", "Insights/Churn cause.md", "Procedures/Churn cause-2.md"]


def test_a_page_restored_from_trash_into_a_held_name_takes_a_number(root_vault: Path) -> None:
    """The trash holds no name, so a new page may take the freed name; the restored page then numbers, as a new page would."""
    with library_scope():
        original = _write(root_vault, "Retry budget")
        trashed = commands.op_delete(root_vault, path=original, confirm=True, force_orphan=True)
        reused = _write(root_vault, "Retry budget", note_type="insight")
        assert reused == "Insights/Retry budget.md"

        recovered = commands.op_recover_from_trash(root_vault, trash_path=trashed["trash_path"])

    assert recovered["restored_path"] == "Procedures/Retry budget (2).md"
    assert (root_vault / recovered["restored_path"]).is_file()


def test_a_restore_to_a_path_the_caller_chose_keeps_its_refusal(root_vault: Path) -> None:
    """A `restore_path` is the caller's own name for the page, so a held path is refused rather than renamed."""
    with library_scope():
        original = _write(root_vault, "Retry budget")
        trashed = commands.op_delete(root_vault, path=original, confirm=True, force_orphan=True)
        _write(root_vault, "Retry budget")

        with pytest.raises(ValueError, match="DEST_EXISTS"):
            commands.op_recover_from_trash(
                root_vault, trash_path=trashed["trash_path"], restore_path="Procedures/Retry budget.md"
            )


def test_an_import_plan_gives_same_titled_notes_of_different_areas_numbers_the_leaf_keeps(root_vault: Path) -> None:
    """The plan freezes each note's name and the note leaf must land there, or the import fails as stale."""
    from exomem import adoption_plan
    from exomem import note as note_module

    destinations = adoption_plan.Destinations(root_vault)
    day = "2026-07-14"
    planned = [
        adoption_plan.plan_note(
            root_vault, {"title": "Entry"}, "# Entry\n\nText.\n", {"note_type": kind}, destinations=destinations, date_iso=day
        )
        for kind in ("insight", "procedure", "insight")
    ]

    assert [note["target_path"] for note in planned] == [
        "Insights/Entry.md",
        "Procedures/Entry (2).md",
        "Insights/Entry (3).md",
    ]
    for note in planned:
        preflight = note_module.note(
            root_vault,
            validate_only=True,
            content=UNIT,
            note_type=note["note_type"],
            title=note["title"],
            status="draft",
            _name_number=note.get("name_number"),
            _path_date=day,
        )
        assert preflight.destination == note["target_path"]


def test_an_entity_takes_a_number_when_a_page_in_another_area_holds_its_name(root_vault: Path) -> None:
    """The entity writer names a page too, so a person called like a note must not make a second `Mira Okafor.md`."""
    note = _write(root_vault, "Mira Okafor")
    with library_scope():
        entity = commands.op_connect_memory(
            root_vault,
            operation="create-entity",
            entity_type="person",
            name="mira okafor",
            summary="Maintains the pump fleet.",
            why_in_kb="She owns the maintenance runbook.",
        )["path"]

    assert note == "Procedures/Mira Okafor.md"
    assert entity == "Entities/People/mira okafor (2).md"
