"""Public store queries preserve governed metadata when selection excludes rows."""

import pytest
from test_collection_store_writer import CID, manifest_path, manifest_text
from test_collection_store_writer import store as store

from exomem import record_governance
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import owner_principal, request_scope


def test_store_query_keeps_columns_established_by_excluded_rows(store):
    """Selecting first would lose a column or order it before its first visible witness."""
    text = manifest_text().replace(
        "    count: {type: integer}\n",
        "    count: {type: integer}\n"
        "    one_link: {type: link}\n"
        "    two_link: {type: link}\n",
    )
    store.create_collection(manifest_path(), text, why="capture", scaffold=False)
    target = store.root / "Knowledge Base/Notes/visible.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Visible\n")
    link = "[[Knowledge Base/Notes/visible]]"
    store.append_record(CID, item={"title": "Excluded", "count": 0,
                                  "one_link": "[[Knowledge Base/Notes/missing]]",
                                  "two_link": link}, why="observe")
    store.append_record(CID, item={"title": "Returned", "count": 1,
                                  "one_link": link}, why="observe")
    store.reconcile_views()

    with request_scope(owner_principal(surface="mcp")), preview_store(store.root, store.handle):
        result = record_governance.query_collection(
            store.root, CID, filters=[{"column": "count", "op": "eq", "value": 1}], limit=1,
        )

    assert result.total_matched == 1 and result.returned == 1
    assert result.rows[0]["title"] == "Returned" and result.rows[0]["one_link"] == link
    assert "two_link" not in result.rows[0]
    assert result.columns.index("two_link") < result.columns.index("one_link")


@pytest.mark.parametrize("field_spec, value", [
    ("{type: link}", "[[Knowledge Base/Notes/missing]]"),
    ("{type: array, items: {type: string}}", []),
])
def test_store_query_filters_on_governed_values(store, field_spec, value):
    """A raw predicate would count a withheld link or an array that projection removes."""
    text = manifest_text().replace(
        "    count: {type: integer}\n", f"    count: {{type: integer}}\n    target: {field_spec}\n",
    )
    store.create_collection(manifest_path(), text, why="capture", scaffold=False)
    store.append_record(CID, item={"title": "Example", "target": value}, why="observe")
    store.reconcile_views()

    with request_scope(owner_principal(surface="mcp")), preview_store(store.root, store.handle):
        result = record_governance.query_collection(
            store.root, CID, filters=[{"column": "target", "op": "eq", "value": value}],
        )

    assert result.total_matched == 0 and result.returned == 0 and result.rows == []


def test_optional_schema_collision_cannot_remove_synthesized_store_metadata(store):
    """A declared but absent link field must not govern the query's collection identity."""
    text = manifest_text().replace(
        "    count: {type: integer}\n",
        "    count: {type: integer}\n    collection_id: {type: link}\n",
    )
    store.create_collection(manifest_path(), text, why="capture", scaffold=False)
    store.append_record(CID, item={"title": "Example"}, why="observe")
    store.reconcile_views()

    with request_scope(owner_principal(surface="mcp")), preview_store(store.root, store.handle):
        result = record_governance.query_collection(store.root, CID)

    assert result.returned == 1 and result.rows[0]["collection_id"] == CID


def test_store_staged_pages_keep_source_versions_and_string_comparisons(store):
    """Column selection must not lose page identity, source versions, or declared string matching."""
    store.create_collection(manifest_path(), manifest_text(), why="capture", scaffold=False)
    paths = {}
    for title in ["01", "1"]:
        receipt = store.append_record(CID, item={"title": title, "count": 1}, why="observe")
        paths[title] = receipt["affected_paths"][0]
    store.reconcile_views()

    with request_scope(owner_principal(surface="mcp")), preview_store(store.root, store.handle):
        first = record_governance.query_collection(
            store.root, CID, columns=["title"], sort_by="count", limit=1,
        )
        second = record_governance.query_collection(
            store.root, CID, columns=["title"], sort_by="count", limit=1,
            continuation=first.continuation,
        )
        exact = record_governance.query_collection(
            store.root, CID, filters=[{"column": "title", "value": "1"}],
        )

    assert first.total_matched == second.total_matched == 2
    assert first.continuation is not None and second.continuation is None
    assert {first.rows[0]["title"], second.rows[0]["title"]} == {"01", "1"}
    for page in [first, second]:
        expected = {manifest_path(), paths[page.rows[0]["title"]]}
        assert {version.path for version in page.source_versions} == expected
        assert "record_id" in page.rows[0] and "item_version" in page.rows[0]
    assert exact.total_matched == 1 and exact.rows[0]["title"] == "1"
