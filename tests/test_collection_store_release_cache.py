"""Canonical cache work is incremental without reusing operation authority."""

import json
import sqlite3

import pytest
from test_collection_store_writer import CID, KEY, OTHER, create, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope

from exomem import structured_collections as collections
from exomem.collection_store import governance
from exomem.collection_store.preview import preview_store
from exomem.governance import membership
from exomem.governance.principal import request_scope
from exomem.record_memory import record_memory


def test_warm_nonuniform_writes_classify_only_the_changed_subject_across_bindings(store, monkeypatch):
    create(store)
    for index in range(8):
        store.append_record(CID, item={"title": f"Row {index}"}, why="seed")
    write_scope(store.root, paths=f"Records/**/{KEY}.md")
    write_rule(store.root, ceiling=6)
    classified = []
    evaluate = membership.evaluate_metadata

    def counted(subject, candidate):
        classified.append(subject.refs)
        return evaluate(subject, candidate)

    monkeypatch.setattr(membership, "evaluate_metadata", counted)
    with request_scope(_external()), preview_store(store.root, store.handle):
        first = record_memory(store.root, "append", collection=CID,
                              item={"title": "Warm"}, item_key=KEY, why="capture")
    classified.clear()
    statements = []
    store.connection.set_trace_callback(statements.append)
    try:
        with request_scope(_external()), preview_store(store.root, store.handle):
            second = record_memory(store.root, "update", collection=CID, item_key=KEY,
                                   changes={"count": 2}, why="correct",
                                   expected_container_hash=first["after_container_hash"],
                                   expected_item_version=first["after_item_hash"])
    finally:
        store.connection.set_trace_callback(None)
    assert second["outcome"] == "committed"
    assert classified == [(collections.record_ref(CID, KEY),)]
    assert not any("FROM items WHERE collection_id=" in statement and "item_key" not in statement.partition("WHERE")[2]
                   for statement in statements if statement.startswith("SELECT row_id,item_key"))
    assert store.connection.execute("SELECT row_version FROM items WHERE item_key=?", (KEY,)).fetchone() == (2,)


def test_warm_partial_refusal_does_not_rebuild_or_evaluate_the_collection(store, monkeypatch):
    create(store)
    store.append_record(CID, item={"title": "Hidden"}, item_key=KEY, why="seed")
    write_scope(store.root, paths=f"Records/**/{KEY}.md")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.append_record(CID, item={"title": "New"}, item_key=OTHER, why="capture")
    evaluated = []
    decide = governance.decide

    def counted(*args, **kwargs):
        evaluated.append(args)
        return decide(*args, **kwargs)

    monkeypatch.setattr(governance, "decide", counted)
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.append_record(CID, item={"title": "New"}, item_key=OTHER, why="capture")
    assert evaluated == []


def test_manifest_bindings_return_fresh_heads_and_revised_contract(store):
    create(store)
    with preview_store(store.root, store.handle):
        collections.load_manifest(store.root, manifest_path())
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    with preview_store(store.root, store.handle):
        manifest = collections.load_manifest(store.root, manifest_path())
    assert manifest.audit_head == store.connection.execute("SELECT audit_head FROM collections").fetchone()[0]
    store.revise_collection(CID, manifest_text=manifest_text().replace("title: Work", "title: Revised"),
                            expected_manifest_hash=manifest.manifest_version.hash,
                            expected_container_hash=first["after_container_hash"], why="revise")
    with preview_store(store.root, store.handle):
        revised = collections.load_manifest(store.root, manifest_path())
    assert revised.title == "Revised"
    assert revised.manifest_version.hash != manifest.manifest_version.hash


