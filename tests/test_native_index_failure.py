"""An unsupported native index candidate must not interrupt canonical writes."""

import pytest
from test_collection_query_index_migrations import revise
from test_collection_store_writer import CID, KEY, manifest_path, manifest_text
from test_collection_store_writer import store as store

from exomem.collection_store import index_migrations, typed_storage


@pytest.mark.parametrize("trigger", ["backfill", "append"])
def test_a_native_index_failure_preserves_the_ready_payload_and_canonical_writes(store, trigger):
    """A storage-valid timestamp outside index precision must fail only the new query candidate."""
    original = manifest_text().replace("count: {type: integer}", "count: {type: datetime}")
    store.create_collection(manifest_path(), original, why="declare", scaffold=False)
    first = store.append_record(CID, item={"title": "Entry", "count": "2026-10-04T00:00:00.0000001Z"},
                                item_key=KEY, why="record")
    assert store.migrate_typed_encoding(CID) == "ready"
    ready = index_migrations.ready_plan(store.connection, CID)
    payload = typed_storage.require_layout(store.connection, CID)
    proposed = original.replace("count: {type: datetime}", "count: {type: datetime, filterable: true}")
    revise(store, original, proposed, first["after_container_hash"])
    if trigger == "backfill":
        assert not store.backfill_query_indexes(CID)
    else:
        store.append_record(CID, item={"title": "Triggers candidate", "count": "2026-10-04T00:00:00.0000002Z"}, why="record")
    assert index_migrations.ready_plan(store.connection, CID) == ready
    assert typed_storage.require_layout(store.connection, CID) == payload
    assert store.connection.execute("SELECT state FROM query_projection_mappings ORDER BY generation DESC LIMIT 1").fetchone() == ("failed",)
    store.append_record(CID, item={"title": "Another entry", "count": "2026-10-04T00:00:00.0000002Z"}, why="record")
