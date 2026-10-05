"""Query dependencies must track exact values without whole-row invalidation."""

import pytest
from test_collection_store_writer import CID, KEY, manifest_path, manifest_text
from test_collection_store_writer import store as store

from exomem.collection_store import query_freshness


def basis(store, fields):
    result = query_freshness.uniform_basis(store.connection, CID, fields)
    assert result is not None, "governed writes have not established cursor dependency coverage"
    return result


def test_unrelated_field_correction_preserves_dependency_basis(store):
    """A count-only cursor cannot go stale because an unselected title changed."""
    store.create_collection(manifest_path(), manifest_text(), why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="observe")
    count, title = basis(store, ["count"]), basis(store, ["title"])
    store.update_record(CID, item_key=KEY, changes={"title": "Corrected"}, why="correct",
                        expected_item_version=first["after_item_hash"], expected_container_hash=first["after_container_hash"])
    assert basis(store, ["count"]) == count
    assert basis(store, ["title"]) != title


@pytest.mark.parametrize("before,after", [(1, 1.0), (-0.0, 0.0), (None, 0)])
def test_exact_numeric_representation_changes_cursor_basis(store, before, after):
    """Equal comparison keys do not hide a changed JSON value in row results."""
    text = manifest_text().replace("count: {type: integer}", "count: {type: number}")
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "One", "count": before}, item_key=KEY, why="observe")
    original = basis(store, ["count"])
    store.update_record(CID, item_key=KEY, changes={"count": after}, why="correct",
                        expected_item_version=first["after_item_hash"], expected_container_hash=first["after_container_hash"])
    assert basis(store, ["count"]) != original


def test_failed_canonical_transaction_cannot_advance_cursor_basis(store):
    """The cursor cannot report a change from a rolled-back update."""
    store.create_collection(manifest_path(), manifest_text(), why="create", scaffold=False)
    store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="observe")
    original = basis(store, ["count"])
    with pytest.raises(RuntimeError, match="interrupt"):
        with store.handle.transaction():
            query_freshness.maintain(store.connection, CID, {"title": "One", "count": 2},
                                    previous={"title": "One", "count": 1})
            raise RuntimeError("interrupt")
    assert basis(store, ["count"]) == original
    assert query_freshness.uniform_basis(store.connection, CID, ["absent"]) is None