def test_failed_commit_cannot_authorize_different_content_at_the_reused_row_version(store, monkeypatch):
    """A precommit-approved version must not outlive the failed durable boundary."""
    text = manifest_text().replace("    count:", "    tags: {type: array, items: {type: string}}\n    count:")
    store.create_collection(manifest_path(), text, why="create")
    first = store.append_record(CID, item={"title": "One", "tags": []}, item_key=KEY, why="capture")
    write_scope(store.root, paths="Unrelated/**")
    path = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    path.write_text(path.read_text() + "tags: [secret]\n")
    write_rule(store.root, ceiling=0)
    before = tuple(store.connection.iterdump())
    precommit = store._precommit

    def fail_commit(manifest):
        precommit(manifest)
        store.connection.execute("PRAGMA defer_foreign_keys=ON")
        store.connection.execute("INSERT INTO item_sources VALUES(-1,1,0,'missing')")

    arguments = dict(item_key=KEY, expected_container_hash=first["after_container_hash"],
                     expected_item_version=first["after_item_hash"], why="correct")
    monkeypatch.setattr(store, "_precommit", fail_commit)
    with request_scope(_external()), pytest.raises(sqlite3.IntegrityError):
        store.update_record(CID, changes={"count": 2}, **arguments)
    assert tuple(store.connection.iterdump()) == before
    monkeypatch.setattr(store, "_precommit", precommit)
    with request_scope(_external()), pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
        store.update_record(CID, changes={"tags": ["secret"]}, **arguments)
    assert tuple(store.connection.iterdump()) == before
    with request_scope(_external()):
        store.update_record(CID, changes={"count": 3}, **arguments)
    stored = store.connection.execute("SELECT row_version,values_json FROM items WHERE item_key=?", (KEY,)).fetchone()
    assert stored[0] == 2 and json.loads(stored[1])["count"] == 3


def test_rebound_canonical_manifest_cannot_supply_another_collections_identity(store):
    """A cached contract must validate identity after trusted header replacement."""
    import hashlib

    create(store)
    with preview_store(store.root, store.handle):
        assert collections.load_manifest(store.root, manifest_path()).collection_id == CID
        text = manifest_text().replace(CID, OTHER)
        with store.handle.transaction() as conn:
            conn.execute(
                "INSERT INTO collection_manifests SELECT collection_id,2,?,?,schema_json,natural_key_json,txn_id,governance_json "
                "FROM collection_manifests WHERE collection_id=? AND manifest_version=1",
                (text, hashlib.sha256(text.encode()).hexdigest(), CID),
            )
            conn.execute("UPDATE collections SET manifest_version=2 WHERE collection_id=?", (CID,))
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            collections.load_manifest(store.root, manifest_path())


@pytest.mark.parametrize("before_authorization", [True, False])
def test_unreported_sql_cannot_borrow_or_publish_a_warm_release(store, monkeypatch, before_authorization):
    """A sibling change must invalidate both proposed and committed authority."""
    from test_due_state_bulk_carriers import _command

    from exomem import writer_lease

    text = manifest_text().replace("    count:", "    tags: {type: array, items: {type: string}}\n    count:")
    store.create_collection(manifest_path(), text, why="create")
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    second = store.append_record(CID, item={"title": "Other"}, item_key=OTHER, why="capture")
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [secret]\n")
    write_rule(store.root, ceiling=0)
    original = store._precommit if before_authorization else store.handle.release_cache.prepare

    def interleave(manifest=None):
        store.connection.execute("UPDATE items SET governance_json=? WHERE item_key=?",
                                 ('{"classes":[],"projects":[],"tags":["secret"]}', OTHER))
        if before_authorization:
            original(manifest)
        else:
            original()

    with request_scope(_external()), preview_store(store.root, store.handle):
        def query():
            return writer_lease.invoke_command(_command("record_memory"), store.root,
                       action="query", collection=CID, response_detail="full")
        assert query()["total_matched"] == 2
        before = tuple(store.connection.iterdump())
        if before_authorization:
            monkeypatch.setattr(store, "_precommit", interleave)
        else:
            monkeypatch.setattr(store.handle.release_cache, "prepare", interleave)
        arguments = dict(changes={"count": 2}, item_key=KEY, why="correct",
                         expected_container_hash=second["after_container_hash"],
                         expected_item_version=first["after_item_hash"])
        if before_authorization:
            with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
                store.update_record(CID, **arguments)
            assert tuple(store.connection.iterdump()) == before
        else:
            store.update_record(CID, **arguments)
        expected = 2 if before_authorization else 1
        assert query()["total_matched"] == expected
        store.handle.release_cache.clear()
        assert query()["total_matched"] == expected


