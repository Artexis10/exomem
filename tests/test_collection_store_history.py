"""Canonical history survives corrections, policy changes and derived-state damage."""

from dataclasses import replace
from pathlib import Path

import pytest
from test_collection_store_edit_back import served as served
from test_collection_store_legacy import _event, _rewrite
from test_collection_store_legacy_import import CONTEXT, _capture, _items
from test_collection_store_migration import env as env
from test_collection_store_service import service as service
from test_collection_store_writer import CID, KEY, OTHER, create, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope

from exomem import mutation_terminal, record_formats, record_history, records
from exomem import structured_collections as collections
from exomem.collection_store import chain
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import request_scope


def _append(writer, count=1):
    container, _ = writer.current_guards(CID)
    return writer.append_record(CID, item={"title": "First", "count": count}, item_key=KEY,
                                expected_container_hash=container, why="capture")


def _correct(writer, count):
    container, version = writer.current_guards(CID, KEY)
    return writer.update_record(CID, item_key=KEY, changes={"count": count},
                                expected_item_version=version, expected_container_hash=container,
                                why=f"correct count to {count}")


def test_native_history_exposes_prior_values_and_reads_do_not_write(store):
    create(store)
    _append(store)
    _correct(store, 2)
    _correct(store, 3)
    before = store.connection.total_changes
    watermarks = store.connection.execute("SELECT verified_through_txn FROM collections").fetchall()
    with preview_store(store.root, store.handle):
        first = record_history.read(store.root, CID, item_key=KEY, semantic_profile="records", limit=1)
        second = record_history.read(store.root, CID, item_key=KEY, semantic_profile="records", limit=1,
                                     continuation=first["continuation"])
        agent = records.agent_audit_history(store.root, CID)
    assert first["status"] == second["status"] == "complete"
    assert first["revisions"][0]["prior"]["values"]["count"] == 2
    assert second["revisions"][0]["prior"]["values"]["count"] == 1
    assert first["continuation"] != second["revisions"][0]["version"]
    assert [event["operation"] for event in agent["events"]] == ["update", "update", "append", "create"]
    assert store.inspect_collection(CID)["audit"] == {"status": "ok", "gaps": []}
    assert store.connection.total_changes == before
    assert store.connection.execute("SELECT verified_through_txn FROM collections").fetchall() == watermarks


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_public_facades_return_native_agent_and_row_history(store, profile):
    from exomem.plan_memory import plan_memory
    from exomem.record_memory import record_memory

    create(store, profile)
    appended = store.append_record(CID, item={"title": "Initial"}, item_key=KEY, why="capture")
    store.update_record(CID, item_key=KEY, changes={"title": "Corrected"}, why="correction",
        expected_container_hash=appended["after_container_hash"], expected_item_version=appended["after_item_hash"])
    facade = record_memory if profile == "records" else plan_memory
    with preview_store(store.root, store.handle):
        query = facade(store.root, action="query", collection=CID, include_agent_history=True)
        kept = facade(store.root, action="history", collection=CID,
                      **({"item_key": KEY} if profile == "records" else {"plan_id": KEY}))
    assert query["agent_history"]["status"] == "ok"
    assert query["agent_history"]["events"][0]["rationale"] == "correction"
    assert kept["revisions"][0]["prior"]["values"]["title"] == "Initial"


def test_managed_service_serves_typed_history_and_publishes_it_after_restart(service):
    from test_collection_store_s1_gate import _until
    from test_collection_store_service import call

    from exomem import writer_lease

    root = service.root
    appended = call(root, "record_memory", action="append", collection=CID, item={"title": "Initial", "count": 1},
                    item_key=KEY, why="capture")
    call(root, "record_memory", action="update", collection=CID, item_key=KEY, changes={"count": 2}, why="correction",
         expected_container_hash=appended["after_container_hash"], expected_item_version=appended["after_item_hash"])
    before = call(root, "record_memory", action="history", collection=CID, item_key=KEY)
    assert before["revisions"][0]["prior"]["values"]["count"] == 1
    service.stop()
    service.manager = writer_lease.start_server_lifecycle()
    _until(lambda: service.manager.status().get("collection_store", {}).get("status") == "admitted")
    after = call(root, "record_memory", action="history", collection=CID, item_key=KEY)
    assert after == before
    query = call(root, "record_memory", action="query", collection=CID, include_agent_history=True)
    assert query["agent_history"]["events"][0]["rationale"] == "correction"
    page = root / "Knowledge Base/Records/Work/_history.md"
    _until(lambda: page.exists() and "correction" in page.read_text())
    assert "Initial" not in page.read_text()


def test_control_history_keeps_business_predecessor_and_verifies_after_reopen(store):
    from exomem.collection_store import history

    create(store)
    before = store.connection.execute("SELECT audit_head,generation FROM collections").fetchone()
    store.record_control_transition("store_reconcile", {CID: {"counts": {}, "ids": {}}}, why="reconcile views")
    assert store.connection.execute("SELECT audit_head,generation FROM collections").fetchone() == before
    _append(store)
    assert store.inspect_collection(CID)["audit"]["status"] == "ok"
    store.handle.release_cache.history.clear()
    with store.read_snapshot():
        history.verify(store.connection, CID)


