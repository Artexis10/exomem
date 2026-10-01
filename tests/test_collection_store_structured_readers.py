"""Structured consumers read committed rows before Markdown publication."""

import datetime as dt
from dataclasses import replace

import pytest

from exomem import audit, due_state, plan_progress, record_governance, working_set_index
from exomem import structured_collections as collections, working_set_state
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import owner_principal, request_scope
from lifecycle_fixtures import PLANNING_PATH, RECORDS_PATH, planning_manifest, records_manifest
from test_collection_store_writer import CID, KEY, OTHER, create, manifest_path, manifest_text, store as store
from test_governance_egress import _external, write_rule, write_scope
from test_plan_progress_review import (
    PLANNING_COLLECTION, RECORDS_COLLECTION, RECORDS_REF, _PLANNING_MANIFEST,
    _committed, _records_manifest,
)
from test_working_set_state import _anchor


def _log_manifest(text):
    return (text.replace("strategy: markdown-items", "strategy: markdown-log")
            .replace("  source: Items", "  source: Log.md")
            .replace("  source: Entries", "  source: Log.md")
            .replace("  format_version: 1\n", "  format_version: 1\n"
                     "  section: {level: 2, title: Entries}\n"
                     "  item_heading: {level: 3, fields: [{name: title, type: string}], separator: ' · '}\n"
                     "  insertion: newest-first\n"
                     "  child_rows: {prefix: '- ', delimiter: '|', fields: [field, value], container_field: details}\n")
            .replace("  fields:\n", "  fields:\n    details: {type: array, items: {type: object}}\n"))


def test_inventory_counts_canonical_committed_and_held_without_views(store):
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    with pytest.raises(collections.CollectionError, match="SCHEMA_FIELD_TYPE"):
        store.append_record(CID, item={"title": "Bad", "count": "invalid"},
                            item_key=OTHER, why="capture", hold=True)
    with preview_store(store.root, store.handle):
        result = record_governance.inventory_collections(store.root)
    assert len(result["collections"]) == 1
    assert result["collections"][0]["committed"] == 1
    assert result["collections"][0]["held"] == 1


def test_current_state_uses_canonical_contract_when_manifest_view_is_stale(store):
    text = manifest_text().replace("    count: {type: integer}",
                                  "    status: {type: string}")
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    store.append_record(CID, item={"title": "One", "status": "ready"},
                        item_key=KEY, why="capture")
    path = store.root / manifest_path()
    path.parent.mkdir(parents=True)
    path.write_text(manifest_text().replace("semantic_profile: records", "semantic_profile: planning"))
    routed = collections.parse_manifest_bytes(store.root, manifest_path(), text.encode())
    with preview_store(store.root, store.handle):
        result = working_set_state.current_state_for(store.root, anchors=[_anchor(routed)])
    assert len(result) == 1
    assert result[0]["statement"] == "status: ready"


def test_outcome_audit_joins_canonical_rows_without_manifest_or_item_views(store):
    store.create_collection(PLANNING_PATH, planning_manifest(), why="create", scaffold=False)
    store.create_collection(RECORDS_PATH, records_manifest(), why="create", scaffold=False)
    store.append_record(PLANNING_PATH, item={"title": "Batch", "kind": "outcome",
                        "status": "planned", "commitment": "committed", "horizon": "quarter"},
                        item_key=KEY, why="plan")
    store.append_record(RECORDS_PATH, item={"title": "Batch", "occurred_on": "2026-10-01",
                        "event_type": "produced"}, item_key=OTHER, why="observe")
    with preview_store(store.root, store.handle):
        findings, metadata = audit._check_unreflected_outcomes(store.root)
    assert len(findings) == 1
    assert findings[0].meta["joined_total"] == 1
    assert metadata == {}


