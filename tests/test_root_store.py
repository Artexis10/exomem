"""A bound `root` vault in store mode is written and counted from the store, with no second tree.

The writer journey of `test_root_writers` runs here against a vault whose pages live in the store.
Every write lands at its root area, the store rows carry root paths, and the index counts come
from the store rather than from the folders on disk.
"""

from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path

import pytest

from exomem import commands, state_migration
from exomem import schema as schema_module
from exomem.collection_store import connection, preview
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope
from exomem.vault_store import migrate, ownership, rederive, write
from exomem.vault_store import read as store_read
from tests.conftest import FIXTURE_VAULT, bind_root_vault, initialize_vault_state_offline

KB = "Knowledge Base"
NOTES = "Notes"
AT = "2026-11-08T00:00:00Z"
UNIT = "## Observations\n\n- [finding] The runbook lists the restart order.\n"
LEASE_ENV = ("EXOMEM_WRITER_LEASE_URL", "EXOMEM_WRITER_LEASE_TOPOLOGY", "EXOMEM_CLOUD_CELL")


@pytest.fixture
def stored_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The root-vault fixture of `test_root_writers` with its pages in the store, served in store mode.

    The store is activated directly, without a coordinator, and the writer is bound as the service binds it.
    """
    for name in LEASE_ENV:
        monkeypatch.delenv(name, raising=False)
    dest = tmp_path / "rootvault"
    shutil.copytree(FIXTURE_VAULT / KB, dest, ignore=shutil.ignore_patterns(NOTES))
    shutil.move(dest / "_Schema", dest / ".exomem" / "schema")
    bind_root_vault(dest, monkeypatch)
    initialize_vault_state_offline(dest, source="root store fixture")
    with connection.open_writer(connection.store_path(dest), lease_check=lambda: True) as handle:
        with handle.transaction() as tx:
            migrate.create_owner_schema(tx)
            tx.execute("INSERT INTO store_mode(id, mode, revision, changed_at, release) "
                       "VALUES (1, 'store', 1, ?, 'test')", (AT,))
            rederive.record_derived_from_files(tx, 1)
        pages = tuple(
            write.PagePut(owned.rel, (dest / owned.rel).read_bytes(), owned.stat.st_mtime_ns)
            for owned in ownership.iter_owned(dest)
            if owned.ownership is ownership.Ownership.PAGE
        )
        write.commit(
            CollectionWriter(dest, handle),
            write.StoreChange(write.Origin.MIGRATION, "test fixture", puts=pages),
        )
        state_migration.record_vault_store_compatibility(dest, authority_check=lambda: True, active=True)
        token = preview._BOUND.set((dest.resolve(), CollectionWriter(dest, handle), False))
        try:
            yield dest
        finally:
            preview._BOUND.reset(token)


def _store_paths(root: Path) -> list[str]:
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return [row[0] for row in reader.execute("SELECT path FROM store_pages ORDER BY path")]


def test_writer_journey_in_store_mode_lands_at_the_root_areas(stored_root: Path) -> None:
    root = stored_root
    schema = schema_module.load_source_schema(root)
    with library_scope():
        created = commands.op_remember(
            root, content=UNIT, status="draft", title="Why churn rose", note_type="insight"
        )
        assert created["path"] == "Insights/why-churn-rose.md"
        saved = commands.op_capture_source(
            root, schema, title="Pump maintenance handbook", content="Replace the seal every year.",
            source_kind="article", url="https://example.com/pump-handbook",
        )["source"]
        assert saved["path"].startswith("Sources/Articles/")
        evidence = commands.op_preserve_evidence(
            root, scope="Pump case", category="Readings", filename="pressure.csv",
            content="minute,kpa\n0,310\n",
        )
        assert evidence["path"] == "Evidence/Pump case/Readings/pressure.csv"
        entity = commands.op_connect_memory(
            root, operation="create-entity", entity_type="person", name="Mira Okafor",
            summary="Maintains the pump fleet.", why_in_kb="She owns the maintenance runbook.",
        )
        assert entity["path"].startswith("Entities/People/")
        commands.op_edit(
            root, path=created["path"], why="Add the cause.",
            old_string="restart order", new_string="restart order and cause",
        )
        replaced = commands.op_replace(
            root, created["path"], content=UNIT, note_type="insight", title="Why churn rose again",
            reason="The cause changed.", relation_disposition="reviewed_none",
            relation_review_reason="Nothing in this vault relates to the churn cause.",
        )
        commands.op_move_file(root, replaced["new_path"], "Insights/churn-causes.md")
        trashed = commands.op_delete(
            root, path="Insights/churn-causes.md", confirm=True, force_orphan=True
        )
        assert trashed["trash_path"].startswith("_trash/")
        recovered = commands.op_recover_from_trash(root, trash_path=trashed["trash_path"])
        assert recovered["restored_path"] == "Insights/churn-causes.md"

    stored = _store_paths(root)
    for expected in (
        "Insights/churn-causes.md",
        "Insights/why-churn-rose.md",
        f"{saved['path']}",
        entity["path"],
        "Sources/index.md",
    ):
        assert expected in stored
    assert [path for path in stored if path.split("/")[0] in {KB, NOTES}] == []
    assert not (root / KB).exists()
    assert not (root / NOTES).exists()


def test_index_counts_come_from_the_store_in_a_root_vault(stored_root: Path) -> None:
    """Pages written in store mode are not on disk, so a count that walks the folders misses the first article.

    The second capture is the proof: only the store holds the first.
    """
    schema = schema_module.load_source_schema(stored_root)
    with library_scope():
        for title, url in (
            ("Pump maintenance handbook", "https://example.com/pump-handbook"),
            ("Valve inspection guide", "https://example.com/valve-guide"),
        ):
            commands.op_capture_source(
                stored_root, schema, title=title, content="Replace the seal every year.",
                source_kind="article", url=url,
            )
    with store_read.snapshot(stored_root) as view:
        sources_index = view.page("Sources/index.md").body.decode()
        top_index = view.page("index.md").body.decode()
    assert "|Articles]] — captured web/PDF content (4)" in sources_index
    assert "- Sources: 6 (articles: 4, books: 1, sessions: 1)" in top_index
