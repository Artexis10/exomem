"""Canonical capture classification and exact receipts must survive partial publications."""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from uuid import uuid4

import numpy as np
import pytest
import yaml
from test_passage_evidence import passage_tools  # noqa: F401

from exomem import (
    capture_inventory,
    client_artifacts,
    embeddings,
    recall_space,
    semantic_contract,
    source_capture,
)
from exomem import vault as vault_module
from exomem.embedding_index import ordered_chunk_digest


@pytest.fixture(autouse=True)
def corpus_cache(monkeypatch):
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)


def captured_bytes(record="record", revision="one"):
    fields = {"type": "source", "title": "Stored record", "exomem_id": str(uuid4()),
              "source_identity": {"system": "archive", "record_id": record, "revision": revision},
              "capture_input_digest": "a" * 64}
    return ("---\n" + yaml.safe_dump(fields) + "---\n\n# Stored record\n\nOriginal body.\n").encode()


def test_a_raw_input_that_a_commit_replaced_during_its_read_is_read_again(vault, monkeypatch):
    # The inventory measures a raw input, then reads it. A commit that installed a new file with
    # os.replace in between failed the read as a changed guard instead of reading the new file.
    import os

    path = "Knowledge Base/Sources/Books/2026-05-03-elimination-diet.md"
    target = vault / path
    measured = target.lstat()
    newer = target.read_text(encoding="utf-8") + "\nA line a later commit added.\n"
    real_snapshot = vault_module._read_bounded_guarded_snapshot_tolerating_transient_sharing
    replaced: list[bool] = []

    def a_commit_replaces_the_file_after_the_snapshot(root, rel, limit):
        snapshot = real_snapshot(root, rel, limit)
        if not replaced:
            replaced.append(True)
            staged = target.with_name("staged.tmp")
            staged.write_text(newer, encoding="utf-8")
            os.replace(staged, target)
        return snapshot

    monkeypatch.setattr(
        vault_module, "_read_bounded_guarded_snapshot_tolerating_transient_sharing",
        a_commit_replaces_the_file_after_the_snapshot,
    )
    read = capture_inventory.read_input(vault, path, measured)

    assert read.signature == capture_inventory.file_signature(target.lstat())