def test_progress_review_executes_canonical_saved_evidence_without_views(store):
    from exomem.plan_memory import plan_memory

    store.create_collection(RECORDS_COLLECTION, _records_manifest(), why="create", scaffold=False)
    store.create_collection(PLANNING_COLLECTION, _PLANNING_MANIFEST, why="create", scaffold=False)
    store.append_record(RECORDS_COLLECTION, item={"occurred_on": "2026-10-01", "label": "session",
                        "status": "worked"}, item_key=OTHER, why="observe")
    store.append_record(PLANNING_COLLECTION,
                        item=_committed("Ship", [{"collection": RECORDS_REF,
                                                  "role": "progress", "view": "worked"}]),
                        item_key=KEY, why="plan")
    with preview_store(store.root, store.handle):
        assert plan_memory(store.root, "query", collection=PLANNING_COLLECTION)["returned"] == 1
        result = plan_progress.review(store.root)
    assert result["items_reviewed"] == result["bindings_executed"] == 1
    assert result["items"][0]["evidence"][0]["observed"]["matched"] == 1


def test_compiler_candidates_discover_canonical_planning_items_without_views(store):
    create(store, "planning")
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="plan")
    with preview_store(store.root, store.handle):
        records, plans = working_set_index._collection_candidates(store.root)
    assert records == []
    assert [candidate.title for candidate in plans] == ["One"]
    assert plans[0].ref.endswith(f"/{KEY}.md")


def test_compiler_manifest_registry_cannot_hide_new_canonical_collections(store):
    create(store)
    working_set_index.publish_collection_manifests(store.root, 1, ())
    with preview_store(store.root, store.handle):
        result = working_set_index.collection_manifests(store.root, 1)
    assert [manifest.collection_id for manifest in result] == [CID]


def test_compiler_refresh_observes_canonical_item_edits_without_view_changes(store):
    text = manifest_text("planning").replace("    area:",
              "    tags: {type: array, items: {type: string}}\n    area:")
    store.create_collection(manifest_path("planning"), text, why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="plan")
    index = working_set_index.WorkingSetIndex(store.root)
    with preview_store(store.root, store.handle):
        before = index.update()
    store.update_record(CID, item_key=KEY, changes={"tags": ["changed"]},
                        expected_container_hash=first["after_container_hash"],
                        expected_item_version=first["after_item_hash"], why="edit")
    with preview_store(store.root, store.handle):
        after = index.update()
    assert after["generation"] > before["generation"]
    index.close()


def test_canonical_discovery_ignores_hidden_manifests_before_applying_limits(store):
    create(store)
    hidden_path = "Knowledge Base/Records/Hidden/_collection.md"
    store.create_collection(hidden_path, manifest_text().replace(CID, OTHER),
                            why="create", scaffold=False)
    write_scope(store.root, paths="Records/Hidden/**")
    write_rule(store.root, ceiling=0)
    with preview_store(store.root, store.handle), request_scope(_external()):
        manifests = collections.discover_collections(store.root, max_candidates=1,
                                                     max_raw_candidates=1)
    assert [manifest.collection_id for manifest in manifests] == [CID]


def test_routing_read_cannot_reuse_a_snapshot_for_an_explicit_different_principal(store):
    create(store)
    with preview_store(store.root, store.handle) as writer, request_scope(owner_principal()):
        with writer.read_snapshot():
            with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
                due_state.routing_targets(store.root, principal=_external())
            assert len(record_governance.inventory_collections(store.root)["collections"]) == 1


def test_bound_manifest_resolution_never_falls_back_to_a_file_collection(store):
    path = store.root / manifest_path()
    path.parent.mkdir(parents=True)
    path.write_text(manifest_text())
    with preview_store(store.root, store.handle):
        with pytest.raises(collections.CollectionError):
            collections.load_manifest(store.root, path)


def test_claims_census_reads_canonical_rows_without_views(store):
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    with preview_store(store.root, store.handle):
        result = due_state._recompute_claims(store.root)
    assert result[manifest_path()]["complete"] is True
    assert [item["key"] for item in result[manifest_path()]["items"]] == [KEY]


