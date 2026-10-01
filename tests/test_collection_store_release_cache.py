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


def test_contract_cache_survives_bindings_but_returns_fresh_heads_and_revised_contract(store, monkeypatch):
    create(store)
    with preview_store(store.root, store.handle):
        collections.load_manifest(store.root, manifest_path())
    parsed = []
    parse = collections.parse_manifest_bytes

    def counted(*args, **kwargs):
        parsed.append(args[1])
        return parse(*args, **kwargs)

    monkeypatch.setattr(collections, "parse_manifest_bytes", counted)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    with preview_store(store.root, store.handle):
        manifest = collections.load_manifest(store.root, manifest_path())
    assert manifest.audit_head == store.connection.execute("SELECT audit_head FROM collections").fetchone()[0]
    assert parsed == []
    store.revise_collection(CID, manifest_text=manifest_text().replace("title: Work", "title: Revised"),
                            expected_manifest_hash=manifest.manifest_version.hash,
                            expected_container_hash=first["after_container_hash"], why="revise")
    with preview_store(store.root, store.handle):
        revised = collections.load_manifest(store.root, manifest_path())
    assert revised.title == "Revised"
    assert revised.manifest_version.hash != manifest.manifest_version.hash
    assert parsed


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
    original = store._precommit

    def interleave(manifest):
        if not before_authorization:
            original(manifest)
        store.connection.execute("UPDATE items SET governance_json=? WHERE item_key=?",
                                 ('{"classes":[],"projects":[],"tags":["secret"]}', OTHER))
        if before_authorization:
            original(manifest)

    with request_scope(_external()), preview_store(store.root, store.handle):
        def query():
            return writer_lease.invoke_command(_command("record_memory"), store.root,
                       action="query", collection=CID, response_detail="full")
        assert query()["total_matched"] == 2
        before = tuple(store.connection.iterdump())
        monkeypatch.setattr(store, "_precommit", interleave)
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