def test_unexplained_transaction_rollback_does_not_leave_released_subject_metadata(store):
    """Joining trusted SQL must not retain its proposed release after rollback."""
    text = manifest_text().replace("    count:", "    tags: {type: array, items: {type: string}}\n    count:")
    store.create_collection(manifest_path(), text, why="create")
    store.append_record(CID, item={"title": "One", "tags": ["secret"]}, item_key=KEY, why="capture")
    store.append_record(CID, item={"title": "Two", "tags": []}, item_key=OTHER, why="capture")
    write_scope(store.root, paths="Unrelated/**")
    path = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    path.write_text(path.read_text() + "tags: [secret]\n")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()), preview_store(store.root, store.handle):
        assert record_memory(store.root, "query", collection=CID)["total_matched"] == 1
        store.connection.execute("BEGIN")
        try:
            store.connection.execute("UPDATE items SET governance_json=? WHERE item_key=?",
                                     ('{"classes":[],"projects":[],"tags":[]}', KEY))
            assert record_memory(store.root, "query", collection=CID)["total_matched"] == 2
        finally:
            store.connection.execute("ROLLBACK")
        assert record_memory(store.root, "query", collection=CID)["total_matched"] == 1


def test_warm_complete_release_rechecks_lifecycle_even_without_a_row_change(store, monkeypatch):
    """A fresh tombstone snapshot must invalidate a previously complete mutation release."""
    from exomem.governance import lifecycle

    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    with request_scope(_external()):
        assert store.inspect_collection(CID)["coverage"]["committed"] == 1
        store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    monkeypatch.setattr(lifecycle, "tombstoned_paths", lambda root: frozenset({collections.record_ref(CID, KEY)}))
    before = tuple(store.connection.iterdump())
    with request_scope(_external()):
        assert store.inspect_collection(CID)["coverage"]["committed"] == 0
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.append_record(CID, item={"title": "Two"}, item_key=OTHER, why="capture")
    assert tuple(store.connection.iterdump()) == before


def test_warm_inspection_keeps_l0_and_policy_denial_receipts_distinct(store, monkeypatch):
    """A cached L6 policy result cannot override a fresh tombstone's L0 receipt."""
    from exomem.governance import egress, lifecycle

    create(store)
    for key in (KEY, OTHER, "33333333-3333-4333-8333-333333333333"):
        store.append_record(CID, item={"title": key}, item_key=key, why="seed")
    with pytest.raises(collections.CollectionError) as error:
        store.append_record(CID, item={"title": "Held", "count": "invalid"}, why="seed")
    write_scope(store.root, paths=error.value.details["held"]["path"])
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [{collections.record_ref(CID, OTHER)}]\n")
    write_rule(store.root, ceiling=2)
    who = _external()
    with preview_store(store.root, store.handle), request_scope(who):
        store.inspect_collection(CID)
        monkeypatch.setattr(lifecycle, "tombstoned_paths", lambda root: frozenset({collections.record_ref(CID, KEY)}))
        with egress.disclosure_boundary(store.root, "inspect") as collector:
            result = store.inspect_collection(CID)
            actual = egress._bounded_outcomes(collector.outcomes)
        with store.read_snapshot(), store._authorization(mutation=False) as operation:
            catalog = operation.canonical_subjects(CID)
            decisions = tuple(operation.decision(subject) for subject in catalog)
            with egress.disclosure_boundary(store.root, "inspect") as collector:
                for subject, decision in zip(catalog, decisions, strict=True):
                    egress._outcome_for_decision(
                        store.root, subject.basis.identity, decision=decision, policy=operation.policy,
                        audience=who.audience_id, outcome="release_authorized" if decision.level >= 6 else "withheld",
                        content_hash=subject.basis.payload_hash, purpose=operation.purpose, purpose_is_bound=True)
                expected = egress._bounded_outcomes(collector.outcomes)
        assert sorted(decision.level for decision in decisions) == [0, 2, 2, 6, 6]
        assert actual == expected
        assert result["coverage"]["committed"] == 1
        assert [notice["level"] for notice in result["governance"]["notices"]] == [2, 2]


