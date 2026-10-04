"""Private deferred creation keeps canonical commits ahead of marker admission."""

import json
import sqlite3
from pathlib import PureWindowsPath

import pytest
from test_collection_store_runtime import runtime as runtime
from test_collection_store_writer import CID, manifest_path, manifest_text
from test_collection_store_writer import store as store

from exomem import records
from exomem import structured_collections as collections
from exomem.collection_store import admission, authority, chain, connection
from exomem.collection_store.preview import preview_store
from exomem.collection_store.writer import CollectionWriter


@pytest.fixture
def mixed(store):
    for profile, cid in (
        ("records", "11111111-1111-4111-8111-111111111111"),
        ("planning", "22222222-2222-4222-8222-222222222222"),
    ):
        records.create_collection(
            store.root, manifest_path(profile).replace("Work", "Legacy"),
            manifest_text(profile).replace(CID, cid), why="legacy fixture", scaffold=False,
        )
    before = {p.relative_to(store.root): p.read_bytes()
              for p in store.root.rglob("*") if p.is_file()}
    sid = store.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    marker = {
        "version": 1, "mode": "store", "default_authority": "file", "store_id": sid,
        "authority_epoch": 1,
        "collections": [{"collection_id": CID, "manifest_path": manifest_path(),
                         "authority": "store", "store_id": sid}],
        "collection_store_fence": {"capability": "collections-store-v1", "generation": 1},
    }
    return store, json.dumps(marker).encode(), before


def prepare(store, target, **changes):
    args = dict(why="prepare summary", scaffold=False, request_id="summary-create",
                expected_marker=None, target_marker=target)
    args.update(changes)
    return admission._prepare_create(store, manifest_path(), manifest_text(), **args)


