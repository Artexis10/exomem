"""A retired collection is set aside, not deleted: recall and the inventory leave it, reads by identity still answer,
and it takes no new rows until it is restored. A retired field keeps its values. A collection's creation is its
first shape-history entry. Cases run on a real temporary vault of each storage mode through the shared dispatcher.
"""

from __future__ import annotations

import uuid

import pytest
import yaml
from conftest import initialize_vault_state_offline
from test_collection_store_writer import CID, manifest_path, manifest_text
from test_restructure import (
    WHY,
    Work,
    make_file_work,
    make_store_work,
    verify_from_genesis,
)
from test_restructure import file_work as file_work  # noqa: F401 - the fixture, by name
from test_restructure import mode as mode  # noqa: F401 - the fixture, by name
from test_restructure import store as store  # noqa: F401 - the fixture, by name
from test_restructure import work as work  # noqa: F401 - the fixture, by name

from exomem import commands, recall_policy, vault, working_set_index
from exomem.cli_ops import OpError
from exomem.collection_store import shape_history
from exomem.plan_memory import plan_memory

SUCCESSOR = str(uuid.uuid4())


def inventory(work: Work, **arguments) -> dict:
    return work.operate("inspect", **arguments)


def refused(call) -> dict:
    with pytest.raises(OpError) as error:
        call()
    return error.value.as_public_dict()


def history(work: Work, collection_id: str | None = None) -> dict:
    return work.operate("inspect", collection=collection_id or work.collection_id)["shape_history"]


@pytest.fixture
def merged_work(mode, tmp_path, request):
    """A collection whose manifest names the collection that took its rows, as a merge leaves it, then retired."""
    key = f"successor: {SUCCESSOR}\n"
    made = (
        make_store_work(request.getfixturevalue("store"), views=key) if mode == "store"
        else make_file_work(tmp_path, views=key)
    )
    made.restructure(steps=[{"step": "retire"}])
    return made


def test_a_retired_collection_leaves_search_and_activation_and_returns_when_restored(file_work, tmp_path):
    root = tmp_path / "file-vault"
    manifest = root / manifest_path()

    def found() -> tuple[bool, bool]:
        hits = commands.op_find(root, query="Work", mode="keyword", scope="kb")
        index = working_set_index.WorkingSetIndex(root)
        index.rebuild()
        return (
            any(hit["path"] == manifest_path() for hit in hits),
            any(row.kind == "collection" and row.title == "Work" for row in index.anchors()),
        )

    assert recall_policy.is_recall_candidate(root, manifest) and found() == (True, True)

    file_work.restructure(steps=[{"step": "retire"}])
    assert not recall_policy.is_recall_candidate(root, manifest) and found() == (False, False)

    file_work.restructure(steps=[{"step": "restore"}])
    assert recall_policy.is_recall_candidate(root, manifest) and found() == (True, True)


def test_the_inventory_counts_a_retired_collection_and_lists_it_only_on_request(work):
    assert [row["lifecycle"] for row in inventory(work)["collections"]] == ["active"]
    assert inventory(work)["retired_collections"] == 0

    work.restructure(steps=[{"step": "retire"}])

    default = inventory(work)
    assert default["collections"] == [] and default["retired_collections"] == 1
    listed = inventory(work, include_retired=True)
    assert [row["lifecycle"] for row in listed["collections"]] == ["retired"] and listed["retired_collections"] == 1

    work.restructure(steps=[{"step": "restore"}])
    restored = inventory(work)
    assert [row["lifecycle"] for row in restored["collections"]] == ["active"] and restored["retired_collections"] == 0


def test_a_retired_collection_is_read_by_identity_marked_retired_with_its_rows(work):
    rows = work.rows()
    assert "retired" not in work.operate("query", collection=work.collection_id)

    work.restructure(steps=[{"step": "retire"}])

    queried = work.operate("query", collection=work.collection_id)
    inspected = work.operate("inspect", collection=work.collection_id)
    assert queried["retired"] is True and inspected["contract"]["retired"] is True
    assert work.rows() == rows

    work.restructure(steps=[{"step": "restore"}])
    assert "retired" not in work.operate("query", collection=work.collection_id)
    assert "retired" not in work.operate("inspect", collection=work.collection_id)["contract"]


def test_a_collection_that_a_merge_retired_names_its_successor_in_the_read_and_the_refusal(merged_work):
    queried = merged_work.operate("query", collection=merged_work.collection_id)
    error = refused(lambda: merged_work.append({"title": "c", "count": 5}))

    assert queried["retired"] is True and queried["successor"] == SUCCESSOR
    assert error["code"] == "COLLECTION_RETIRED" and error["successor"] == SUCCESSOR
    # The agent repeats this sentence to the person, so an id has no place in it; an unresolved successor is a plain phrase.
    assert SUCCESSOR not in error["message"] and "its successor" in error["message"]


