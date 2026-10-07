"""Private deferred creation keeps canonical commits ahead of marker admission."""

import json
import multiprocessing
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from io import BytesIO
from pathlib import PureWindowsPath

import pytest
from test_collection_store_runtime import runtime as runtime
from test_collection_store_writer import CID, manifest_path, manifest_text
from test_collection_store_writer import store as store

from exomem import records
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import admission, authority, chain, connection
from exomem.collection_store.preview import preview_store
from exomem.collection_store.writer import CollectionWriter


@pytest.fixture
def producer(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from exomem.lease_coordinator import create_app
    from exomem.writer_lease import LeaseConfig, LeaseCoordinatorClient, LeaseManager

    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
    app = create_app(database=tmp_path / "coordinator.sqlite", bearer_token="lease", operator_token="isolated")
    with closing(TestClient(app)) as transport:
        def urlopen(request, timeout):
            response = transport.request(request.method, request.full_url, content=request.data,
                                         headers=dict(request.header_items()))
            assert response.status_code == 200, response.text
            return BytesIO(response.content)
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        with admission._isolated_session(tmp_path) as session:
            manager = LeaseManager(LeaseConfig(
                url="http://localhost", vault_id="isolated", replica_id="producer", token="lease",
                state_dir=tmp_path / "lease",
            ))
            operator = LeaseCoordinatorClient(replace(manager.config, token="isolated"))
            yield session, manager, operator
            manager._stop.set()
            runtime = manager._collection_store
            if runtime is not None and runtime._handle is not None:
                runtime._handle.close()


def test_real_producer_creates_replica_marker_and_exact_receipt(producer):
    # A canonical commit alone is not success: durable routing and replica must admit that same receipt.
    from exomem import state_migration
    from exomem.collection_store import replica

    session, manager, operator = producer
    result = admission.create_new(session, manager, manifest_path(), manifest_text(),
                                  why="isolated creation", request_id="create", scaffold=False,
                                  fence_client=operator)
    assert result["status"] == "marker_admitted"
    marker = authority.parse_marker(session.root, authority.read_marker(session.root))
    assert marker["collections"][0]["collection_id"] == CID
    with connection.open_reader(replica.replica_path(session.root)) as reader:
        assert reader.execute("SELECT receipt_json FROM txns WHERE request_id='create'").fetchone() == (
            json.dumps(result["receipt"], sort_keys=True, separators=(",", ":")),
        )
    assert (session.root / manifest_path()).exists()
    assert "collections-store-v1" not in state_migration.supported_state_compatibility_ids()
    with pytest.raises(state_migration.StateCompatibilityUnsupported):
        state_migration.require_vault_state_ready(session.root)


def create(producer, **changes):
    session, manager, operator = producer
    arguments = dict(why="isolated creation", request_id="create", scaffold=False, fence_client=operator)
    arguments.update(changes)
    return admission.create_new(session, manager, manifest_path(), manifest_text(), **arguments)


def test_exact_retry_and_second_create_keep_current_head_and_fence(producer):
    # A second create cannot reset the holder or replay the first creation's older head.
    from test_collection_store_writer import KEY

    from exomem.collection_store import replica

    session, manager, operator = producer
    first = create(producer)
    token = manager._fencing_token
    with preview_store(session.root, manager._collection_store) as writer:
        with manager.mutation_guard(session.root):
            writer.append_record(CID, item={"title": "Later row"}, item_key=KEY, why="later write")
    manager._renew_collection_store(token)
    assert manager.client.status().collection_store_head.commit_seq == 2
    second = admission.create_new(
        session, manager, manifest_path().replace("Work", "Next"),
        manifest_text().replace(CID, "33333333-3333-4333-8333-333333333333"),
        why="second collection", request_id="next", scaffold=False, fence_client=operator,
    )
    assert second["head"]["commit_seq"] == 3
    assert manager._fencing_token == token
    assert operator.collection_store_fence().generation == 1
    before = replica.replica_path(session.root).stat().st_mtime_ns
    retry = create(producer)
    assert retry["receipt"] == first["receipt"]
    assert retry["txn_id"] == first["txn_id"]
    assert retry["head"] == second["head"]
    assert replica.replica_path(session.root).stat().st_mtime_ns == before
    with connection.open_reader(session.path) as reader:
        assert reader.execute("SELECT count(*) FROM txns").fetchone() == (3,)
        assert authority.pending_create(reader) is None


def test_clean_retry_does_not_admit_a_foreign_acquisition(producer):
    # A stored receipt is not authority to overwrite a newly acquired foreign store head.
    from exomem.writer_lease import CollectionStoreHead

    session, manager, operator = producer
    create(producer)
    other = "33333333-3333-4333-8333-333333333333"
    operator.transition_collection_store_fence(expected_generation=1, store_id=other)
    lease = manager.ensure_writer()
    head = CollectionStoreHead(other, "44444444-4444-4444-8444-444444444444", 1, "a" * 64)
    manager.client.renew(lease.fencing_token, collection_store_capability="collections-store-v1",
                         collection_store_head=head)
    with pytest.raises(connection.CollectionStoreError, match="CONFLICT|SYNC_PENDING"):
        create(producer)
    assert manager.client.status().collection_store_head == head


def test_clean_retry_rechecks_current_write_governance(producer):
    # A receipt cannot turn a now-readonly collection into an authorized create replay.
    session, _, _ = producer
    create(producer)
    (session.root / "Knowledge Base" / "_access.yaml").write_text("readonly: [Records]\n")
    with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
        create(producer)


@pytest.mark.parametrize("pending_replica", [False, True])
def test_clean_retry_with_new_token_publishes_current_head_and_epoch(producer, monkeypatch, pending_replica):
    # A fresh token must recover an unpublished tail, including old-epoch scratch, before reporting it.
    from test_collection_store_writer import KEY

    from exomem.collection_store import replica, schema

    session, manager, operator = producer
    first = create(producer)
    token = manager._fencing_token
    with preview_store(session.root, manager._collection_store) as writer:
        with manager.mutation_guard(session.root):
            writer.append_record(CID, item={"title": "Unpublished row"}, item_key=KEY, why="later write")
    if pending_replica:
        filesystem_type = type(session.fs)
        rename = filesystem_type.rename

        def stop_ready(filesystem, source, destination, leaf, **kwargs):
            if leaf == connection.STORE_FILENAME:
                raise InterruptedError("ready replica")
            return rename(filesystem, source, destination, leaf, **kwargs)

        with monkeypatch.context() as stopped:
            stopped.setattr(filesystem_type, "rename", stop_ready)
            with preview_store(session.root, manager._collection_store) as writer:
                with manager.mutation_guard(session.root):
                    result = replica.publish_replica(session.root, writer.handle,
                                                     authority_check=lambda: session.require(token),
                                                     deadline=time.monotonic() + 30)
                    assert result.status != "published"
            with connection.open_reader(session.path) as reader:
                assert json.loads(reader.execute("SELECT value FROM store_meta WHERE key=?", (
                    schema.META_PENDING_REPLICA_PUBLICATION,
                )).fetchone()[0])["phase"] == "ready"
    manager.client.release(token)
    publish = replica.publish_replica

    def inspect_bootstrap(*args, **kwargs):
        assert not manager._collection_store.reporting_ready(manager._fencing_token)
        manager._renew_collection_store(manager._fencing_token)
        assert manager.client.status().collection_store_head.commit_seq == 1
        return publish(*args, **kwargs)

    monkeypatch.setattr(replica, "publish_replica", inspect_bootstrap)
    retry = create(producer)
    assert retry["receipt"] == first["receipt"]
    assert retry["txn_id"] == first["txn_id"]
    assert retry["head"]["commit_seq"] == 2
    assert manager._fencing_token > token
    for path in (session.path, replica.replica_path(session.root)):
        with connection.open_reader(path) as reader:
            assert chain.verify_store_chain(reader) == (2, retry["head"]["head_hash"])
            assert reader.execute("SELECT value FROM store_meta WHERE key=?", (schema.META_LEASE_EPOCH,)).fetchone() == (
                str(manager._fencing_token),
            )
    with connection.open_reader(session.path) as reader:
        assert authority.pending_create(reader) is None
        assert reader.execute("SELECT value FROM store_meta WHERE key=?", (
            schema.META_PENDING_REPLICA_PUBLICATION,
        )).fetchone() is None
    manager._renew_collection_store(manager._fencing_token)
    assert manager.client.status().collection_store_head.commit_seq == 2


@pytest.mark.parametrize("phase", ["before_cas", "enrolled"])
def test_resume_rechecks_write_permission_before_cutover(producer, monkeypatch, phase):
    # Revoking mutation permission must preserve routing, intent and replica even before the first CAS.
    from exomem import state_migration
    from exomem.collection_store import replica

    session, manager, operator = producer
    enroll = state_migration.record_collection_store_compatibility

    def interrupt(*args, **kwargs):
        if phase == "enrolled":
            enroll(*args, **kwargs)
        raise InterruptedError(phase)

    with monkeypatch.context() as stopped:
        if phase == "enrolled":
            stopped.setattr(state_migration, "record_collection_store_compatibility", interrupt)
        else:
            stopped.setattr(operator, "transition_collection_store_fence", interrupt)
        with pytest.raises(InterruptedError):
            create(producer)
    fence = operator.collection_store_fence()
    with connection.open_reader(session.path) as reader:
        intent = authority.pending_create(reader)
    (session.root / "Knowledge Base" / "_access.yaml").write_text("readonly: [Records]\n")
    with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
        admission.resume_local(session, manager, fence_client=operator)
    assert operator.collection_store_fence() == fence
    assert authority.read_marker(session.root) is None
    assert not replica.replica_path(session.root).exists()
    with connection.open_reader(session.path) as reader:
        assert authority.pending_create(reader) == intent
    (session.root / "Knowledge Base" / "_access.yaml").write_text("readonly: []\n")
    assert admission.resume_local(session, manager, fence_client=operator)["status"] == "marker_admitted"


def test_invalid_second_create_keeps_existing_store_admitted(producer):
    # Rejected input with no committed intent cannot strand the already-admitted collection.
    from test_collection_store_writer import KEY

    session, manager, operator = producer
    create(producer)
    token = manager._fencing_token
    with pytest.raises(collections.CollectionError):
        admission.create_new(session, manager, manifest_path().replace("Work", "Next"), "invalid",
                             why="second collection", request_id="next", scaffold=False, fence_client=operator)
    assert manager._fencing_token == token
    with connection.open_reader(session.path) as reader:
        assert authority.pending_create(reader) is None
        assert reader.execute("SELECT count(*) FROM txns").fetchone() == (1,)
    with preview_store(session.root, manager._collection_store) as writer:
        with manager.mutation_guard(session.root):
            writer.append_record(CID, item={"title": "Still admitted"}, item_key=KEY, why="later write")


def test_redundant_resume_keeps_existing_store_admitted(producer):
    # Refusing recovery without a pending intent must leave the completed collection writable.
    from test_collection_store_writer import KEY

    session, manager, operator = producer
    assert create(producer)["status"] == "marker_admitted"
    with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_CREATE_CONFLICT"):
        admission.resume_local(session, manager, fence_client=operator)
    with preview_store(session.root, manager._collection_store) as writer:
        with manager.mutation_guard(session.root):
            result = writer.append_record(CID, item={"title": "Still admitted"}, item_key=KEY, why="later write")
    assert result["outcome"] == "committed"


def test_resume_rejects_delegating_session_before_cas(producer, monkeypatch):
    # The existing exact-session restriction must apply before any coordinator transition.
    from exomem import state_migration

    session, manager, operator = producer

    def interrupt(*args, **kwargs):
        raise InterruptedError("before CAS")

    with monkeypatch.context() as stopped:
        stopped.setattr(operator, "transition_collection_store_fence", interrupt)
        with pytest.raises(InterruptedError):
            create(producer)

    class Proxy:
        def __getattr__(self, name):
            return getattr(session, name)

    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        admission.resume_local(Proxy(), manager, fence_client=operator)
    assert not operator.collection_store_fence().enrolled


def _restart_child(session, config, operator_config, database, phase, sender):
    from starlette.testclient import TestClient

    from exomem import state_migration, writer_lease
    from exomem.collection_store import replica
    from exomem.lease_coordinator import create_app

    manager = writer_lease.LeaseManager(config)
    operator = writer_lease.LeaseCoordinatorClient(operator_config)
    app = create_app(database=database, bearer_token="lease", operator_token="isolated")
    with TestClient(app) as transport, pytest.MonkeyPatch.context() as patch:
        def urlopen(request, timeout):
            response = transport.request(request.method, request.full_url, content=request.data,
                                         headers=dict(request.header_items()))
            assert response.status_code == 200, response.text
            return BytesIO(response.content)
        patch.setattr("urllib.request.urlopen", urlopen)

        def interrupt(function):
            def call(*args, **kwargs):
                if phase == "cas":
                    assert writer_lease._ACTIVE_WRITE_FENCE.get() is None
                    assert session.path.resolve() not in connection._WRITER_PATHS
                result = function(*args, **kwargs)
                sender.send({"phase": phase})
                os._exit(73)
                return result
            return call

        if phase == "cas":
            patch.setattr(operator, "transition_collection_store_fence", interrupt(operator.transition_collection_store_fence))
        elif phase == "enroll":
            patch.setattr(state_migration, "record_collection_store_compatibility",
                          interrupt(state_migration.record_collection_store_compatibility))
        elif phase == "replica":
            patch.setattr(replica, "publish_replica", interrupt(replica.publish_replica))
        elif phase == "scratch":
            filesystem_type = type(session.fs)
            rename = filesystem_type.rename
            def stop_with_ready_stage(filesystem, source, destination, leaf, **kwargs):
                if leaf == connection.STORE_FILENAME:
                    sender.send({"phase": phase})
                    os._exit(73)
                return rename(filesystem, source, destination, leaf, **kwargs)
            patch.setattr(filesystem_type, "rename", stop_with_ready_stage)
        elif phase == "marker":
            rename = session.fs.rename
            def rename_and_stop(source, destination, leaf, **kwargs):
                if leaf == "mode.json":
                    return interrupt(rename)(source, destination, leaf, **kwargs)
                return rename(source, destination, leaf, **kwargs)
            patch.setattr(session.fs, "rename", rename_and_stop)
        if phase is None:
            result = admission.resume_local(session, manager, fence_client=operator)
            assert "collections-store-v1" not in state_migration.supported_state_compatibility_ids()
            with pytest.raises(state_migration.StateCompatibilityUnsupported):
                state_migration.require_vault_state_ready(session.root)
            sender.send(result)
        else:
            admission.create_new(session, manager, manifest_path(), manifest_text(),
                                 why="isolated creation", request_id="create", scaffold=False,
                                 fence_client=operator)


@pytest.mark.skipif(os.name != "posix", reason="private supervisor restart uses inherited held directory handles")
@pytest.mark.parametrize("phase", ["cas", "enroll", "scratch", "replica", "marker"])
def test_surviving_supervisor_restarts_crashed_producer(producer, phase, tmp_path):
    # Each durable cut must survive process death without a new creation or a runtime-support override.
    session, manager, operator = producer
    context = multiprocessing.get_context("fork")
    for checkpoint in (phase, None):
        receiver, sender = context.Pipe(duplex=False)
        child = context.Process(target=_restart_child, args=(
            session, manager.config, operator.config, tmp_path / "coordinator.sqlite", checkpoint, sender,
        ))
        child.start()
        sender.close()
        child.join(12)
        try:
            assert not child.is_alive(), "isolated producer did not finish"
            assert child.exitcode == (73 if checkpoint is not None else 0)
            assert receiver.poll(1)
            result = receiver.recv()
        finally:
            if child.is_alive():
                child.terminate()
                child.join(3)
            receiver.close()
        with connection.open_reader(session.path) as reader:
            assert reader.execute("SELECT COUNT(*) FROM txns").fetchone() == (1,)
            if checkpoint is not None:
                assert authority.pending_create(reader) is not None
                assert not (session.root / manifest_path()).exists()
                if checkpoint == "scratch":
                    from exomem.collection_store import schema
                    pending = json.loads(reader.execute(
                        "SELECT value FROM store_meta WHERE key=?", (schema.META_PENDING_REPLICA_PUBLICATION,),
                    ).fetchone()[0])
                    assert pending["phase"] == "ready"
                    # Recovery must resolve this old epoch before stamping the new acquisition.
                    manager.client.release(manager.client.status().fencing_token)
            else:
                assert authority.pending_create(reader) is None
                assert result["status"] == "marker_admitted"
                assert json.loads(reader.execute("SELECT receipt_json FROM txns").fetchone()[0]) == result["receipt"]
    assert (session.root / manifest_path()).exists()


def test_lost_namespace_custody_preserves_routing_and_pending_create(producer, monkeypatch):
    # Private replica staging cannot authorize overwriting a replaced marker namespace.
    from exomem.collection_store import replica

    session, manager, operator = producer
    create(producer)
    marker = authority.marker_path(session.root)
    before = marker.read_bytes()
    publish = replica.publish_replica

    def replace_namespace(*args, **kwargs):
        result = publish(*args, **kwargs)
        marker.parent.rename(marker.parent.with_name("retained-namespace"))
        marker.parent.mkdir()
        marker.write_bytes(before)
        return result

    monkeypatch.setattr(replica, "publish_replica", replace_namespace)
    with pytest.raises(connection.CollectionStoreError, match="CUSTODY_REQUIRED"):
        admission.create_new(
            session, manager, manifest_path().replace("Work", "Next"),
            manifest_text().replace(CID, "33333333-3333-4333-8333-333333333333"),
            why="second collection", request_id="next", scaffold=False, fence_client=operator,
        )
    assert marker.read_bytes() == before
    assert not (session.root / manifest_path().replace("Work", "Next")).exists()
    with connection.open_reader(session.path) as reader:
        assert authority.pending_create(reader)["request_id"] == "next"
        assert reader.execute("SELECT COUNT(*) FROM txns").fetchone() == (2,)


def test_initial_marker_install_never_clobbers_an_arriving_name(producer, monkeypatch):
    # An absent preimage is not a reservation: the final install must remain no-clobber.
    from exomem import held_fs

    session, manager, _ = producer
    rename = session.fs.rename
    arriving = b"an arriving routing marker"

    def arrive_before_install(source, parent, leaf, **kwargs):
        if leaf == "mode.json":
            authority.marker_path(session.root).write_bytes(arriving)
        return rename(source, parent, leaf, **kwargs)

    monkeypatch.setattr(session.fs, "rename", arrive_before_install)
    with pytest.raises(held_fs.HeldFsError, match="DESTINATION_EXISTS"):
        create(producer)
    assert authority.marker_path(session.root).read_bytes() == arriving
    assert not (session.root / manifest_path()).exists()
    assert not manager._collection_store.reporting_ready(manager._fencing_token)
    with connection.open_reader(session.path) as reader:
        assert authority.pending_create(reader) is not None


def test_token_loss_stops_current_create_and_separate_resume_reacquires(producer, monkeypatch):
    # Once the admitted token is revoked, this operation cannot quietly acquire another.
    from exomem import state_migration

    session, manager, operator = producer
    enroll = state_migration.record_collection_store_compatibility
    lost = []

    def revoke_after_enrollment(*args, **kwargs):
        enroll(*args, **kwargs)
        lost.append(manager._fencing_token)
        manager.client.release(manager._fencing_token)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(state_migration, "record_collection_store_compatibility", revoke_after_enrollment)
        with pytest.raises(OpError, match="WRITER_FENCED"):
            create(producer)
    assert not authority.marker_path(session.root).exists()
    assert not (session.root / manifest_path()).exists()
    assert manager.client.status().holder is None
    with pytest.raises(connection.CollectionStoreError, match="LEASE_REQUIRED"):
        with manager._collection_store.checkout():
            pytest.fail("bootstrap checkout reacquired a lost token")
    assert manager.client.status().holder is None
    result = admission.resume_local(session, manager, fence_client=operator)
    assert result["status"] == "marker_admitted"
    assert manager._fencing_token > lost[0]


def test_bootstrap_keeps_acquisition_head_and_blocks_release(producer, monkeypatch):
    # During enrollment renewal must be headless and normal checkout/release must stay closed.
    from exomem import state_migration
    from exomem.collection_store import replica

    session, manager, operator = producer
    enroll = state_migration.record_collection_store_compatibility

    def inspect_bootstrap(*args, **kwargs):
        runtime = manager._collection_store
        acquired = runtime._acquisition
        manager._renew_collection_store(manager._fencing_token)
        assert runtime._acquisition is acquired
        assert manager.client.status().collection_store_head is None
        with pytest.raises(connection.CollectionStoreError, match="LEASE_REQUIRED"):
            with runtime.checkout():
                pytest.fail("bootstrap admitted normal checkout")
        assert not manager._release_collection_store(manager._fencing_token, deadline=9999999999)
        assert not replica.replica_path(session.root).exists()
        return enroll(*args, **kwargs)

    monkeypatch.setattr(state_migration, "record_collection_store_compatibility", inspect_bootstrap)
    assert create(producer)["status"] == "marker_admitted"


def test_producer_rollback_keeps_legacy_files_and_has_no_c_cutover(producer, monkeypatch):
    # The orchestrator must not publish a marker/replica for a rolled-back canonical prepare.
    session, manager, operator = producer
    (session.root / "Knowledge Base" / "log.md").write_text("# Activity\n")
    for profile, cid in (("records", "11111111-1111-4111-8111-111111111111"),
                         ("planning", "22222222-2222-4222-8222-222222222222")):
        records.create_collection(session.root, manifest_path(profile).replace("Work", "Legacy"),
                                  manifest_text(profile).replace(CID, cid), why="legacy", scaffold=False)
    before = {path: path.read_bytes() for path in session.root.rglob("*") if path.is_file()}
    prepare = admission._prepare_create

    def reject_commit(writer, *args, **kwargs):
        writer.connection.set_authorizer(lambda action, arg, *_: sqlite3.SQLITE_DENY
                                        if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
                                        else sqlite3.SQLITE_OK)
        return prepare(writer, *args, **kwargs)

    monkeypatch.setattr(admission, "_prepare_create", reject_commit)
    with pytest.raises(sqlite3.DatabaseError):
        create(producer)
    assert not authority.marker_path(session.root).exists()
    assert not (session.root / manifest_path()).parent.exists()
    assert not operator.collection_store_fence().enrolled
    assert all(path.read_bytes() == raw for path, raw in before.items())
    with connection.open_reader(session.path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM txns").fetchone() == (0,)
        assert authority.pending_create(reader) is None


def test_resume_refuses_foreign_acquired_head_without_replacing_it(producer, monkeypatch):
    # Same store ID is insufficient when the real acquisition reports a head outside this chain.
    from exomem import state_migration
    from exomem.writer_lease import CollectionStoreHead

    session, manager, operator = producer
    enroll = state_migration.record_collection_store_compatibility

    def stop_after_enroll(*args, **kwargs):
        enroll(*args, **kwargs)
        raise InterruptedError("enrolled")

    with monkeypatch.context() as stopped:
        stopped.setattr(state_migration, "record_collection_store_compatibility", stop_after_enroll)
        with pytest.raises(InterruptedError):
            create(producer)
    token = manager._fencing_token
    sid = operator.collection_store_fence().store_id
    foreign = CollectionStoreHead(sid, "44444444-4444-4444-8444-444444444444", 99, "a" * 64)
    manager.client.renew(token, collection_store_capability="collections-store-v1", collection_store_head=foreign)
    manager.client.release(token)
    with pytest.raises(connection.CollectionStoreError, match="SYNC_PENDING"):
        admission.resume_local(session, manager, fence_client=operator)
    assert manager._collection_store._acquisition.collection_store_head == foreign
    manager._renew_collection_store(manager._fencing_token)
    assert manager.client.status().collection_store_head == foreign
    assert not authority.marker_path(session.root).exists()


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
        "collections": [authority.marker_entry(CID, manifest_path(), sid, "records")],
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


def test_removed_membership_blocks_projection_after_intent_cleanup(mixed):
    # Removing routing membership must not let recovery republish a retired collection.
    store, target, _ = mixed
    prepare(store, target)
    marker = authority.marker_path(store.root)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(target)
    store.reconcile_views()
    store.connection.execute("DELETE FROM store_meta WHERE key=?", (authority.PENDING_CREATE,))
    (store.root / manifest_path()).unlink()
    orphan = (store.root / manifest_path()).parent / (".exomem-collection-stage-" + "d" * 32)
    orphan.write_bytes(b"unadmitted recovery input")
    replacement = json.loads(target)
    replacement["collections"][0].update(
        collection_id="33333333-3333-4333-8333-333333333333",
        manifest_path=manifest_path().replace("Work", "Other"),
    )
    marker.write_text(json.dumps(replacement))
    store.reconcile_views()
    assert not (store.root / manifest_path()).exists()
    assert orphan.read_bytes() == b"unadmitted recovery input"


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
            "collections": [authority.marker_entry(CID, manifest_path(), sid, "records")],
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
