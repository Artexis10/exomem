"""Areas come from registries: a type's `folder` names an area, and the layout supplies its parent.

A compiled type's area sits below `Notes/` in a `nested` vault and at the vault root in a `root`
vault. The shipped pack, the vault overlay and the homes registry spell an area with or without
the `Notes/` parent, and every reader of a folder reads the same area from either spelling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import access, commands, homes, init, layout, note_types
from exomem.governance.principal import library_scope
from exomem.layout import Layout
from exomem.vocabulary import registry

KB = "Knowledge Base"
UNIT = "## Observations\n\n- [finding] The runbook lists the restart order.\n"


@pytest.fixture(params=["Playbooks", "Notes/Playbooks"])
def playbook_spelling(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(params=["Equipment", "Notes/Equipment"])
def equipment_spelling(request: pytest.FixtureRequest) -> str:
    return request.param


def _write(vault: Path, rel: str, text: str = "") -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _save_note_type(vault: Path, key: str, attributes: dict) -> dict:
    row = {"label": key.title(), "description": f"A {key}.", "attributes": attributes}
    with library_scope():
        inspected = commands.op_schema_memory(vault, subject="note-types", operation="inspect")
        return commands.op_schema_memory(
            vault,
            subject="note-types",
            operation="save",
            proposal={"upsert": {key: row}},
            expected_hash=inspected["content_hash"],
            why="the owner keeps these notes in their own area",
        )


def _parse_overlay(entries: dict) -> note_types.NoteTypeRegistry:
    rows = {
        key: {"label": key, "description": f"A {key}.", "attributes": attributes}
        for key, attributes in entries.items()
    }
    return note_types.SPEC.adapter.parse_document({"schema_version": 1, "entries": rows})


# --------------------------------------------------------------------------- #
# The typed writer reads the area from the effective registry
# --------------------------------------------------------------------------- #


def test_an_overlay_type_is_created_in_its_area_below_notes_in_a_nested_vault(
    vault: Path, playbook_spelling: str
) -> None:
    """The writer read the shipped pack only, so a vault's own compiled type could not be created at all."""
    _save_note_type(vault, "playbook", {"role": "compiled", "folder": playbook_spelling})

    with library_scope():
        created = commands.op_remember(
            vault, title="Restart order", note_type="playbook", content=UNIT, status="draft"
        )

    assert created["path"] == f"{KB}/Notes/Playbooks/restart-order.md"
    assert (vault / created["path"]).is_file()


def test_a_home_named_by_its_area_alone_receives_notes_below_notes_in_a_nested_vault(
    vault: Path,
) -> None:
    """A home path joined to the managed root without its parent would file the page beside `Notes/`."""
    _write(
        vault,
        f"{KB}/_Schema/homes.yaml",
        "schema_version: 1\nentries:\n  equipment:\n    label: Equipment\n"
        "    description: Workshop notes.\n    attributes: {folder: Equipment, topics: [equipment]}\n",
    )

    with library_scope():
        created = commands.op_remember(
            vault,
            title="Pump pressure holds",
            note_type="insight",
            content=UNIT,
            status="draft",
            topics=["equipment"],
        )

    assert created["path"] == f"{KB}/Notes/Equipment/pump-pressure-holds.md"


# --------------------------------------------------------------------------- #
# Both spellings of a folder name the same area
# --------------------------------------------------------------------------- #


def test_a_type_folder_with_and_without_the_notes_parent_validates_and_names_a_single_area() -> None:
    """Both spellings are saved in the field, and the root layout needs a single answer for each."""
    typed = _parse_overlay(
        {
            "runbook": {"role": "compiled", "folder": "Runbooks"},
            "playbook": {"role": "compiled", "folder": "Notes/Playbooks"},
        }
    )

    for key, nested, root in (
        ("runbook", "Notes/Runbooks", "Runbooks"),
        ("playbook", "Notes/Playbooks", "Playbooks"),
    ):
        assert layout.area_folder(typed.types[key], Layout.NESTED) == nested
        assert layout.area_folder(typed.types[key], Layout.ROOT) == root


def test_the_two_spellings_of_a_compiled_folder_count_as_a_single_folder() -> None:
    """The uniqueness check compared the whole value, so a single folder could have held two types."""
    for first, second in (("Playbooks", "Notes/playbooks"), ("Notes/Playbooks", "PLAYBOOKS")):
        with pytest.raises(registry.RegistryError, match="NOTE_TYPE_FOLDER_TAKEN"):
            _parse_overlay(
                {
                    "playbook": {"role": "compiled", "folder": first},
                    "runbook": {"role": "compiled", "folder": second},
                }
            )


def test_a_folder_the_router_exempts_is_refused_in_either_spelling() -> None:
    for spelling in ("Data", "Notes/Data"):
        with pytest.raises(registry.RegistryError, match="NOTE_TYPE_FOLDER_RESERVED"):
            _parse_overlay({"data-note": {"role": "compiled", "folder": spelling}})


