"""Store-only bulk guarantees not supplied by the file bulk workflow tests."""

import json

import pytest

from exomem import records, writer_lease
from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.collection_store.preview import preview_store
from exomem.record_memory import record_memory
from test_collection_store_writer import CID, create, manifest_path, manifest_text, store as store
from test_records_bulk_upsert import EVIDENCE, _evidence


def bulk(store, rows, **kwargs):
    kwargs.setdefault("expected_container_hash", store.inspect_collection(CID)["lifecycle_guards"]["expected_container_hash"])
    kwargs.setdefault("why", "import")
    kwargs.setdefault("source", EVIDENCE)
    with preview_store(store.root, store.handle):
        return record_memory(store.root, action="bulk_upsert", collection=CID, rows=rows, **kwargs)


def test_500_rows_share_one_transition_and_transaction_without_file_depth(store, monkeypatch):
    create(store)
    _evidence(store.root)
    monkeypatch.setattr(records, "_MAX_AUDIT_CHAIN_DEPTH", 1)
    statements = []
    store.connection.set_trace_callback(statements.append)
    try:
        result = bulk(store, [{"item": {"title": f"Row {index}"}} for index in range(500)])
    finally:
        store.connection.set_trace_callback(None)
    assert result["counts"]["inserted"] == 500
    assert {row["transition_id"] for row in result["rows"]} == {result["first_transition"]}
    assert result["first_transition"] == result["last_transition"]
    assert statements.count("BEGIN IMMEDIATE") == statements.count("COMMIT") == 1
    assert store.connection.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 2
    assert store.connection.execute("SELECT COUNT(*) FROM audit_effects").fetchone()[0] == 500
    assert store.connection.execute("SELECT COUNT(*) FROM item_versions").fetchone()[0] == 500
    assert store.connection.execute("SELECT COUNT(*) FROM item_sources").fetchone()[0] == 500
    assert "Row 0" not in repr(store.connection.execute("SELECT * FROM txns").fetchall())
    assert "Row 0" not in repr(store.connection.execute("SELECT * FROM audit_effects").fetchall())
    assert "depth_check" not in result
    with pytest.raises(OpError) as error:
        bulk(store, [{"item": {"title": "Too many"}}] * 501)
    assert error.value.code == "BULK_UPSERT_TOO_MANY_ROWS"
    assert error.value.details["maximum"] == 500


def test_public_bulk_refuses_authored_view_stamp_without_changing_canonical_data(store):
    # A custom schema must not let bulk persist an authored stamp hidden by the renderer.
    text = manifest_text().replace("    count:", "    exomem_view: {type: string}\n    count:")
    store.create_collection(manifest_path(), text, why="create")
    _evidence(store.root)
    before = tuple(store.connection.iterdump())
    manifest = store.root / manifest_path()
    published = manifest.read_bytes()
    result = bulk(store, [{"item": {"title": "One", "exomem_view": "authored"}}])
    assert result["committed"] is False and result["counts"]["rejected"] == 1
    assert result["rows"][0]["code"] == "RESERVED_RECORD_FIELD"
    assert tuple(store.connection.iterdump()) == before
    assert manifest.read_bytes() == published


def test_generated_identity_receipt_replays_after_reopen_without_another_write(store, monkeypatch):
    text = manifest_text().replace("natural_key: [title]", "natural_key: [count]")
    store.create_collection(manifest_path(), text, why="create")
    _evidence(store.root)
    monkeypatch.setattr(writer_lease, "active_mutation_request_id", lambda: "bulk-lost-response")
    guard = store.inspect_collection(CID)["snapshot"]
    rows = [{"item": {"title": "Generated"}}]
    result = bulk(store, rows, expected_container_hash=guard)
    before = tuple(store.connection.iterdump())
    path = store.handle.path
    store.handle.close()
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        with preview_store(store.root, handle):
            replay = record_memory(store.root, action="bulk_upsert", collection=CID, rows=rows,
                                   why="import", source=EVIDENCE, expected_container_hash=guard)
        assert replay == result
        assert tuple(handle.connection.iterdump()) == before


def test_dispatcher_reconstructs_native_replay_without_an_outer_ledger(store):
    """A lost transport ledger must not turn a verified replay into a bare receipt."""
    from test_due_state_bulk_carriers import _command

    create(store)
    _evidence(store.root)
    args = dict(action="bulk_upsert", collection=CID, rows=[{"item": {"title": "Retry"}}],
                source=EVIDENCE, why="import", expected_container_hash=store.inspect_collection(CID)["snapshot"],
                response_detail="full", mutation_request_id="same-bulk-request")
    with preview_store(store.root, store.handle):
        first = writer_lease.invoke_command(_command("record_memory"), store.root, **args)
        before = tuple(store.connection.iterdump())
        retried = writer_lease.invoke_command(_command("record_memory"), store.root, **args)
    assert first["status"] == "committed"
    assert retried["status"] == "replayed"
    assert retried["terminal"] is True and retried["mutated"] is False
    assert retried["diagnostics"] == first["diagnostics"]
    assert "due_state" not in retried and "capture_sweep" not in retried
    assert tuple(store.connection.iterdump()) == before


def test_a_changed_preserved_source_rolls_back_the_completed_batch(store, monkeypatch):
    create(store)
    _evidence(store.root)
    before = tuple(store.connection.iterdump())
    precommit = store.__class__._precommit

    def change_source(self, manifest):
        precommit(self, manifest)
        (self.root / EVIDENCE).write_text("Changed preserved import\n")

    monkeypatch.setattr(store.__class__, "_precommit", change_source)
    with pytest.raises(OpError):
        bulk(store, [{"item": {"title": "One"}}, {"item": {"title": "Two"}}])
    assert tuple(store.connection.iterdump()) == before