def test_excluded_bound_original_cannot_forge_identity_and_owner_deletion_reclassifies(vault, monkeypatch):
    """An unchanged Markdown original becomes a capture only after its binding owner disappears."""
    original = "Knowledge Base/Sources/_archive/original.md"
    owner = client_artifacts._expected_companion_paths(PurePosixPath(original), lane="source")[0].as_posix()
    data = captured_bytes("forged")
    fields = {"type": "source", "evidence_file": original, "binary_sha256": hashlib.sha256(data).hexdigest(), "binary_size": len(data)}
    companion = ("---\n" + yaml.safe_dump(fields) + "---\n\n# Companion\n").encode()
    for path, content in ((original, data), (owner, companion)):
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    from exomem import freshness

    freshness.seed(vault, "vault", [(str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(vault)])
    context, _ = semantic_contract._build_corpus_context_with_census(vault)
    view = context.capture_inventory
    assert original not in context.pages
    assert original not in view.entries
    assert owner in view.canonical
    (vault / owner).unlink()
    reads = []
    real = semantic_contract._read_corpus_source
    monkeypatch.setattr(semantic_contract, "_read_corpus_source", lambda root, path: (reads.append(path), real(root, path))[1])
    semantic_contract.on_corpus_files_changed(vault, deleted=(owner,))
    changed = semantic_contract._build_corpus_context_with_census(vault)[0].capture_inventory
    assert changed.entries[original].key[:3] == ("archive", "forged", "one")
    assert vault / original not in reads
    assert original not in view.entries  # Readers retain their immutable generation.


def test_changed_target_invalidates_companion_and_exposes_incomplete_classification(vault):
    """A same-path replacement must not preserve a stale original binding."""
    original = "Knowledge Base/Sources/original.md"
    owner = client_artifacts._expected_companion_paths(PurePosixPath(original), lane="source")[0].as_posix()
    data = captured_bytes()
    fields = {"type": "source", "evidence_file": original, "binary_sha256": hashlib.sha256(data).hexdigest(), "binary_size": len(data)}
    companion = ("---\n" + yaml.safe_dump(fields) + "---\n").encode()
    inputs = {original: capture_inventory.input_from_bytes(vault, original, data), owner: capture_inventory.input_from_bytes(vault, owner, companion)}
    view = capture_inventory.CaptureInventory.build(vault, inputs)
    changed = view.with_delta({original: capture_inventory.input_from_bytes(vault, original, captured_bytes(revision="two"))})
    assert owner in changed.incomplete
    assert changed.entries[original].key[2] == "two"
    assert original not in view.entries


def test_cursor_scope_validation_and_namespace_authority_precede_disclosure(vault, monkeypatch):
    """Malformed and foreign-scope cursors never broaden an authorized inventory request."""
    import base64
    import json

    from exomem.cli_ops import OpError

    cursor = base64.urlsafe_b64encode(json.dumps({"scope": ["other"], "after": ["other", "r", "v", "p"]}).encode()).decode()
    from exomem.governance.principal import owner_principal, request_scope

    with request_scope(owner_principal()), pytest.raises(OpError, match="INVALID_CURSOR"):
        capture_inventory.inventory(vault, system="archive", cursor=cursor)
    monkeypatch.setattr(source_capture, "require_authority", lambda root: (_ for _ in ()).throw(source_capture.SourceCaptureError("SOURCE_IDENTITY_FORBIDDEN")))
    monkeypatch.setattr(semantic_contract, "borrow_capture_inventory", lambda root: pytest.fail("unauthorized inventory disclosure"))
    with pytest.raises(OpError, match="SOURCE_IDENTITY_FORBIDDEN"):
        capture_inventory.inventory(vault, system="archive")


def test_exact_receipts_survive_unrelated_publication_but_reject_same_count_wrong_text(vault):
    """Row counts cannot prove text; a logged unrelated publication must preserve exact authority."""
    index = embeddings.get_embedding_index(vault)
    identity = recall_space.current_identity(4, model="test-space")
    chunks = ["exact original"]
    path = "Knowledge Base/Sources/capture.md"
    expected = {path: (ordered_chunk_digest(chunks), 1)}
    index.upsert_batch([(path, chunks, np.zeros((1, 4)), 0)], identity=identity)
    assert index.chunk_inventory(expected)[path][:2] == ("indexed", 1)
    index.upsert_batch([("Knowledge Base/Sources/unrelated.md", ["other"], np.zeros((1, 4)), 0)], identity=identity)
    assert index.chunk_inventory(expected)[path][:2] == ("indexed", 1)
    index.upsert_batch([(path, ["wrong original"], np.zeros((1, 4)), 0)], identity=identity)
    assert index.chunk_inventory(expected)[path][:2] == ("not_yet_indexed", 1)
    index.purge_paths_if_present([path])
    assert index.chunk_inventory(expected)[path][0] == "unknown"
    assert index.reconstruct_chunk_receipt(path, chunks, space=identity.identity_digest)
    assert index.chunk_inventory(expected)[path][:2] == ("not_yet_indexed", 0)


def test_cold_receipts_cover_two_hundred_sources_and_constant_sql_work(vault, monkeypatch, record_property):
    """A 64-entry proof cache loses a full inventory page; N per-path reads grow with its size."""
    from exomem import embedding_index

    index = embeddings.get_embedding_index(vault)
    identity = recall_space.current_identity(4, model="test-space")
    replacements = [(f"Knowledge Base/Sources/{n}.md", [f"text {n}"], np.zeros((1, 4)), 0) for n in range(200)]
    index.upsert_batch(replacements, identity=identity)
    restarted = embedding_index.EmbeddingIndex(vault)
    expected = {path: (ordered_chunk_digest(chunks), len(chunks)) for path, chunks, *_ in replacements}
    assert {result[0] for result in restarted.chunk_inventory(expected).values()} == {"unknown"}
    for path, chunks, *_ in replacements:
        assert restarted.reconstruct_chunk_receipt(path, chunks, space=identity.identity_digest)
    import sqlite3

    original_connect = sqlite3.connect
    statements = []
    def traced(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(sqlite3, "connect", traced)
    counts = []
    for size in (2, 200):
        statements.clear()
        result = restarted.chunk_inventory(dict(list(expected.items())[:size]))
        assert {row[0] for row in result.values()} == {"indexed"}
        counts.append(len(statements))
    assert counts[0] == counts[1] and counts[0] > 0
    record_property("inventory_receipt_sql_statements_2_and_200", counts)
    with original_connect(index.path) as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT file_path, COUNT(*) FROM chunks WHERE file_path IN (SELECT value FROM json_each(?)) GROUP BY file_path", ('["Knowledge Base/Sources/1.md"]',)).fetchall()
    record_property("inventory_chunk_query_plan", plan)
    assert any("INDEX" in row[3] and "file_path" in row[3] for row in plan)


def test_public_inventory_revocation_before_terminal_release_refuses_disclosure(vault, passage_tools, monkeypatch):
    """Authority can disappear after selection; the public reply must not release captured identities."""
    from test_governance_egress import write_rule, write_scope

    from exomem.governance.principal import OWNER_AUDIENCE

    call, _encoder = passage_tools
    original = capture_inventory.inventory
    def revoked(root, **kwargs):
        result = original(root, **kwargs)
        write_scope(root, paths="Sources/**")
        write_rule(root, ceiling=0, audience=OWNER_AUDIENCE)
        return result
    monkeypatch.setattr(capture_inventory, "inventory", revoked)
    result = call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive"}})
    assert "SOURCE_IDENTITY_FORBIDDEN" in str(result)
    assert "items" not in result


def test_public_inventory_traverses_live_generations_and_exact_states_without_source_reads(vault, passage_tools, monkeypatch, record_property):
    """The public MCP inventory must paginate published captures without parsing Sources or encoding."""
    from exomem import file_watcher, semantic_drain

    call, encoder = passage_tools
    for revision in ("one", "two", "three"):
        saved = call("save_source", {"title": f"Revision {revision}", "source_kind": "session", "detail": "full",
                                     "options": {"source_identity": {"system": "archive", "record_id": "record", "revision": revision},
                                                 "passages": [{"key": "entry", "text": "The lantern burns oil."}]}})
        assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    watcher = file_watcher.FileWatcher(vault)
    watcher._raw_witness.activate()  # This test drives the existing watcher synchronously.
    semantic_contract.warm_corpus_context(vault)
    try:
        assert watcher._flush_raw(census=True)
        before = len(encoder.texts)
        canonical_read = capture_inventory.read_canonical
        monkeypatch.setattr(capture_inventory, "read_canonical", lambda *args: pytest.fail("request read a Source body"))
        monkeypatch.setattr(semantic_contract, "_read_corpus_source", lambda *args: pytest.fail("request parsed a Source"))
        first = call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive", "limit": 2}})
        assert first["complete"], first
        assert len(first["items"]) == 2
        assert {item["index_state"] for item in first["items"]} == {"indexed"}
        second = call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive", "limit": 2, "cursor": first["next_cursor"]}})
        assert second["complete"]
        assert len(second["items"]) == 1
        assert second["generation"] == first["generation"]
        assert second["next_cursor"] is None
        assert len(encoder.texts) == before
        monkeypatch.setattr(capture_inventory, "read_canonical", canonical_read)
        index = embeddings.get_embedding_index(vault)
        index._chunk_receipts.clear()
        drain = semantic_drain.SemanticDrain(vault)
        monkeypatch.setitem(semantic_drain._ACTIVE, semantic_drain._key(vault), drain)
        cold = call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive"}})
        assert {item["index_state"] for item in cold["items"]} == {"unknown"}
        for _ in range(3):
            drain._turn()
        recovered = call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive"}})
        assert {item["index_state"] for item in recovered["items"]} == {"indexed"}
        assert len(encoder.texts) == before
        watcher._raw_witness.observe("Knowledge Base/Sources/unpublished.md")
        pending = call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive"}})
        assert pending["state"] == "pending"
        assert not pending["complete"]
        record_property("inventory_public_file_source_reads", 0)
    finally:
        watcher.stop()


def test_raw_directory_moves_excluded_and_sync_conflict_pages_without_semantic_fanout(vault, monkeypatch):
    """Directory-only events must publish every raw child, including semantically excluded Markdown."""
    from exomem import file_watcher
    from exomem.governance.principal import owner_principal, request_scope

    watcher = file_watcher.FileWatcher(vault)
    watcher._raw_witness.activate()  # This test drives the existing watcher synchronously.
    semantic_contract.warm_corpus_context(vault)
    try:
        assert watcher._flush_raw(census=True)
        folder = vault / "Knowledge Base/Sources/_archive/incoming"
        folder.mkdir(parents=True)
        (folder / "entry.sync-conflict-example.MD").write_bytes(captured_bytes("directory"))
        watcher._note_directories(str(folder))
        with request_scope(owner_principal()):
            assert not capture_inventory.inventory(vault, system="archive")["complete"]
        parsed = semantic_contract._parse_page_state
        def checked_parse(root, path, *args, **kwargs):
            assert "_archive" not in path and ".sync-conflict-" not in path
            return parsed(root, path, *args, **kwargs)
        monkeypatch.setattr(semantic_contract, "_parse_page_state", checked_parse)
        assert watcher._flush_raw()
        with request_scope(owner_principal()):
            first = capture_inventory.inventory(vault, system="archive")
        assert [item["source_identity"]["record_id"] for item in first["items"]] == ["directory"]
        destination = folder.with_name("renamed")
        folder.rename(destination)
        watcher._note_directories(str(folder), str(destination))
        assert watcher._flush_raw()
        with request_scope(owner_principal()):
            moved = capture_inventory.inventory(vault, system="archive")
        assert moved["complete"] and len(moved["items"]) == 1
        assert moved["generation"] != first["generation"]
        (destination / "entry.sync-conflict-example.MD").unlink()
        destination.rmdir()
        watcher._note_directories(str(destination))
        assert watcher._flush_raw()
        with request_scope(owner_principal()):
            empty = capture_inventory.inventory(vault, system="archive")
        assert empty["complete"] and empty["items"] == []
    finally:
        watcher.stop()


@pytest.mark.parametrize("census", [False, True])
def test_delivered_same_signature_edit_and_event_during_publication_remain_pending(vault, monkeypatch, census):
    """A late event must survive an older batch's acknowledgement, even if signatures collide."""
    from exomem import file_watcher
    from exomem.governance.principal import owner_principal, request_scope

    path = vault / "Knowledge Base/Sources/_archive/entry.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(captured_bytes(revision="one"))
    monkeypatch.setattr(capture_inventory, "file_signature", lambda info: (1, 2, 3))
    watcher = file_watcher.FileWatcher(vault)
    watcher._raw_witness.activate()  # This test drives the existing watcher synchronously.
    semantic_contract.warm_corpus_context(vault)
    try:
        assert watcher._flush_raw(census=True)
        path.write_bytes(captured_bytes(revision="two"))
        watcher._record(path, deleted=False)
        original = capture_inventory.refreshed
        def late_event(*args, **kwargs):
            result = original(*args, **kwargs)
            watcher._record(path, deleted=False)
            return result
        with monkeypatch.context() as patch:
            patch.setattr(capture_inventory, "refreshed", late_event)
            assert watcher._flush_raw(census=census)
        with request_scope(owner_principal()):
            pending = capture_inventory.inventory(vault, system="archive")
        assert not pending["complete"]
        assert watcher._flush_raw()
        with request_scope(owner_principal()):
            current = capture_inventory.inventory(vault, system="archive")
        assert current["complete"]
        assert current["items"][0]["source_identity"]["revision"] == "two"
    finally:
        watcher.stop()


def test_raw_reseed_restart_missed_events_and_alias_failure_withdraw_authority(vault, monkeypatch):
    """Only a complete live census can restore absence proof after a missed event or watcher replacement."""
    from exomem import file_watcher
    from exomem.governance.principal import owner_principal, request_scope

    watcher = file_watcher.FileWatcher(vault)
    watcher._raw_witness.activate()  # This test drives the existing watcher synchronously.
    semantic_contract.warm_corpus_context(vault)
    try:
        assert watcher._flush_raw(census=True)
        with request_scope(owner_principal()):
            initial = capture_inventory.inventory(vault, system="archive")
        path = vault / "Knowledge Base/Sources/_archive/missed.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(captured_bytes("missed"))
        assert watcher._flush_raw(census=True)
        with request_scope(owner_principal()):
            healed = capture_inventory.inventory(vault, system="archive")
        assert len(healed["items"]) == 1
        # A census that finds unannounced drift must advance the raw generation too.
        assert healed["generation"] != initial["generation"]
        link = path.parent / "alias"
        link.symlink_to(path.parent, target_is_directory=True)
        assert not watcher._flush_raw(census=True)
        with request_scope(owner_principal()):
            assert not capture_inventory.inventory(vault, system="archive")["complete"]
        link.unlink()
        assert watcher._flush_raw(census=True)
        watcher.stop()
        watcher = file_watcher.FileWatcher(vault)
        watcher._raw_witness.activate()  # The replacement publisher remains unseeded.
        with request_scope(owner_principal()):
            assert not capture_inventory.inventory(vault, system="archive")["complete"]
        assert watcher._flush_raw(census=True)
        with request_scope(owner_principal()):
            restarted = capture_inventory.inventory(vault, system="archive")
        assert restarted["generation"] != healed["generation"]
    finally:
        watcher.stop()


def test_public_file_inventory_has_constant_sql_for_two_and_two_hundred(vault, passage_tools, monkeypatch, record_property):
    """The MCP boundary must keep database work constant as an inventory page reaches its limit."""
    import sqlite3

    from exomem import file_watcher

    call, encoder = passage_tools
    saved = call("save_source", {"title": "Inventory reference", "source_kind": "session", "detail": "full",
                                 "options": {"source_identity": {"system": "inventory-reference", "record_id": "record-000", "revision": "one"},
                                             "passages": [{"key": "entry", "text": "The lantern burns oil."}]}})
    from exomem import page_names
    from exomem.governance.principal import owner_principal, request_scope

    with request_scope(owner_principal()):
        path = vault / page_names.resolve_page(vault, saved["source"]["id"])
    fields, body, _ = vault_module.parse_frontmatter(path.read_text())
    for number in range(1, 200):
        fields = {**fields, "exomem_id": str(uuid4()), "source_identity": {"system": "inventory-reference", "record_id": f"record-{number:03}", "revision": "one"}}
        path.with_name(f"inventory-{number:03}.md").write_text("---\n" + yaml.safe_dump(fields) + "---\n" + body)
    embeddings.index_incremental(vault)
    watcher = file_watcher.FileWatcher(vault)
    watcher._raw_witness.activate()  # This test drives the existing watcher synchronously.
    semantic_contract.warm_corpus_context(vault)
    try:
        assert watcher._flush_raw(census=True)
        statements = []
        original = sqlite3.connect
        def traced(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        monkeypatch.setattr(sqlite3, "connect", traced)
        monkeypatch.setattr(capture_inventory, "read_canonical", lambda *args: pytest.fail("inventory read Source bodies"))
        monkeypatch.setattr(semantic_contract, "_read_corpus_source", lambda *args: pytest.fail("inventory parsed a Source"))
        before = len(encoder.texts)
        counts = []
        for limit in (2, 200):
            statements.clear()
            reply = call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "inventory-reference", "limit": limit}})
            assert reply["complete"] and len(reply["items"]) == limit, reply
            assert {item["index_state"] for item in reply["items"]} == {"indexed"}
            counts.append(len(statements))
        assert counts[0] == counts[1], counts
        assert len(encoder.texts) == before
        record_property("inventory_public_file_all_sql_statements_2_and_200", counts)
        record_property("inventory_public_file_source_reads_2_and_200", [0, 0])
    finally:
        watcher.stop()


