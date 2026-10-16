"""Native migration publishes only a proved payload/query pair."""

import json
from contextlib import closing

from test_collection_query_index_migrations import _import_files, _rows, revise
from test_collection_store_typed_storage import bulk, create, exact, migrate, row_ids, typed_page
from test_collection_store_writer import CID, manifest_text
from test_collection_store_writer import store as store

from exomem.collection_store import connection, index_migrations, typed_storage


def test_native_migration_catches_a_concurrent_update_and_retains_history_after_reopen(store):
    """A version changed between batches must be recopied before the native pair can serve."""
    create(store)
    bulk(store, {"title": "Entry A", "amount": -0.0}, {"title": "Entry B", "amount": 2**80 + 1})
    original = {name: store.connection.execute(f"SELECT * FROM {name}").fetchall()
                for name in ("item_versions", "version_identity", "item_sources", "txns")}
    old_query = index_migrations.ready_plan(store.connection, CID)
    assert store.migrate_typed_encoding(CID, limit=1) == "building"
    assert index_migrations.ready_plan(store.connection, CID) == old_query
    bulk(store, {"title": "Entry A", "amount": 1.0})
    for _ in range(8):
        state = store.migrate_typed_encoding(CID, limit=1)
        if state != "building":
            break
    assert state == "ready"
    layout = typed_storage.require_layout(store.connection, CID)
    assert layout.encoding == typed_storage.TYPED_V2
    assert layout.current_table == index_migrations.ready_plan(store.connection, CID).table_name
    assert [row["amount"] for row in typed_page(store, select=["amount"])] == [1.0, 2**80 + 1]
    ids = row_ids(store)
    values = {row_id: typed_storage.current_values(store.connection, layout, row_id) for row_id in ids.values()}
    assert {value["amount"] for value in values.values()} == {1.0, 2**80 + 1}
    for name, before in original.items():
        after = store.connection.execute(f"SELECT * FROM {name}").fetchall()
        assert all(row in after for row in before)
    assert bulk(store, {"title": "Entry A", "amount": -0.0}) == ["updated"]
    path = store.handle.path
    store.handle.close()
    with closing(connection.open_reader(path)) as reader:
        current = typed_storage.require_layout(reader, CID)
        assert current.encoding == typed_storage.TYPED_V2
        assert reader.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert reader.execute("PRAGMA foreign_key_check").fetchall() == []
        assert any(typed_storage.version_values(reader, row_id, 1)[0]["amount"] == 2**80 + 1 for row_id in ids.values())


def test_native_conversion_from_typed_v1_does_not_rewrite_its_retained_history(store):
    """Retiring the old current table must leave every old version bound to its original layout."""
    create(store)
    assert migrate(store) == "ready"
    bulk(store, {"title": "Entry", "amount": -0.0})
    old = typed_storage.require_layout(store.connection, CID)
    history = store.connection.execute(f"SELECT * FROM {old.version_table}").fetchall()
    assert store.migrate_typed_encoding(CID) == "ready"
    assert store.connection.execute(f"SELECT * FROM {old.version_table}").fetchall() == history
    assert store.connection.execute("SELECT state FROM typed_encoding_mappings WHERE generation=?", (old.generation,)).fetchone() == ("retired",)
    assert bulk(store, {"title": "Entry", "amount": 2**70}) == ["updated"]
    [row_id] = row_ids(store).values()
    assert typed_storage.canonical_json(typed_storage.version_values(store.connection, row_id, 1)[0]) == '{"amount":-0.0,"title":"Entry"}'
    assert typed_storage.version_values(store.connection, row_id, 2)[0]["amount"] == 2**70


def test_native_query_only_rebuild_keeps_payload_history_and_later_writes_on_one_layout(store):
    """A query generation must not become a new payload generation or leave later writes on its retired table."""
    original = create(store)
    bulk(store, {"title": "Entry A", "amount": 1}, {"title": "Entry B", "amount": 2})
    assert store.migrate_typed_encoding(CID) == "ready"
    layout = typed_storage.require_layout(store.connection, CID)
    before = store.connection.execute("SELECT * FROM typed_encoding_mappings").fetchall()
    history = store.connection.execute(f"SELECT * FROM {layout.version_table}").fetchall()
    proposed = original.replace("\n---", "\nindexes:\n  reverse_amount:\n    keys: [{field: amount, direction: desc}]\n---")
    after = store.inspect_collection(CID)["lifecycle_guards"]["expected_container_hash"]
    revise(store, original, proposed, after)
    assert not store.backfill_query_indexes(CID, limit=1)
    bulk(store, {"title": "Entry A", "amount": 3})
    assert store.backfill_query_indexes(CID, limit=1)
    current = typed_storage.require_layout(store.connection, CID)
    assert current.generation == layout.generation and current.encoded == layout.encoded
    assert current.current_table != layout.current_table
    assert store.connection.execute("SELECT * FROM typed_encoding_mappings").fetchall() == before
    assert store.connection.execute(f"SELECT * FROM {layout.version_table}").fetchall()[:len(history)] == history
    bulk(store, {"title": "Entry B", "amount": 4})
    assert [row["amount"] for row in typed_page(store, select=["amount"])] == [3, 4]