def test_log_bulk_projects_the_final_batch_and_updates_in_place(store):
    text = manifest_text(layout="markdown-log").replace(
        "  item: {level: 3, key: record_id}",
        "  item_heading: {level: 3, fields: [{name: title, type: string}], separator: ' · '}\n"
        "  child_rows: {prefix: '- ', delimiter: '|', fields: [field, value], container_field: details}\n"
        "  insertion: newest-first",
    )
    text = text.replace("    count: {type: integer}", "    count: {type: integer}\n"
                        "    details: {type: array, items: {type: object}}")
    store.create_collection(manifest_path(), text, why="create")
    _evidence(store.root)
    first = bulk(store, [{"item": {"title": "One", "count": 1}},
                         {"item": {"title": "Two", "count": 2}}])
    second = bulk(store, [{"item": {"title": "One", "count": 3}},
                          {"item": {"title": "Three", "count": 4}}],
                  expected_container_hash=first["after_container_hash"])
    assert [row["outcome"] for row in second["rows"]] == ["updated", "inserted"]
    assert store.inspect_collection(CID)["coverage"]["committed"] == 3
    assert store.connection.execute("SELECT kind, pending_row_version FROM projection_state WHERE kind='log'").fetchall() == [("log", 3)]
    values = [json.loads(row[0]) for row in store.connection.execute("SELECT values_json FROM items")]
    assert {row["title"]: row["count"] for row in values} == {"One": 3, "Two": 2, "Three": 4}
    refused = bulk(store, [{"item": {"title": "Body"}, "body": "Not representable"}])
    assert refused["rows"][0]["code"] == "UNREPRESENTABLE_RECORD_BODY"


def test_describe_uses_store_limits_without_changing_the_file_contract(store):
    files = record_memory(store.root, action="describe")["bulk_upsert"]
    with preview_store(store.root, store.handle):
        canonical = record_memory(store.root, action="describe")["bulk_upsert"]
    assert files["limits"]["max_rows"] == 50
    assert "BULK_UPSERT_AUDIT_DEPTH" in files["limits"]["refusals"]
    assert canonical["limits"]["max_rows"] == 500
    assert "BULK_UPSERT_AUDIT_DEPTH" not in repr(canonical)
    assert "one transition" in canonical["audit"]


def test_old_row_grant_cannot_commit_changed_bulk_content(store, monkeypatch):
    from exomem.governance.principal import request_scope
    from test_collection_store_governance import inspection_token, redeem, session

    create(store)
    _evidence(store.root)
    bulk(store, [{"item": {"title": "One", "count": 1}}])
    who = session(store, monkeypatch)
    with preview_store(store.root, store.handle):
        redeem(store, who, inspection_token(store, who))
    before = tuple(store.connection.iterdump())
    with request_scope(who), pytest.raises(OpError) as error:
        bulk(store, [{"item": {"title": "One", "count": 2}}])
    assert error.value.code == "COLLECTION_NOT_FOUND"
    assert tuple(store.connection.iterdump()) == before


def test_public_bulk_advances_due_projection_only_after_commit(store):
    from exomem import due_state
    from _nag_governance_helpers import overdue_prediction
    from test_due_state_bulk_carriers import _command
    from lifecycle_fixtures import (
        PLANNING_PATH, RECORDS_PATH, RECORDS_ID, planning_manifest, records_manifest,
    )

    store.create_collection(PLANNING_PATH, planning_manifest(), why="create", scaffold=False)
    made = store.create_collection(RECORDS_PATH, records_manifest(), why="create", scaffold=False)
    _evidence(store.root)
    binding = {"records": RECORDS_PATH, "planning": PLANNING_PATH, "join": {"title": "title"}}
    due_state.save(store.root, {"version": due_state.SCHEMA_VERSION, "categories": {},
                              "bindings": {PLANNING_PATH: [binding], RECORDS_PATH: [binding]}})
    rows = [{"item": {"title": title, "occurred_on": "2026-10-01", "event_type": "produced"}}
            for title in ("One", "Two")]
    kwargs = dict(action="bulk_upsert", collection=RECORDS_ID, why="import", source=EVIDENCE,
                  expected_container_hash=made["after_container_hash"])
    with preview_store(store.root, store.handle):
        overdue_prediction(store.root)
        due_state.reconcile(store.root)
        response = writer_lease.invoke_command(_command("record_memory"), store.root,
                                              rows=rows, response_detail="full", **kwargs)
        assert response["status"] == "committed"
        assert response["due_state"]["total"] >= 1
        result = response["diagnostics"]
        projection = due_state.load(store.root)
        assert len(projection["claims"][RECORDS_PATH]["items"]) == 2
        before = due_state.state_path(store.root).read_bytes()
        unchanged = record_memory(store.root, rows=rows,
                                  **{**kwargs, "expected_container_hash": result["after_container_hash"]})
        assert not unchanged["committed"] and "due_state" not in unchanged
        assert due_state.state_path(store.root).read_bytes() == before
        aborted = record_memory(store.root, rows=[{"item": {**rows[0]["item"], "title": "Three"}},
                                                 {"item": {"bogus": True}}],
                                **{**kwargs, "expected_container_hash": result["after_container_hash"]})
        assert not aborted["committed"] and "due_state" not in aborted
        assert due_state.state_path(store.root).read_bytes() == before