@pytest.mark.parametrize("table,statement,reason", [
    ("txns", "UPDATE txns SET why='changed outside the writer' WHERE operation='append'", "event hash differs"),
    ("audit_effects", "UPDATE audit_effects SET hash_before='" + "0" * 64 + "' WHERE version_before IS NOT NULL",
     "effect predecessor payload differs"),
])
def test_native_corruption_is_an_integrity_error_even_for_cached_inspection(store, table, statement, reason):
    create(store)
    _append(store)
    _correct(store, 2)
    store.inspect_collection(CID)
    trigger = store.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=? "
                                       "AND sql LIKE '%BEFORE UPDATE%'", (table,)).fetchone()[0]
    store.connection.execute(f'DROP TRIGGER "{trigger}"')
    store.connection.execute(statement)
    with pytest.raises(chain.StoreChainError, match=reason):
        store.inspect_collection(CID)


def test_a_warm_history_page_decodes_only_the_returned_prior_payload(store, monkeypatch):
    from exomem.collection_store import typed_storage

    create(store)
    _append(store)
    _correct(store, 2)
    _correct(store, 3)
    store.inspect_collection(CID)
    decoded = []
    original = typed_storage.version_values

    def read(conn, row_id, version):
        decoded.append((row_id, version))
        return original(conn, row_id, version)

    monkeypatch.setattr(typed_storage, "version_values", read)
    with preview_store(store.root, store.handle):
        result = record_history.read(store.root, CID, item_key=KEY, semantic_profile="records", limit=1)
    assert result["returned"] == 1 and result["truncated"]
    assert len(decoded) == 1


def test_cold_bulk_history_does_not_decode_the_whole_receipt_for_every_item(store, monkeypatch, record_property):
    """A cold history page after bulk import must not multiply receipt parsing by its item count."""
    from test_records_bulk_upsert import EVIDENCE, _evidence

    from exomem.collection_store import history

    create(store)
    _evidence(store.root)
    inserted = store.bulk_upsert_records(CID, rows=[{"item": {"title": f"Sample {number}", "count": number}}
                                                   for number in range(100)],
        expected_container_hash=store.current_guards(CID)[0], source=EVIDENCE, why="capture observations")
    assert inserted["counts"]["inserted"] == 100
    receipt = store.connection.execute("SELECT receipt_json FROM txns WHERE operation='bulk_upsert'").fetchone()[0]
    receipt_bytes, decoded_bytes = len(receipt.encode()), 0
    decode = history.json.loads

    def counted(raw, *args, **kwargs):
        nonlocal decoded_bytes
        if raw == receipt:
            decoded_bytes += receipt_bytes
        return decode(raw, *args, **kwargs)

    monkeypatch.setattr(history.json, "loads", counted)
    manifest = store._projection_manifest(CID)
    rendered = history.render_page(store.connection, store.root, manifest, history.main_path(manifest))[1]
    assert "capture observations" in rendered
    assert all(row["item_key"] in rendered for row in inserted["rows"])
    record_property("bulk_receipt_bytes", receipt_bytes)
    record_property("decoded_bulk_receipt_bytes", decoded_bytes)
    assert decoded_bytes <= 8 * receipt_bytes, (decoded_bytes, receipt_bytes)


def test_invalid_derived_watermark_reverifies_without_inventing_a_gap(store):
    create(store)
    _append(store)
    store.connection.execute("UPDATE collections SET verified_through_txn=99999")
    before = store.connection.total_changes
    assert store.inspect_collection(CID)["audit"]["status"] == "ok"
    assert store.connection.total_changes == before


def test_repeated_pending_inspection_reuses_released_history_digest(store, record_property):
    """A queued page must not make every unchanged inspection scan the history again."""
    create(store)
    _append(store)
    _correct(store, 2)

    def measured(action):
        work = 0

        def progressed():
            nonlocal work
            work += 1
            return 0

        store.connection.set_progress_handler(progressed, 100)
        try:
            action()
        finally:
            store.connection.set_progress_handler(None, 0)
        return work

    def warm_read_work():
        assert store.inspect_collection(CID)["projection"]["pending_views"] > 0
        return measured(lambda: store.inspect_collection(CID))

    small = warm_read_work()
    small_write = measured(lambda: _correct(store, 3))
    for count in range(4, 103):
        _correct(store, count)
    large_write = measured(lambda: _correct(store, 103))
    before = store.connection.total_changes
    large = warm_read_work()
    record_property("small_history_vm_hundreds", small)
    record_property("large_history_vm_hundreds", large)
    record_property("small_write_vm_hundreds", small_write)
    record_property("large_write_vm_hundreds", large_write)
    assert large <= 2 * small + 5, (small, large)
    assert large_write <= 2 * small_write + 5, (small_write, large_write)
    assert store.connection.total_changes == before


def test_history_pages_are_queued_then_repair_generated_edits(store):
    from test_collection_store_writer import manifest_path, manifest_text

    store.create_collection(manifest_path(), manifest_text(), why="create", scaffold=False)
    receipt = _append(store)
    path = "Knowledge Base/Records/Work/_history.md"
    assert not (store.root / path).exists()
    assert store.connection.execute("SELECT state FROM projection_state WHERE path=?", (path,)).fetchone() == ("pending",)
    store.reconcile_views()
    content = (store.root / path).read_text()
    assert "capture" in content and receipt["after_item_hash"] not in content
    assert "[[" + receipt["affected_paths"][0] + "]]" in content
    (store.root / path).write_text("untrusted edit")
    store.reconcile_views()
    assert (store.root / path).read_text() == content
    assert not store.connection.execute("SELECT 1 FROM held_candidates").fetchone()


