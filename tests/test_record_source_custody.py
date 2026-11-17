"""Retained record history keeps its Source incarnation after the current citation disappears."""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811
from __future__ import annotations

import json

import pytest
from record_fixtures import LEDGER_COLLECTION_PATH, ledger_item, setup_ledger_collection
from test_capture_attempts import attempts, authored, reserve  # noqa: F401
from test_origin_bindings import _write
from test_records_bulk_upsert import _hash

from exomem import memory_refs, provenance, record_formats, record_history, records, source_custody
from exomem import vault as vault_module
from exomem.governance.principal import owner_principal, request_scope


def test_partial_rollback_cannot_publish_a_source_from_the_failed_replacement(vault, attempts, monkeypatch):
    """An independent prior snapshot must not invent dependence on the failed replacement's input."""
    manager, _ = attempts
    setup_ledger_collection(vault)
    source_path = "Knowledge Base/Sources/new-input.md"
    binding = _write(vault, "New retained input.\n", relative=source_path)
    reference = binding["reference"]
    carrier = provenance.encode_origin({"inputs": {"new": binding}, "assessments": [], "bindings": []})
    with request_scope(owner_principal(surface="mcp")):
        created = records.append_record(vault, LEDGER_COLLECTION_PATH, item=ledger_item(),
            body="Independent prior body.\n", expected_container_hash=_hash(vault), why="Record the independent observation.")
        manifest = records.collections.load_manifest(vault, vault / LEDGER_COLLECTION_PATH)
        prior = record_formats.load_adapter(vault, manifest).read().records[0]
        with manager.mutation_guard(vault):
            source_custody.publish(vault, source_custody.prepare_enrollment(vault, reference=reference,
                source_hash=vault_module.content_hash((vault / source_path).read_text())))
        after_publish = vault_module._after_batch_destination_published
        unlink = vault_module._BatchWorkspace.unlink_installed
        history_root = vault / record_history.history_root()
        history_paths = []

        def fail_after_history(path):
            after_publish(path)
            if path.is_relative_to(history_root):
                history_paths.append(path)
                raise OSError("fault after history publication")

        def fail_history_inverse(workspace, final, *args, **kwargs):
            if final.is_relative_to(history_root):
                raise OSError("fault unlinking history during rollback")
            return unlink(workspace, final, *args, **kwargs)

        monkeypatch.setattr(vault_module, "_after_batch_destination_published", fail_after_history)
        monkeypatch.setattr(vault_module._BatchWorkspace, "unlink_installed", fail_history_inverse)
        with pytest.raises(vault_module.BatchWriteError, match="BATCH_ROLLBACK_INCOMPLETE"):
            records.update_record(vault, LEDGER_COLLECTION_PATH, item_key=created["item_key"],
                changes={"word_count": 43}, body="New interpretation.\n" + carrier,
                expected_container_hash=_hash(vault), expected_item_version=prior.source.hash,
                why="Interpret the new input.")
        assert reference not in (vault / prior.source.path).read_text()
        assert len(history_paths) == 1
        retained = json.loads(history_paths[0].read_text())
        assert reference not in retained["prior"]["body"]
        custody, _ = source_custody.read_manifest(vault, reference)
        assert custody.dependency.state == "uncertain"


def test_episode_origin_binds_the_retained_source_not_its_group_digest(vault, attempts):
    """An episode's grouping digest is not a Source incarnation or a reusable erasure target."""
    from exomem import note
    from exomem.episode_recovery import EpisodeInputOwner

    manager, _ = attempts
    setup_ledger_collection(vault)
    first_path, second_path = "Knowledge Base/Sources/first.md", "Knowledge Base/Sources/second.md"
    first = _write(vault, "First retained recap.\n", relative=first_path,
                   metadata="source_type: episode\nepisode: shared-survey\n")
    second = _write(vault, "Second retained recap.\n", relative=second_path,
                    metadata="source_type: episode\nepisode: shared-survey\n", identity=memory_refs.new_id())
    with request_scope(owner_principal(surface="mcp")):
        owner = EpisodeInputOwner(vault)
        owner.bind_committed_input("shared-survey", path=first_path, reference=first["reference"])
        owner.bind_committed_input("shared-survey", path=second_path, reference=second["reference"])
        with manager.mutation_guard(vault):
            source_custody.publish(vault, source_custody.prepare_enrollment(vault, reference=second["reference"],
                source_hash=vault_module.content_hash((vault / second_path).read_text())))
        carrier = provenance.encode_origin({"inputs": {"original": second}, "assessments": [], "bindings": []})
        result = records.append_record(vault, LEDGER_COLLECTION_PATH, item=ledger_item(),
            body="An interpretation of the retained recap.\n" + carrier, why="Record the interpretation.",
            expected_container_hash=_hash(vault))
        assert result["outcome"] == "committed"
        custody, _ = source_custody.read_manifest(vault, second["reference"])
        assert custody.dependency.state == "present"
        event = next(event for event in records._audit_events(vault, collection_id=result["collection_id"]).events
                     if event["transition_id"] == result["audit_correlation"])
        assert [binding["source_ref"] for binding in event["dependency_bindings"]] == [second["reference"]]
        note.note(vault, content=carrier + "\n\nA retained interpretation.\n", note_type="insight",
                  title="Retained recap interpretation", status="draft")
        custody, _ = source_custody.read_manifest(vault, second["reference"])
        assert custody.reservations["semantic_writes"].source_ref == second["reference"]


