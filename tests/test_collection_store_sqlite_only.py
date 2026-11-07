"""SQLite-only Records collections (OpenSpec move-structured-collections-to-sqlite C2.1, C2.7).

On a vault with a collection store, a new Records collection declares ``storage.strategy: sqlite``:
its rows live in the store and its folder holds only the manifest and the bounded summary page.
A vault without a store keeps file-canonical create until the cutover enrolls one.
Each case drives the public ``record_memory`` route through the shared dispatcher, as MCP
does, or the owner's offline ``exomem collections create``.
"""

from __future__ import annotations

from contextlib import closing

import pytest
from test_collection_store_s1_gate import _until, coordinator
from test_collection_store_service import (  # noqa: F401 - the service fixture
    _served_vault,
    call,
    offline_create,
    refused,
    service,
)
from test_collection_store_writer import CID, manifest_path, manifest_text

from exomem import state_migration
from exomem.collection_store import capability, connection, runtime

READINGS = "5a5a5a5a-5a5a-4a5a-8a5a-5a5a5a5a5a5a"
PATH = manifest_path().replace("Work", "Readings")
FOLDER = PATH.removesuffix("/_collection.md")
# No view_mode: a sqlite collection is a summary collection by default.
SQLITE = (manifest_text().replace(CID, READINGS).replace("title: Work", "title: Readings")
          .replace("strategy: markdown-items\n  source: Items\n", "strategy: sqlite\n  source: Summary\n"))
ITEMS = manifest_text().replace(CID, READINGS).replace("title: Work", "title: Readings")
SUMMARY_ITEMS = ITEMS.replace("lifecycle: active\n", "lifecycle: active\nview_mode: summary\n")
FIRST = "6b6b6b6b-6b6b-4b6b-8b6b-6b6b6b6b6b6b"
SECOND = "7c7c7c7c-7c7c-4c7c-8c7c-7c7c7c7c7c7c"
# The on-disk descriptor ID an older release reads; it must never change.
FLOOR = "collections-sqlite-only-v1"


def files_under(root, folder):
    base = root / folder
    return sorted(path.relative_to(base).as_posix() for path in base.rglob("*") if path.is_file())


def test_record_memory_create_makes_a_store_summary_collection_with_no_item_files(
        service, monkeypatch):  # noqa: F811 - the imported fixture, by name
    """Defect: create writes an Items directory or a file-canonical collection, or the new sqlite
    collection refuses the append, update and query that a Records collection serves."""
    root = service.root
    monkeypatch.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 0.1)

    call(root, "record_memory", action="create", manifest_path=PATH, manifest_text=SQLITE, why="new readings")
    first = call(root, "record_memory", action="append", collection=READINGS, item={"title": "Morning", "count": 1},
                 item_key=FIRST, why="first reading")
    assert first["outcome"] == "committed"
    updated = call(root, "record_memory", action="update", collection=READINGS, item_key=FIRST,
                   changes={"count": 5}, why="corrected reading",
                   expected_container_hash=first["after_container_hash"],
                   expected_item_version=first["after_item_hash"])
    assert updated["outcome"] == "committed", updated
    assert call(root, "record_memory", action="append", collection=READINGS,
                item={"title": "Evening", "count": 2}, item_key=SECOND, why="second reading")["outcome"] == "committed"

    rows = call(root, "record_memory", action="query", collection=READINGS)["rows"]
    assert sorted((row["title"], row["count"]) for row in rows) == [("Evening", 2), ("Morning", 5)]
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        stored = reader.execute("SELECT view_mode,(SELECT COUNT(*) FROM items i WHERE i.collection_id=c.collection_id) "
                                "FROM collections c WHERE collection_id=?", (READINGS,)).fetchone()
    assert stored == ("summary", 2)
    page = root / FOLDER / "Summary/_summary.md"
    _until(lambda: page.exists() and "2 committed rows" in page.read_text(), 30)
    # The store's views only: the manifest, its history page and the summary page; no file per row.
    _until(lambda: files_under(root, FOLDER) == ["Summary/_summary.md", "_collection.md", "_history.md"], 30)
    assert FLOOR in state_migration.recorded_descriptor_ids(root)