def test_history_page_partition_grows_only_at_its_tail_and_policy_repairs_old_pages(store, monkeypatch):
    from test_collection_store_edit_back import passes

    from exomem.collection_store import capability, history

    create(store)
    _append(store)
    for count in range(2, 702):
        _correct(store, count)
    store.reconcile_views()
    store.reconcile_views()
    manifest = store._projection_manifest(CID)
    main = (store.root / history.main_path(manifest)).read_text()
    assert main.count("## ") == 200
    assert "Reason: correct count to 701\n" in main and "Reason: correct count to 501\n" not in main
    first = store.root / history.page_path(manifest, 1)
    tail = store.root / history.page_path(manifest, 2)
    before = first.read_bytes(), first.stat().st_mtime_ns
    assert first.read_text().count("## ") == 500
    assert tail.read_text().count("## ") == 2
    _correct(store, 702)
    store.reconcile_views()
    store.reconcile_views()
    assert (first.read_bytes(), first.stat().st_mtime_ns) == before
    assert tail.read_text().count("## ") == 3
    assert KEY in first.read_text()
    first.write_text("Edited generated history")
    store.reconcile_views(paths={first.relative_to(store.root).as_posix(): None})
    assert first.read_bytes() == before[0]
    access_file = store.root / "Knowledge Base/_access.yaml"
    access_file.write_text(f"excluded: [{tail.relative_to(store.root / 'Knowledge Base').as_posix()}]\n")
    assert tail.relative_to(store.root).as_posix() not in history.render_page(
        store.connection, store.root, manifest, history.main_path(manifest), cache=store.handle.release_cache)[1]
    access_file.unlink()
    write_scope(store.root, paths=f"Records/**/{KEY}.md", default_deny=True)
    # An interrupted old repair can leave a matching empty header at a now-retired path.
    first.write_text(history.render_page(store.connection, store.root, manifest, first.relative_to(store.root).as_posix())[1])
    from exomem import commands

    with request_scope(_external()), preview_store(store.root, store.handle):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_get(store.root, path=first.relative_to(store.root).as_posix())
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {capability.COLLECTION_VIEWS_V1})
    store.invalidate_history_pages()  # the store thread queues every page when a release input changes, as in production
    passes(store, 0.0, 2.0)
    assert not first.exists() and not tail.exists()
    assert KEY not in (store.root / history.main_path(manifest)).read_text()


def _import_history(tmp_path, store, *, unknown=False, missing_key=None):
    from exomem.collection_store import legacy_import

    root, path = _items(tmp_path, values={"title": "First", "count": 3})
    path.write_text(record_formats.render_manifest_audit_head(path.read_text(), f"{3:024x}", semantic_profile="records"))
    manifest = collections.load_manifest(root, path)
    target = (path.parent / "Items/Original.md").relative_to(root).as_posix()
    lines = [_rewrite(_event(manifest, number), item_key=missing_key if number == 1 and missing_key else KEY,
                      canonical_path=target,
                      **({"operation": "update", "before_item_hash": "1" * 64, "payload_hash": None}
                         if number > 1 else {})) for number in range(1, 4)]
    (root / "Knowledge Base/log.md").write_text("\n".join(lines) + "\n")
    kept = record_history.plan_entry(root, manifest, item_key=KEY, canonical_path=target,
        prior_values={"title": "First", "count": 1}, prior_body="prior body", after_values={"title": "First", "count": 2},
        after_body="next body", operation="update", why="retained correction", transition_id=f"{2:024x}",
        before_item_hash="1" * 64, after_item_hash="2" * 64)
    kept.path.parent.mkdir(parents=True)
    kept.path.write_text(kept.content)
    with _capture(tmp_path, root, path) as (audit, captured):
        if unknown:
            # Unknown bounded provenance remains unknown even when exhaustive material verifies.
            captured = replace(captured, legacy_inspection={"status": "history_incomplete", "gaps": []})
        with store.connection:
            store.connection.execute("BEGIN")
            legacy_import.import_legacy_collection(store.connection, captured, audit=audit, context=CONTEXT)
    return captured.legacy_inspection


def test_imported_kept_content_and_missing_content_survive_a_native_correction(tmp_path, store):
    assessment = _import_history(tmp_path, store)
    assert assessment["status"] == "gap"
    _correct(store, 4)
    with preview_store(store.root, store.handle):
        first = record_history.read(store.root, CID, item_key=KEY, semantic_profile="records", limit=1)
        second = record_history.read(store.root, CID, item_key=KEY, semantic_profile="records", limit=1,
                                     continuation=first["continuation"])
    assert first["unavailable"] == second["unavailable"] == 1
    assert first["retained"] == 2
    assert first["revisions"][0]["prior"]["values"]["count"] == 3
    assert second["revisions"][0]["prior"]["values"]["count"] == 1
    assert store.inspect_collection(CID)["audit"] == assessment