@pytest.mark.parametrize("changed", ["revoked", "expired", "authority-unavailable"])
def test_warm_canonical_release_never_reuses_session_authority(store, monkeypatch, changed):
    """Unchanged canonical rows cannot keep a grant whose current authority is gone."""
    from test_collection_store_governance import NOW, inspection_token, redeem, session

    from exomem.governance import store as authority_store

    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    who = session(store, monkeypatch)
    with preview_store(store.root, store.handle):
        redeem(store, who, inspection_token(store, who))
        with request_scope(who):
            assert store.inspect_collection(CID)["coverage"]["committed"] == 1
        if changed == "authority-unavailable":
            def unavailable(*args, **kwargs):
                raise sqlite3.OperationalError("authority unavailable")

            monkeypatch.setattr(authority_store, "open_authorization_session_connection", unavailable)
            with request_scope(who), pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
                store.inspect_collection(CID)
        else:
            conn = authority_store.open_authorization_session_connection(store.root)
            try:
                if changed == "revoked":
                    conn.execute("UPDATE governance_session_grants SET status='revoked'")
                else:
                    conn.execute("UPDATE governance_session_grants SET expires_at=?", (NOW + 10,))
                    monkeypatch.setattr("time.time", lambda: NOW + 11)
                conn.commit()
            finally:
                conn.close()
            with request_scope(who):
                assert store.inspect_collection(CID)["coverage"]["committed"] == 0


def _create_filename_collection(store):
    text = manifest_text().replace("natural_key: [title]", "natural_key: [title, count]")
    text = text.replace("storage:\n", "item_filename:\n  version: 1\n  fields: [title]\nstorage:\n")
    store.create_collection(manifest_path(), text, why="create")


def test_warm_inspection_refreshes_frequencies_and_unchanged_collision_siblings(store):
    """A changed row must update counts and the unchanged sibling's filename finding."""
    _create_filename_collection(store)
    first = store.append_record(CID, item={"title": "Straße", "count": 1}, item_key=KEY, why="seed")
    latest = store.append_record(CID, item={"title": "STRASSE", "count": 2}, item_key=OTHER, why="seed")
    before = store.inspect_collection(CID)
    assert before["presentation"]["counts"]["filename_drift"] == 0
    store.update_record(CID, item_key=KEY, changes={"title": "Elsewhere"}, why="correct",
                        expected_container_hash=latest["after_container_hash"],
                        expected_item_version=first["after_item_hash"])
    current = store.inspect_collection(CID)
    assert {entry["value"] for entry in current["observed_values"]["title"]["values"]} == {"Elsewhere", "STRASSE"}
    sibling = [finding for finding in current["presentation"]["items"] if finding["item_key"] == OTHER]
    assert any(finding["state"] == "filename_drift" for finding in sibling)
    assert all(finding["version"] == latest["after_item_hash"] for finding in sibling)
    mutated = store.inspect_collection(CID)
    mutated["presentation"]["items"][0]["state"] = "Caller mutation"
    assert store.inspect_collection(CID) == current
    store.handle.release_cache.clear()
    assert store.inspect_collection(CID) == current