def test_excluded_canonical_self_writes_publish_despite_suppressed_echoes(vault, monkeypatch):
    """A suppressed watchdog echo must not leave a canonical excluded capture absent from inventory."""
    from exomem import file_watcher
    from exomem.governance.principal import owner_principal, request_scope

    watcher = file_watcher.FileWatcher(vault)
    watcher._raw_witness.activate()  # This test drives the existing watcher synchronously.
    semantic_contract.warm_corpus_context(vault)
    try:
        assert watcher._flush_raw(census=True)
        path = vault / "Knowledge Base/Sources/_archive/self-written.MD"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(captured_bytes("self-written"))
        original = semantic_contract._parse_page_state
        def no_excluded(root, relative, *args, **kwargs):
            assert "_archive" not in relative
            return original(root, relative, *args, **kwargs)
        monkeypatch.setattr(semantic_contract, "_parse_page_state", no_excluded)
        file_watcher.register_self_write(vault, [path])
        with request_scope(owner_principal()):
            reply = capture_inventory.inventory(vault, system="archive")
        assert reply["complete"] and len(reply["items"]) == 1
        watcher._record(path, deleted=False)
        assert watcher._flush_raw()
        path.unlink()
        file_watcher.register_self_delete(vault, [path.relative_to(vault).as_posix()])
        with request_scope(owner_principal()):
            deleted = capture_inventory.inventory(vault, system="archive")
        assert deleted["complete"] and deleted["items"] == []
    finally:
        watcher.stop()


