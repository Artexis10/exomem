"""Navigation previews retain their counts and log bytes when projections disappear."""

from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_graph_readers import record_store_reads, write_pages
from test_vault_store_lifecycle_writers import store_folder
from test_vault_store_migration import build_indexes, migrate_apply, vault  # noqa: F401
from test_vault_store_readers import damage_every_owned_projection

from exomem import (
    commands,
    delete_directory,
    delete_file,
    indexes,
    move_file,
    recover_from_trash,
    writer_lease,
)
from exomem import vault as vaults
from exomem.vault_store import read

pytestmark = pytest.mark.timeout(900)
stored, writer, served = read_write.stored, read_write.writer, write_path.served


@pytest.mark.parametrize("damage", ["deleted", "poisoned"])
def test_navigation_preview_keeps_nested_counts_and_log_bytes(vault, monkeypatch, damage):  # noqa: F811
    """Lost index/log guards and nested-folder counts change the bytes a writer previews."""
    prefix = "Knowledge Base/Notes/"
    write_pages(vault, {
        prefix + rel: "---\ntype: insight\n---\n## Claim\n\nKeep the lantern lit.\n"
        for rel in (
            "Research/Navigation/one.md", "Research/Navigation/_nested/two.md",
            "Research/Navigation/section.md/inside.md",
            "Research/Navigation/.loose/outside.md",
            "Research/_ignored/three.md", "Research/top.md",
            "Insights/one.md", "Insights/_nested/two.md",
        )
    })
    (vault / prefix / "Research/Empty").mkdir(parents=True)
    (vault / prefix / "Empty").mkdir()
    write_pages(vault, {
        prefix + "index.md": "# Notes\n\n## By type\n\n### Research (0)\n\n"
        "- [[Knowledge Base/Notes/Research/Navigation/|Navigation]] (0)\n\n### Insights (0)\n",
        "Knowledge Base/Entities/index.md": "# Entities\n\n## By type\n",
    })
    build_indexes(vault)
    counts = indexes._count_notes_by_subfolder(vault / prefix)
    assert counts["Research"] == {"Navigation": 5, "Empty": 0, "": 1}
    assert counts["Insights"] == {"": 2}
    assert counts["Empty"] == {"": 0}

    def preview():
        top, _guard = vaults.read_guarded_text(vault, vault / "Knowledge Base/index.md")
        writes, counted_top = indexes.compute_subindex_writes(
            vault, top_index_text=top, include_unchanged=True,
            pending_paths=[prefix + "Research/Navigation/new.md"],
        )
        log = vaults.plan_log_entries(vault, [
            vaults.LogEntry("2026-01-01", "note", prefix + "Research/Navigation/new", "New finding.", "preview"),
        ], operation_token="preview")
        return counted_top, {
            write.path.relative_to(vault).as_posix(): write.content
            for write in (*writes, *log.writes)
        }, log.warning

    expected = preview()
    assert "Knowledge Base/Notes/index.md" in expected[1]
    assert "Knowledge Base/log.md" in expected[1]
    assert migrate_apply(vault) == 0
    with read.snapshot(vault) as view:
        assert view.page(prefix + "Research/Navigation/.loose/outside.md") is None
    damage_every_owned_projection(vault, damage)

    with record_store_reads(vault, monkeypatch) as events, read.request_snapshot(vault):
        assert indexes._count_notes_by_subfolder(vault / prefix) == counts
        actual = preview()

    assert actual == expected
    assert events["page_io"] == []
    assert events["sidecars"] == []
    assert events["machinery"] == []
    assert events["snapshots"] == [1]


def test_index_counts_follow_a_store_only_create_edit_move_and_delete(served):
    """A projected or previously counted page must never keep a stale count after a write."""
    notes = served / "Knowledge Base/Notes"
    initial = indexes._count_notes_by_subfolder(notes)
    created = commands.op_remember(
        served,
        title="A count that follows its page",
        content="## Observations\n\n- [finding] The lamp is lit.\n",
        summary="The lamp is lit.",
        relation_disposition="reviewed_none",
        relation_review_reason="The note stands on its own.",
    )["path"]
    original_parent = PurePosixPath(created).parent.name
    assert not (served / created).exists()
    after_create = indexes._count_notes_by_subfolder(notes)
    assert after_create == initial | {original_parent: {"": 1}}

    commands.op_edit_memory(
        served,
        path=created,
        why="Record the corrected reading.",
        operation={
            "kind": "replace_string",
            "old_string": "lit",
            "new_string": "dim",
            "relation_disposition": "reviewed_none",
            "relation_review_reason": "The correction leaves the note self-contained.",
        },
    )
    assert indexes._count_notes_by_subfolder(notes) == after_create

    new_path = f"Knowledge Base/Notes/Navigation/Dock/{PurePosixPath(created).name}"
    move_file.move_file(served, old_path=created, new_path=new_path)
    assert indexes._count_notes_by_subfolder(notes) == initial | {"Navigation": {"Dock": 1}}

    delete_file.delete_file(served, path=new_path, confirm=True)
    assert indexes._count_notes_by_subfolder(notes) == initial


def test_index_counts_drop_a_trashed_folder_and_return_after_recovery(served):
    """A folder still in the store's trash must count only after its recovery."""
    notes = served / "Knowledge Base/Notes"
    initial = indexes._count_notes_by_subfolder(notes)
    folder = "Knowledge Base/Notes/Navigation/Dock"
    store_folder(served, 2, folder=folder)
    assert indexes._count_notes_by_subfolder(notes) == initial | {"Navigation": {"Dock": 2}}

    trashed = delete_directory.delete_directory(served, path=folder, confirm=True, recursive=True)
    assert indexes._count_notes_by_subfolder(notes) == initial

    recover_from_trash.recover_from_trash(served, trash_path=trashed.trash_path)
    assert indexes._count_notes_by_subfolder(notes) == initial | {"Navigation": {"Dock": 2}}


def test_a_source_capture_plans_its_index_and_log_writes_from_pages_with_no_projection(vault):  # noqa: F811
    """`add` plans against the indexes and the log; a page that the store holds and no file does is no refusal."""
    assert migrate_apply(vault) == 0
    damage_every_owned_projection(vault, "deleted")

    update = indexes.compute_updates(
        vault,
        source_type="articles",
        folder_title="Articles",
        folder_description="captured web/PDF content",
        rel_source_path="Knowledge Base/Sources/Articles/2026-01-01-tide",
        date_iso="2026-01-01",
        activity_summary="Captured a tide table.",
        log_entry_body="Filed the tide table.",
        source_counts={"Articles": 1},
    )

    assert "2026-01-01-tide" in update.sources_index_content
    assert "Captured a tide table." in update.top_index_content
    assert "Filed the tide table." in update.log_content


def test_store_status_reports_an_unreadable_revision_as_unknown(vault, monkeypatch):  # noqa: F811
    """A failed diagnostic must not report a zero graph generation or a zero store revision."""
    assert migrate_apply(vault) == 0

    def unavailable(_root):
        raise sqlite3.OperationalError("store reader temporarily unavailable")

    monkeypatch.setattr(read, "snapshot", unavailable)
    status = writer_lease.coordination_status(vault)

    assert status["graph_sync"] == {"state": "unknown", "generation": None}
    assert status["vault_store"] == {"state": "unavailable", "revision": None}
    assert status["coordinator_healthy"] is True
