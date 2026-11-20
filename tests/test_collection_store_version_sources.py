"""New provenance can authenticate a retained legacy version without changing v1 citation hashes."""
# Imported fixture is requested by the test.
# ruff: noqa: F811
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from test_collection_store_writer import CID, KEY, manifest_path, manifest_text
from test_collection_store_writer import store as store  # noqa: F401

from exomem import memory_refs, provenance, source_custody, writer_lease
from exomem import structured_collections as collections
from exomem.collection_store import (
    edit_back,
    held_sources,
    history,
    source_dependencies,
    version_sources,
)
from exomem.governance.principal import library_scope


def test_backfilled_prior_source_keeps_legacy_hash_and_authenticates_its_actual_owner(store, monkeypatch):
    root, writer = store.root, store
    reference = memory_refs.memory_ref(memory_refs.new_id())
    source = root / "Knowledge Base/Sources/original.md"
    source.parent.mkdir(parents=True)
    source.write_text(f"---\ntype: source\nexomem_id: {reference.rsplit('/', 1)[1]}\n---\n\nOriginal observation.\n")
    carrier = provenance.encode_origin({"inputs": {"original": {"reference": reference,
        "version": provenance.evidence_version(source.read_text())}}, "assessments": [], "bindings": []})
    with library_scope():
        writer.create_collection(manifest_path(), manifest_text(), why="Declare publication records.")
        original_txn = type(writer)._txn

        def legacy_txn(current, *args, **kwargs):
            # Emulate the old writer once; the update must discover its retained origin itself.
            kwargs.pop("_source_inputs", None)
            return original_txn(current, *args, **kwargs)

        monkeypatch.setattr(type(writer), "_txn", legacy_txn)
        created = writer.append_record(CID, item={"title": "Retained record", "count": 1}, item_key=KEY,
            body="Original interpretation.\n" + carrier, sources=[source.relative_to(root).as_posix()],
            why="Record the original interpretation.")
        before = writer._item(CID, KEY)
        creator, old_hash = writer.connection.execute(
            "SELECT transition_id,event_hash FROM txns WHERE txn_id=?", (before["updated_txn"],)).fetchone()
        monkeypatch.setattr(type(writer), "_txn", original_txn)
        updated = writer.update_record(CID, item_key=KEY, changes={"count": 2}, body="Independent replacement.",
            expected_container_hash=created["after_container_hash"], expected_item_version=created["after_item_hash"],
            why="Correct the interpretation while retaining its history.")
        assert updated["outcome"] == "committed"
        operation = writer.connection.execute("SELECT dep_operation FROM version_source_bindings").fetchone()[0]
        binding = version_sources.Binding(effect_ordinal=0, item_key=KEY, row_version=1,
            payload_hash=before["payload_hash"], creation_transition=creator, canonical_ref=reference,
            dep_epoch=None, dep_operation=operation)
        assert writer.connection.execute("SELECT event_hash FROM txns WHERE transition_id=?", (creator,)).fetchone() == (old_hash,)
        assert writer.connection.execute("SELECT row_version,canonical_ref,dep_epoch FROM version_source_bindings").fetchall() == [
            (1, reference, None)]
        assert reference not in writer._item(CID, KEY)["body"]
        txn_cursor = writer.connection.execute("SELECT * FROM txns WHERE transition_id=?", (updated["audit_correlation"],))
        txn = dict(zip((column[0] for column in txn_cursor.description), txn_cursor.fetchone(), strict=True))
        assert txn["event_version"] == 2
        assert version_sources.material(version_sources.reconstruct(writer.connection, txn)) == [binding.model_dump(mode="json")]
        history.verify(writer.connection, CID)
        # A replacement path cannot change the authenticated incarnation of this prior owner.
        replacement_ref = memory_refs.memory_ref(memory_refs.new_id())
        source.write_text(source.read_text().replace(reference.rsplit('/', 1)[1], replacement_ref.rsplit('/', 1)[1]))
        history.verify_event(writer.connection, txn)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            writer.connection.execute("UPDATE version_source_bindings SET canonical_ref=?", (replacement_ref,))
        writer.connection.execute("DROP TRIGGER version_source_bindings_append_only_update")
        writer.connection.execute("UPDATE version_source_bindings SET canonical_ref=?", (replacement_ref,))
        with pytest.raises(history.chain.StoreChainError, match="event hash differs"):
            history.verify_event(writer.connection, txn)


def test_explicit_source_incarnation_survives_path_reuse_without_changing_ordered_citations(store):
    root, writer = store.root, store
    reference = memory_refs.memory_ref(memory_refs.new_id())
    replacement = memory_refs.memory_ref(memory_refs.new_id())
    source = root / "Knowledge Base/Sources/observation.md"
    source.parent.mkdir(parents=True)
    source.write_text(f"---\ntype: source\nexomem_id: {reference.rsplit('/', 1)[1]}\n---\n\nObservation.\n")
    path = source.relative_to(root).as_posix()
    with library_scope():
        writer.create_collection(manifest_path(), manifest_text(), why="Declare publication records.")
        created = writer.append_record(CID, item={"title": "Observation", "count": 1}, item_key=KEY,
            body="Interpretation.", sources=[path, path], why="Retain the resolved observation.")
        assert writer.connection.execute("SELECT ordinal,source_ref FROM item_sources ORDER BY ordinal").fetchall() == [
            (0, path), (1, path)]
        assert writer.connection.execute("SELECT row_version,canonical_ref FROM version_source_bindings").fetchall() == [
            (1, reference)]
        source.write_text(source.read_text().replace(reference.rsplit('/', 1)[1], replacement.rsplit('/', 1)[1]))
        updated = writer.update_record(CID, item_key=KEY, changes={"count": 2}, body="Independent replacement.",
            expected_container_hash=created["after_container_hash"], expected_item_version=created["after_item_hash"],
            why="Retain history after replacing the interpretation.")
        assert updated["outcome"] == "committed"
        assert writer.connection.execute("SELECT row_version,canonical_ref FROM version_source_bindings").fetchall() == [
            (1, reference)]
        assert writer.connection.execute(
            "SELECT dependency_current_complete,dependency_prior_complete FROM audit_effects ORDER BY txn_id"
        ).fetchall() == [(1, 1), (1, 1)]
        history.verify(writer.connection, CID)


