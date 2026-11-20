"""Inventory uses canonical store bytes when Markdown projections do not exist."""
# Imported fixtures are requested by the test arguments.
# ruff: noqa: F811
from __future__ import annotations

import sys
from contextvars import copy_context

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_embeddings import catch_up, encoder  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401
from test_vault_store_write_path import served  # noqa: F401

from exomem import commands, embeddings, schema, semantic_contract, semantic_drain, source_capture
from exomem.governance.principal import owner_principal, request_scope
from exomem.vault_store import read

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only")


@pytest.fixture(autouse=True)
def corpus_cache(monkeypatch):
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)


def test_repeated_capture_reuses_unprojected_canonical_source(served, encoder):
    inputs = dict(
        title="Stored receipt", source_kind="session",
        source_identity={"system": "archive", "record_id": "receipt", "revision": "one"},
        passages=[{"key": "entry", "text": "The lantern burns oil."}],
    )
    first = commands.op_capture_source(served, schema.load_source_schema(served), **inputs)
    assert not (served / first["source"]["path"]).exists()
    second = commands.op_capture_source(served, schema.load_source_schema(served), **inputs)
    assert first["capture_outcome"] == "created"
    assert second["capture_outcome"] == "reused"
    assert second["source"]["ref"] == first["source"]["ref"]
    classified = source_capture.canonical_pages(served)
    with read.snapshot(served) as view:
        assert classified.store_revision == view.revision
    assert first["source"]["path"] in classified.canonical


def test_overlapping_capture_requests_recheck_after_both_read_snapshots_open(served, encoder):
    """Both requests start before the first commit; the second mutation must see that commit."""
    inputs = dict(
        title="Shared receipt", source_kind="session",
        source_identity={"system": "archive", "record_id": "overlapping", "revision": "one"},
        passages=[{"key": "entry", "text": "The lantern burns oil."}],
    )
    first_context, second_context = copy_context(), copy_context()
    first_scope, second_scope = read.request_snapshot(served), read.request_snapshot(served)
    first_context.run(first_scope.__enter__)
    second_context.run(second_scope.__enter__)
    try:
        first = first_context.run(commands.op_capture_source, served, schema.load_source_schema(served), **inputs)
        second = second_context.run(commands.op_capture_source, served, schema.load_source_schema(served), **inputs)
        assert first["capture_outcome"] == "created" and second["capture_outcome"] == "reused"
        assert second["source"]["ref"] == first["source"]["ref"]
    finally:
        second_context.run(second_scope.__exit__, None, None, None)
        first_context.run(first_scope.__exit__, None, None, None)


def test_bound_inventory_traverses_stored_revisions_and_recovers_without_encoding(served, encoder, writer, monkeypatch, record_property):
    """A cold index needs proof-only recovery without mutation debt or projected files."""
    paths = []
    for revision in ("one", "two", "three"):
        saved = commands.op_capture_source(served, schema.load_source_schema(served), title=f"Revision {revision}", source_kind="session",
                                           source_identity={"system": "archive", "record_id": "stored", "revision": revision},
                                           passages=[{"key": "entry", "text": "The lantern burns oil."}])
        paths.append(saved["source"]["path"])
    catch_up(served)
    semantic_contract.warm_corpus_context(served)
    assert all(not (served / path).exists() for path in paths)
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "inspect_captured_sources")
    bound = commands.bind_vault(command.leaf, served, command=command)
    def call(**arguments):
        with request_scope(owner_principal()):
            result = bound(system="archive", record_id="stored", **arguments)
        return result.get("result", result)
    before = len(encoder.texts)
    reply = call(limit=2)
    assert reply["complete"], reply
    assert len(reply["items"]) == 2
    assert {item["index_state"] for item in reply["items"]} == {"indexed"}
    tail = call(limit=2, cursor=reply["next_cursor"])
    assert len(tail["items"]) == 1
    assert tail["next_cursor"] is None
    assert tail["generation"] == reply["generation"]
    index = embeddings.get_embedding_index(served)
    index._chunk_receipts.clear()  # Simulate loss of volatile receipts on restart.
    drain = semantic_drain.SemanticDrain(served)
    monkeypatch.setitem(semantic_drain._ACTIVE, semantic_drain._key(served), drain)
    cold = call()
    assert {item["index_state"] for item in cold["items"]} == {"unknown"}
    for _ in paths:
        drain._turn()
    reads = []
    original = read.StoreSnapshot.page
    monkeypatch.setattr(read.StoreSnapshot, "page", lambda view, path: (reads.append(path), original(view, path))[1])
    recovered = call()
    assert {item["index_state"] for item in recovered["items"]} == {"indexed"}
    assert len(encoder.texts) == before
    assert reads == []
    record_property("inventory_store_request_source_reads", len(reads))
    from exomem.embedding_index import StoreStamp

    with read.snapshot(served) as view:
        metadata = view.page_meta(paths[0])
    index.record_store_stamps({paths[0]: StoreStamp(metadata.sha256, metadata.revision, True)})
    # Choose by the revision held at paths[0], not by its sort position.
    matching = [item for item in call()["items"] if item["index_state"] == "failed"]
    assert len(matching) == 1
    assert matching[0]["failure"]["source_hash"] == metadata.sha256
    assert matching[0]["indexed_chunks"] is None
    index.record_store_stamps({paths[0]: StoreStamp("b" * 64, metadata.revision, True)})
    assert {item["index_state"] for item in call()["items"]} == {"indexed"}
    saved = commands.op_capture_source(served, schema.load_source_schema(served), title="Later revision", source_kind="session",
                                       source_identity={"system": "archive", "record_id": "stored", "revision": "four"},
                                       passages=[{"key": "entry", "text": "The lantern needs oil."}])
    assert not (served / saved["source"]["path"]).exists()
    semantic_contract.warm_corpus_context(served)
    advanced = call()
    assert advanced["complete"] and len(advanced["items"]) == 4
    assert advanced["generation"] != recovered["generation"]


