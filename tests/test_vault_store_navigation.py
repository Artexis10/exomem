"""Navigation previews retain their counts and log bytes when projections disappear."""

from __future__ import annotations

import sqlite3
import sys

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_graph_readers import record_store_reads, write_pages
from test_vault_store_migration import build_indexes, migrate_apply, vault  # noqa: F401
from test_vault_store_readers import damage_every_owned_projection

from exomem import indexes, writer_lease
from exomem import vault as vaults
from exomem.vault_store import read

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only"),
    pytest.mark.timeout(900),
]


@pytest.mark.parametrize("damage", ["deleted", "poisoned"])
def test_navigation_preview_keeps_nested_counts_and_log_bytes(vault, monkeypatch, damage):  # noqa: F811
    """Lost index/log guards and nested-folder counts change the bytes a writer previews."""
    prefix = "Knowledge Base/Notes/"
    write_pages(vault, {
        prefix + rel: "---\ntype: insight\n---\n## Claim\n\nKeep the lantern lit.\n"
        for rel in (
            "Research/Navigation/one.md", "Research/Navigation/_nested/two.md",
            "Research/Navigation/section.md/inside.md",
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
    assert counts["Research"] == {"Navigation": 4, "Empty": 0, "": 1}
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
    damage_every_owned_projection(vault, damage)

    with record_store_reads(vault, monkeypatch) as events, read.request_snapshot(vault):
        assert indexes._count_notes_by_subfolder(vault / prefix) == counts
        actual = preview()

    assert actual == expected
    assert events["page_io"] == []
    assert events["sidecars"] == []
    assert events["machinery"] == []
    assert events["snapshots"] == [1]


def test_store_status_reports_an_unreadable_revision_as_unknown(vault, monkeypatch):  # noqa: F811
    """A failed diagnostic must not report a zero graph generation or a zero store revision."""
    assert migrate_apply(vault) == 0

    def unavailable(_root):
        raise sqlite3.OperationalError("store reader temporarily unavailable")

    monkeypatch.setattr(read, "snapshot", unavailable)
    status = writer_lease.coordination_status(vault)

    assert status["graph_sync"] == {"state": "not_applicable", "generation": None}
    assert status["vault_store"] == {"state": "unavailable", "revision": None}
    assert status["coordinator_healthy"] is True