def test_rebaseline_acknowledges_only_an_imported_positive_gap_and_survives_later_writes(tmp_path, store):
    from exomem.collection_store import connection
    from exomem.collection_store.writer import CollectionWriter

    assessment = _import_history(tmp_path, store)
    inspection = store.inspect_collection(CID)
    receipt = store.rebaseline_collection(CID, **inspection["lifecycle_guards"],
        acknowledged_gap_codes=assessment["gaps"], why="acknowledge the imported mismatch")
    assert mutation_terminal.valid_collection_receipt(receipt)
    _correct(store, 4)
    path, root = store.handle.path, store.root
    store.handle.close()
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        reopened = CollectionWriter(root, handle)
        audit = reopened.inspect_collection(CID)["audit"]
        assert audit["status"] == "acknowledged_gap"
        assert audit["discontinuity"]["checkpoint_transition"] == receipt["audit_correlation"]
        with pytest.raises(collections.CollectionError, match="RECORD_AUDIT_GAP"):
            inspection = reopened.inspect_collection(CID)
            reopened.rebaseline_collection(CID, **inspection["lifecycle_guards"],
                acknowledged_gap_codes=assessment["gaps"], why="repeat")


def test_native_history_cannot_be_rebaselined(store):
    create(store)
    inspection = store.inspect_collection(CID)
    with pytest.raises(collections.CollectionError, match="RECORD_AUDIT_GAP"):
        store.rebaseline_collection(CID, **inspection["lifecycle_guards"],
            acknowledged_gap_codes=["current-container-mismatch"], why="no historical gap exists")


def test_verified_prefix_reuses_only_accounted_committed_work_and_a_new_handle_rechecks(store, monkeypatch):
    from exomem.collection_store import connection, history
    from exomem.collection_store.writer import CollectionWriter

    create(store)
    _append(store)
    store.inspect_collection(CID)
    original = history.verify_event
    checked = []

    def verify(conn, txn):
        checked.append(txn["txn_id"])
        return original(conn, txn)

    monkeypatch.setattr(history, "verify_event", verify)
    store.inspect_collection(CID)
    assert checked == []
    _correct(store, 2)
    assert len(checked) == 1
    checked.clear()
    insert = store._insert_txn

    manifest = store._projection_manifest(CID)
    page = history.main_path(manifest)
    rendered = history.render_page(store.connection, store.root, manifest, page, cache=store.handle.release_cache)

    def interrupted(txn, receipt):
        insert(txn, receipt)
        assert "correct count to 3" in history.render_page(
            store.connection, store.root, manifest, page, cache=store.handle.release_cache)[1]
        raise RuntimeError("interrupt before commit")

    monkeypatch.setattr(store, "_insert_txn", interrupted)
    watermark = store.connection.execute("SELECT verified_through_txn FROM collections").fetchone()
    with pytest.raises(RuntimeError, match="interrupt"):
        _correct(store, 3)
    assert store.connection.execute("SELECT verified_through_txn FROM collections").fetchone() == watermark
    assert history.render_page(store.connection, store.root, manifest, page, cache=store.handle.release_cache) == rendered
    checked.clear()
    store.inspect_collection(CID)
    assert checked == []  # The preceding public page read already reverified the rolled-back prefix.
    store.connection.execute("UPDATE collections SET verified_through_txn=NULL")
    store.inspect_collection(CID)
    assert len(checked) == 3
    checked.clear()
    path, root = store.handle.path, store.root
    store.handle.close()
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        reopened = CollectionWriter(root, handle)
        assert reopened.inspect_collection(CID)["audit"]["status"] == "ok"
        assert len(checked) == 3
        assert history.render_page(reopened.connection, root, manifest, page, cache=handle.release_cache) == rendered


def test_unknown_imported_provenance_stays_unknown_after_native_writes(tmp_path, store):
    _import_history(tmp_path, store, unknown=True)
    _correct(store, 4)
    inspection = store.inspect_collection(CID)
    assert inspection["audit"] == {"status": "history_incomplete", "gaps": []}
    with pytest.raises(collections.CollectionError, match="RECORD_AUDIT_GAP"):
        store.rebaseline_collection(CID, **inspection["lifecycle_guards"],
            acknowledged_gap_codes=["current-container-mismatch"], why="unknown is not a positive gap")


@pytest.mark.parametrize("restriction", ["named", "default", "purpose"])
@pytest.mark.parametrize("published", [False, True])
def test_history_page_metadata_and_bytes_ignore_hidden_only_transactions(store, restriction, published):
    from test_collection_store_writer import OTHER, manifest_path, manifest_text

    from exomem.collection_store import history

    if published:
        create(store)
    else:
        store.create_collection(manifest_path(), manifest_text(), why="capture", scaffold=False)
    _append(store)
    store.append_record(CID, item={"title": "Withheld", "count": 1}, item_key=OTHER, why="private capture")
    write_scope(store.root, paths=f"Records/**/{OTHER}.md", default_deny=restriction == "default")
    if restriction != "default":
        write_rule(store.root, ceiling=0, extra='purpose: analysis\n' if restriction == "purpose" else "")
    manifest = store._projection_manifest(CID)
    path = history.main_path(manifest)
    file = store.root / path
    if published:
        store.reconcile_views()
        store.reconcile_views()
        physical = file.read_bytes(), file.stat().st_mtime_ns
    else:
        assert not file.exists()
    before = history.render_page(store.connection, store.root, manifest, path, cache=store.handle.release_cache)
    with request_scope(_external("analysis" if restriction == "purpose" else None)):
        pending_before = store.inspect_collection(CID)["projection"]["pending_views"]
    container, version = store.current_guards(CID, OTHER)
    store.update_record(CID, item_key=OTHER, changes={"count": 2}, expected_container_hash=container,
                        expected_item_version=version, why="private correction")
    after = history.render_page(store.connection, store.root, manifest, path, cache=store.handle.release_cache)
    with request_scope(_external("analysis" if restriction == "purpose" else None)):
        assert store.inspect_collection(CID)["projection"]["pending_views"] == pending_before
    if published:
        store.reconcile_views()
        assert (file.read_bytes(), file.stat().st_mtime_ns) == physical
    else:
        assert not file.exists()
        store.reconcile_views()
        assert file.read_text() == after[1]
    assert before == after
    assert "private" not in after[1] and OTHER not in after[1]
    assert "create" in after[1]  # A meaningful effectless transition stays visible.