def test_store_bound_inventory_has_constant_sql_and_zero_source_reads_for_two_and_two_hundred(
    served, encoder, writer, monkeypatch, record_property
):
    """The actual command dispatcher must not turn a larger page into per-Source queries."""
    import sqlite3
    from contextlib import contextmanager
    from uuid import uuid4

    import yaml

    from exomem import vault as vault_module
    from exomem.vault_store import write

    saved = commands.op_capture_source(served, schema.load_source_schema(served), title="Inventory reference", source_kind="session",
                                       source_identity={"system": "inventory-reference", "record_id": "record-000", "revision": "one"},
                                       passages=[{"key": "entry", "text": "The lantern burns oil."}])
    path = saved["source"]["path"]
    with read.snapshot(served) as view:
        template = view.page(path).body.decode()
    fields, body, _ = vault_module.parse_frontmatter(template)
    parent = path.rsplit("/", 1)[0]
    puts = []
    for number in range(1, 200):
        fields = {**fields, "exomem_id": str(uuid4()), "source_identity": {"system": "inventory-reference", "record_id": f"record-{number:03}", "revision": "one"}}
        puts.append(write.PagePut(f"{parent}/inventory-{number:03}.md", ("---\n" + yaml.safe_dump(fields) + "---\n" + body).encode()))
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "reference", puts=tuple(puts)))
    catch_up(served)
    semantic_contract.warm_corpus_context(served)
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "inspect_captured_sources")
    bound = commands.bind_vault(command.leaf, served, command=command)
    statements = []
    connected = sqlite3.connect
    snapshot = read.snapshot
    def traced_connect(*args, **kwargs):
        connection = connected(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    @contextmanager
    def traced_snapshot(root):
        with snapshot(root) as view:
            view.connection.set_trace_callback(statements.append)
            yield view
    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    monkeypatch.setattr(read, "snapshot", traced_snapshot)
    monkeypatch.setattr(read.StoreSnapshot, "page", lambda *args: pytest.fail("inventory hydrated a Source"))
    monkeypatch.setattr(read.StoreSnapshot, "bodies", lambda *args: pytest.fail("inventory hydrated Source bodies"))
    counts = []
    for limit in (2, 200):
        statements.clear()
        with request_scope(owner_principal()):
            result = bound(system="inventory-reference", limit=limit)
        result = result.get("result", result)
        assert result["complete"], result
        assert len(result["items"]) == limit
        assert {item["index_state"] for item in result["items"]} == {"indexed"}
        counts.append(len(statements))
    record_property("inventory_store_bound_all_sql_statements_2_and_200", counts)
    record_property("inventory_store_bound_source_reads_2_and_200", [0, 0])
    assert counts[0] == counts[1], counts


def test_slow_inventory_proof_yields_a_turn_to_real_store_mutation_debt(served, encoder, monkeypatch):
    """A backlog of expensive cold proofs must not postpone newly captured Source indexing indefinitely."""
    from exomem import capture_inventory
    from exomem.governance.principal import library_scope

    for record in ("older-a", "older-b"):
        commands.op_capture_source(served, schema.load_source_schema(served), title=record, source_kind="session",
                                   source_identity={"system": "fairness", "record_id": record, "revision": "one"},
                                   passages=[{"key": "entry", "text": "The lantern burns oil."}])
    catch_up(served)
    index = embeddings.get_embedding_index(served)
    index._chunk_receipts.clear()
    saved = commands.op_capture_source(served, schema.load_source_schema(served), title="Newest Source", source_kind="session",
                                       source_identity={"system": "fairness", "record_id": "newest", "revision": "one"},
                                       passages=[{"key": "entry", "text": "The keeper orders oil."}])
    semantic_contract.warm_corpus_context(served)
    drain = semantic_drain.SemanticDrain(served)
    monkeypatch.setitem(semantic_drain._ACTIVE, semantic_drain._key(served), drain)
    with request_scope(owner_principal()):
        initial = capture_inventory.inventory(served, system="fairness")
    assert {item["index_state"] for item in initial["items"]} == {"unknown"}
    original = index.reconstruct_chunk_receipt
    clock = semantic_drain.time.monotonic
    elapsed = [0.0]
    def slow_proof(*args, **kwargs):
        result = original(*args, **kwargs)
        elapsed[0] += semantic_drain.TURN_OVERHEAD_SECONDS + 0.1
        return result
    monkeypatch.setattr(index, "reconstruct_chunk_receipt", slow_proof)
    monkeypatch.setattr(semantic_drain.time, "monotonic", lambda: clock() + elapsed[0])
    with library_scope():
        drain._turn()
        drain._turn()
    with request_scope(owner_principal()):
        current = capture_inventory.inventory(served, system="fairness")
    newest = next(item for item in current["items"] if item["source_identity"]["record_id"] == "newest")
    assert newest["index_state"] == "indexed"
    assert not (served / saved["source"]["path"]).exists()
