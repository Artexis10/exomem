"""A Planning item's `results` link it to the record or page where its outcome lives.

The fixtures declare `results` on the store-backed Planning collection of the writer tests, beside the
link-values log that holds the records the results name.
"""

import json

import pytest
from test_collection_link_values import ALPHA, K1, LOG, _ready, append, log_text, page
from test_collection_row_references import path
from test_collection_store_writer import CID as PLAN
from test_collection_store_writer import KEY, manifest_path, manifest_text
from test_collection_store_writer import store as store  # noqa: F401

from exomem import page_citations
from exomem import structured_collections as collections
from exomem.collection_store import connection
from exomem.governance.principal import owner_principal, request_scope

ALPHA_REF = f"exomem://memory/{ALPHA}"
ROW_REF = f"exomem://record/{LOG}/{K1}"
PLAN_REF = f"exomem://plan/{PLAN}/{KEY}"


@pytest.fixture
def world(store, monkeypatch):
    monkeypatch.setattr(connection, "store_path", lambda root: store.handle.path)
    page(store.root, "Notes/Alpha.md", "Alpha", ALPHA)
    store.create_collection(path("Log"), log_text(), why="declare", scaffold=False)
    declared = manifest_text("planning").replace(
        "    area: {type: string}\n",
        "    area: {type: string}\n    results: {type: array, items: {type: link}}\n"
        "    motivation: {type: array, items: {type: link}}\n")
    store.create_collection(manifest_path("planning"), declared, why="declare", scaffold=False)
    append(store, {}, key=K1)
    return store


def plan_item(store, **item):
    return store.append_record(PLAN, item={"title": "Ship it", "kind": "outcome", **item}, item_key=KEY, why="plan")


def complete(store, added, **changes):
    guards = store.inspect_collection(PLAN)["lifecycle_guards"]
    return store.update_record(
        PLAN, item_key=KEY, changes={"status": "completed", "commitment": "committed", "horizon": "week", **changes},
        expected_item_version=added["after_item_hash"], expected_container_hash=guards["expected_container_hash"],
        why="done")


def _ready_both(store):
    _ready(store, PLAN)
    _ready(store, LOG)


def stored_results(store):
    return json.loads(store._item(PLAN, KEY)["values_json"]).get("results")


def citations_of(store, ref):
    with request_scope(owner_principal()):
        return page_citations.inbound_citations(ref, store.root)


def test_results_are_stored_as_refs_and_the_outcome_lists_the_plan_item(world):
    plan_item(world, results=["Alpha", ROW_REF])
    assert stored_results(world) == [ALPHA_REF, ROW_REF]
    _ready_both(world)
    expected = {"profile": "planning", "collection": PLAN, "collection_title": "Work", "label": "Ship it",
                "ref": PLAN_REF, "field": "results"}
    assert citations_of(world, ALPHA_REF)["items"] == [expected]
    assert citations_of(world, ROW_REF)["items"] == [expected]


def test_more_than_sixteen_results_are_refused_before_anything_is_stored(world):
    refs = [f"exomem://memory/00000000-0000-4000-8000-{index:012d}" for index in range(17)]
    with pytest.raises(collections.CollectionError, match="INVALID_PLAN.*16"):
        plan_item(world, results=refs)
    assert world._item(PLAN, KEY) is None


def test_completing_an_item_without_a_result_commits_and_reports_no_result_linked(world):
    added = plan_item(world)
    done = complete(world, added)
    assert done["outcome"] == "committed" and "no_result_linked" in done


def test_completing_an_item_that_links_a_result_reports_nothing(world):
    added = plan_item(world, results=["Alpha"])
    assert "no_result_linked" not in complete(world, added)


def test_a_status_that_the_registry_does_not_class_as_done_is_not_a_completion(world):
    """`cancelled` is class `dropped`, so closing an item that way is not a missing result."""
    added = plan_item(world)
    guards = world.inspect_collection(PLAN)["lifecycle_guards"]
    dropped = world.update_record(
        PLAN, item_key=KEY, changes={"status": "cancelled"}, expected_item_version=added["after_item_hash"],
        expected_container_hash=guards["expected_container_hash"], why="drop")
    assert dropped["outcome"] == "committed" and "no_result_linked" not in dropped


def test_motivation_declared_with_link_items_stays_memory_refs_only_and_is_cited(world):
    """Write behaviour is unchanged: a title is refused as before. A memory ref is indexed like a link."""
    with pytest.raises(collections.CollectionError, match="motivation reference is invalid"):
        plan_item(world, motivation=["Alpha"])
    plan_item(world, motivation=[ALPHA_REF])
    _ready_both(world)
    [cited] = citations_of(world, ALPHA_REF)["items"]
    assert (cited["ref"], cited["field"]) == (PLAN_REF, "motivation")