def test_history_intersection_covers_unnamed_page_only_session_grant_readers(store):
    from exomem.collection_store import history

    create(store)
    _append(store)
    # Standing access names only the owner. A page-only session can release the page without its item.
    write_scope(store.root, paths="Records/**", default_deny=True)
    manifest = store._projection_manifest(CID)
    content = history.render_page(store.connection, store.root, manifest, history.main_path(manifest))[1]
    assert KEY not in content


def test_effectless_control_history_requires_its_whole_scope(store):
    from test_collection_store_writer import OTHER

    from exomem.record_memory import record_memory

    create(store)
    _append(store)
    store.append_record(CID, item={"title": "Withheld"}, item_key=OTHER, why="capture")
    store.record_control_transition("store_reconcile", {CID: {"counts": {"held": 2}, "ids": {}}},
                                    why="private reconciliation rationale")
    from exomem.collection_store import history

    manifest = store._projection_manifest(CID)
    assert "private reconciliation rationale" not in history.render_page(
        store.connection, store.root, manifest, history.main_path(manifest))[1]
    with preview_store(store.root, store.handle):
        public = record_memory(store.root, action="query", collection=CID, include_agent_history=True)
        assert public["agent_history"]["events"][0]["operation"] == "store_reconcile"
    with request_scope(_external()), preview_store(store.root, store.handle):
        released_rows = record_memory(store.root, action="query", collection=CID, include_agent_history=True)
    assert released_rows["total_matched"] == 2
    assert "private reconciliation rationale" not in str(released_rows["agent_history"])
    write_scope(store.root, paths=f"Records/**/{OTHER}.md")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()), preview_store(store.root, store.handle):
        result = record_memory(store.root, action="query", collection=CID, include_agent_history=True)["agent_history"]
    assert result["status"] == "history_incomplete"
    assert "private reconciliation rationale" not in str(result)


def test_mixed_bulk_history_drops_its_reason_and_repairs_stale_pages_at_egress(store):
    from test_records_bulk_upsert import EVIDENCE, _evidence

    from exomem.collection_store import history

    create(store)
    _evidence(store.root)
    container, _ = store.current_guards(CID)
    inserted = store.bulk_upsert_records(CID, rows=[{"item": {"title": "First", "count": 1}},
                                                   {"item": {"title": "Withheld", "count": 1}}],
        expected_container_hash=container, source=EVIDENCE, why="capture observations")
    assert inserted["counts"]["inserted"] == 2
    hidden = inserted["rows"][1]["item_key"]
    manifest = store._projection_manifest(CID)
    path = history.main_path(manifest)
    store.reconcile_views()
    stale = (store.root / path).read_bytes()
    container, _ = store.current_guards(CID)
    updated = store.bulk_upsert_records(CID, rows=[{"item": {"title": "First", "count": 2}},
                                         {"item": {"title": "Withheld", "count": 2}}],
                              expected_container_hash=container, source=EVIDENCE, why="private shared rationale")
    assert updated["counts"]["updated"] == 2
    write_scope(store.root, paths=f"Records/**/{hidden}.md")
    write_rule(store.root, ceiling=0)
    rendered = history.render_page(store.connection, store.root, manifest, path)[1]
    assert "bulk_upsert" in rendered and "private shared rationale" not in rendered
    assert hidden not in rendered
    with request_scope(_external()), store.read_snapshot():
        assert store._operation.projection_decision(path, content=stale,
            manifest_for=lambda cid: manifest).level == 0
    store.reconcile_views()
    current = (store.root / path).read_bytes()
    with request_scope(_external()), store.read_snapshot():
        assert store._operation.projection_decision(path, content=current,
            manifest_for=lambda cid: manifest).level == 6


def test_row_metadata_change_reselects_prior_history_and_repairs_its_page(store):
    """A tag edit must withdraw old contributions, rather than append to a stale release window."""
    from test_collection_store_writer import manifest_path, manifest_text

    from exomem.collection_store import history

    text = manifest_text().replace("    count:", "    tags: {type: array, items: {type: string}}\n    count:")
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    _append(store)
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [restricted]\n")
    write_rule(store.root, ceiling=0)
    store.reconcile_views()
    store.reconcile_views()
    manifest = store._projection_manifest(CID)
    path = history.main_path(manifest)
    assert KEY in (store.root / path).read_text()
    container, version = store.current_guards(CID, KEY)
    store.update_record(CID, item_key=KEY, changes={"tags": ["restricted"]}, why="change row metadata",
                        expected_item_version=version, expected_container_hash=container)
    with request_scope(_external()):
        assert store.inspect_collection(CID)["projection"]["pending_views"] == 1
    store.reconcile_views()
    assert KEY not in (store.root / path).read_text()