@pytest.mark.parametrize("text", [ITEMS, SUMMARY_ITEMS], ids=["items-mode", "summary-mode"])
@pytest.mark.parametrize("action", ["validate", "create"])
def test_a_new_markdown_items_collection_is_refused_and_writes_nothing(
        service, text, action):  # noqa: F811 - the imported fixture, by name
    """Defect: create or validate still accepts a Markdown-items manifest, so a new collection
    writes one file per row or the agent learns of the refusal only at create."""
    root = service.root
    arguments = {"why": "markdown rows"} if action == "create" else {}
    refused(lambda: call(root, "record_memory", action=action, manifest_path=PATH, manifest_text=text, **arguments),
            "SQLITE_STORAGE_REQUIRED")
    assert not (root / FOLDER).exists()


def test_describe_on_a_vault_with_a_store_teaches_a_sqlite_example_that_creates(
        service):  # noqa: F811 - the imported fixture, by name
    """Defect: describe on a vault with a store teaches a Markdown-items example, which its create refuses."""
    root = service.root
    example = call(root, "record_memory", action="describe")["examples"]["minimal"]
    assert "strategy: sqlite" in example["manifest_text"]
    call(root, "record_memory", action="create", manifest_path=example["manifest_path"],
         manifest_text=example["manifest_text"], why="the described example")
    appended = call(root, "record_memory", action="append", collection=example["manifest_path"],
                    item=example["append_item"], why="the described row")
    assert appended["outcome"] == "committed", appended


def test_a_sqlite_create_on_a_vault_without_a_store_names_the_enrolment_and_writes_nothing(tmp_path, monkeypatch):
    """Defect: a sqlite create on a file-only vault falls back to a file collection, which no
    adapter can read, instead of naming the owner's enrolment step."""
    root = _served_vault(tmp_path, monkeypatch)
    with coordinator(tmp_path / "coordinator.sqlite"):
        refused(lambda: call(root, "record_memory", action="create", manifest_path=PATH, manifest_text=SQLITE,
                             why="no store yet"), "COLLECTION_STORE_ENROLLMENT_REQUIRED")
        monkeypatch.setattr(capability, "RELEASED", frozenset())
        refused(lambda: call(root, "record_memory", action="create", manifest_path=PATH, manifest_text=SQLITE,
                             why="release dark"))
    assert not (root / FOLDER).exists()


def test_the_first_sqlite_create_records_the_floor_that_an_older_release_refuses(tmp_path, monkeypatch, capsys):
    """Defect: a vault holding a sqlite collection stays servable by a release that would write
    item files for it again, because the create records no compatibility floor."""
    root = _served_vault(tmp_path, monkeypatch)
    assert FLOOR not in state_migration.recorded_descriptor_ids(root)
    (tmp_path / "readings.md").write_text(SQLITE)
    with coordinator(tmp_path / "coordinator.sqlite"):
        assert offline_create(root, tmp_path / "readings.md", PATH, why="first sqlite collection") == 0, \
            capsys.readouterr().err
    assert FLOOR in state_migration.recorded_descriptor_ids(root)
    state_migration.require_vault_state_ready(root)

    # A release that knows the floor but cannot serve it names a compatible runtime.
    supported = state_migration.supported_state_compatibility_ids()
    monkeypatch.setattr(state_migration, "supported_state_compatibility_ids",
                        lambda: tuple(entry for entry in supported if entry != FLOOR))
    with pytest.raises(state_migration.StateMigrationOfflineRequired, match="compatible runtime"):
        state_migration.require_vault_state_ready(root)
    # A release from before the floor lacks the ID in its catalog and refuses the state at startup,
    # as an actual older runtime refuses an unknown descriptor (test_connector_boundary_portability).
    monkeypatch.setattr(state_migration, "_OPTIONAL_COMPATIBILITY_IDS",
                        state_migration._OPTIONAL_COMPATIBILITY_IDS - {FLOOR})
    with pytest.raises(state_migration.StateMigrationManifestError):
        state_migration.require_vault_state_ready(root)
