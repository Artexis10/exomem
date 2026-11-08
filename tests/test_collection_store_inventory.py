"""The collections inventory of check_memory kind ``collections-store`` (OpenSpec
move-structured-collections-to-sqlite C0.1).

The vault is invented: the migration fixture's file collections moved into the store,
then a Records collection kept in files with one item an ID backfill stamped, a new
summary collection and a derived import. Each value the test expects comes from the
files before the move or from the rows the test writes, never from the report.
"""

from __future__ import annotations

import pytest
from record_fixtures import LEDGER_COLLECTION_PATH
from test_collection_store_derived import DAYS, DERIVED, _archive, _preserve
from test_collection_store_derived import PATH as SAMPLES_PATH
from test_collection_store_derived import TEXT as DERIVED_TEXT
from test_collection_store_import_exports import SAMPLES
from test_collection_store_migration import (  # noqa: F401 - the env fixture
    MIGRATE,
    PLANNING,
    WORK,
    X3,
    env,
    legacy_vault,
    migrate,
    start_service,
)
from test_collection_store_s1_gate import _until
from test_collection_store_service import ON, call, imported
from test_collection_store_sqlite_only import PATH as READINGS_PATH
from test_collection_store_sqlite_only import READINGS, SQLITE
from test_collection_store_writer import CID, manifest_path, manifest_text

from exomem import record_formats, records
from exomem import structured_collections as collections
from exomem.collection_store import capability

KEPT = "4e4e4e4e-4e4e-4e4e-8e4e-4e4e4e4e4e4e"
KEPT_PATH = manifest_path().replace("Work", "Kept")
DATASET = "Knowledge Base/Records/dataset/_collection.md"
SHOWN = ("authority", "storage_strategy", "view_mode", "rows", "item_views", "held", "stamped_items", "blocker")


def file_counts(root, path):
    """A file collection's rows as its reader parses them, and its item files on disk."""
    adapter = record_formats.load_adapter(root, collections.load_manifest(root, root / path))
    source = adapter.source_path
    return len(adapter.read().records), len(list(source.rglob("*.md"))) if source.is_dir() else 0


def walk(root):
    """Every inventory row, page by page through next_cursor, through the handler check_memory calls."""
    rows, cursor, report = [], None, None
    while True:
        report = call(root, "maintain_memory", mode="collections-store", **({"cursor": cursor} if cursor else {}))
        assert "inventory" in report, sorted(report)
        rows.extend(report["inventory"]["collections"])
        cursor = report["inventory"]["next_cursor"]
        if cursor is None:
            return report, rows


def test_the_inventory_reports_every_store_and_file_collection_with_its_counts(
        env, monkeypatch):  # noqa: F811 - the imported fixture, by name
    """Defect: the cutover inventory lists only file collections, so a store collection's rows,
    item views and held candidates, a derived collection's imported rows or a stamped item stay
    invisible; or one page carries the whole vault."""
    root = legacy_vault(env)
    moved = {path: file_counts(root, path) for path in (LEDGER_COLLECTION_PATH, WORK, PLANNING, X3)}
    assert migrate(root, "prepare", operator=False) == 0 and migrate(root, "apply") == 0
    # A Records collection that stays in files after the cutover.
    records.create_collection(root, KEPT_PATH, manifest_text().replace(CID, KEPT).replace("title: Work", "title: Kept"),
                              why="kept in files", scaffold=True)
    monkeypatch.setattr(capability, "RELEASED", MIGRATE | ON)
    service = start_service(root)
    try:
        call(root, "record_memory", action="append", collection=KEPT_PATH, item={"title": "Stamped", "count": 1},
             why="one kept row")
        [item] = (root / KEPT_PATH).parent.glob("Items/*.md")
        # The stamp an ID backfill writes: the vault's note identity, outside the item schema.
        item.write_text(item.read_text().replace("---\n", "---\nexomem_id: 0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e\n", 1))
        call(root, "record_memory", action="create", manifest_path=READINGS_PATH, manifest_text=SQLITE,
             why="new readings")
        call(root, "record_memory", action="append", collection=READINGS, item={"title": "Morning", "count": 1},
             why="first reading")
        call(root, "record_memory", action="create", manifest_path=SAMPLES_PATH, manifest_text=DERIVED_TEXT,
             why="derived samples")
        job = imported(root, collection=DERIVED, mode="start", source_ref=_preserve(root, _archive(DAYS))["path"],
                       format="json-document", members="samples/*", mapping=SAMPLES)
        _until(lambda: imported(root, collection=DERIVED, mode="status", continuation=job["continuation"])["state"]
               == "complete", 30)

        report, rows = walk(root)
        found = {row["path"]: {key: row[key] for key in SHOWN} for row in rows}
        store = {"authority": "store", "storage_strategy": "markdown-items", "view_mode": "items", "held": 0,
                 "stamped_items": None, "blocker": None}
        assert found == {
            LEDGER_COLLECTION_PATH: {**store, "rows": moved[LEDGER_COLLECTION_PATH][0],
                                     "item_views": moved[LEDGER_COLLECTION_PATH][1]},
            # The migration fixture holds one refused candidate for this collection.
            WORK: {**store, "rows": moved[WORK][0], "item_views": moved[WORK][1], "held": 1},
            PLANNING: {**store, "rows": moved[PLANNING][0], "item_views": moved[PLANNING][1]},
            X3: {**store, "storage_strategy": "markdown-log", "rows": moved[X3][0], "item_views": 0},
            READINGS_PATH: {**store, "storage_strategy": "sqlite", "view_mode": "summary", "rows": 1,
                            "item_views": 0},
            # Four samples on each of three days, counted from the import log, not the store's items.
            SAMPLES_PATH: {**store, "storage_strategy": "sqlite", "view_mode": "summary", "rows": 12,
                           "item_views": 0},
            KEPT_PATH: {"authority": "file", "storage_strategy": "markdown-items", "view_mode": "items", "rows": 1,
                        "item_views": 1, "held": None, "stamped_items": 1, "blocker": None},
            DATASET: {"authority": "file", "storage_strategy": "dataset", "view_mode": "items", "rows": None,
                      "item_views": None, "held": None, "stamped_items": None, "blocker": None},
        }
        assert {row["path"]: row["collection_id"] for row in rows}[READINGS_PATH] == READINGS
        audit = {row["path"]: row["legacy_audit_status"] for row in rows}
        # The migration fixture's chains: complete for the ledger, a gap for Work, an acknowledged gap for Planning.
        assert (audit[LEDGER_COLLECTION_PATH], audit[WORK], audit[PLANNING]) == ("ok", "gap", "acknowledged_gap")
        assert [row["path"] for row in rows] == sorted(found)
        # The fields current callers read keep their meaning: file collections only.
        assert report["verdict"] == "migratable" and report["blocked"] == []
        assert [row["path"] for row in report["collections"]] == sorted([DATASET, KEPT_PATH])

        # A small page walks the same rows; a cursor from elsewhere is refused with its remedy.
        monkeypatch.setattr("exomem.collection_store.inventory.PAGE_SIZE", 3)
        assert walk(root)[1] == rows
        with pytest.raises(ValueError, match="INVALID_ARGUMENTS: cursor is not a next_cursor"):
            call(root, "maintain_memory", mode="collections-store", cursor="not-a-cursor")
    finally:
        service.stop()