def test_a_refused_row_for_a_merged_collection_names_where_it_went_by_title(store):
    from test_restructure_identity import other

    merged = make_store_work(store)
    other(store, "view-per-row")
    merged.restructure(steps=[{"step": "merge_into", "target": "Estimates"}])

    error = refused(lambda: merged.append({"title": "c", "count": 5}))

    assert error["code"] == "COLLECTION_RETIRED"
    assert "into collection 'Estimates'" in error["message"] and error["successor"] not in error["message"]


def test_a_retired_collection_refuses_a_new_row_and_an_update_and_points_to_restore(work):
    work.restructure(steps=[{"step": "retire"}])
    rows = work.rows()

    appended = refused(lambda: work.append({"title": "c", "count": 5}))
    updated = refused(lambda: work.update("a", {"count": 9}))

    assert appended["code"] == updated["code"] == "COLLECTION_RETIRED"
    assert "restore" in appended["remediation"] and "restore" in updated["remediation"]
    assert work.rows() == rows

    work.restructure(steps=[{"step": "restore"}])
    work.append({"title": "c", "count": 5})
    assert set(work.rows()) == {"a", "b", "c"}


def test_add_record_with_new_collection_on_a_retired_title_refuses_and_points_to_restore(work):
    work.restructure(steps=[{"step": "retire"}])

    error = refused(lambda: work.operate(
        "append", collection="Work", item={"title": "c", "count": 5}, why=WHY, new_collection={"natural_key": ["title"]},
    ))

    assert error["code"] == "COLLECTION_RETIRED" and "restore" in error["remediation"]
    assert set(work.rows()) == {"a", "b"}


def test_a_retired_field_keeps_its_values_refuses_new_ones_and_leaves_the_schema_agents_see(work):
    work.restructure(steps=[{"step": "add_field", "field": "note", "type": "string"}])
    work.append({"title": "c", "note": "kept"})
    assert "note" in work.operate("inspect", collection=work.collection_id)["observed_values"]

    work.restructure(steps=[{"step": "retire_field", "field": "note"}])

    default = work.operate("query", collection=work.collection_id)["rows"]
    asked_for = work.operate("query", collection=work.collection_id, columns=["title", "note"])["rows"]
    observed = work.operate("inspect", collection=work.collection_id)["observed_values"]
    written = refused(lambda: work.append({"title": "d", "note": "new"}))

    assert all("note" not in row for row in default)
    assert {row["title"]: row.get("note") for row in asked_for}["c"] == "kept"
    assert "note" not in observed and "title" in observed
    assert written["code"] == "SCHEMA_RETIRED_FIELD" and "restore_field" in written["message"]
    work.append({"title": "d"})  # the rest of the schema still takes rows
    work.update("c", {"count": 7})  # a row that holds a retired value is still edited, and keeps the value
    assert refused(lambda: work.update("c", {"note": "new"}))["code"] == "SCHEMA_RETIRED_FIELD"
    assert {row["title"]: row.get("note") for row in work.operate(
        "query", collection=work.collection_id, columns=["title", "note"])["rows"]}["c"] == "kept"

    work.restructure(steps=[{"step": "restore_field", "field": "note"}])
    assert work.rows()["c"]["note"] == "kept" and "note" in work.operate("inspect", collection=work.collection_id)["observed_values"]


def test_a_whole_row_upsert_that_omits_a_retired_field_keeps_its_stored_value(mode, work, tmp_path, request):
    """`bulk_upsert` replaces a row whole; a retired field the row omits is not part of that replacement."""
    root = request.getfixturevalue("store").root if mode == "store" else tmp_path / "file-vault"
    evidence = "Knowledge Base/Evidence/export.md"
    (root / evidence).parent.mkdir(parents=True, exist_ok=True)
    (root / evidence).write_text("---\ntype: evidence\n---\n\nPreserved export.\n", encoding="utf-8")
    work.restructure(steps=[{"step": "add_field", "field": "note", "type": "string"}])
    work.append({"title": "c", "count": 1, "note": "kept"})
    work.restructure(steps=[{"step": "retire_field", "field": "note"}])
    guard = work.operate("query", collection=work.collection_id)["snapshot"]

    work.operate("bulk_upsert", collection=work.collection_id, why=WHY, expected_container_hash=guard,
                 source=evidence, rows=[{"item": {"title": "c", "count": 2}}])

    rows = work.operate("query", collection=work.collection_id, columns=["title", "count", "note"])["rows"]
    assert {row["title"]: (row.get("count"), row.get("note")) for row in rows}["c"] == (2, "kept")


def test_a_collection_created_with_origin_user_design_records_it_as_its_first_shape_history_entry(mode, tmp_path, request):
    made = (
        make_store_work(request.getfixturevalue("store"), origin="user_design") if mode == "store"
        else make_file_work(tmp_path, origin="user_design")
    )

    pages = history(made)

    (entry,) = pages["entries"]
    assert pages["total"] == 1
    assert entry["origin"] == "user_design" and entry["steps"] == [] and entry["undoes"] is None and entry["why"] == WHY
    if mode == "store":
        verify_from_genesis(request.getfixturevalue("store"), made.collection_id)