def test_new_current_row_releases_a_missing_imported_history_key(tmp_path, store, monkeypatch):
    """Creating an unresolved legacy key must reselect the old stream, not only its suffix."""
    from test_collection_store_writer import OTHER

    from exomem.collection_store import history
    from exomem.governance import lifecycle
    from exomem.governance.principal import owner_principal

    _import_history(tmp_path, store, missing_key=OTHER)
    manifest = store._projection_manifest(CID)
    path = history.main_path(manifest)
    context = history.PageContext(store.connection, store.root, manifest, cache=store.handle.release_cache)
    before = context.window(path)
    assert before.unresolved
    with preview_store(store.root, store.handle), request_scope(_external()):
        denied = records.agent_audit_history(store.root, CID)
        assert f"{1:024x}" not in {event["transition_id"] for event in denied["events"]}
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        released = records.agent_audit_history(store.root, CID)
        assert f"{1:024x}" in {event["transition_id"] for event in released["events"]}
        with monkeypatch.context() as changed:
            changed.setattr(lifecycle, "tombstoned_paths", lambda root: frozenset({collections.record_ref(CID, OTHER)}))
            denied = records.agent_audit_history(store.root, CID)
            assert f"{1:024x}" not in {event["transition_id"] for event in denied["events"]}
    store.append_record(CID, item={"title": "Recovered"}, item_key=OTHER, why="capture recovered key")
    context = history.PageContext(store.connection, store.root, manifest, cache=store.handle.release_cache)
    after = context.window(path)
    assert not after.unresolved
    assert after.released_count == before.released_count + 2


@pytest.mark.parametrize("boundary", ["excluded", "tombstoned"])
def test_history_withdraws_rows_when_current_content_access_changes(store, monkeypatch, boundary):
    """A stable policy fingerprint must not preserve content withdrawn by current access or lifecycle state."""
    from exomem.collection_store import history
    from exomem.governance import lifecycle

    create(store)
    receipt = _append(store)
    manifest = store._projection_manifest(CID)
    path = history.main_path(manifest)
    assert KEY in history.render_page(store.connection, store.root, manifest, path, cache=store.handle.release_cache)[1]
    if boundary == "excluded":
        relative = receipt["affected_paths"][0].removeprefix("Knowledge Base/")
        (store.root / "Knowledge Base/_access.yaml").write_text(f"excluded: [{relative}]\n")
    else:
        monkeypatch.setattr(lifecycle, "tombstoned_paths", lambda root: frozenset({collections.record_ref(CID, KEY)}))
    assert KEY not in history.render_page(store.connection, store.root, manifest, path, cache=store.handle.release_cache)[1]


@pytest.mark.parametrize("boundary", ["excluded", "tombstoned"])
def test_a_running_store_republishes_history_when_access_or_tombstones_change(served, monkeypatch, boundary):
    """The store thread must queue every history page when an access file or tombstone changes, with no census render."""
    from test_collection_store_edit_back import VIEWS, WORK, published_view, store_rows
    from test_collection_store_s1_gate import _until

    from exomem.collection_store import capability, runtime
    from exomem.governance import lifecycle

    root = served.root
    relative, _ = published_view(root, "Withdrawn", count=1)
    page = root / Path(WORK).parent / "_history.md"
    _until(lambda: page.exists() and relative in page.read_text())
    # No edit-back census: only the tick can repair a page, as on a release that publishes views without it.
    monkeypatch.setattr(capability, "RELEASED", (capability.RELEASED - {VIEWS}) | {capability.RECORDS_SUMMARY_V1})
    txns = store_rows(root, "SELECT COUNT(*) FROM txns")
    # Withdraw the page's own path: a withheld row would make the publishing write refuse the whole collection.
    withdrawn_path = page.relative_to(root).as_posix()
    if boundary == "excluded":
        (root / "Knowledge Base/_access.yaml").write_text(f"excluded: [{withdrawn_path.removeprefix('Knowledge Base/')}]\n")
    else:
        monkeypatch.setattr(lifecycle, "tombstoned_paths", lambda _root: frozenset({withdrawn_path}))

    def republished():
        runtime._server(root).call(lambda: None)  # each served request wakes the store thread's views tick
        return relative not in page.read_text()

    _until(republished)
    assert store_rows(root, "SELECT COUNT(*) FROM txns") == txns


def test_blocked_history_policy_cannot_broaden_the_generated_page(store):
    """A failed policy read must refuse instead of releasing every transition through empty scopes."""
    from test_governance_egress import write_broken_policy

    from exomem.collection_store import history

    create(store)
    _append(store)
    manifest = store._projection_manifest(CID)
    write_broken_policy(store.root)
    with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
        history.render_page(store.connection, store.root, manifest, history.main_path(manifest),
                            cache=store.handle.release_cache)


def test_reader_windows_share_the_existing_byte_budget_and_evict_old_policy_namespaces(store):
    """Repeated policy changes must not retain unlimited history reader windows on a serving handle."""
    from exomem.collection_store import history

    create(store)
    _append(store)
    manifest = store._projection_manifest(CID)
    path = history.main_path(manifest)
    for index in range(20):
        write_scope(store.root, paths="Unrelated/**", name=f"Policy revision {index}")
        assert KEY in history.render_page(store.connection, store.root, manifest, path, cache=store.handle.release_cache)[1]
    cache = store.handle.release_cache
    assert len(cache.history_windows) == 16
    assert all(len(window.newest) <= history.MAIN_ENTRIES + history.PAGE_ENTRIES
               and len(window.digests) <= 2 for window in cache.history_windows.values())
    assert cache.inspections._bytes <= cache.inspections.maximum_bytes
    cache.inspections.maximum_bytes = 1
    assert KEY in history.render_page(store.connection, store.root, manifest, path, cache=cache)[1]
    assert not cache.history_windows


