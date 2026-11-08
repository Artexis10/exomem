"""Which files store mode owns: one rule that migration, rollback, the sweep and the projector share.

Each test names the realistic failure that only it catches. They use real files in a temporary vault.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem.collection_store import replica
from exomem.vault_store.ownership import (
    OwnedFile,
    Ownership,
    Skipped,
    SkipReason,
    classify,
    iter_owned,
)

KB = "Knowledge Base"


def _write(vault: Path, rel: str, data: bytes = b"x") -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _walk(vault: Path) -> tuple[list[OwnedFile], list[Skipped]]:
    skipped: list[Skipped] = []
    return list(iter_owned(vault, on_skip=skipped.append)), skipped


# --- classify: one case per rule branch ---------------------------------------------------------


@pytest.mark.parametrize("rel", ["README.md", "Daily/2026-11-08.md", "Knowledge Base Archive/a.md"])
def test_a_path_outside_the_knowledge_base_folder_is_never_owned(rel):
    """Defect: store mode takes vault-root files and notes beside the Knowledge Base, which stay read-only inputs.

    The sibling folder whose name only starts with the Knowledge Base name catches a prefix check without the slash.
    """
    assert classify(rel) is Ownership.NONE


def test_the_knowledge_base_folder_is_the_configured_one(monkeypatch):
    """Defect: the rule names the default folder, so a vault with its own folder name owns nothing, or the wrong tree."""
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Notebook")
    assert classify("Notebook/idea.md") is Ownership.PAGE
    assert classify(f"{KB}/idea.md") is Ownership.NONE


@pytest.mark.parametrize(
    "rel",
    [
        f"{KB}/.obsidian/workspace.json",
        f"{KB}/Notes/.trash/gone.md",
        f"{KB}/Notes/.hidden.md",
        f"{KB}/_Schema/.cache/registry.yaml",
    ],
)
def test_a_dot_named_component_below_the_knowledge_base_makes_the_path_not_owned(rel):
    """Defect: migration stores editor and trash state that the sweep never walks, so the two disagree on what exists.

    The Markdown and schema-overlay shapes show the dot rule outranks the rules that would make them pages.
    """
    assert classify(rel) is Ownership.NONE


@pytest.mark.parametrize("rel", [f"{KB}/Records/Work/_collection.md", f"{KB}/Planning/Goals/view.md"])
def test_a_records_or_planning_view_belongs_to_the_collection_store(rel):
    """Defect: both stores claim a collection view, so one overwrites the other's projection."""
    assert classify(rel) is Ownership.NONE


def test_the_collection_replica_belongs_to_the_collection_store(tmp_path):
    """Defect: the store takes its own published replica as a binary and projects a stale copy over it."""
    rel = replica.replica_path(tmp_path).relative_to(tmp_path).as_posix()
    assert classify(rel) is Ownership.NONE


def test_governance_state_belongs_to_its_own_owner():
    """Defect: store mode takes governance files, which a separate owner governs and may refuse to let move."""
    assert classify(f"{KB}/_governance/policy.md") is Ownership.NONE


def test_compiled_history_is_a_page_although_it_is_a_reserved_family():
    """Defect: compiled history stays on disk only, so a store-mode write to it never reaches the store."""
    assert classify(f"{KB}/_compiled/2026/history.md") is Ownership.PAGE


def test_the_schema_overlay_is_pages_whatever_the_extension():
    """Defect: the overlay's YAML registries count as binaries, so readers of the store never see an edit to one."""
    assert classify(f"{KB}/_Schema/registry.yaml") is Ownership.PAGE
    assert classify(f"{KB}/_Schema/contracts/note.json") is Ownership.PAGE


@pytest.mark.parametrize("rel", [f"{KB}/Notes/idea.md", f"{KB}/Notes/SHOUTED.MD", f"{KB}/log.md"])
def test_a_markdown_file_is_a_page_in_any_letter_case(rel):
    """Defect: an upper-case extension makes a note a binary, so the store keeps no bytes for it."""
    assert classify(rel) is Ownership.PAGE


@pytest.mark.parametrize("rel", [f"{KB}/Evidence/scan.pdf", f"{KB}/_attachments/photo.png", f"{KB}/Notes/README"])
def test_every_other_file_is_a_binary(rel):
    """Defect: the store loads a PDF as a page, so its bytes enter the database and its hash covers the wrong rows."""
    assert classify(rel) is Ownership.BINARY


# --- iter_owned: the walk agrees with the rule -------------------------------------------------


