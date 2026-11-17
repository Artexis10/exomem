"""`exomem init` creates a `root` vault, with a layout record and the registry's areas at the vault root.

The CLI runs in a subprocess for the product default, because the suite pins fixture vaults to
`nested`; the rest call `init_vault` with the layout they test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from exomem import cell_init, commands, init, layout, note_types
from exomem.__main__ import _init_main
from exomem.governance.principal import library_scope
from exomem.layout import Layout, LayoutError
from exomem.vault import WikilinkResolver

KB = "Knowledge Base"
UNIT = "## Observations\n\n- [finding] The runbook lists the restart order.\n"


def _record(vault: Path) -> dict:
    return yaml.safe_load((vault / "_Schema" / "vault-layout.yaml").read_text(encoding="utf-8"))


def _exomem(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "exomem", *args],
        capture_output=True,
        text=True,
        timeout=150,
        env={**os.environ, "EXOMEM_DISABLE_EMBEDDINGS": "1"},
        check=False,
    )


def test_the_cli_gives_a_root_vault_by_default_and_its_notes_share_one_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new vault has no wrapper folder, status reports `root`, and titles that differ only in case number across areas."""
    vault = tmp_path / "fresh"

    made = _exomem("init", "--vault", str(vault))
    assert made.returncode == 0, made.stderr
    assert _record(vault) == {"layout": "root", "unmanaged": []}
    assert not (vault / KB).exists()
    assert (vault / "Insights").is_dir() and (vault / "Entities" / "Concepts").is_dir()
    status = _exomem("status", "--json", "--vault", str(vault))
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["layout"]["layout"] == "root"

    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    layout.bind(vault)
    with library_scope():
        pages = [
            commands.op_remember(vault, content=UNIT, status="draft", title=title, note_type=kind)["path"]
            for title, kind in (("Churn cause", "procedure"), ("churn cause", "insight"), ("Churn cause", "insight"))
        ]
    assert pages == ["Procedures/Churn cause.md", "Insights/churn cause (2).md", "Insights/Churn cause (3).md"]
    resolver = WikilinkResolver(vault)
    for page in pages:
        found = resolver.obsidian_match(Path(page).stem)
        assert found is not None and found.path == page
    assert not (vault / KB).exists()


def test_layout_nested_keeps_the_wrapper_shape(tmp_path: Path) -> None:
    vault = tmp_path / "wrapped"

    assert _init_main(["--vault", str(vault), "--layout", "nested"]) == 0

    assert (vault / KB / "index.md").is_file() and (vault / KB / "Notes" / "Insights").is_dir()
    assert not (vault / "_Schema").exists()
    assert layout.of(vault) is Layout.NESTED


def test_the_folders_init_creates_are_the_registrys_areas(tmp_path: Path) -> None:
    """`init` kept its own list once, and the area added to the pack after it was never created."""
    vault = tmp_path / "areas"

    init.init_vault(vault, initialize_state=False, vault_layout="root")

    top = {path.name for path in vault.iterdir() if path.is_dir() and not path.name.startswith(".")}
    compiled = layout.load_registry(vault).areas
    held_apart = {
        note_types.folder_below_notes(note_type.folder)[0]
        for note_type in note_types.shipped()
        if note_type.folder is not None and not note_types.compiled(note_type)
    }
    shipped_folders = {path.name for path in init._SCAFFOLD.iterdir() if path.is_dir()} - {note_types.NOTES_FOLDER}
    assert top == compiled | held_apart | shipped_folders


def test_entries_the_folder_already_holds_are_listed_as_unmanaged(tmp_path: Path) -> None:
    vault = tmp_path / "obsidian"
    for name in ("Daily", ".obsidian", "_Imports"):
        (vault / name).mkdir(parents=True)
    (vault / "todo.txt").write_text("buy milk\n", encoding="utf-8")

    init.init_vault(vault, initialize_state=False, vault_layout="root")

    assert _record(vault) == {"layout": "root", "unmanaged": ["Daily", "todo.txt"]}


def test_an_entry_that_carries_a_name_init_creates_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """A folder `insights` would be hidden as unmanaged or overwritten by the scaffold; both lose the user's files."""
    vault = tmp_path / "clash"
    (vault / "insights").mkdir(parents=True)
    (vault / "index.md").write_text("my index\n", encoding="utf-8")

    with pytest.raises(LayoutError) as refused:
        init.init_vault(vault, initialize_state=False, vault_layout="root")

    assert refused.value.code == "INIT_NAME_TAKEN"
    assert "insights" in refused.value.message and "index.md" in refused.value.message
    assert sorted(path.name for path in vault.iterdir()) == ["index.md", "insights"]


def test_init_never_converts_a_vault(tmp_path: Path) -> None:
    for existing, other in (("nested", "root"), ("root", "nested")):
        vault = tmp_path / existing
        init.init_vault(vault, initialize_state=False, vault_layout=existing)

        with pytest.raises(LayoutError) as conflict:
            init.init_vault(vault, initialize_state=False, vault_layout=other)
        with pytest.raises(FileExistsError):
            init.init_vault(vault, initialize_state=False)

        assert conflict.value.code == "LAYOUT_CONFLICT"
        assert layout.of(vault).value == existing
        assert (vault / KB).exists() is (existing == "nested")


def test_a_schema_folder_at_the_root_without_a_record_is_a_vault_whose_layout_init_cannot_read(tmp_path: Path) -> None:
    vault = tmp_path / "unrecorded"
    (vault / "_Schema").mkdir(parents=True)

    with pytest.raises(LayoutError) as refused:
        init.init_vault(vault, initialize_state=False)

    assert refused.value.code == "LAYOUT_RECORD_MISSING"
    assert not (vault / "_Schema" / "vault-layout.yaml").exists()


def test_a_forced_overlay_of_a_root_vault_keeps_root_and_creates_a_vault_defined_area(tmp_path: Path) -> None:
    """The areas of an overlay type are folders at the vault root after an overlay, not only the shipped ones."""
    vault = tmp_path / "overlay"
    init.init_vault(vault, vault_layout="root")
    with library_scope():
        inspected = commands.op_schema_memory(vault, subject="note-types", operation="inspect")
        row = {"label": "Playbook", "description": "A playbook.", "attributes": {"role": "compiled", "folder": "Playbooks"}}
        commands.op_schema_memory(
            vault,
            subject="note-types",
            operation="save",
            proposal={"upsert": {"playbook": row}},
            expected_hash=inspected["content_hash"],
            why="the owner keeps these notes in their own area",
        )

    report = init.init_vault(vault, force=True, vault_layout="root")

    assert report["layout"] == "root" and (vault / "Playbooks").is_dir()
    assert not (vault / KB).exists()


def test_a_cell_builds_a_root_vault_in_staging_and_recognises_it_on_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cell builds the vault in a sibling folder and renames it; the next run must not build it again."""
    monkeypatch.setattr(init, "NEW_VAULT_LAYOUT", Layout.ROOT)
    vault = tmp_path / "volume" / "vault"

    first = cell_init.run_cell_init(vault)
    second = cell_init.run_cell_init(vault)

    assert (first.vault_created, second.vault_created) == (True, False)
    assert _record(vault) == {"layout": "root", "unmanaged": []}
    assert not (vault / KB).exists()