def test_external_sql_rechecks_history_release_without_local_total_changes(store):
    """Another SQLite connection must not leave an old history release cached by local change counters."""
    import sqlite3

    from exomem.collection_store import history

    create(store)
    _append(store)
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [restricted]\n")
    write_rule(store.root, ceiling=0)
    manifest = store._projection_manifest(CID)
    path = history.main_path(manifest)
    assert KEY in history.render_page(store.connection, store.root, manifest, path, cache=store.handle.release_cache)[1]
    changes = store.connection.total_changes
    with sqlite3.connect(store.handle.path) as external:
        external.execute("UPDATE items SET governance_json=? WHERE item_key=?",
                         ('{"classes":[],"projects":[],"tags":["restricted"]}', KEY))
    assert store.connection.total_changes == changes
    assert KEY not in history.render_page(store.connection, store.root, manifest, path, cache=store.handle.release_cache)[1]


def test_generated_history_does_not_release_rationale_from_withheld_fields(store):
    """An open history page must not bypass field admission that withholds the same API audit event."""
    from test_collection_store_writer import manifest_path, manifest_text
    from test_governance_egress import _hit

    from exomem import commands
    from exomem.cli_ops import OpError
    from exomem.collection_store import history
    from exomem.governance import egress
    from exomem.record_memory import record_memory

    text = manifest_text().replace("    count:", "    place: {type: string, classification: location}\n    count:")
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    store.append_record(CID, item={"title": "First", "place": "Restricted value"}, item_key=KEY,
                        why="rationale contains restricted field detail")
    store.reconcile_views()
    manifest = store._projection_manifest(CID)
    path = history.main_path(manifest)
    content = (store.root / path).read_bytes()
    with preview_store(store.root, store.handle):
        assert "rationale contains restricted field detail" in commands.op_get(store.root, path=path)["body"]
        with request_scope(_external()):
            result = record_memory(store.root, action="query", collection=CID, include_agent_history=True)
            assert result["agent_history"] == {"status": "history_incomplete", "complete": False,
                                                "truncated": False, "events": []}
            with pytest.raises(OpError, match="RECORD_NOT_FOUND"):
                record_memory(store.root, action="history", collection=CID, item_key=KEY)
            with pytest.raises(ValueError, match="NOT_FOUND"):
                commands.op_get(store.root, path=path)
            assert path not in str(commands.op_list_directory(store.root, path="Knowledge Base", recursive=True))
            assert not egress.annotate_hits(store.root, [_hit(path)]).hits
            with store.read_snapshot():
                assert store._operation.projection_decision(path, content=content,
                                                            manifest_for=lambda cid: manifest).level == 0