@pytest.mark.parametrize("remove_origin", [False, True])
def test_updating_a_legacy_origin_keeps_the_historical_dependency(vault, attempts, remove_origin):
    """Unchanged authoring preparation cannot hide the origin copied into a new history entry."""
    manager, call = attempts
    setup_ledger_collection(vault)
    captured = call("capture_source", **authored())
    text = (vault / captured["source"]["path"]).read_text()
    reference = memory_refs.ref_from_markdown(text)
    carrier = provenance.encode_origin({"inputs": {"original": {"reference": reference,
        "version": provenance.evidence_version(text)}}, "assessments": [], "bindings": []})
    with request_scope(owner_principal(surface="mcp")):
        appended = records.append_record(vault, LEDGER_COLLECTION_PATH, item=ledger_item(),
            body="Retained interpretation.\n" + carrier, why="Record the original interpretation.",
            expected_container_hash=_hash(vault))
        # This Source predates enrollment; the update must discover its retained before-image itself.
        assert source_custody.read_manifest(vault, reference)[0] is None
        with manager.mutation_guard(vault):
            source_custody.publish(vault, source_custody.prepare_enrollment(
                vault, reference=reference, source_hash=vault_module.content_hash(text)))
        manifest = records.collections.load_manifest(vault, vault / LEDGER_COLLECTION_PATH)
        prior = record_formats.load_adapter(vault, manifest).read().records[0]
        updated = records.update_record(vault, LEDGER_COLLECTION_PATH, item_key=appended["item_key"],
            changes={"word_count": 43}, body="A replacement without a current citation." if remove_origin else None,
            expected_container_hash=_hash(vault), expected_item_version=prior.source.hash,
            why="Correct the interpretation while keeping its history.")
        assert updated["outcome"] == "committed"
        custody, _ = source_custody.read_manifest(vault, reference)
        assert custody.dependency.state == "present"
        kept = list((vault / record_history.history_dir(manifest.collection_id, appended["item_key"])).glob("*.json"))
        assert len(kept) == 1
        entry = json.loads(kept[0].read_text())
        assert reference in entry["prior"]["body"]
        assert entry["dependency_bindings"] == [{"source_ref": reference,
            "epoch_id": custody.dependency_epochs["file_records"].epoch_id, "payload": "prior"}]
        assert (reference in (vault / prior.source.path).read_text()) == (not remove_origin)
        history = record_history.read(vault, LEDGER_COLLECTION_PATH, item_key=appended["item_key"], semantic_profile="records")
        assert history["status"] == "complete" and history["retained"] == 1


def test_bulk_operation_reserves_once_and_path_reuse_cannot_change_its_source(vault, attempts):
    """Two rows share one operation, and later reuse of their Source path preserves the old incarnation."""
    manager, call = attempts
    setup_ledger_collection(vault)
    args = authored()
    captured = call("capture_source", **args, capture_reservation=reserve(call, args))
    source_path = vault / captured["source"]["path"]
    reference = memory_refs.ref_from_markdown(source_path.read_text())
    with request_scope(owner_principal(surface="mcp")):
        result = records.bulk_upsert_records(vault, LEDGER_COLLECTION_PATH,
            rows=[{"item": ledger_item(slug="first")}, {"item": ledger_item(slug="second")}],
            source=captured["source"]["path"], expected_container_hash=_hash(vault), why="Import two original rows.")
        assert result["counts"]["inserted"] == 2
        custody, _ = source_custody.read_manifest(vault, reference)
        epoch = custody.dependency_epochs["file_records"]
        assert epoch.revision == 2 and custody.dependency.state == "present"
        history = records._audit_events(vault, collection_id=result["collection_id"])
        assert history.parsed
        original_events = [event for event in history.events if event["transition_id"] in
                           {row["transition_id"] for row in result["rows"]}]
        assert len(original_events) == 2
        assert {event["dependency_operation_id"] for event in original_events} == {result["batch_id"]}
        assert all(event["dependency_bindings"] == [{"source_ref": reference, "epoch_id": epoch.epoch_id, "payload": "current"}]
                   for event in original_events)
        replacement_ref = memory_refs.memory_ref(memory_refs.new_id())
        replacement = source_path.read_text().replace(reference.rsplit("/", 1)[1], replacement_ref.rsplit("/", 1)[1])
        source_path.write_text(replacement)
        with manager.mutation_guard(vault):
            source_custody.publish(vault, source_custody.prepare_enrollment(
                vault, reference=replacement_ref, source_hash=vault_module.content_hash(replacement)))
        later = records.bulk_upsert_records(vault, LEDGER_COLLECTION_PATH, rows=[{"item": ledger_item(slug="third")}],
            source=captured["source"]["path"], expected_container_hash=_hash(vault), why="Import the replacement incarnation.")
        assert later["counts"]["inserted"] == 1
        history = records._audit_events(vault, collection_id=result["collection_id"])
        by_transition = {event["transition_id"]: event for event in history.events}
        assert all(by_transition[event["transition_id"]] == event for event in original_events)
        assert by_transition[later["rows"][0]["transition_id"]]["dependency_bindings"][0]["source_ref"] == replacement_ref
