"""Query descriptors retain payload identity independently of disposable query names."""

import hashlib
import json
from dataclasses import replace

import pytest
from test_collection_store_typed_storage import create
from test_collection_store_writer import CID
from test_collection_store_writer import store as store

from exomem.collection_store import index_migrations, native_layout, typed_storage
from exomem.collection_store.query_indexes import build_projection_plan
from exomem.query_engine.indexes import IndexKey, IndexSpec
from exomem.structured_collections import FieldSpec


def test_native_descriptor_binds_payload_generation_and_refuses_an_unpublished_payload(store):
    """A query generation, TEMP ordinal or ready query row cannot replace its payload binding."""
    create(store)
    layout = native_layout.build(CID, 7, {"amount": FieldSpec("number")}, identity="uuid16")
    plan = build_projection_plan(CID, {"amount": {"type": "number"}},
                                 (IndexSpec("amount", (IndexKey("amount"),)),),
                                 generation=13, payload_layout=layout)
    encoded = index_migrations._encoded(plan)
    assert json.loads(encoded)["payload_generation"] == 7
    assert json.loads(encoded)["file_role"] == "store"
    assert index_migrations._encoded(replace(plan, generation=21)) == encoded
    assert index_migrations._hash(replace(plan, layout=replace(layout, generation=8))) != index_migrations._hash(plan)
    with store.handle.transaction() as tx:
        tx.execute("INSERT INTO typed_encoding_mappings(collection_id,generation,state,layout_json,layout_hash) "
                   "VALUES(?,?,'building',?,?)", (CID, 7, layout.encoded, hashlib.sha256(layout.encoded.encode()).hexdigest()))
        tx.execute("UPDATE collections SET query_plan_hash=? WHERE collection_id=?", (index_migrations._hash(plan), CID))
        index_migrations.begin_build(tx, plan)
        assert index_migrations._plan(CID, 13, encoded, connection=tx) == plan
        tx.execute("UPDATE query_projection_mappings SET state='failed' WHERE collection_id=? AND state='ready'", (CID,))
        tx.execute("UPDATE query_projection_mappings SET state='ready' WHERE collection_id=? AND generation=13", (CID,))
        with pytest.raises(typed_storage.TypedStorageError, match="not published"):
            index_migrations.ready_plan(tx, CID)
        tx.execute("UPDATE typed_encoding_mappings SET state='ready' WHERE collection_id=? AND generation=7", (CID,))
        assert index_migrations.ready_plan(tx, CID) == plan
        with pytest.raises(ValueError, match="payload binding"):
            index_migrations._plan(CID, 13, encoded)
