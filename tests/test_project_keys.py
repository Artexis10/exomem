"""Tests for project-key registry + auto-register behaviour.

The writer auto-registers unknown slug-shaped project keys so LLMs don't
have to drop out to YAML edits. Invalid slugs still get rejected.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from kb_mcp import note as note_module
from kb_mcp import project_keys as project_keys_module


TODAY = dt.date(2026, 5, 28)


def test_load_registry_falls_back_when_yaml_missing(vault: Path) -> None:
    """Fixture vault has no project-keys.yaml — loader returns fallback set."""
    registry = project_keys_module.load_project_registry(vault)
    assert "substrate" in registry.project_to_folder
    assert registry.folder_for("q") == "Q"


def test_register_new_project_key_writes_yaml(vault: Path) -> None:
    """`register_project_key` bootstraps the YAML if missing + adds the key."""
    key, folder, was_new = project_keys_module.register_project_key(
        vault, "vehicles"
    )
    assert was_new is True
    assert key == "vehicles"
    assert folder == "Vehicles"
    yaml_path = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    assert yaml_path.exists()
    text = yaml_path.read_text(encoding="utf-8")
    assert "vehicles:" in text
    assert "folder: Vehicles" in text
    # Folder was created.
    folder_path = vault / "Knowledge Base" / "Notes" / "Research" / "Vehicles"
    assert folder_path.is_dir()


def test_register_existing_key_is_idempotent(vault: Path) -> None:
    """Registering the same key twice no-ops the second time."""
    project_keys_module.register_project_key(vault, "vehicles")
    _, _, was_new = project_keys_module.register_project_key(vault, "vehicles")
    assert was_new is False


def test_register_rejects_non_slug_keys(vault: Path) -> None:
    with pytest.raises(ValueError, match="not a valid slug"):
        project_keys_module.register_project_key(vault, "Vehicles!")
    with pytest.raises(ValueError):
        project_keys_module.register_project_key(vault, "UPPERCASE")
    with pytest.raises(ValueError):
        project_keys_module.register_project_key(vault, "")


def test_note_auto_registers_unknown_project_key(vault: Path) -> None:
    """`note(project=<new-slug>)` registers + creates folder + writes."""
    result = note_module.note(
        vault,
        content="# Test\n\n## Question\n\nWhat?\n",
        note_type="research-note",
        title="Vehicle test note",
        project="vehicles",
        today=TODAY,
    )
    # Note landed under the new folder.
    assert result.path == "Knowledge Base/Notes/Research/Vehicles/vehicle-test-note.md"
    assert (vault / result.path).exists()
    # Warning surfaced the registration.
    assert any(
        "Auto-registered" in w and "vehicles" in w for w in result.warnings
    ), result.warnings
    # YAML now contains the key.
    yaml_path = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    assert "vehicles:" in yaml_path.read_text(encoding="utf-8")


def test_note_auto_register_is_silent_on_second_use(vault: Path) -> None:
    """Once a key is registered, subsequent uses don't warn again."""
    note_module.note(
        vault,
        content="# First\n\n## Question\n\nWhat?\n",
        note_type="research-note",
        title="First in new scope",
        project="vehicles",
        today=TODAY,
    )
    second = note_module.note(
        vault,
        content="# Second\n\n## Question\n\nWhat?\n",
        note_type="research-note",
        title="Second in new scope",
        project="vehicles",
        today=TODAY,
    )
    assert not any(
        "Auto-registered" in w and "vehicles" in w for w in second.warnings
    ), second.warnings


def test_note_rejects_non_slug_project(vault: Path) -> None:
    """Invalid project slugs (uppercase, special chars) fall through to validation."""
    with pytest.raises(note_module.NoteError) as excinfo:
        note_module.note(
            vault,
            content="# Test\n\n## Question\n\nWhat?\n",
            note_type="research-note",
            title="Bad project",
            project="BAD-SLUG",
            today=TODAY,
        )
    assert excinfo.value.code == "INVALID_NOTE"


def test_note_projects_plural_auto_registers_each(vault: Path) -> None:
    """`projects:` list (plural) auto-registers every unknown key."""
    result = note_module.note(
        vault,
        content="# Pat\n\n## Problem\n\nX.\n",
        note_type="pattern",
        title="Cross-project pattern",
        projects=["vehicles", "automotive-something"],
        today=TODAY,
    )
    # Both keys appear in warnings.
    msgs = " ".join(result.warnings)
    assert "vehicles" in msgs
    assert "automotive-something" in msgs


def test_project_category_lands_in_yaml(vault: Path) -> None:
    """`project_category` arg should propagate into the YAML on new-key registration."""
    note_module.note(
        vault,
        content="# Note\n\n## Question\n\nbody",
        note_type="research-note",
        title="Categorised probe",
        project="new-domain-key",
        project_category="domain",
        today=TODAY,
    )
    yaml_path = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    assert "new-domain-key:" in text
    # The entry block should carry category: domain (not uncategorized).
    snippet = text.split("new-domain-key:", 1)[1].split("\n\n", 1)[0]
    assert "category: domain" in snippet


def test_levenshtein_basic() -> None:
    """Sanity check the edit-distance helper used by the typo guard."""
    lev = project_keys_module._levenshtein
    assert lev("substrate", "substrate") == 0
    assert lev("substrate", "subtsrate") == 2  # adjacent swap = 2 edits
    assert lev("q", "Q") == 1
    assert lev("substrate", "completely-different") > 2


def test_typo_within_distance_2_blocks_registration(vault: Path) -> None:
    """A single- or double-char typo of an existing key should raise."""
    # "subtsrate" → distance 2 from "substrate" (the adjacent-swap typo).
    with pytest.raises(project_keys_module.ProjectKeyTypoError) as exc_info:
        project_keys_module.register_project_key(vault, "subtsrate")
    assert exc_info.value.close_match == "substrate"
    assert exc_info.value.distance <= 2
    # YAML wasn't created (the fallback registry was used; no mutation happened).
    yaml_path = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    assert not yaml_path.exists()


def test_typo_distance_3_or_more_registers_silently(vault: Path) -> None:
    """A clearly-new key (distance 3+) goes through without challenge."""
    key, folder, was_new = project_keys_module.register_project_key(
        vault, "wholly-unrelated-key"
    )
    assert was_new
    assert folder == "Wholly Unrelated Key"


def test_note_surfaces_typo_as_project_key_typo_error(vault: Path) -> None:
    """Typo via the note() entry point should raise NoteError code=PROJECT_KEY_TYPO."""
    with pytest.raises(note_module.NoteError) as exc_info:
        note_module.note(
            vault,
            content="# Note\n\n## Question\n\nbody",
            note_type="research-note",
            title="Typo probe",
            project="helath",  # missing 't' — distance 1 from "health"
            today=TODAY,
        )
    assert exc_info.value.code == "PROJECT_KEY_TYPO"
    assert "health" in exc_info.value.reason  # suggestion present