def test_the_walk_yields_exactly_the_files_that_the_rule_owns(tmp_path):
    """Defect: migration and the sweep disagree about which files exist, so a file looks created or deleted.

    The tree holds the shapes that disagreed in the spike: a dot-directory in the Knowledge Base, files at
    the vault root, the collection replica, a Records view, an Evidence PDF and the schema overlay.
    """
    _write(tmp_path, "README.md")
    _write(tmp_path, "Daily/outside.md")
    _write(tmp_path, f"{KB}/index.md", b"# index\n")
    _write(tmp_path, f"{KB}/Notes/idea.md", b"# idea\n")
    _write(tmp_path, f"{KB}/Notes/.trash/gone.md")
    _write(tmp_path, f"{KB}/.obsidian/workspace.json")
    _write(tmp_path, f"{KB}/Evidence/scan.pdf", b"%PDF-1.7 body")
    _write(tmp_path, f"{KB}/_Schema/registry.yaml", b"types: []\n")
    _write(tmp_path, f"{KB}/_compiled/history.md")
    _write(tmp_path, f"{KB}/_governance/policy.md")
    _write(tmp_path, f"{KB}/Records/Work/_collection.md")
    replica_path = replica.replica_path(tmp_path)
    _write(tmp_path, replica_path.relative_to(tmp_path).as_posix())

    owned, skipped = _walk(tmp_path)

    assert {entry.rel: entry.ownership for entry in owned} == {
        f"{KB}/Evidence/scan.pdf": Ownership.BINARY,
        f"{KB}/Notes/idea.md": Ownership.PAGE,
        f"{KB}/_Schema/registry.yaml": Ownership.PAGE,
        f"{KB}/_compiled/history.md": Ownership.PAGE,
        f"{KB}/index.md": Ownership.PAGE,
    }
    assert all(classify(entry.rel) is entry.ownership for entry in owned)
    assert {entry.rel: entry.stat.st_size for entry in owned}[f"{KB}/Evidence/scan.pdf"] == len(b"%PDF-1.7 body")
    assert skipped == []


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs a privilege on Windows")
def test_a_symlinked_page_is_left_out_and_reported(tmp_path):
    """Defect: migration stores a symlink's target under the link's path, so the projection later writes a copy.

    A symlink in a Records folder would never have been owned, so reporting it would only block a migration.
    """
    target = _write(tmp_path, f"{KB}/Notes/idea.md")
    (target.parent / "alias.md").symlink_to(target.name)
    (tmp_path / KB / "Evidence").symlink_to(target.parent, target_is_directory=True)
    (tmp_path / KB / "Records").mkdir()
    (tmp_path / KB / "Records" / "alias.md").symlink_to(target)

    owned, skipped = _walk(tmp_path)

    assert [entry.rel for entry in owned] == [f"{KB}/Notes/idea.md"]
    assert skipped == [
        Skipped(f"{KB}/Evidence", SkipReason.SYMLINK),
        Skipped(f"{KB}/Notes/alias.md", SkipReason.SYMLINK),
    ]


def test_a_hard_linked_file_is_left_out_and_reported(tmp_path):
    """Defect: two paths share one inode, so an outside edit to one looks like an edit to both."""
    first = _write(tmp_path, f"{KB}/Notes/first.md")
    os.link(first, first.with_name("second.md"))
    _write(tmp_path, f"{KB}/Notes/single.md")

    owned, skipped = _walk(tmp_path)

    assert [entry.rel for entry in owned] == [f"{KB}/Notes/single.md"]
    assert skipped == [
        Skipped(f"{KB}/Notes/first.md", SkipReason.HARD_LINKED),
        Skipped(f"{KB}/Notes/second.md", SkipReason.HARD_LINKED),
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO contract")
def test_a_named_pipe_is_left_out_without_being_opened(tmp_path):
    """Defect: the walk opens a named pipe in the Knowledge Base, so a migration blocks forever on it."""
    (tmp_path / KB).mkdir()
    os.mkfifo(tmp_path / KB / "feed.md")

    owned, skipped = _walk(tmp_path)

    assert owned == []
    assert skipped == [Skipped(f"{KB}/feed.md", SkipReason.NOT_REGULAR)]


def test_a_vault_without_a_knowledge_base_folder_reports_the_unread_folder(tmp_path):
    """Defect: an empty walk reads as an empty vault, so a migration projects over files it never saw."""
    _write(tmp_path, "README.md")

    owned, skipped = _walk(tmp_path)

    assert owned == []
    assert skipped == [Skipped(KB, SkipReason.UNREADABLE)]
