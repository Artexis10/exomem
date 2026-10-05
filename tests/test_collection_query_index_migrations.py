"""Derived indexes follow canonical transactions and survive interrupted rebuilds."""

import importlib
import json

import pytest
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text
from test_collection_store_writer import store as store


def indexed_text():
    return manifest_text().replace("count: {type: integer}", "count: {type: integer, filterable: true}")


def ready(store):
    # This is a behaviour check on an actual committed collection, not an
    # import/symbol check: declaring an index must install a ready projection.
    assert store.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='query_projection_mappings'"
    ).fetchone(), "declared indexes have no durable readiness mapping"
    manager = importlib.import_module("exomem.collection_store.index_migrations")
    return manager, manager.ready_plan(store.connection, CID)


def test_declared_index_tracks_correction_and_canonical_rollback(store):
    store.create_collection(manifest_path(), indexed_text(), why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="observe")
    manager, plan = ready(store)
    assert plan is not None
    assert store.connection.execute(
        f"SELECT item_key,row_version,k0_tag,k0_key FROM {plan.table_name}"
    ).fetchone() == (KEY, 1, 3, "0280000000000000000200")
    store.update_record(CID, item_key=KEY, changes={"count": 2}, why="correct",
                        expected_item_version=first["after_item_hash"],
                        expected_container_hash=first["after_container_hash"])
    assert store.connection.execute(f"SELECT row_version,k0_key FROM {plan.table_name}").fetchone() == (
        2, "0280000000000000000300")
    before = store.connection.execute(f"SELECT * FROM {plan.table_name}").fetchall()
    with pytest.raises(RuntimeError, match="interrupt"):
        with store.handle.transaction():
            manager.maintain_item(store.connection, CID, 1, KEY, 99, {"count": 9})
            raise RuntimeError("interrupt")
    assert store.connection.execute(f"SELECT * FROM {plan.table_name}").fetchall() == before


def test_bounded_rebuild_resumes_and_writes_catch_up_before_cutover(store):
    original = indexed_text()
    store.create_collection(manifest_path(), original, why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="observe")
    manager, old = ready(store)
    from exomem.collection_store.query_indexes import build_projection_plan
    from exomem.query_engine.indexes import IndexKey, IndexSpec

    replacement = build_projection_plan(CID, {"count": {"type": "integer"}, "title": {"type": "string"}},
        (IndexSpec("z_count", (IndexKey("count", "desc"),)), IndexSpec("a_title", (IndexKey("title"),))),
        generation=old.generation + 1)
    revised = original.replace("\n---", "\nindexes:\n  z_count:\n    keys: [{field: count, direction: desc}]\n  a_title:\n    keys: [{field: title}]\n---")
    revise(store, original, revised, first["after_container_hash"])
    assert manager.ready_plan(store.connection, CID) == old
    store.append_record(CID, item={"title": "Two", "count": 2}, item_key=OTHER, why="observe")
    assert not store.backfill_query_indexes(CID, limit=1)
    assert store.backfill_query_indexes(CID, limit=1)
    published = manager.ready_plan(store.connection, CID)
    assert published == replacement
    assert store.connection.execute(f"SELECT item_key,row_version FROM {published.table_name} ORDER BY item_key").fetchall() == [(KEY, 1), (OTHER, 1)]
    assert store.connection.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 4


def test_failed_candidate_preserves_old_ready_values_and_audit(store):
    original = manifest_text().replace("title: {type: string, required: true}", "title: {type: string, required: true, sortable: true}").replace("count: {type: integer}", "count: {type: datetime}")
    store.create_collection(manifest_path(), original, why="create", scaffold=False)
    # File-mode accepts this original byte string, but typed instant indexes
    # cannot represent its sub-microsecond precision without changing it.
    instant = "2026-10-04T00:00:00.0000001Z"
    first = store.append_record(CID, item={"title": "One", "count": instant}, item_key=KEY, why="observe")
    manager, old = ready(store)
    revised = original.replace("count: {type: datetime}", "count: {type: datetime, filterable: true}")
    revise(store, original, revised, first["after_container_hash"])
    generation = store.connection.execute("SELECT generation FROM query_projection_mappings WHERE state='building'").fetchone()[0]
    before = store.connection.execute("SELECT * FROM txns").fetchall()
    with store.handle.transaction():
        assert not manager.backfill_batch(store.connection, CID, limit=1)
    assert manager.ready_plan(store.connection, CID) == old
    assert store.connection.execute("SELECT state FROM query_projection_mappings WHERE generation=?", (generation,)).fetchone() == ("failed",)
    assert store.connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (f"cq_{CID.replace('-', '')}_{generation}",)).fetchone() is None
    assert store.connection.execute("SELECT * FROM txns").fetchall() == before
    assert json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0]) == {"title": "One", "count": instant}


