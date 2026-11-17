"""A `root` vault's dot-named entries stay outside what Exomem writes.

In a `nested` vault a path spelled `.exomem/schema/SKILL.md` names a file under the wrapper folder, so no
governed writer can reach the shipped contract or an editor's settings that sit beside the wrapper. In a
`root` vault the same spelling names that very file. The managed root's containment check must refuse it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from exomem import activation_conventions as ac
from exomem import commands, kbdir, reserved_paths
from exomem.governance.principal import library_scope
from tests.conftest import FIXTURE_VAULT, bind_root_vault, initialize_vault_state_offline

KB = "Knowledge Base"
NOTES = "Notes"
CONTRACT = ".exomem/schema/SKILL.md"
EDITOR_PAGE = ".obsidian/workspace.md"


@pytest.fixture
def root_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dest = tmp_path / "rootvault"
    shutil.copytree(FIXTURE_VAULT / KB, dest, ignore=shutil.ignore_patterns(NOTES))
    shutil.move(dest / "_Schema", dest / ".exomem" / "schema")
    (dest / ".obsidian").mkdir()
    (dest / EDITOR_PAGE).write_text("---\ntitle: Workspace\n---\nlayout notes\n", encoding="utf-8")
    bind_root_vault(dest, monkeypatch)
    initialize_vault_state_offline(dest, source="root guards fixture")
    return dest


def _bytes(vault: Path, path: str) -> bytes:
    return (vault / path).read_bytes()


_REFUSED_WRITES = {
    "append to the contract": lambda vault: commands.op_append_to_file(
        vault, path=CONTRACT, content="tail\n"
    ),
    "create beside the contract": lambda vault: commands.op_create_file(
        vault, path=".exomem/schema/extra.md", content="x"
    ),
    "create in the editor folder": lambda vault: commands.op_create_file(
        vault, path=".obsidian/new.md", content="x"
    ),
    "edit an editor page": lambda vault: commands.op_edit(
        vault, path=EDITOR_PAGE, why="tidy", old_string="layout notes", new_string="x"
    ),
    "replace an editor page": lambda vault: commands.op_replace(
        vault, old_path=EDITOR_PAGE, content="x\n", note_type="insight", title="Workspace"
    ),
}


@pytest.mark.parametrize("write", _REFUSED_WRITES.values(), ids=_REFUSED_WRITES.keys())
def test_no_writer_reaches_the_contract_or_an_editor_page(root_vault: Path, write) -> None:
    """Each writer resolved a dot-named top-level path inside the root's own tree and wrote it."""
    contract, editor = _bytes(root_vault, CONTRACT), _bytes(root_vault, EDITOR_PAGE)
    before = sorted(p.relative_to(root_vault).as_posix() for p in root_vault.rglob("*"))

    with library_scope(), pytest.raises(ValueError, match="INVALID_PATH"):
        write(root_vault)

    assert _bytes(root_vault, CONTRACT) == contract
    assert _bytes(root_vault, EDITOR_PAGE) == editor
    assert sorted(p.relative_to(root_vault).as_posix() for p in root_vault.rglob("*")) == before


def test_a_governed_write_beside_the_dot_names_still_lands(root_vault: Path) -> None:
    with library_scope():
        created = commands.op_create_file(root_vault, path="Scratch/plain.md", content="plain\n")
        commands.op_append_to_file(root_vault, path="Scratch/plain.md", content="more\n")

    assert created["path"] == "Scratch/plain.md"
    assert _bytes(root_vault, "Scratch/plain.md").endswith(b"more\n")


def test_a_nested_vault_still_writes_a_dot_named_spelling_under_the_wrapper(
    tmp_path: Path,
) -> None:
    """The refusal belongs to `root`: nested keeps today's behaviour."""
    vault = tmp_path / "nested"
    shutil.copytree(FIXTURE_VAULT, vault)
    initialize_vault_state_offline(vault, source="nested guards fixture")

    with library_scope():
        created = commands.op_create_file(vault, path=".exomem/note.md", content="x\n")

    assert created["path"] == f"{KB}/.exomem/note.md"
    assert not (vault / ".exomem" / "note.md").exists()


def test_a_wrapper_prefixed_folder_rule_is_rejected_in_both_layouts(
    root_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Folder rules are managed-root-relative, so a rule that leads with the wrapper's name can match no page.

    The check asked the bound layout whether the segment names the managed root. A `root` vault has no
    such name, so a rule copied from a `nested` vault passed and sat dead.
    """
    proposal = {"schema_version": 1, "anchors": {"resource": {"add_folders": [f"{KB}/Products"]}}}

    in_root = ac.load_conventions(proposal=proposal)
    assert any(finding["code"] == "rule_kb_prefixed" for finding in in_root.findings)
    assert f"{KB}/Products" not in in_root.conventions.anchors["resource"].folders

    kbdir.unbind()
    in_nested = ac.load_conventions(proposal=proposal)
    assert any(finding["code"] == "rule_kb_prefixed" for finding in in_nested.findings)


def test_a_scan_of_the_managed_files_prunes_a_root_vaults_outside_entries(root_vault: Path) -> None:
    """The governance manifests hashed every file under the vault root, a repository's objects and the contract among them."""
    (root_vault / ".git" / "objects").mkdir(parents=True)
    (root_vault / ".git" / "objects" / "blob").write_bytes(b"x")
    (root_vault / "_Imports" / "upload").mkdir(parents=True)
    (root_vault / "_Imports" / "upload" / "draft.md").write_text("draft\n", encoding="utf-8")
    (root_vault / "Insights").mkdir(exist_ok=True)
    (root_vault / "Insights" / "kept.md").write_text("kept\n", encoding="utf-8")

    scanned = {path.relative_to(root_vault).as_posix() for path in kbdir.managed_files(root_vault)}

    assert "Insights/kept.md" in scanned
    assert not [path for path in scanned if path.split("/")[0] in {".git", ".obsidian", ".exomem", "_Imports"}]


def test_a_nested_scan_of_the_managed_files_reads_everything_below_the_wrapper(tmp_path: Path) -> None:
    vault = tmp_path / "nested"
    shutil.copytree(FIXTURE_VAULT, vault)
    (vault / KB / ".obsidian").mkdir()
    (vault / KB / ".obsidian" / "inside.md").write_text("inside\n", encoding="utf-8")
    (vault / "Elsewhere").mkdir()
    (vault / "Elsewhere" / "outside.md").write_text("outside\n", encoding="utf-8")

    scanned = {path.relative_to(vault).as_posix() for path in kbdir.managed_files(vault)}

    assert f"{KB}/.obsidian/inside.md" in scanned
    assert "Elsewhere/outside.md" not in scanned


@pytest.mark.skipif(os.geteuid() == 0, reason="a root user reads a folder that has no permissions")
def test_an_unreadable_editor_folder_does_not_fail_the_identity_catalogue(root_vault: Path) -> None:
    """The catalogue walked the whole vault root, so one folder the service could not list failed every catalogue."""
    locked = root_vault / ".obsidian" / "plugins"
    locked.mkdir()
    locked.chmod(0)
    try:
        catalogue = reserved_paths.IdentityCatalogue.from_vault(root_vault)
    finally:
        locked.chmod(0o700)

    assert catalogue.vault_root == root_vault
