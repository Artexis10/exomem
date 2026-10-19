"""Native payloads retain authenticated paging and governed field admission."""

import pytest
from test_collection_store_writer import CID, KEY, OTHER, manifest_path
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope
from test_query_engine_cursors import THIRD, after, page, query, setup

from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import route


def test_native_numeric_cursor_survives_snapshot_close_and_uses_exact_overflow_boundaries(store):
    """A binary overflow boundary must not narrow to float or lose logical identity in a public cursor."""
    declaration = setup(store)
    fourth = "44444444-4444-4444-8444-444444444444"
    for key, count in ((KEY, -(2**90)), (OTHER, 1.0), (THIRD, 2**90), (fourth, 2**90 + 1)):
        store.append_record(CID, item={"title": key, "count": count}, item_key=key, why="record")
    assert store.migrate_typed_encoding(CID) == "ready"
    intent = query(declaration, select=["item_key", "count"])
    found = []
    for _ in range(4):
        result = page(store, intent)
        found.extend(result["rows"])
        if not result["has_more"]:
            break
        intent = after(intent, result)
    assert found == [{"item_key": key, "count": count} for key, count in
                     ((KEY, -(2**90)), (OTHER, 1.0), (THIRD, 2**90), (fourth, 2**90 + 1))]
    assert result["next_cursor"] is None


def test_native_mixed_admission_keeps_an_unreleased_change_out_of_the_cursor_basis(store):
    """A withheld native row must not enter TEMP operands or invalidate an admitted continuation."""
    declaration = setup(store)
    store.append_record(CID, item={"title": "First", "count": 1}, item_key=KEY, why="record")
    hidden = store.append_record(CID, item={"title": "Hidden", "count": 2}, item_key=OTHER, why="record")
    store.append_record(CID, item={"title": "Last", "count": 3}, item_key=THIRD, why="record")
    assert store.migrate_typed_encoding(CID) == "ready"
    write_scope(store.root, paths=f"Records/Work/Items/{OTHER}.md")
    write_rule(store.root, ceiling=0)
    intent = query(declaration)
    with request_scope(_external()):
        continuation = after(intent, page(store, intent))
    with request_scope(owner_principal()):
        store.update_record(CID, item_key=OTHER, changes={"count": 2**100}, why="correct",
                            expected_item_version=hidden["after_item_hash"],
                            expected_container_hash=store.inspect_collection(CID)["lifecycle_guards"]["expected_container_hash"])
    with request_scope(_external()):
        assert page(store, continuation)["rows"] == [{"count": 3}]


@pytest.mark.parametrize(("view_mode", "sort_title"), [("items", False), ("summary", False), ("summary", True)])
def test_a_small_released_property_remains_readable_beside_a_large_withheld_container(store, view_mode, sort_title):
    """Neither a native index nor scoped TEMP preparation may load withheld siblings before projection."""
    manifest = f"""---
type: collection
exomem_id: {CID}
title: Sample
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
view_mode: {view_mode}
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
indexes:
  by_title:
    keys: [{{field: title}}]
item_schema:
  natural_key: [title]
  fields:
    title: {{type: string, required: true}}
    meta:
      type: object
      properties:
        small: {{type: integer}}
        parts: {{type: array, items: {{type: string}}, classification: location}}
---
"""
    store.create_collection(manifest_path(), manifest, why="create", scaffold=False)
    written = store.append_record(CID, item_key=KEY,
                                  item={"title": "Large record", "meta": {"small": 7, "parts": ["x" * 10000 for _ in range(140)]}},
                                  why="record")
    assert written["outcome"] == "committed"
    query = {"version": 1, "select": ["meta"], "page": {"limit": 1}}
    if sort_title:
        query["order_by"] = [{"field": "title", "direction": "desc"}]
    with request_scope(_external()):
        result = route.run(store, CID, query, facade_profile="records")
    assert result["rows"] == [{"meta": {"small": 7}}]