def test_a_create_that_names_a_server_origin_is_refused_and_creates_nothing(mode, tmp_path, request):
    with pytest.raises(OpError) as error:
        if mode == "store":
            make_store_work(request.getfixturevalue("store"), origin="automatic")
        else:
            make_file_work(tmp_path, origin="automatic")

    assert error.value.as_public_dict()["code"] == "INVALID_RESTRUCTURE"
    if mode == "file-only":
        assert not (tmp_path / "file-vault" / manifest_path()).exists()


def test_the_creation_entry_cannot_be_undone(work):
    (entry,) = history(work)["entries"]

    error = work.refused(undo=entry["id"])

    assert error["code"] == "RESTRUCTURE_UNDO_REFUSED"


def test_a_store_page_of_shape_history_is_one_index_range_that_needs_no_sort(store):
    """The page reads one range of the (collection, time, transaction) index in order, so its cost follows its limit."""
    make_store_work(store)
    first = shape_history.FIRST_PAGE_SQL, {"cid": CID, "fetch": 3}
    after = shape_history.NEXT_PAGE_SQL, {"cid": CID, "fetch": 3, "at": "2026-01-01T00:00:00Z", "txn": 1}

    with store.read_snapshot():
        plans = [
            " ".join(row[-1] for row in store.connection.execute("EXPLAIN QUERY PLAN " + sql, bound))
            for sql, bound in (first, after)
        ]

    for plan in plans:
        assert "USING INDEX collection_shape_history_by_time (collection_id=?" in plan
        assert "SCAN" not in plan and "TEMP B-TREE" not in plan


class PlanningWork:
    """A Planning collection whose `restructure` runs through `plan_memory`, on a file vault or the store."""

    def __init__(self, call, collection_id):
        self.call, self.collection_id = call, collection_id

    def restructure(self, steps):
        return self.call("restructure", collection=self.collection_id, why=WHY, steps=steps)

    def inspected(self) -> dict:
        return self.call("inspect", collection=self.collection_id)


@pytest.fixture
def planning(mode, tmp_path, request):
    if mode == "store":
        from test_automatic_collection import call

        store = request.getfixturevalue("store")
        store.create_collection(manifest_path("planning"), manifest_text("planning"), why=WHY, scaffold=False)
        return PlanningWork(lambda action, **arguments: call(store, "plan_memory", action=action, **arguments), CID)
    from test_planning_mutation import _manifest

    root = tmp_path / "planning-vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Existing log\n")
    initialize_vault_state_offline(root, source="retirement planning fixture")
    path = "Knowledge Base/Planning/Work/_collection.md"
    plan_memory(root, "create", manifest_path=path, manifest_text=_manifest(), why=WHY)
    front = yaml.safe_load(vault.parse_frontmatter((root / path).read_text(), strict=True)[2])
    return PlanningWork(lambda action, **arguments: plan_memory(root, action, **arguments), front["exomem_id"])


@pytest.mark.parametrize(
    "step",
    [
        {"step": "rename_field", "field": "priority", "to": "rank"},
        {"step": "retire_field", "field": "status"},
        {"step": "add_field", "field": "tags", "type": "string"},
    ],
    ids=["rename a core field", "retire a core field", "add a core field"],
)
def test_a_step_on_a_planning_core_field_is_refused(planning, step):
    before = planning.inspected()["shape_history"]["total"]

    with pytest.raises(OpError) as error:
        planning.restructure([step])

    assert error.value.as_public_dict()["code"] == "RESTRUCTURE_STEP_REFUSED"
    assert "Planning core field" in error.value.message
    assert planning.inspected()["shape_history"]["total"] == before


def test_a_planning_field_of_its_own_cannot_be_renamed_onto_a_core_name(planning):
    planning.restructure([{"step": "add_field", "field": "estimate", "type": "string"}])

    with pytest.raises(OpError) as error:
        planning.restructure([{"step": "rename_field", "field": "estimate", "to": "tags"}])

    assert "Planning core field" in error.value.message


def test_a_planning_collection_still_takes_a_field_of_its_own(planning):
    applied = planning.restructure([{"step": "add_field", "field": "estimate", "type": "string"}])

    assert applied["shape"]["steps"] == [{"step": "add_field", "field": "estimate", "type": "string"}]


def test_a_retired_planning_collection_is_read_marked_retired_and_takes_no_new_item(planning):
    planning.restructure([{"step": "retire"}])

    queried = planning.call("query", collection=planning.collection_id)
    with pytest.raises(OpError) as error:
        planning.call("add", collection=planning.collection_id, item={"title": "Ship", "kind": "outcome"}, why=WHY)

    assert queried["retired"] is True and planning.inspected()["contract"]["retired"] is True
    assert error.value.as_public_dict()["code"] == "COLLECTION_RETIRED"

    planning.restructure([{"step": "restore"}])
    assert "retired" not in planning.call("query", collection=planning.collection_id)
