"""File-mode curation receipts remain provable after migration and view regeneration."""

import json

import pytest
from test_collection_store_file_writers import (
    EPISODE,
    episode,
    migrated,
    prepare_and_route,
    record_an_episode,
    store_head,
)
from test_collection_store_migration import env as env
from test_collection_store_migration import stop_writer
from test_collection_store_service import OWNER, call
from test_episode_records_leaf import COLLECTION, READING, _collection

from exomem import command_surface, commands, episode_workflow, record_formats, records
from exomem import schema as schema_module
from exomem import structured_collections as collections
from exomem.collection_store import governance as canonical_governance
from exomem.collection_store import runtime
from exomem.collection_store.preview import bound_writer
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, request_scope
from exomem.init import init_vault


@pytest.mark.parametrize("kind", ["append-record", "update-record"])
def test_a_file_receipt_survives_migration_and_view_regeneration(env, capsys, monkeypatch, kind):
    """A historical witness must use the imported byte binding after its file hash disappears."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = env
    init_vault(root)
    _collection(root)
    with request_scope(OWNER):
        if kind == "update-record":
            commands.op_record_memory(root, action="append", collection=COLLECTION,
                                      item=READING, why="Record an observed reading.")
        guard = commands.op_record_memory(root, action="inspect", collection=COLLECTION)[
            "lifecycle_guards"]["expected_container_hash"]
        args = {"collection": COLLECTION, "why": "Record the episode's observation.",
                "expected_container_hash": guard}
        if kind == "append-record":
            args["item"] = dict(READING)
        else:
            manifest = collections.load_manifest(root, COLLECTION)
            (item,) = record_formats.load_adapter(root, manifest).read().records
            args.update(item_key=item.identity.key, changes={"temperature_c": 42},
                        expected_item_version=item.source.hash)
    record_an_episode(root)
    prepare_and_route(root, {
        "route": "records", "alternatives": [], "evidence": "complete", "title": "One reading",
        "reason": "Preserve an observed reading.",
        "leaves": [{"leaf_key": "reading", "effect_revision": 1, "kind": kind, "args": args}],
    })
    reviewed = episode(root, action="candidates")
    completed = episode(root, action="resume", input_revision=reviewed["input_revision"],
                        journal_digest=reviewed["journal_digest"])
    assert completed["status"] == "ok", completed
    assert [row["readback"] for row in episode(root, action="coverage")["receipts"]] == ["verified"]
    with request_scope(OWNER):
        manifest = collections.load_manifest(root, COLLECTION)
        (item,) = record_formats.load_adapter(root, manifest).read().records
    path = root / item.source.path
    original = path.read_bytes()
    stop_writer()
    service = migrated(root, capsys)
    try:
        head = store_head(root)
        # Regenerate a missing projection without changing the canonical row or version.
        path.unlink()

        def regenerate():
            with request_scope(OWNER), service.manager.mutation_guard(root, operation="publish-views"):
                bound_writer(root).reconcile_views()

        runtime._SERVERS[root.resolve()].call(regenerate)
        assert path.read_bytes() != original
        assert [row["readback"] for row in episode(root, action="coverage")["receipts"]] == ["verified"]
        assert store_head(root) == head
        current = call(root, "record_memory", action="query", collection=COLLECTION)["rows"]
        assert len(current) == 1 and current[0]["temperature_c"] == (41 if kind == "append-record" else 42)
        if kind == "update-record":
            # Reverting values does not restore the imported item version.
            for temperature in (43, 42):
                current = call(root, "record_memory", action="query", collection=COLLECTION)["rows"][0]
                guard = call(root, "record_memory", action="inspect", collection=COLLECTION)[
                    "lifecycle_guards"]["expected_container_hash"]
                call(root, "record_memory", action="update", collection=COLLECTION,
                     item_key=current["record_id"], changes={"temperature_c": temperature},
                     expected_container_hash=guard, expected_item_version=current["item_version"],
                     why="Correct the current observation.")
            head = store_head(root)
            covered = episode(root, action="coverage")
            assert [row["readback"] for row in covered["receipts"]] == ["changed"]
            command = next(command for command in commands.product_commands_for("mcp")
                           if command.name == "episode_memory")
            bound = command_surface.bind_vault(
                command.leaf, root, schema_module.load_source_schema(root), command=command)
            with request_scope(OWNER):
                assert bound(action="coverage", episode=EPISODE)["receipts"] == covered["receipts"]
                # A wire copy has no authority to name stale physical bytes.
                assert egress.postfilter("episode_memory", json.loads(json.dumps(covered)), root)["receipts"] == []
                evidence = canonical_governance._metadata_evidence(covered)
                receipt = covered["receipts"][0]
                unproven = {**receipt, "leaf_id": "unproven-leaf", "operation_id": "unproven-operation"}
                mixed = canonical_governance._seal_metadata_projection({
                    **covered, "receipts": [unproven, receipt],
                    "sink": [{"path": receipt["path"]}],
                }, evidence)
                cleaned = egress.postfilter("episode_memory", mixed, root)
                assert cleaned["receipts"] == [receipt]
                assert cleaned["sink"] == []
                assert egress.postfilter("episode_memory", cleaned, root)["receipts"] == [receipt]
                # Editing a carrier cannot retain its producer's whole-payload binding.
                covered["body"] = f"Read [[{receipt['path']}]] next."
                with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
                    egress.postfilter("episode_memory", covered, root)
                del covered["body"]
                covered["coverage_current"] = "verified"
                with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
                    egress.postfilter("episode_memory", covered, root)
                covered["coverage_current"] = "changed"
            with request_scope(RequestPrincipal(audience_id="another-reader", surface="mcp")):
                with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
                    egress.postfilter("episode_memory", covered, root)
            assert store_head(root) == head
    finally:
        service.stop()


def test_an_old_update_event_cannot_certify_different_bytes_imported_with_its_marker(env, capsys, monkeypatch):
    """A manual edit can retain a valid old marker; row version one alone cannot prove the witness."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    root = env
    init_vault(root)
    _collection(root)
    with request_scope(OWNER):
        commands.op_record_memory(root, action="append", collection=COLLECTION,
                                  item=READING, why="Record an observed reading.")
        manifest = collections.load_manifest(root, COLLECTION)
        (item,) = record_formats.load_adapter(root, manifest).read().records
        guard = commands.op_record_memory(root, action="inspect", collection=COLLECTION)[
            "lifecycle_guards"]["expected_container_hash"]
    record_an_episode(root)
    prepare_and_route(root, {
        "route": "records", "alternatives": [], "evidence": "complete", "title": "One correction",
        "reason": "Correct the observed reading.",
        "leaves": [{"leaf_key": "reading", "effect_revision": 1, "kind": "update-record", "args": {
            "collection": COLLECTION, "item_key": item.identity.key, "changes": {"temperature_c": 42},
            "why": "Correct an observed reading.", "expected_container_hash": guard,
            "expected_item_version": item.source.hash,
        }}],
    })
    reviewed = episode(root, action="candidates")
    completed = episode(root, action="resume", input_revision=reviewed["input_revision"],
                        journal_digest=reviewed["journal_digest"])
    assert completed["status"] == "ok", completed
    assert [row["readback"] for row in episode(root, action="coverage")["receipts"]] == ["verified"]
    with request_scope(OWNER):
        manifest = collections.load_manifest(root, COLLECTION)
        snapshot = record_formats.load_adapter(root, manifest).read()
        (item,) = snapshot.records
        marker = records._record_audit_correlation(manifest, snapshot, item)
    path = root / item.source.path
    path.write_text(record_formats.render_markdown_item_update(
        path.read_text(), {"temperature_c": 43}, audit_correlation=marker,
    ))
    stop_writer()
    service = migrated(root, capsys)
    try:
        head = store_head(root)
        # Make the imported current item readable through its canonical render.
        path.unlink()

        def regenerate():
            with request_scope(OWNER), service.manager.mutation_guard(root, operation="publish-views"):
                bound_writer(root).reconcile_views()

        runtime._SERVERS[root.resolve()].call(regenerate)
        covered = episode(root, action="coverage")
        assert [row["readback"] for row in covered["receipts"]] == ["changed"], covered
        assert store_head(root) == head
        rows = call(root, "record_memory", action="query", collection=COLLECTION)["rows"]
        assert len(rows) == 1 and rows[0]["temperature_c"] == 43
    finally:
        service.stop()