def test_history_catchup_keeps_its_target_through_inspection_append_rollback_and_restart(store, monkeypatch):
    """A lagging projector must not skip intermediate pages or replace durable queue progress with inspection state."""
    import sqlite3

    from exomem import held_fs
    from exomem.collection_store import connection, history, views
    from exomem.collection_store.writer import CollectionWriter

    create(store)
    _append(store)
    for count in range(2, 702):
        _correct(store, count)
    store.inspect_collection(CID)  # Selection has reached 702 transitions before discovery starts.
    manifest = store._projection_manifest(CID)
    main = history.main_path(manifest)
    first, second = history.page_path(manifest, 1), history.page_path(manifest, 2)
    store.reconcile_views(limit=1, after=manifest.path)
    assert store.connection.execute("SELECT state FROM projection_state WHERE path=?", (first,)).fetchone() == ("pending",)
    assert store.connection.execute("SELECT 1 FROM projection_state WHERE path=?", (second,)).fetchone() is None
    checkpoint = store.handle.release_cache.history_sweep
    assert checkpoint is not None
    store.connection.set_authorizer(lambda action, arg, *_: sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            store.reconcile_views(limit=1, after=manifest.path)
    finally:
        store.connection.set_authorizer(None)
    assert store.handle.release_cache.history_sweep == checkpoint
    assert store.connection.execute("SELECT 1 FROM projection_state WHERE path=?", (second,)).fetchone() is None
    for count in range(702, 1702):
        _correct(store, count)
    store.inspect_collection(CID)
    assert store.handle.release_cache.history_sweep == checkpoint

    original = views.PublicationBatch._install

    def interrupted(batch, publication):
        raise held_fs.HeldFsError("IO_REFUSED", "interrupted after durable queueing")

    monkeypatch.setattr(views.PublicationBatch, "_install", interrupted)
    store.reconcile_views(limit=1, after=manifest.path)
    assert store.handle.release_cache.history_sweep is None
    assert store.connection.execute("SELECT state FROM projection_state WHERE path=?", (second,)).fetchone() == ("pending",)
    # The next sweep covers the newer suffix across multiple boundaries, while installation still fails.
    store.reconcile_views(paths={main: None})
    for number in (3, 4):
        assert store.connection.execute("SELECT state FROM projection_state WHERE path=?",
                                        (history.page_path(manifest, number),)).fetchone() == ("pending",)
    monkeypatch.setattr(views.PublicationBatch, "_install", original)
    path, root = store.handle.path, store.root
    store.handle.close()
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        reopened = CollectionWriter(root, handle)
        for _ in range(6):
            reopened.reconcile_views()
            if not reopened.connection.execute("SELECT 1 FROM projection_state WHERE state='pending'").fetchone():
                break
        assert not reopened.connection.execute("SELECT 1 FROM projection_state WHERE state='pending'").fetchone()
        assert (root / main).read_text().count("## ") == 200
        assert [(root / history.page_path(manifest, number)).read_text().count("## ")
                for number in range(1, 5)] == [500, 500, 500, 2]


def test_a_refused_collection_holding_the_history_sweep_does_not_stall_the_others(store):
    """Defect: a withheld row refuses its collection's publishing write, and the unfinished history sweep it
    holds then refuses every later window, so a collection sorting after it never publishes."""
    from exomem.collection_store import history

    create(store)
    store.create_collection("Knowledge Base/Records/Zeta/_collection.md",
                            manifest_text().replace(CID, OTHER).replace("title: Work", "title: Zeta"),
                            why="a collection that sorts after Work", scaffold=False)
    _append(store)
    for count in range(2, 702):
        _correct(store, count)
    manifest = store._projection_manifest(CID)
    store.reconcile_views(limit=1, after=manifest.path)
    assert store.handle.release_cache.history_sweep is not None  # Work's page discovery is queued and unfinished
    store.append_record(OTHER, item={"title": "Later"}, item_key="33333333-3333-4333-8333-333333333333",
                        why="a write beside the refused collection")
    row = store.connection.execute("SELECT path FROM projection_state WHERE collection_id=? AND kind='item'",
                                   (CID,)).fetchone()[0]
    (store.root / "Knowledge Base/_access.yaml").write_text(f"excluded: [{row.removeprefix('Knowledge Base/')}]\n")
    for _ in range(3):
        store.reconcile_views(pending_only=True, limit=16)
    later = store.connection.execute("SELECT path,state FROM projection_state WHERE collection_id=? AND kind='item'",
                                     (OTHER,)).fetchone()
    assert later[1] == "current" and "Later" in (store.root / later[0]).read_text()
    # Work no longer holds the single sweep slot, so Zeta's own history page is discovered and published too.
    assert (store.root / history.main_path(store._projection_manifest(OTHER))).exists()
    assert store.connection.execute("SELECT state FROM projection_state WHERE path=?",
                                    (history.main_path(manifest),)).fetchone() == ("pending",)
    assert store.connection.execute("SELECT state FROM projection_state WHERE path=?",
                                    (history.page_path(manifest, 1),)).fetchone() == ("pending",)


def test_interrupted_discovery_keeps_negative_holes_and_page_specific_readers_private(store):
    """Cold discovery must advance past an absent page, then queue a differently governed page after a cutoff."""
    from test_governance_egress import _gov_dir

    from exomem import commands
    from exomem.collection_store import history

    create(store)
    store.reconcile_views()
    _append(store)
    for count in range(2, 702):
        _correct(store, count)
    manifest = store._projection_manifest(CID)
    main, first, second = history.main_path(manifest), history.page_path(manifest, 1), history.page_path(manifest, 2)
    write_scope(store.root, paths=f"Records/**/{KEY}.md")
    write_rule(store.root, ceiling=0)
    page_scope, page_rule = "01ARZ3NDEKTSV4RRFFQ69G5FC2", "01ARZ3NDEKTSV4RRFFQ69G5FC3"
    directory = _gov_dir(store.root)
    (directory / "scopes/page.yaml").write_text(
        f'governance_version: 1\nid: {page_scope}\npaths: ["Records/**/_history/0002.md"]\n')
    (directory / "rules/page.yaml").write_text(
        f'governance_version: 1\nid: {page_rule}\nkind: org_cap\nscope_ids: ["{page_scope}"]\n'
        f'audience: {_external().audience_id}\nceiling: 0\n')
    prepared_after_cutoff = False
    for _ in range(16):
        work = [0]

        def progressed(work=work):
            work[0] += 1
            return 0

        store.connection.set_progress_handler(progressed, 100)
        try:
            store.reconcile_views(limit=1, after=manifest.path, stop=lambda work=work: work[0] >= 100)
        finally:
            store.connection.set_progress_handler(None, 0)
        sweep = store.handle.release_cache.history_sweep
        prepared_after_cutoff |= sweep is not None and sweep.prepared is not None
        store.inspect_collection(CID)
        assert store.handle.release_cache.history_sweep == sweep
        if sweep is None and store.connection.execute("SELECT 1 FROM projection_state WHERE path=?", (second,)).fetchone():
            break
    assert prepared_after_cutoff
    assert store.handle.release_cache.history_sweep is None
    assert not store.connection.execute("SELECT 1 FROM projection_state WHERE path=?", (first,)).fetchone()
    assert store.connection.execute("SELECT state FROM projection_state WHERE path=?", (second,)).fetchone() == ("pending",)
    with request_scope(_external()):
        # The page's own cap applies before its file exists, including pending navigation metadata.
        assert store.inspect_collection(CID)["projection"]["pending_views"] == 0
    for _ in range(3):
        store.reconcile_views()
    assert not (store.root / first).exists()
    content = (store.root / second).read_text()
    assert content.count("## ") == 2
    assert "[[" + main not in content and "[[" + first not in content
    with request_scope(_external()), preview_store(store.root, store.handle):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_get(store.root, path=second)
        assert second not in str(commands.op_list_directory(store.root, path="Knowledge Base", recursive=True))