def test_failed_commit_leaves_no_scaffold_or_intent(mixed):
    # A rejected COMMIT must not leave C directories/stages, or a recoverable false success.
    store, target, before = mixed
    store.connection.set_authorizer(lambda action, arg, *_: sqlite3.SQLITE_DENY
                                    if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
                                    else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            prepare(store, target)
    finally:
        store.connection.set_authorizer(None)
    assert not (store.root / manifest_path()).parent.exists()
    assert not authority.marker_path(store.root).exists()
    assert store.connection.execute("SELECT COUNT(*) FROM collections").fetchone() == (0,)
    assert store.connection.execute("SELECT COUNT(*) FROM txns").fetchone() == (0,)
    assert admission._recover_create(store) is None
    assert all((store.root / path).read_bytes() == raw for path, raw in before.items())


def test_restart_replays_same_commit_and_reports_current_verified_head(mixed):
    # Restart must not mint another collection/receipt, or confuse its creation head with a later head.
    store, target, before = mixed
    first = prepare(store, target)
    db = store.handle.path
    store.handle.close()
    with connection.open_writer(db, lease_check=lambda: True) as handle:
        reopened = CollectionWriter(store.root, handle)
        retry = prepare(reopened, target)
        assert retry == first
        assert retry["status"] == "pending"
        assert reopened.connection.execute("SELECT COUNT(*) FROM txns").fetchone() == (1,)
        reopened.create_collection(
            manifest_path().replace("Work", "Preview"),
            manifest_text().replace(CID, "33333333-3333-4333-8333-333333333333"),
            why="unrelated dark preview", scaffold=False,
        )
        recovered = admission._recover_create(reopened)
        assert recovered["receipt"] == first["receipt"]
        assert recovered["txn_id"] == first["txn_id"]
        assert recovered["head"] == dict(zip(("commit_seq", "head_hash"),
                                             chain.verify_store_chain(reopened.connection), strict=True))
        assert recovered["head"]["commit_seq"] == 2
        with pytest.raises(collections.CollectionError, match="IDEMPOTENCY_KEY_REUSED"):
            prepare(reopened, target, why="different request")
        with pytest.raises(connection.CollectionStoreError, match="CREATE_CONFLICT"):
            prepare(reopened, target + b" ")
    assert not (store.root / manifest_path()).parent.exists()
    assert all((store.root / path).read_bytes() == raw for path, raw in before.items())


def test_reconcile_does_not_expose_hidden_create_or_recover_its_orphans(mixed):
    # Generic recovery must not publish C or erase a stage in C's directory before marker cutover.
    store, target, before = mixed
    prepare(store, target)
    result = store.reconcile_views()
    assert "projection_pending" in result["warnings"]
    assert not (store.root / manifest_path()).parent.exists()
    parent = (store.root / manifest_path()).parent
    parent.mkdir(parents=True)
    orphan = parent / (".exomem-collection-stage-" + "a" * 32)
    orphan.write_bytes(b"interrupted input")
    store.reconcile_views()
    assert orphan.read_bytes() == b"interrupted input"
    assert not (store.root / manifest_path()).exists()
    assert all((store.root / path).read_bytes() == raw for path, raw in before.items())


def test_conflicting_marker_preserves_pending_create_and_receipt(mixed):
    # Malformed/changed marker bytes cannot be mistaken for absence or overwritten by recovery.
    store, target, _ = mixed
    first = prepare(store, target)
    marker = authority.marker_path(store.root)
    marker.parent.mkdir(parents=True)
    for raw in (b"not json", target.replace(b'"authority_epoch": 1', b'"authority_epoch": 2')):
        marker.write_bytes(raw)
        recovered = admission._recover_create(store)
        assert recovered["status"] == "conflict"
        assert recovered["receipt"] == first["receipt"]
        store.reconcile_views()
        assert marker.read_bytes() == raw
        assert not (store.root / manifest_path()).parent.exists()
    assert store.connection.execute("SELECT COUNT(*) FROM txns").fetchone() == (1,)


def test_pending_orphan_cleanup_preserves_portable_descendants(mixed):
    # A pending collection's nested scratch must stay protected with native Windows separators.
    store, target, _ = mixed
    prepare(store, target)
    assert not authority.orphan_cleanup_eligible(
        store, PureWindowsPath("Knowledge Base/Records/Work/Held")
    )
    assert authority.orphan_cleanup_eligible(
        store, PureWindowsPath("Knowledge Base/Records/Workshops")
    )


def test_exact_marker_target_allows_existing_no_clobber_projection(mixed):
    # Cutover may publish C through existing recovery without touching legacy A/B or creating another txn.
    store, target, before = mixed
    first = prepare(store, target)
    marker = authority.marker_path(store.root)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(target)  # Isolated fixture stands in for the future fenced marker producer.
    recovered = admission._recover_create(store)
    assert recovered["status"] == "marker_admitted"
    assert recovered["receipt"] == first["receipt"]
    store.reconcile_views()
    assert (store.root / manifest_path()).is_file()
    store.reconcile_views()
    assert store.connection.execute("SELECT state FROM projection_state").fetchall() == [("current",)]
    assert store.connection.execute("SELECT COUNT(*) FROM txns").fetchone() == (1,)
    assert admission._recover_create(store)["receipt"] == first["receipt"]
    assert all((store.root / path).read_bytes() == raw for path, raw in before.items())


@pytest.mark.parametrize("admitted_before_commit", [False, True])
def test_pending_create_preserves_shared_orphans_but_recovers_admitted_views(mixed, admitted_before_commit):
    # A neighbor's admitted item must recover without assigning its identity to C's orphan.
    store, target, _ = mixed
    earlier_id = "33333333-3333-4333-8333-333333333333"
    earlier_key = "44444444-4444-4444-8444-444444444444"
    earlier_path = manifest_path().replace("Work", "Earlier")
    earlier_text = manifest_text().replace(CID, earlier_id).replace(
        "source: Items", "source: Knowledge Base/Records/Work"
    )
    store.create_collection(earlier_path, earlier_text,
                            why="earlier preview", scaffold=False)
    store.append_record(earlier_id, item={"title": "Earlier row"}, item_key=earlier_key, why="earlier row")
    old = json.loads(target)
    old["collections"][0].update(collection_id=earlier_id, manifest_path=earlier_path)
    old_bytes = json.dumps(old).encode()
    marker = authority.marker_path(store.root)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(old_bytes)
    new = json.loads(target)
    new["collections"] = old["collections"] + new["collections"]
    new["authority_epoch"] = 2
    new_bytes = json.dumps(new).encode()
    prepare(store, new_bytes, expected_marker=old_bytes)
    orphan = (store.root / manifest_path()).parent / (".exomem-collection-stage-" + "c" * 32)
    orphan.write_bytes(b"C pending orphan; marker has not admitted C")
    (store.root / earlier_path).unlink()
    earlier_item = (store.root / manifest_path()).parent / f"{earlier_key}.md"
    earlier_item.unlink()
    if admitted_before_commit:
        marker.write_bytes(new_bytes)

        def withdraw_admission(action, arg, *_):
            if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT":
                marker.write_bytes(old_bytes)
            return sqlite3.SQLITE_OK

        store.connection.set_authorizer(withdraw_admission)
    try:
        store.reconcile_views()
    finally:
        store.connection.set_authorizer(None)
    assert (store.root / earlier_path).is_file()
    assert "Earlier row" in earlier_item.read_text()
    assert orphan.read_bytes() == b"C pending orphan; marker has not admitted C"
    assert not (store.root / manifest_path()).exists()
    assert admission._recover_create(store)["status"] == "pending"
    assert marker.read_bytes() == old_bytes


def test_preparation_and_recovery_keep_lease_and_governance_guards(mixed, monkeypatch):
    # A durable receipt must not grant access after writer authority or collection release is lost.
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.governance.principal import request_scope

    store, target, _ = mixed
    with monkeypatch.context() as lost_lease:
        lost_lease.setattr(store.handle, "_lease_check", lambda: False)
        with pytest.raises(connection.CollectionStoreError, match="LEASE_REQUIRED"):
            prepare(store, target)
    assert store.connection.execute("SELECT COUNT(*) FROM txns").fetchone() == (0,)
    prepare(store, target)
    write_scope(store.root, paths="Records/**")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            admission._recover_create(store)
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            prepare(store, target)
    assert not (store.root / manifest_path()).parent.exists()


def test_commit_acknowledgement_survives_actual_runtime_fence(runtime):
    # The real LeaseManager raises OpError after COMMIT; the exact receipt must survive it.
    with preview_store(runtime.root, runtime) as writer:
        sid = writer.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
        target = json.dumps({
            "version": 1, "mode": "store", "default_authority": "file", "store_id": sid,
            "authority_epoch": 1,
            "collections": [{"collection_id": CID, "manifest_path": manifest_path(),
                             "authority": "store", "store_id": sid}],
            "collection_store_fence": {"capability": "collections-store-v1", "generation": 1},
        }).encode()

        def at_commit(action, arg, *_):
            if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT":
                runtime.manager.client.holder = "other"
            return sqlite3.SQLITE_OK

        writer.connection.set_authorizer(at_commit)
        try:
            with runtime.manager.consistency_guard(runtime.root):
                result = prepare(writer, target)
        finally:
            writer.connection.set_authorizer(None)
        assert result["status"] == "unavailable"
        assert result["reason"] == "WRITER_FENCED"
        assert result["head"] is None
        assert result["receipt"]["outcome"] == "committed"
        assert result["receipt"] == json.loads(writer.connection.execute("SELECT receipt_json FROM txns").fetchone()[0])
        assert writer.connection.execute("SELECT COUNT(*) FROM txns").fetchone() == (1,)
        assert authority.pending_create(writer.connection) is not None
    assert not (runtime.root / manifest_path()).parent.exists()


def test_marker_conflict_at_commit_preserves_recovery_inputs(mixed):
    # A synced marker change across COMMIT must also stop queued orphan cleanup, not just C's install.
    store, target, _ = mixed
    prepare(store, target)
    marker = authority.marker_path(store.root)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(target)
    parent = (store.root / manifest_path()).parent
    parent.mkdir(parents=True)
    orphan = parent / (".exomem-collection-stage-" + "b" * 32)
    orphan.write_bytes(b"unpublished input")

    def change_marker(action, arg, *_):
        if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT":
            marker.write_bytes(b"conflicting sync input")
        return sqlite3.SQLITE_OK

    store.connection.set_authorizer(change_marker)
    try:
        store.reconcile_views()
    finally:
        store.connection.set_authorizer(None)
    assert orphan.read_bytes() == b"unpublished input"
    assert not (store.root / manifest_path()).exists()
    assert admission._recover_create(store)["status"] == "conflict"
