"""A `root` vault is written the way an agent writes it, and no writer grows a second tree.

Each test runs the tool entry point against a vault whose layout record says `layout: root`, with the
process bound to that vault as the service binds it at start. A writer that still spells the old
wrapper folder or the `Notes/` parent leaves a folder that no reader of a `root` vault looks in.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from exomem import commands
from exomem import schema as schema_module
from exomem.governance.principal import library_scope
from tests.conftest import FIXTURE_VAULT, bind_root_vault, initialize_vault_state_offline

KB = "Knowledge Base"
NOTES = "Notes"
UNIT = "## Observations\n\n- [finding] The runbook lists the restart order.\n"


@pytest.fixture
def root_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fixture's managed folder as the vault root, less its `Notes/` tree, bound as the served vault.

    The shipped schema sits in `.exomem/schema/`, where a `root` vault keeps it.
    """
    dest = tmp_path / "rootvault"
    shutil.copytree(FIXTURE_VAULT / KB, dest, ignore=shutil.ignore_patterns(NOTES))
    shutil.move(dest / "_Schema", dest / ".exomem" / "schema")
    bind_root_vault(dest, monkeypatch)
    initialize_vault_state_offline(dest, source="root writers fixture")
    return dest


@pytest.fixture
def source_schema(root_vault: Path) -> schema_module.SourceSchema:
    return schema_module.load_source_schema(root_vault)


def _folders(vault: Path) -> set[str]:
    return {path.relative_to(vault).as_posix() for path in vault.rglob("*") if path.is_dir()}


def _assert_no_second_tree(vault: Path) -> None:
    """No wrapper folder and no `Notes/` folder anywhere in the vault, however deep."""
    stray = sorted(
        folder for folder in _folders(vault) if folder.split("/")[-1] in {KB, NOTES}
    )
    assert stray == []


def _remember(vault: Path, **kwargs: object) -> dict:
    with library_scope():
        return commands.op_remember(vault, content=UNIT, status="draft", **kwargs)


def test_a_typed_note_lands_in_its_area(root_vault: Path) -> None:
    """The typed writer joined the wrapper folder and `Notes/` in front of the area."""
    created = _remember(root_vault, title="Why churn rose", note_type="insight")

    assert created["path"] == "Insights/Why churn rose.md"
    assert (root_vault / created["path"]).is_file()
    _assert_no_second_tree(root_vault)


def test_a_source_lands_under_its_kind_and_updates_the_root_index_and_log(
    root_vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """The source writer and the navigation writers addressed the wrapper's index and log, so a root vault grew a second pair."""
    saved = commands.op_capture_source(
        root_vault,
        source_schema,
        title="Pump maintenance handbook",
        content="Replace the seal every year.",
        source_kind="article",
        url="https://example.com/pump-handbook",
    )["source"]

    assert saved["path"].startswith("Sources/Articles/")
    assert (root_vault / saved["path"]).is_file()
    stem = Path(saved["path"]).stem
    for navigation in ("Sources/index.md", "index.md", "log.md"):
        assert stem in (root_vault / navigation).read_text(encoding="utf-8"), navigation
    _assert_no_second_tree(root_vault)


def test_evidence_lands_under_its_scope_and_category(root_vault: Path) -> None:
    """The evidence writer checked for the wrapper folder before it created the scope folder."""
    with library_scope():
        saved = commands.op_preserve_evidence(
            root_vault,
            scope="Pump case",
            category="Readings",
            filename="pressure.csv",
            content="minute,kpa\n0,310\n",
        )

    assert saved["path"] == "Evidence/Pump case/Readings/pressure.csv"
    assert (root_vault / saved["path"]).read_text(encoding="utf-8") == "minute,kpa\n0,310\n"
    _assert_no_second_tree(root_vault)


def test_an_entity_lands_under_its_type_folder(root_vault: Path) -> None:
    """The entity writer joined the wrapper folder in front of the entity area."""
    with library_scope():
        created = commands.op_connect_memory(
            root_vault,
            operation="create-entity",
            entity_type="person",
            name="Mira Okafor",
            summary="Maintains the pump fleet.",
            why_in_kb="She owns the maintenance runbook.",
        )

    path = created["path"]
    assert path.startswith("Entities/People/")
    assert (root_vault / path).is_file()
    _assert_no_second_tree(root_vault)


def test_edit_replace_move_delete_and_recover_keep_a_root_vault_flat(root_vault: Path) -> None:
    """Each writer that rewrites a page resolved its path through the wrapper, so the page or its trash left the root."""
    created = _remember(root_vault, title="Why churn rose", note_type="insight")
    path = created["path"]

    with library_scope():
        commands.op_edit(
            root_vault, path=path, why="Add the cause.", old_string="restart order", new_string="restart order and cause"
        )
        assert "restart order and cause" in (root_vault / path).read_text(encoding="utf-8")

        replaced = commands.op_replace(
            root_vault,
            path,
            content=UNIT,
            note_type="insight",
            title="Why churn rose again",
            reason="The cause changed.",
            relation_disposition="reviewed_none",
            relation_review_reason="Nothing in this vault relates to the churn cause.",
        )
        assert replaced["new_path"].startswith("Insights/")
        assert (root_vault / replaced["new_path"]).is_file()

        moved = commands.op_move_file(root_vault, replaced["new_path"], "Insights/churn-causes.md")
        assert moved["new_path"] == "Insights/churn-causes.md"
        assert (root_vault / "Insights/churn-causes.md").is_file()

        trashed = commands.op_delete(
            root_vault, path="Insights/churn-causes.md", confirm=True, force_orphan=True
        )
        assert trashed["trash_path"].startswith("_trash/")
        assert (root_vault / trashed["trash_path"]).is_file()
        assert not (root_vault / "Insights/churn-causes.md").exists()

        recovered = commands.op_recover_from_trash(root_vault, trash_path=trashed["trash_path"])
        assert recovered["restored_path"] == "Insights/churn-causes.md"
        assert (root_vault / "Insights/churn-causes.md").is_file()

    _assert_no_second_tree(root_vault)