def test_occurrence_disposition_returns_runnable_canonical_guards_under_policy(store):
    from exomem import records_disposition

    text = manifest_text().replace("    count: {type: integer}",
                                  "    sources: {type: array, items: {type: string}}")
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    write_scope(store.root, paths="Unrelated/**")
    write_rule(store.root, ceiling=0)
    with preview_store(store.root, store.handle), request_scope(_external()):
        manifest = due_state._load_manifest(store.root, manifest_path())
        call = records_disposition._occurrence_call(store.root, manifest, manifest.path, KEY,
                                                  "exomem://memory/33333333-3333-4333-8333-333333333333",
                                                  "add occurrence")
        result = store.update_record(call.pop("collection"), **{k: v for k, v in call.items() if k != "action"})
        rows = record_governance.query_collection(store.root, CID).rows
    assert result["outcome"] == "committed"
    assert rows[0]["sources"] == ["exomem://memory/33333333-3333-4333-8333-333333333333"]


def test_write_delta_keeps_hidden_log_rows_but_serve_matches_rows_absent(store):
    plan_path = manifest_path("planning")
    plan_text = manifest_text("planning").replace(CID, OTHER)
    record_text = _log_manifest(manifest_text().replace("natural_key: [title]", "natural_key: [title, count]"))
    record_text = record_text.rsplit("---", 1)[0] + (
        f"links:\n  plans:\n    - reference: exomem://memory/{OTHER}\n"
        "      query: {limit: 50}\n      join: {title: title}\n---\n"
    )
    store.create_collection(plan_path, plan_text, why="create", scaffold=False)
    store.create_collection(manifest_path(), record_text, why="create", scaffold=False)
    values = {"title": "Batch", "kind": "outcome", "status": "planned",
              "commitment": "committed", "horizon": "quarter"}
    added = store.append_record(OTHER, item=values, item_key=KEY, why="plan")
    store.append_record(CID, item={"title": "Batch", "count": 1}, item_key=KEY, why="observe")
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [{collections.record_ref(CID, OTHER)}]\n")
    write_rule(store.root, ceiling=0)
    row = {"records": manifest_path(), "planning": plan_path, "join": {"title": "title"}}
    due_state.save(store.root, {"version": due_state.SCHEMA_VERSION, "categories": {},
                              "bindings": {plan_path: [row], manifest_path(): [row]}})
    planning = collections.parse_manifest_bytes(store.root, plan_path, plan_text.encode())
    with preview_store(store.root, store.handle), request_scope(_external()):
        due_state.apply_plan_write_delta(store.root, planning, path=added["affected_paths"][0],
                                        key=KEY, values=values)
        absent = due_state.served_entries(store.root, today=dt.date(2026, 10, 1))
    assert len(absent) == 1
    with request_scope(owner_principal()):
        store.append_record(CID, item={"title": "Batch", "count": 2}, item_key=OTHER, why="observe")
    with preview_store(store.root, store.handle), request_scope(_external()):
        projected = due_state.apply_plan_write_delta(
            store.root, planning, path=added["affected_paths"][0], key=KEY, values=values
        )
        entry = projected["categories"]["unreflected_outcomes"][added["affected_paths"][0]]["open"][0]
        assert len(entry["component"]["joined"]) == 2
        assert due_state.served_entries(store.root, today=dt.date(2026, 10, 1)) == absent
        claims = due_state._recompute_claims(store.root)
        assert [item["key"] for item in claims[manifest_path()]["items"]] == [KEY]
        from exomem.plan_memory import plan_memory

        completed = plan_memory(store.root, "triage", collection=OTHER, plan_id=KEY,
                                transition={"status": "completed"}, why="finish",
                                expected_container_hash=added["after_container_hash"],
                                expected_item_version=added["after_item_hash"])
        assert completed["outcome"] == "committed"
        assert plan_memory(store.root, "query", collection=OTHER)["rows"][0]["status"] == "completed"
        assert not [entry for entry in due_state.served_entries(store.root, today=dt.date(2026, 10, 1))
                    if entry["category"] == "unreflected_outcomes"]