def test_a_home_folder_with_and_without_the_notes_parent_names_a_single_area(
    tmp_path: Path, equipment_spelling: str
) -> None:
    _write(
        tmp_path,
        f"{KB}/_Schema/homes.yaml",
        "schema_version: 1\nentries:\n  equipment:\n    label: Equipment\n"
        f"    description: Workshop notes.\n    attributes: {{folder: {equipment_spelling}, topics: [equipment]}}\n",
    )

    loaded = homes.load(tmp_path)

    assert loaded.findings == ()
    [home] = loaded.homes
    assert home.parts == ("Equipment",)
    assert layout.home_folder(home, Layout.NESTED) == "Notes/Equipment"
    assert layout.home_folder(home, Layout.ROOT) == "Equipment"


def test_a_home_in_a_shipped_type_area_is_refused_in_either_spelling(tmp_path: Path) -> None:
    for spelling in ("Insights", "Notes/Insights"):
        _write(
            tmp_path,
            f"{KB}/_Schema/homes.yaml",
            "schema_version: 1\nentries:\n  rigs:\n    label: Rigs\n    description: Rigs.\n"
            f"    attributes: {{folder: {spelling}, topics: [rigs]}}\n",
        )

        findings = homes.load(tmp_path).findings

        assert [item["detail"] for item in findings if "HOME_FOLDER_TAKEN" in item["detail"]], spelling


# --------------------------------------------------------------------------- #
# Areas of the other roles
# --------------------------------------------------------------------------- #


def test_the_shipped_compiled_types_keep_their_nested_folders_and_drop_notes_in_root() -> None:
    expected = {
        "research-note": "Research",
        "insight": "Insights",
        "failure": "Failures",
        "pattern": "Patterns",
        "experiment": "Experiments",
        "production-log": "Productions",
        "procedure": "Procedures",
    }
    types = note_types.shipped_registry().types

    assert {key: layout.area_folder(types[key], Layout.ROOT) for key in expected} == expected
    assert {key: layout.area_folder(types[key], Layout.NESTED) for key in expected} == {
        key: f"Notes/{area}" for key, area in expected.items()
    }


def test_source_evidence_and_entity_types_sit_at_the_managed_root_in_both_layouts() -> None:
    by_role = {note_type.role: note_type for note_type in note_types.shipped() if note_type.folder}

    for role, area in (("source", "Sources"), ("evidence", "Evidence"), ("entity", "Entities")):
        assert layout.area_folder(by_role[role], Layout.NESTED) == area
        assert layout.area_folder(by_role[role], Layout.ROOT) == area


def test_a_save_gives_a_source_type_an_area_and_refuses_to_change_it(vault: Path) -> None:
    """Only a compiled type needs a folder, and a saved folder stays because pages already sit there."""
    saved = _save_note_type(vault, "letter", {"role": "source", "folder": "Letters"})
    assert saved["valid"] and saved["saved"]

    with pytest.raises(registry.RegistryError, match="IMMUTABLE_REGISTRY_MEANING"):
        _save_note_type(vault, "letter", {"role": "source", "folder": "Mail"})


def test_a_type_without_a_role_cannot_declare_a_folder() -> None:
    """A folder with no role would give a type an area that nothing reads."""
    with pytest.raises(registry.RegistryError, match="INVALID_NOTE_TYPE_FOLDER"):
        _parse_overlay({"memo": {"folder": "Memos"}})


# --------------------------------------------------------------------------- #
# What init lays down and what access treats as append-only
# --------------------------------------------------------------------------- #


def test_init_creates_every_registered_area_and_the_entity_groups(tmp_path: Path) -> None:
    """`init` kept its own folder list, so the area added to the pack after it (`Notes/Procedures`) was never created."""
    init.init_vault(tmp_path, initialize_state=False)
    kb = tmp_path / KB

    compiled_areas = {
        note_types.folder_below_notes(note_type.folder)[0]
        for note_type in note_types.shipped(note_types.compiled)
    }
    assert {path.name for path in (kb / "Notes").iterdir() if path.is_dir()} == compiled_areas
    for note_type in note_types.shipped():
        if note_type.folder is not None and not note_types.compiled(note_type):
            assert (kb / note_type.folder).is_dir(), note_type.key
    assert (kb / "Notes" / "Procedures").is_dir()
    assert (kb / "Entities" / "Concepts").is_dir()


def test_the_append_only_tier_covers_the_source_and_evidence_areas_and_no_other_area(
    tmp_path: Path,
) -> None:
    """A hand-kept list would let a new source area stay read-write, or a new entity area turn append-only."""
    for note_type in note_types.shipped():
        folder = layout.area_folder(note_type, Layout.NESTED)
        if folder is None:
            continue
        tier = access.access_tier(tmp_path, f"{KB}/{folder}/page.md")
        expected = access.TIER_APPEND_ONLY if note_types.raw(note_type) else access.TIER_READ_WRITE
        assert tier == expected, note_type.key
    assert access.append_only_areas() == {
        note_type.folder for note_type in note_types.shipped(note_types.raw)
    }