def test_removing_a_view_index_requires_revising_its_dependency_together(store):
    from exomem import structured_collections as collections
    from exomem.collection_store import tokens

    text = indexed_text().replace("\n---", "\nviews:\n  by_count:\n    query: {sort_by: count}\n---")
    created = store.create_collection(manifest_path(), text, why="create", scaffold=False)
    manager, old = ready(store)
    proposed = text.replace(", filterable: true", "")
    with pytest.raises(collections.CollectionError, match="INDEX_DEPENDENCY_REQUIRED"):
        store.revise_collection(CID, manifest_text=proposed,
                                expected_manifest_hash=tokens.manifest_hash(text),
                                expected_container_hash=created["after_container_hash"], why="revise")
    assert manager.ready_plan(store.connection, CID) == old
    assert store.connection.execute("SELECT COUNT(*) FROM txns").fetchone() == (1,)


def revise(store, before, after, container):
    from exomem.collection_store import tokens

    return store.revise_collection(CID, manifest_text=after,
                                  expected_manifest_hash=tokens.manifest_hash(before),
                                  expected_container_hash=container, why="revise")


def test_reverting_declaration_cancels_the_obsolete_rebuild(store):
    original = indexed_text()
    store.create_collection(manifest_path(), original, why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="observe")
    manager, old = ready(store)
    changed = original.replace("\n---", "\nindexes:\n  reverse:\n    keys: [{field: count, direction: desc}]\n---")
    pending = revise(store, original, changed, first["after_container_hash"])
    revise(store, changed, original, pending["after_container_hash"])
    with store.handle.transaction():
        assert not manager.backfill_batch(store.connection, CID)
    assert manager.ready_plan(store.connection, CID) == old
    assert store.connection.execute("SELECT 1 FROM query_projection_mappings WHERE state='building'").fetchone() is None


def test_integer_widening_keeps_indexed_writes_available_before_backfill(store):
    original = indexed_text()
    store.create_collection(manifest_path(), original, why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="observe")
    revised = original.replace("count: {type: integer", "count: {type: number")
    revise(store, original, revised, first["after_container_hash"])
    second = store.append_record(CID, item={"title": "Two", "count": 1.5}, item_key=OTHER, why="observe")
    assert second["outcome"] == "committed"
    manager, plan = ready(store)
    assert plan.scalars[0].kind == "number"
    assert store.connection.execute(f"SELECT COUNT(*) FROM {plan.table_name}").fetchone() == (2,)


def test_indexed_write_keeps_known_authorization_cache_state(store):
    store.create_collection(manifest_path(), indexed_text(), why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="observe")
    store.inspect_collection(CID)
    store.update_record(CID, item_key=KEY, changes={"count": 2}, why="correct",
                        expected_item_version=first["after_item_hash"],
                        expected_container_hash=first["after_container_hash"])
    assert not store.handle.release_cache.unmanaged
    assert store.handle.release_cache.states


def test_rebuild_requires_current_full_collection_authority(store):
    from test_governance_egress import _external, write_rule, write_scope

    from exomem import structured_collections as collections
    from exomem.governance.principal import request_scope

    original = indexed_text()
    store.create_collection(manifest_path(), original, why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="observe")
    changed = original.replace("\n---", "\nindexes:\n  reverse:\n    keys: [{field: count, direction: desc}]\n---")
    revise(store, original, changed, first["after_container_hash"])
    before = store.connection.execute("SELECT * FROM query_projection_mappings").fetchall()
    write_scope(store.root, paths=f"Records/Work/Items/{KEY}.md")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()), pytest.raises(collections.CollectionError):
        store.backfill_query_indexes(CID, limit=1)
    assert store.connection.execute("SELECT * FROM query_projection_mappings").fetchall() == before
