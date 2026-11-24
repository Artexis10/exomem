"""The Records and Planning inventories show each collection's newest shape-history entries, and a total.

A collection's history grows with every change, so the inventory shows the 5 newest entries and the total, and its
cost stays flat as the history grows. `inspect` pages the rest (tests/test_restructure.py).
"""

from __future__ import annotations

from conftest import initialize_vault_state_offline
from test_automatic_collection import WHY, inventory, records_manifest
from test_automatic_collection import file_vault as file_vault  # noqa: F401 - the fixture, by name
from test_automatic_collection import (
    silent_capture as silent_capture,  # noqa: F401 - autouse: capture is silent
)
from test_collection_store_writer import manifest_path
from test_collection_store_writer import store as store  # noqa: F401 - the fixture, by name
from test_planning_mutation import _manifest as planning_manifest
from test_restructure import mode as mode  # noqa: F401 - the fixture, by name
from test_restructure import work as work  # noqa: F401 - the fixture, by name

from exomem import automatic_collection, commands
from exomem.plan_memory import plan_memory
from exomem.record_governance import INVENTORY_HISTORY


def listed(work) -> dict:
    """The inventory's row for the collection `work` drives."""
    rows = work.operate("inspect")["collections"]
    return next(row for row in rows if row["collection_id"] == work.collection_id)


def test_the_inventory_shows_the_five_newest_entries_and_the_whole_total(work):
    applied = [
        work.restructure(steps=[{"step": "add_field", "field": f"extra{n}", "type": "string"}])["shape"]["id"]
        for n in range(INVENTORY_HISTORY + 1)
    ]

    shown = listed(work)["shape_history"]

    # The six additions and the create make seven entries; the oldest two are not shown.
    assert shown["total"] == INVENTORY_HISTORY + 2
    assert [entry["id"] for entry in shown["entries"]] == applied[::-1][:INVENTORY_HISTORY]
    assert "complete" not in shown


def test_a_planning_inventory_shows_the_shape_history_too(tmp_path):
    root = tmp_path / "planning-vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Existing log\n")
    initialize_vault_state_offline(root, source="shape history inventory fixture")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    plan_memory(root, "create", manifest_path=collection, manifest_text=planning_manifest(), why=WHY)
    applied = plan_memory(root, "restructure", collection=collection, why=WHY, steps=[{"step": "retitle", "title": "Plan"}])

    (row,) = plan_memory(root, "inspect")["collections"]

    assert row["shape_history"]["total"] == 2
    assert row["shape_history"]["entries"][0]["id"] == applied["shape"]["id"]


def test_the_store_inventory_reads_the_shape_history_once_however_many_collections_it_lists(store):
    def history_reads() -> int:
        statements: list[str] = []
        store.connection.set_trace_callback(statements.append)
        try:
            inventory(store)
        finally:
            store.connection.set_trace_callback(None)
        return sum("collection_shape_history" in statement for statement in statements)

    def make(title, index):
        store.create_collection(
            manifest_path().replace("Work", title), records_manifest(title, index), why=WHY, scaffold=False
        )

    make("Ledger", 0)
    one_collection = history_reads()
    for index, title in enumerate(["Notebook", "Roster", "Journal"], start=1):
        make(title, index)

    assert history_reads() == one_collection >= 1
    assert {title: row["shape_history"]["total"] for title, row in inventory(store).items()} == {
        "Ledger": 1, "Notebook": 1, "Roster": 1, "Journal": 1,
    }


def test_an_inventory_that_cannot_read_the_whole_log_says_its_file_totals_are_partial(file_vault):
    commands.op_record_memory(
        file_vault, "create", manifest_path=manifest_path().replace("Work", "Ledger"),
        manifest_text=records_manifest("Ledger"), why=WHY,
    )
    archives = file_vault / "Knowledge Base/_archive/logs"
    archives.mkdir(parents=True)
    for n in range(automatic_collection.INVENTORY_ARCHIVES + 1):
        (archives / f"log-{n}.md").write_text("# rotated\n")

    (row,) = commands.op_record_memory(file_vault, "inspect")["collections"]

    assert row["shape_history"]["total"] == 1 and row["shape_history"]["complete"] is False