def test_raw_directory_publication_waits_for_active_child_intent_and_recovers_abort(vault):
    """A directory event cannot bypass an active child's exact-byte publication custody."""
    from test_watcher_publication_intents import _register_active_intent

    from exomem import file_watcher
    from exomem.governance.principal import owner_principal, request_scope

    watcher = file_watcher.FileWatcher(vault)
    watcher._raw_witness.activate()  # This test drives the existing watcher synchronously.
    semantic_contract.warm_corpus_context(vault)
    try:
        assert watcher._flush_raw(census=True)
        path = vault / "Knowledge Base/Sources/_archive/batch/child.md"
        intent = _register_active_intent(vault, path, captured_bytes("child"))
        file_watcher.begin_publication_installation([intent])
        file_watcher.mark_publication_installed([intent])
        watcher._note_directories(str(path.parent))
        assert watcher._flush_raw()
        with request_scope(owner_principal()):
            held = capture_inventory.inventory(vault, system="archive")
        assert not held["complete"]
        file_watcher.abort_publication_intents([intent])
        assert watcher._flush_raw()
        with request_scope(owner_principal()):
            settled = capture_inventory.inventory(vault, system="archive")
        assert settled["complete"] and len(settled["items"]) == 1
        assert settled["items"][0]["source_identity"]["record_id"] == "child"
    finally:
        watcher.stop()