def test_warm_inspection_decrements_full_frequency_keys_and_removes_the_last_occurrence(store):
    """Replacing a shared display prefix must preserve distinct full keys and their ranks."""
    text = manifest_text().replace("natural_key: [title]", "natural_key: [title, count]")
    store.create_collection(manifest_path(), text, why="create")
    prefix = "a" * 120
    receipts = [store.append_record(CID, item={"title": title, "count": index}, why="seed")
                for index, title in enumerate((prefix + "A", prefix + "A", prefix + "B", "zeta"))]
    before = store.inspect_collection(CID)
    assert before["observed_values"]["title"]["values"] == [
        {"value": prefix, "count": 2, "value_truncated": True},
        {"value": prefix, "count": 1, "value_truncated": True},
        {"value": "zeta", "count": 1, "value_truncated": False},
    ]
    latest = receipts[-1]
    for index in (0, 1):
        latest = store.update_record(
            CID, item_key=receipts[index]["item_key"], changes={"title": prefix + "B"}, why="correct",
            expected_container_hash=latest["after_container_hash"],
            expected_item_version=receipts[index]["after_item_hash"],
        )
        current = store.inspect_collection(CID)
        expected = [
            {"value": prefix, "count": index + 2, "value_truncated": True},
            *([{"value": prefix, "count": 1, "value_truncated": True}] if index == 0 else []),
            {"value": "zeta", "count": 1, "value_truncated": False},
        ]
        assert current["observed_values"]["title"]["values"] == expected
        store.handle.release_cache.clear()
        assert store.inspect_collection(CID) == current


def test_inspection_overflow_and_unreported_sql_keep_full_public_data(store):
    """A bounded cache must neither truncate data nor hide a same-version trusted change."""
    create(store)
    store.append_record(CID, item={"title": "Initial"}, item_key=KEY, why="seed")
    before = store.inspect_collection(CID)
    before["observed_values"]["title"]["values"][0]["value"] = "Caller mutation"
    assert store.inspect_collection(CID)["observed_values"]["title"]["values"][0]["value"] == "Initial"
    # Trusted SQL is deliberately unexplained, even though its version/hash did not move.
    store.connection.execute("UPDATE items SET values_json=? WHERE item_key=?", ('{"title":"Changed"}', KEY))
    current = store.inspect_collection(CID)
    assert current["observed_values"]["title"]["values"][0]["value"] == "Changed"
    cache = store.handle.release_cache.inspections
    cache.clear()
    cache.maximum_bytes = 1
    assert store.inspect_collection(CID) == current


def test_grant_only_release_change_refreshes_a_warm_filename_group(store, monkeypatch):
    """Revoking one row changes an unchanged, still-granted sibling's collision suffix."""
    from test_collection_store_governance import inspection_token, redeem, session

    from exomem.governance import store as authority_store

    _create_filename_collection(store)
    store.append_record(CID, item={"title": "Straße", "count": 1}, item_key=KEY, why="seed")
    sibling = store.append_record(CID, item={"title": "STRASSE", "count": 2}, item_key=OTHER, why="seed")
    who = session(store, monkeypatch)
    with preview_store(store.root, store.handle):
        redeem(store, who, inspection_token(store, who))
        redeem(store, who, inspection_token(store, who))
        with request_scope(who):
            before = store.inspect_collection(CID)
        assert before["coverage"]["committed"] == 2
        assert before["presentation"]["counts"]["filename_drift"] == 0
        conn = authority_store.open_authorization_session_connection(store.root)
        try:
            conn.execute("UPDATE governance_session_grants SET status='revoked' WHERE json_extract(paths,'$[0]')=?",
                         (collections.record_ref(CID, KEY),))
            conn.commit()
        finally:
            conn.close()
        with request_scope(who):
            after = store.inspect_collection(CID)
        assert after["coverage"]["committed"] == 1
        assert after["observed_values"]["title"]["values"] == [
            {"value": "STRASSE", "count": 1, "value_truncated": False},
        ]
        assert after["presentation"]["items"] == [{
            "item_key": OTHER, "path": sibling["affected_paths"][0], "version": sibling["after_item_hash"],
            "state": "filename_drift", "remedy": "structured_files_preview",
        }]