def test_a_large_accepted_record_migrates_without_import_row_limits_or_small_selection_refusal(store):
    """Existing Records larger than an import row must retain exact payloads and admit a small selected field."""
    create(store)
    values = {"title": "Large entry", "meta": {"parts": ["x" * 10000 for _ in range(140)]}}
    appended = store.append_record(CID, item=values, why="record")
    assert len(typed_storage.canonical_json(values).encode()) > 1024 * 1024
    assert store.migrate_typed_encoding(CID) == "ready"
    layout = typed_storage.require_layout(store.connection, CID)
    [row_id] = row_ids(store).values()
    assert typed_storage.current_values(store.connection, layout, row_id) == values
    assert typed_page(store, select=["title"]) == [{"title": "Large entry"}]
    store.update_record(CID, item_key=appended["item_key"], changes={"count": 1}, why="correct",
                        expected_container_hash=appended["after_container_hash"],
                        expected_item_version=appended["after_item_hash"])
    assert typed_storage.current_values(store.connection, layout, row_id) == {**values, "count": 1}
    path = store.handle.path
    store.handle.close()
    with closing(connection.open_reader(path)) as reopened:
        assert typed_storage.version_values(reopened, row_id, 1)[0] == values
        assert typed_storage.version_values(reopened, row_id, 2)[0] == {**values, "count": 1}


def test_a_typed_v1_record_under_the_copy_budget_converts_to_native(store):
    """A canonical JSON cell charged as escaped text must not refuse a typed-v1 row the budget admits."""
    create(store)
    assert migrate(store) == "ready"
    values = {"title": "Large entry", "meta": {"parts": ["x" * 10000 for _ in range(100)]}}
    store.append_record(CID, item=values, why="record")
    assert store.migrate_typed_encoding(CID) == "ready"
    layout = typed_storage.require_layout(store.connection, CID)
    assert layout.encoding == typed_storage.TYPED_V2
    [row_id] = row_ids(store).values()
    assert exact(typed_storage.current_values(store.connection, layout, row_id)) == exact(values)


def test_an_oversized_migration_stays_pending_after_reopen_while_the_old_collection_serves(store):
    """An unsupported source row cannot cause an unsafe cutover, a retry loop or loss of old writes."""
    create(store)
    values = {"title": "Large entry", "meta": {"parts": ["x" * 10000 for _ in range(450)]}}
    store.append_record(CID, item=values, why="record")
    old = index_migrations.ready_plan(store.connection, CID)
    assert store.migrate_typed_encoding(CID) == "failed"
    assert index_migrations.ready_plan(store.connection, CID) == old
    assert typed_storage.collection_encoding(store.connection, CID) == typed_storage.JSON_V1
    assert store.connection.execute("SELECT count(*) FROM typed_encoding_mappings WHERE state='ready'").fetchone() == (0,)
    diagnostic = json.loads(store.connection.execute("SELECT diagnostic_json FROM typed_encoding_mappings").fetchone()[0])
    assert diagnostic["code"] == "ROW_REQUIRES_STREAMING_MIGRATION"
    assert diagnostic["source_bytes"] > diagnostic["copy_budget_bytes"]
    assert any(d["code"] == "NATIVE_MIGRATION_PENDING" for d in store.inspect_collection(CID)["diagnostics"])
    assert bulk(store, {"title": "Other entry", "amount": 2}) == ["inserted"]
    path = store.handle.path
    store.handle.close()
    with closing(connection.open_reader(path)) as reopened:
        assert index_migrations.ready_plan(reopened, CID) == old
        assert json.loads(reopened.execute("SELECT diagnostic_json FROM typed_encoding_mappings").fetchone()[0]) == diagnostic
        assert reopened.execute("SELECT count(*) FROM items").fetchone() == (2,)


def test_a_first_projection_that_failed_on_a_large_imported_row_converts_to_native(tmp_path, store):
    """An imported row over the JSON index cell limit must not leave native conversion without query intent."""
    fields = "".join(f"    part{n}: {{type: string}}\n" for n in range(9))
    text = manifest_text().replace("    count: {type: integer}\n", "    count: {type: integer, filterable: true}\n" + fields)
    _import_files(tmp_path, store, {"title": "Large", "count": 1, **{f"part{n}": "x" * 30_000 for n in range(9)}},
                  text=text)
    assert not store.backfill_query_indexes(CID)
    assert store.migrate_typed_encoding(CID) == "ready"
    assert typed_storage.collection_encoding(store.connection, CID) == typed_storage.TYPED_V2
    assert _rows(store, {"title": "string", "count": "integer"}) == [{"title": "Large", "count": 1}]