def test_hidden_log_sibling_does_not_reopen_a_reflected_observation(store):
    from test_collection_claims import _write_observation

    path, ref = _write_observation(store.root)
    text = _log_manifest(manifest_text().replace("natural_key: [title]", "natural_key: [title, count]")
                         .replace("    count:", "    sources: {type: array, items: {type: link}}\n    count:")
                         .replace("lifecycle: active\n", "lifecycle: active\nclaims:\n  terms: [account, subscriptions]\n"))
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    store.append_record(CID, item={"title": "Account", "count": 1, "sources": [ref]},
                        item_key=KEY, why="observe")
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        due_state.reconcile(store.root, now=dt.datetime(2026, 10, 1, tzinfo=dt.UTC))
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [{collections.record_ref(CID, OTHER)}]\n")
    write_rule(store.root, ceiling=0)
    with preview_store(store.root, store.handle), request_scope(_external()):
        absent = due_state.served_entries(store.root, today=dt.date(2026, 10, 1))
    with request_scope(owner_principal()):
        store.append_record(CID, item={"title": "Hidden", "count": 2}, item_key=OTHER, why="observe")
    with preview_store(store.root, store.handle), request_scope(_external()):
        served = due_state.served_entries(store.root, today=dt.date(2026, 10, 1))
    assert not [row for row in absent if row["category"] == "unreflected_observations"]
    assert served == absent


def test_current_plan_state_uses_the_committed_row_and_withholds_its_stale_view(store):
    """A completed or hidden plan must not regain its old status from its view."""
    from exomem import writer_lease
    from exomem.plan_memory import plan_memory
    from test_due_state_bulk_carriers import _command

    create(store, "planning")
    with preview_store(store.root, store.handle):
        response = writer_lease.invoke_command(
            _command("plan_memory"), store.root, action="add", collection=CID,
            item={"title": "Ship", "kind": "outcome", "status": "planned",
                  "commitment": "committed", "horizon": "quarter"},
            plan_id=KEY, why="plan", response_detail="full",
        )
    assert response["status"] == "committed"
    made = response["diagnostics"]
    item_path = made["affected_paths"][0]
    view = store.root / item_path
    view.parent.mkdir(parents=True, exist_ok=True)
    view.write_text("---\ntype: plan\ntitle: Ship\nstatus: planned\n---\n")
    manifest = collections.parse_manifest_bytes(store.root, manifest_path("planning"),
                                                manifest_text("planning").encode())
    anchor = replace(_anchor(manifest), path=item_path, ref=f"exomem://plan/{CID}/{KEY}",
                     kind="plan", title="Ship")
    with preview_store(store.root, store.handle):
        response = writer_lease.invoke_command(
            _command("plan_memory"), store.root, action="triage", collection=CID, plan_id=KEY,
            transition={"status": "completed"}, why="finish",
            expected_container_hash=made["after_container_hash"],
            expected_item_version=made["after_item_hash"], response_detail="full",
        )
        assert response["status"] == "committed"
        current = working_set_state.current_state_for(store.root, anchors=[anchor])
    assert len(current) == 1
    assert current[0]["statement"] == "status: completed"
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [exomem://plan/{CID}/{KEY}]\n")
    write_rule(store.root, ceiling=0)
    with preview_store(store.root, store.handle), request_scope(_external()):
        assert plan_memory(store.root, "query", collection=CID)["returned"] == 0
        assert working_set_state.current_state_for(store.root, anchors=[anchor]) == ()


def test_current_state_neighbourhood_keeps_knowledge_notes_not_stale_collection_views(store):
    """Derived collection bytes cannot displace an ordinary current knowledge note."""
    text = manifest_text().replace("    count: {type: integer}", "    status: {type: string}")
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    made = store.append_record(CID, item={"title": "Stale view", "status": "retired"},
                               item_key=KEY, why="record")
    item_path = made["affected_paths"][0]
    view = store.root / item_path
    view.parent.mkdir(parents=True, exist_ok=True)
    view.write_text("---\ntitle: Stale view\nstatus: active\nupdated: 2099-01-01\n---\n")
    note_path = "Knowledge Base/Notes/current.md"
    note = store.root / note_path
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\ntitle: Current knowledge\nstatus: active\nupdated: 2026-10-01\n---\n")
    manifest = collections.parse_manifest_bytes(store.root, manifest_path(), text.encode())
    anchor = replace(_anchor(manifest), path="Knowledge Base/Entities/resource.md",
                     neighbourhood=frozenset({item_path, note_path}))
    with preview_store(store.root, store.handle):
        current = working_set_state.current_state_for(store.root, anchors=[anchor])
    assert len(current) == 1
    assert current[0]["statement"] == "latest active note: Current knowledge"