def test_portable_filename_collisions_survive_fresh_bindings_and_bulk_planning(store):
    """Cached occupancy and batch-local choices must preserve renderer allocation."""
    from test_collection_store_bulk import _evidence, bulk

    _create_filename_collection(store)
    for key, item in ((KEY, {"title": "Straße", "count": 1}),
                      (OTHER, {"title": "STRASSE", "count": 2})):
        with preview_store(store.root, store.handle):
            record_memory(store.root, "append", collection=CID, item=item, item_key=key, why="capture")
    _evidence(store.root)
    result = bulk(store, [{"item": {"title": "ＳＴＲＡＳＳＥ", "count": 3}},
                          {"item": {"title": "Strasse", "count": 4}}])
    assert result["counts"]["inserted"] == 2
    with preview_store(store.root, store.handle):
        manifest = collections.load_manifest(store.root, manifest_path())
    occupied = []
    for key, encoded, path in store.connection.execute("SELECT item_key,values_json,view_path FROM items ORDER BY row_id"):
        assert path == collections.render_item_path(manifest, json.loads(encoded), key, occupied_paths=occupied)
        occupied.append(path)
    assert len({collections._portable_path_key(path) for path in occupied}) == 4


def test_failed_filename_commit_does_not_reserve_the_candidate_path(store, monkeypatch):
    """A rolled-back allocation must not force the next distinct row to a suffix."""
    _create_filename_collection(store)
    store.append_record(CID, item={"title": "Seed", "count": 0}, why="seed")
    before = tuple(store.connection.iterdump())
    precommit = store._precommit

    def fail_commit(manifest):
        precommit(manifest)
        store.connection.execute("PRAGMA defer_foreign_keys=ON")
        store.connection.execute("INSERT INTO item_sources VALUES(-1,1,0,'missing')")

    monkeypatch.setattr(store, "_precommit", fail_commit)
    with pytest.raises(sqlite3.IntegrityError):
        store.append_record(CID, item={"title": "Reserved", "count": 1}, item_key=KEY, why="capture")
    assert tuple(store.connection.iterdump()) == before
    monkeypatch.setattr(store, "_precommit", precommit)
    with preview_store(store.root, store.handle):
        result = record_memory(store.root, "append", collection=CID, item={"title": "Reserved", "count": 2},
                               item_key=OTHER, why="capture")
    assert result["affected_paths"] == ["Knowledge Base/Records/Work/Items/Reserved.md"]


def test_unknown_sql_rebuilds_portable_filename_occupancy_with_duplicate_keys(store):
    """External path changes must neither lose a sibling occupant nor reserve a freed path."""
    _create_filename_collection(store)
    first = store.append_record(CID, item={"title": "Straße", "count": 1}, item_key=KEY, why="capture")
    store.append_record(CID, item={"title": "STRASSE", "count": 2}, item_key=OTHER, why="capture")
    source = "Knowledge Base/Records/Work/Items"
    with store.handle.transaction() as conn:
        conn.execute("UPDATE items SET view_path=? WHERE item_key=?", (f"{source}/ＳＴＲＡＳＳＥ.md", OTHER))
    # Updating one unchanged path must retain the other portable-equivalent occupant.
    guards = store.inspect_collection(CID)["lifecycle_guards"]
    store.update_record(CID, item_key=KEY, changes={"count": 10},
                        expected_container_hash=guards["expected_container_hash"],
                        expected_item_version=first["after_item_hash"], why="correct")
    with store.handle.transaction() as conn:
        conn.execute("UPDATE items SET view_path=? WHERE item_key=?", (f"{source}/Moved.md", KEY))
    remaining = store.append_record(CID, item={"title": "Strasse", "count": 3}, why="capture")
    assert remaining["affected_paths"][0] != f"{source}/Strasse.md"
    with store.handle.transaction() as conn:
        conn.execute("UPDATE items SET view_path=? WHERE item_key=?", (f"{source}/Moved again.md", OTHER))
    freed = store.append_record(CID, item={"title": "Strasse", "count": 4}, why="capture")
    assert freed["affected_paths"] == [f"{source}/Strasse.md"]