def test_receipt_cannot_prove_rows_after_active_space_replacement(vault):
    """A dimension-preserving space replacement must invalidate an earlier exact chunk receipt."""
    from dataclasses import replace

    index = embeddings.get_embedding_index(vault)
    identity = recall_space.current_identity(4, model="original-space")
    path = "Knowledge Base/Sources/capture.md"
    chunks = ["The lantern burns oil."]
    index.upsert_batch([(path, chunks, np.zeros((1, 4)), 0)], identity=identity)
    expected = {path: (ordered_chunk_digest(chunks), 1)}
    assert index.chunk_inventory(expected)[path][0] == "indexed"
    replacement = replace(identity, model="replacement-space")
    with index._connect() as connection:
        recall_space.write_identity(connection, replacement)
    result = index.chunk_inventory(expected)[path]
    assert result == ("unknown", None, replacement.identity_digest)


def test_public_inventory_cannot_prove_absence_when_raw_lane_becomes_a_file(vault, passage_tools):
    """A non-directory raw root must withdraw inventory authority instead of erasing its captures."""
    from exomem import file_watcher

    call, _encoder = passage_tools
    saved = call("save_source", {"title": "Captured revision", "source_kind": "session", "detail": "full",
                                 "options": {"source_identity": {"system": "archive", "record_id": "record", "revision": "one"},
                                             "passages": [{"key": "entry", "text": "The lantern burns oil."}]}})
    assert "error" not in saved, saved
    watcher = file_watcher.FileWatcher(vault)
    watcher._raw_witness.activate()  # This test drives the existing watcher synchronously.
    semantic_contract.warm_corpus_context(vault)
    try:
        assert watcher._flush_raw(census=True)
        before = call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive"}})
        assert before["complete"] and len(before["items"]) == 1
        raw_root = vault / "Knowledge Base/Sources"
        raw_root.rename(vault / "removed-sources")
        raw_root.write_text("A file replaced the raw lane directory.\n")
        watcher._note_directories(str(raw_root))
        watcher._flush_raw()
        after = call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive"}})
        assert after["state"] == "pending"
        assert not after["complete"]
    finally:
        watcher.stop()