def test_refused_candidate_has_exact_source_descriptor_and_fresh_replacement_publication(store):
    reference = memory_refs.memory_ref(memory_refs.new_id())
    body = "Unaccepted interpretation.\n" + provenance.encode_origin({"inputs": {"observation": {
        "reference": reference, "version": "1" * 64}}, "assessments": [], "bindings": []})
    with library_scope():
        store.create_collection(manifest_path(), manifest_text(), why="Declare publication records.")
        publications = []
        for _ in range(2):
            with pytest.raises(collections.CollectionError) as error:
                store.append_record(CID, item={"title": "Candidate", "count": "invalid"}, body=body,
                                    why="Retain the refused interpretation.")
            held = error.value.details["held"]["held_id"]
            candidate, raw, descriptor, path = store.connection.execute(
                "SELECT candidate_json,held_bytes,dependency_json,view_path FROM held_candidates WHERE held_id=?",
                (held,)).fetchone()
            descriptor = held_sources.Descriptor.model_validate_json(descriptor)
            assert descriptor.candidate_sha256 == hashlib.sha256(candidate.encode()).hexdigest()
            assert descriptor.held_sha256 == hashlib.sha256(raw).hexdigest()
            assert [binding.canonical_ref for binding in descriptor.bindings] == [reference]
            assert descriptor.complete is False
            publications.append(descriptor.publication_id)
            install = json.loads(store.connection.execute(
                "SELECT install_json FROM projection_state WHERE path=?", (path,)).fetchone()[0])
            assert install["render_context"]["source_dependency"] == descriptor.model_dump(mode="json")
        assert publications[0] != publications[1]


def test_changed_native_input_waits_without_creating_a_retained_copy(store):
    with library_scope():
        store.create_collection(manifest_path(), manifest_text(), why="Declare publication records.")
        created = store.append_record(CID, item={"title": "Observation", "count": 1}, item_key=KEY,
                                      body="Independent interpretation.", why="Record the interpretation.")
        store.reconcile_views()
        with store.read_snapshot():
            cursor = store.connection.execute("SELECT * FROM projection_state ORDER BY path")
            names = [column[0] for column in cursor.description]
            projections = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
            identity = dict(store.connection.execute("SELECT key,value FROM store_meta WHERE key IN ('store_id','instance_id')"))
        native = edit_back.dependency_inputs(store.root, projections)
        assert native.complete
        path = store._item(CID, KEY)["view_path"]
        target = store.root / path
        unexpected_ref = memory_refs.memory_ref(memory_refs.new_id())
        changed = target.read_text() + "\n" + provenance.encode_origin({"inputs": {"unexpected": {
            "reference": unexpected_ref, "version": "1" * 64}}, "assessments": [], "bindings": []})
        target.write_text(changed)
        store._dependency_plan = source_dependencies.DependencyPlan("detached-operation", identity["store_id"],
            identity["instance_id"], CID, (), native)
        try:
            result = store.update_record(CID, item_key=KEY, changes={"count": 2}, body="Independent correction.",
                expected_container_hash=created["after_container_hash"], expected_item_version=created["after_item_hash"],
                why="Correct the independent interpretation.")
        finally:
            store._dependency_plan = None
        assert result["outcome"] == "committed"
        assert target.read_text() == changed
        assert store.connection.execute("SELECT COUNT(*) FROM held_candidates").fetchone() == (0,)
        assert store.connection.execute("SELECT COUNT(*) FROM version_source_bindings").fetchone() == (0,)


def test_direct_record_writer_observes_retained_dependency_after_current_citation_is_removed(store):
    from test_origin_bindings import _write

    with library_scope():
        store.create_collection(manifest_path(), manifest_text(), why="Declare publication records.")
        binding = _write(store.root, "Retained observation.\n")
        manager = writer_lease.active_manager()
        with manager.mutation_guard(store.root, operation="enroll_test_source"):
            source_custody.publish(store.root, source_custody.prepare_enrollment(
                store.root, reference=binding["reference"], source_hash=binding["version"]))
        body = "Interpretation.\n" + provenance.encode_origin({"inputs": {"observation": binding},
            "assessments": [], "bindings": []})
        created = store.append_record(CID, item={"title": "Observation", "count": 1}, item_key=KEY,
                                      body=body, why="Retain the observation.")
        updated = store.update_record(CID, item_key=KEY, changes={"count": 2}, body="Independent correction.",
            expected_container_hash=created["after_container_hash"], expected_item_version=created["after_item_hash"],
            why="Replace the current interpretation.")
        assert updated["outcome"] == "committed"
        manifest, _ = source_custody.read_manifest(store.root, binding["reference"])
        assert manifest.dependency.first_publication.owner == "collection_records"
        assert manifest.dependency_epochs["collection_records"].revision == 4
        assert store.connection.execute("SELECT row_version,canonical_ref,dep_epoch FROM version_source_bindings").fetchall() == [
            (1, binding["reference"], manifest.dependency_epochs["collection_records"].epoch_id)]
        assert store._item(CID, KEY)["body"] == "Independent correction."
        assert not store.handle._closed
