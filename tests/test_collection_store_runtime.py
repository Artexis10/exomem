"""Cached preview operations and real replica handoff share one lifetime."""

import os
import shutil
import sqlite3
import threading
import time
from contextlib import closing, contextmanager

import pytest
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text

from exomem import held_fs, state_paths
from exomem.cli_ops import OpError
from exomem.collection_store import capability, connection, replica, schema, views
from exomem.collection_store.preview import preview_store
from exomem.collection_store.runtime import CollectionStoreRuntime
from exomem.writer_lease import LeaseConfig, LeaseManager, LeaseRecord


class Client:
    def __init__(self):
        self.released = threading.Event()
        self.heads = []
        self.release_head = None
        self.holder = "local"
        self.token = 1

    def acquire(self, **kwargs):
        if self.holder is None:
            self.token += 1
            self.holder = "local"
        return LeaseRecord(self.holder, time.time() + 30, self.token, True)

    def status(self):
        return LeaseRecord(self.holder, time.time() + 30, self.token, True)

    def renew(self, token, **kwargs):
        self.heads.append(kwargs.get("collection_store_head"))
        return self.status()

    def release(self, token, **kwargs):
        self.release_head = kwargs.get("collection_store_head")
        self.holder = None
        self.released.set()
        return LeaseRecord(None, None, token, True)


def test_idle_release_waits_for_preview_read_borrower(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    client = Client()
    manager = LeaseManager(LeaseConfig(
        url="https://lease.example", vault_id="vault", replica_id="local",
        state_dir=tmp_path / "lease", idle_release_seconds=1,
    ), client=client)
    manager.ensure_writer()
    monkeypatch.setattr("exomem.writer_lease.active_manager", lambda: manager)
    manager._last_activity_monotonic -= 10
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        with preview_store(root, handle) as writer, writer.read_snapshot():
            attempt = threading.Thread(target=manager._maybe_idle_release, args=(1,))
            attempt.start()
            try:
                assert not client.released.wait(0.15), "released while preview read was borrowed"
            finally:
                attempt.join(2)


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    client = Client()
    manager = LeaseManager(LeaseConfig(
        url="https://lease.example", vault_id="vault", replica_id="local",
        state_dir=tmp_path / "lease", idle_release_seconds=1, ttl_seconds=3,
    ), client=client)
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True):
        pass
    runtime = CollectionStoreRuntime(root, manager, authority_check=lambda: True)
    yield runtime
    if runtime._handle is not None:
        runtime._handle.close()
    manager._stop.set()


def _create(runtime):
    from exomem.record_memory import record_memory

    with preview_store(runtime.root, runtime):
        return record_memory(runtime.root, "create", manifest_path=manifest_path(),
                             manifest_text=manifest_text(), why="capture", scaffold=False)


def test_a_cached_handle_cannot_bypass_an_older_reader_ceiling_or_leak_a_borrower(runtime, monkeypatch):
    """A previously cached schema 12 writer must refuse schema 13 installation semantics."""
    _create(runtime)
    cached = runtime._handle
    assert cached is not None
    with monkeypatch.context() as older:
        older.setattr(schema, "SCHEMA_VERSION", 12)
        with pytest.raises(connection.CollectionStoreError, match="SCHEMA_NEWER"):
            with preview_store(runtime.root, runtime):
                pytest.fail("an older cached handler entered the newer store")
        assert runtime.manager._store_borrowers == {}
    with preview_store(runtime.root, runtime) as writer:
        assert writer.connection.execute("SELECT COUNT(*) FROM collections").fetchone() == (1,)


def test_runtime_handoff_drains_read_and_publishes_acknowledged_rows(runtime):
    _create(runtime)
    manager, client = runtime.manager, runtime.manager.client
    manager._last_activity_monotonic -= 10
    completed = []
    with preview_store(runtime.root, runtime) as writer, writer.read_snapshot():
        manager._last_activity_monotonic -= 10
        attempt = threading.Thread(target=lambda: completed.append(manager._maybe_idle_release(1)))
        attempt.start()
        assert not client.released.wait(0.15)
        with manager._mutation_coordinator_for(runtime.root).hold(timeout_seconds=0):
            pass
    attempt.join(5)
    assert completed == [True]
    assert client.release_head == runtime.sample_head()
    with connection.open_reader(replica.replica_path(runtime.root)) as reader:
        assert reader.execute("SELECT COUNT(*) FROM collections").fetchone() == (1,)
    assert manager._fencing_token is None
    manager.close()
    assert client.holder is None
    assert client.token == 1


def _pending_item_views(runtime, monkeypatch):
    _create(runtime)

    def interrupted(batch, publication):
        raise held_fs.HeldFsError("IO_REFUSED", "installation interrupted")

    with runtime.manager.consistency_guard(runtime.root), preview_store(runtime.root, runtime) as writer:
        writer.reconcile_views()
        with monkeypatch.context() as blocked:
            blocked.setattr(views.PublicationBatch, "_install", interrupted)
            writer.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
            writer.append_record(CID, item={"title": "Two"}, item_key=OTHER, why="capture")
        paths = [row[0] for row in writer.connection.execute(
            "SELECT path FROM projection_state WHERE kind='item' AND state='pending' ORDER BY path")]
    assert len(paths) == 2 and all(not (runtime.root / path).exists() for path in paths)
    return paths


def _resume_pending_views(runtime, paths, released):
    from exomem.collection_store.writer import CollectionWriter
    from exomem.governance.principal import library_scope

    with connection.open_writer(runtime.path, lease_check=lambda: True) as handle, library_scope():
        writer = CollectionWriter(runtime.root, handle)
        writer.reconcile_views(pending_only=True)
        writer.reconcile_views(pending_only=True)
        assert writer.connection.execute("SELECT COUNT(*) FROM txns").fetchone() == (3,)
        assert writer.connection.execute("SELECT COUNT(*) FROM projection_state WHERE state='pending'").fetchone() == (0,)
    assert all((runtime.root / path).exists() for path in paths)
    assert runtime.sample_head() == released


def test_handoff_stops_before_the_next_view_when_an_install_uses_the_drain_share(runtime, monkeypatch):
    paths = _pending_item_views(runtime, monkeypatch)
    installed = []
    monotonic, offset = time.monotonic, [0.0]
    install = views.PublicationBatch._install

    def exhaust_share(batch, publication):
        install(batch, publication)
        installed.append(publication.path)
        if len(installed) == 1:
            offset[0] += 12

    with monkeypatch.context() as drain:
        drain.setattr(time, "monotonic", lambda: monotonic() + offset[0])
        drain.setattr(views.PublicationBatch, "_install", exhaust_share)
        runtime.manager.close(deadline=time.monotonic() + 20)
    assert installed == paths[:1]
    assert (runtime.root / paths[0]).exists() and not (runtime.root / paths[1]).exists()
    released = runtime.manager.client.release_head
    assert runtime.manager.client.released.is_set() and released == runtime.sample_head()
    with closing(connection.open_reader(replica.replica_path(runtime.root))) as reader:
        assert reader.execute("SELECT COUNT(*) FROM items").fetchone() == (2,)
        assert reader.execute("SELECT state FROM projection_state WHERE path=?", (paths[1],)).fetchone() == ("pending",)
    _resume_pending_views(runtime, paths, released)


def test_handoff_leaves_views_pending_when_batch_setup_uses_the_drain_share(runtime, monkeypatch):
    paths = _pending_item_views(runtime, monkeypatch)
    monotonic, offset = time.monotonic, [0.0]
    initialize = views.PublicationBatch.__init__

    def exhaust_share(batch, *args, **kwargs):
        initialize(batch, *args, **kwargs)
        offset[0] += 12

    with monkeypatch.context() as drain:
        drain.setattr(time, "monotonic", lambda: monotonic() + offset[0])
        drain.setattr(views.PublicationBatch, "__init__", exhaust_share)
        runtime.manager.close(deadline=time.monotonic() + 20)
    assert all(not (runtime.root / path).exists() for path in paths)
    released = runtime.manager.client.release_head
    assert runtime.manager.client.released.is_set() and released == runtime.sample_head()
    with closing(connection.open_reader(replica.replica_path(runtime.root))) as reader:
        assert reader.execute("SELECT COUNT(*) FROM items").fetchone() == (2,)
        assert reader.execute("SELECT COUNT(*) FROM projection_state WHERE kind='item' AND state='pending'").fetchone() == (2,)
    _resume_pending_views(runtime, paths, released)


def test_cancelled_reconcile_continuation_keeps_the_unfinished_path(runtime, monkeypatch):
    paths = _pending_item_views(runtime, monkeypatch)
    cancelled = threading.Event()
    capture = views.PublicationBatch.capture_previous

    def cancel_after_capture(batch, path):
        previous = capture(batch, path)
        cancelled.set()
        return previous

    with runtime.manager.consistency_guard(runtime.root), preview_store(runtime.root, runtime) as writer:
        with monkeypatch.context() as recovery:
            recovery.setattr(views.PublicationBatch, "capture_previous", cancel_after_capture)
            result = writer.reconcile_views(after=paths[0], pending_only=True, stop=cancelled.is_set)
        assert result["next_path"] == paths[0] and "projection_pending" in result["warnings"]
        assert not (runtime.root / paths[1]).exists()
        writer.reconcile_views(after=result["next_path"], pending_only=True)
        assert (runtime.root / paths[1]).exists()
        assert writer.connection.execute("SELECT COUNT(*) FROM txns").fetchone() == (3,)


def test_shutdown_leaves_the_released_head_in_the_store_file_while_another_reader_is_open(runtime, tmp_path):
    """Defect: another process holding the live store open at a clean shutdown keeps the acknowledged
    commits in the WAL only, so a copy of collections.sqlite alone lacks the head the lease was released at."""
    with closing(connection.open_reader(runtime.path)):  # another process's idle connection
        _create(runtime)
        runtime.manager.close()
        copied = tmp_path / "copied.sqlite"
        shutil.copyfile(runtime.path, copied)
    with closing(sqlite3.connect(copied)) as conn:
        meta = dict(conn.execute("SELECT key,value FROM store_meta"))
    released = runtime.manager.client.release_head
    assert released.commit_seq >= 1
    assert (int(meta[schema.META_COMMIT_SEQ]), meta.get(schema.META_STORE_HEAD_HASH)) == (
        released.commit_seq, released.head_hash)


def test_warm_operations_reuse_cache_and_retained_bindings_expire(runtime, monkeypatch):
    from exomem.collection_store.governance import ReleaseCache
    from exomem.record_memory import record_memory

    created = []
    original = ReleaseCache.__init__

    def counted(cache, *args, **kwargs):
        created.append(cache)
        original(cache, *args, **kwargs)

    monkeypatch.setattr(ReleaseCache, "__init__", counted)
    _create(runtime)
    for _ in range(2):
        with preview_store(runtime.root, runtime) as writer:
            record_memory(runtime.root, "inspect", collection=CID)
            saved_connection = writer.connection
            saved_cursor = saved_connection.execute("SELECT 1")
            saved_dump = saved_connection.iterdump()
            saved_method = writer.inspect_collection
            saved_context = writer.read_snapshot()
    assert len(created) == 1
    for use in (lambda: saved_connection.execute("SELECT 1"), lambda: saved_cursor.fetchone(),
                lambda: saved_method(CID), saved_context.__enter__, lambda: next(saved_dump)):
        with pytest.raises(connection.CollectionStoreError, match="CHECKOUT_CLOSED"):
            use()
    runtime.manager._last_activity_monotonic -= 10
    assert runtime.manager._maybe_idle_release(1)
    with preview_store(runtime.root, runtime):
        assert record_memory(runtime.root, "inspect", collection=CID)
    assert len(created) == 3  # normal cache, lifecycle cache, reopened normal cache


def test_coalesced_publication_keeps_warm_release_work_bounded(runtime, monkeypatch):
    """Publication must not make unchanged foreground authority scan every row again."""
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from exomem.collection_store import governance, snapshot
    from exomem.record_memory import record_memory

    _create(runtime)
    with preview_store(runtime.root, runtime):
        record_memory(runtime.root, "append", collection=CID,
                      item={"title": "One"}, item_key=KEY, why="capture")
        assert record_memory(runtime.root, "inspect", collection=CID)["coverage"]["committed"] == 1

    full_catalogs = []
    subjects = governance.subjects

    def observed_subjects(conn, cid, logical_vault_id, **kwargs):
        if cid == CID and kwargs.get("identity") is None:
            full_catalogs.append(threading.get_ident())
        return subjects(conn, cid, logical_vault_id, **kwargs)

    monkeypatch.setattr(governance, "subjects", observed_subjects)
    copying, resume = threading.Event(), threading.Event()
    staged_snapshot = snapshot.staged_snapshot

    @contextmanager
    def paused_copy(*args, **kwargs):
        copying.set()
        assert resume.wait(5), "publication copy was not resumed"
        with staged_snapshot(*args, **kwargs) as artifact:
            yield artifact

    monkeypatch.setattr(snapshot, "staged_snapshot", paused_copy)
    with ThreadPoolExecutor(max_workers=1) as pool:
        published = pool.submit(runtime.manager._publish_collection_store,
                                runtime.manager._fencing_token, deadline=time.monotonic() + 15)
        try:
            assert copying.wait(5), "publication did not reach its outside copy"
            with preview_store(runtime.root, runtime):
                assert record_memory(runtime.root, "inspect", collection=CID)["coverage"]["committed"] == 1
                record_memory(runtime.root, "append", collection=CID,
                              item={"title": "Two"}, item_key=OTHER, why="capture")
        finally:
            resume.set()
        assert published.result(timeout=10) is not None

    with preview_store(runtime.root, runtime):
        assert record_memory(runtime.root, "inspect", collection=CID)["coverage"]["committed"] == 2
    with closing(connection.open_reader(replica.replica_path(runtime.root))) as reader:
        assert reader.execute("SELECT COUNT(*) FROM items").fetchone() == (2,)
    assert not full_catalogs, "publication discarded an unchanged warm authorization catalogue"


def test_external_sql_during_publication_invalidates_warm_data_without_a_head_change(runtime, monkeypatch):
    """A trusted publication must not hide a foreign commit behind its metadata accounting."""
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from exomem.collection_store import snapshot
    from exomem.record_memory import record_memory

    _create(runtime)
    with preview_store(runtime.root, runtime):
        record_memory(runtime.root, "append", collection=CID,
                      item={"title": "Initial"}, item_key=KEY, why="capture")
        assert record_memory(runtime.root, "inspect", collection=CID)["observed_values"]["title"]["values"][0]["value"] == "Initial"
    head = runtime.sample_head()
    copying, resume = threading.Event(), threading.Event()
    staged_snapshot = snapshot.staged_snapshot

    @contextmanager
    def paused_copy(*args, **kwargs):
        copying.set()
        assert resume.wait(5), "publication copy was not resumed"
        with staged_snapshot(*args, **kwargs) as artifact:
            yield artifact

    monkeypatch.setattr(snapshot, "staged_snapshot", paused_copy)
    with ThreadPoolExecutor(max_workers=1) as pool:
        published = pool.submit(runtime.manager._publish_collection_store,
                                runtime.manager._fencing_token, deadline=time.monotonic() + 15)
        try:
            assert copying.wait(5), "publication did not reach its outside copy"
            with closing(sqlite3.connect(runtime.path)) as external:
                external.execute("UPDATE items SET values_json=? WHERE item_key=?",
                                 ('{"title":"Changed elsewhere"}', KEY))
                external.commit()
            assert runtime.sample_head() == head
        finally:
            resume.set()
        assert published.result(timeout=10) is not None

    with preview_store(runtime.root, runtime):
        current = record_memory(runtime.root, "inspect", collection=CID)
    assert current["observed_values"]["title"]["values"][0]["value"] == "Changed elsewhere"


def test_failed_publication_transaction_returns_foreground_ownership_and_keeps_rows(runtime, monkeypatch):
    """A failed loan must not strand foreground requests on the publication thread's writer."""
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from exomem.record_memory import record_memory

    _create(runtime)
    with preview_store(runtime.root, runtime):
        record_memory(runtime.root, "append", collection=CID,
                      item={"title": "Committed"}, item_key=KEY, why="capture")
    foreground, injected = threading.get_ident(), []
    transaction = connection.WriterConnection.transaction

    @contextmanager
    def fail_publication_commit(handle, **kwargs):
        with transaction(handle, **kwargs) as conn:
            yield conn
            if threading.get_ident() != foreground and not injected:
                injected.append(True)
                raise sqlite3.OperationalError("synthetic publication transaction failure")

    monkeypatch.setattr(connection.WriterConnection, "transaction", fail_publication_commit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        published = pool.submit(runtime.manager._publish_collection_store,
                                runtime.manager._fencing_token, deadline=time.monotonic() + 15)
        with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_FLUSH_PENDING"):
            published.result(timeout=10)
    assert injected == [True]
    with preview_store(runtime.root, runtime):
        assert record_memory(runtime.root, "inspect", collection=CID)["coverage"]["committed"] == 1
        assert record_memory(runtime.root, "append", collection=CID,
                             item={"title": "After failure"}, item_key=OTHER, why="capture")["outcome"] == "committed"
        assert record_memory(runtime.root, "inspect", collection=CID)["coverage"]["committed"] == 2


def test_fresh_head_sample_rejects_absent_or_inconsistent_enrolled_store(runtime):
    initial = runtime.sample_head()
    _create(runtime)
    current = runtime.sample_head()
    assert current.commit_seq == initial.commit_seq + 1
    results = []
    reader = threading.Thread(target=lambda: results.append(runtime.sample_head()))
    reader.start()
    reader.join(2)
    assert results == [current]
    with preview_store(runtime.root, runtime) as writer:
        writer.connection.execute("UPDATE store_meta SET value='90' WHERE key=?",
                                  (schema.META_COMMIT_SEQ,))
    with pytest.raises(connection.CollectionStoreError, match="HEAD_INVALID"):
        runtime.sample_head()
    runtime._handle.close()
    runtime._handle = None
    runtime.path.unlink()
    with pytest.raises(connection.CollectionStoreError, match="ABSENT"):
        runtime.sample_head()


def test_unadmitted_runtime_renews_without_replacing_coordinator_evidence(runtime):
    # Authority loss must leave the lease renewable without advertising an unadmitted head.
    manager = runtime.manager
    lease = manager.ensure_writer()
    runtime._authority_check = lambda: False
    manager._renew_collection_store(lease.fencing_token)
    assert manager.client.heads == [None]
    assert not manager._release_collection_store(
        lease.fencing_token, deadline=time.monotonic() + 1,
    )


def test_cancelled_close_stops_renewal_and_can_retry_handoff(runtime):
    _create(runtime)
    with pytest.raises(OpError, match="FLUSH_PENDING"):
        runtime.manager.close(cancelled=lambda: True)
    assert not runtime.manager.client.released.is_set()
    assert runtime.manager._fencing_token == 1
    assert runtime.manager._stop.is_set()
    runtime.manager.close()
    runtime.manager.close()
    assert runtime.manager.client.released.is_set()
    assert runtime.manager._stop.is_set()


def test_reports_serialize_sampling_so_an_older_head_cannot_arrive_last(runtime, monkeypatch):
    from exomem.record_memory import record_memory

    _create(runtime)
    sampled, resume = threading.Event(), threading.Event()
    original = runtime.sample_head

    def paused_sample():
        head = original()
        if threading.current_thread().name == "older-report":
            sampled.set()
            assert resume.wait(3)
        return head

    monkeypatch.setattr(runtime, "sample_head", paused_sample)
    first = threading.Thread(target=runtime.manager._renew_collection_store, args=(1,),
                             name="older-report")
    second = threading.Thread(target=runtime.manager._renew_collection_store, args=(1,))
    first.start()
    assert sampled.wait(2)
    try:
        with preview_store(runtime.root, runtime):
            record_memory(runtime.root, "append", collection=CID,
                          item={"title": "One"}, item_key=KEY, why="capture")
        second.start()
    finally:
        resume.set()
    first.join(3)
    second.join(3)
    assert [head.commit_seq for head in runtime.manager.client.heads] == [1, 2]


def test_idle_flush_crossing_ttl_renews_from_actual_publisher_progress(runtime, monkeypatch):
    _create(runtime)
    manager, client = runtime.manager, runtime.manager.client
    expiry = [time.monotonic() + 3]
    original_renew, original_status = client.renew, client.status

    def status():
        if time.monotonic() >= expiry[0]:
            client.holder = None
        return original_status()

    def renew(token, **kwargs):
        assert time.monotonic() < expiry[0], "renewal arrived after authority expired"
        expiry[0] = time.monotonic() + 3
        return original_renew(token, **kwargs)

    monkeypatch.setattr(client, "status", status)
    monkeypatch.setattr(client, "renew", renew)
    original_flush = replica._Publisher.flush
    delayed = []

    def slow_flush(publisher):
        if not delayed:
            delayed.append(True)
            until = time.monotonic() + 3.2
            while time.monotonic() < until:
                publisher.check()
                time.sleep(0.1)
        return original_flush(publisher)

    monkeypatch.setattr(replica._Publisher, "flush", slow_flush)
    manager._last_activity_monotonic -= 10
    assert manager._maybe_idle_release(1)
    assert len(client.heads) >= 4
    assert client.release_head == runtime.sample_head()
    assert time.monotonic() < expiry[0]


def test_lease_loss_during_publication_does_not_release_or_reacquire(runtime, monkeypatch):
    _create(runtime)
    manager, client = runtime.manager, runtime.manager.client
    original_flush = replica._Publisher.flush

    def lose_lease(publisher):
        client.holder = "other"
        return original_flush(publisher)

    monkeypatch.setattr(replica._Publisher, "flush", lose_lease)
    manager._last_activity_monotonic -= 10
    assert not manager._maybe_idle_release(1)
    assert not client.released.is_set()
    assert manager._fencing_token is None
    assert client.holder == "other"
    with pytest.raises(OpError, match="FLUSH_PENDING"):
        manager.close()


def test_file_only_release_keeps_legacy_wire_shape(tmp_path):
    client = Client()
    manager = LeaseManager(LeaseConfig(
        url="https://lease.example", vault_id="vault", replica_id="local",
        state_dir=tmp_path, idle_release_seconds=1,
    ), client=client)
    manager.ensure_writer()
    manager._last_activity_monotonic -= 10
    assert manager._maybe_idle_release(1)
    manager.close()
    assert client.released.is_set()
    assert client.release_head is None
    assert not client.heads


def test_head_identity_and_tail_are_sampled_from_one_snapshot(runtime, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from exomem.collection_store import legacy_originals
    from exomem.record_memory import record_memory

    _create(runtime)
    before = runtime.sample_head()
    metadata_read, resume = threading.Event(), threading.Event()
    original_metadata = legacy_originals.metadata

    def metadata(reader):
        result = original_metadata(reader)
        if threading.current_thread().name.startswith("snapshot"):
            metadata_read.set()
            assert resume.wait(3)
        return result

    monkeypatch.setattr(legacy_originals, "metadata", metadata)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="snapshot") as pool:
        sample = pool.submit(runtime.sample_head)
        assert metadata_read.wait(2)
        try:
            with preview_store(runtime.root, runtime):
                record_memory(runtime.root, "append", collection=CID,
                              item={"title": "One"}, item_key=KEY, why="capture")
        finally:
            resume.set()
        assert sample.result(timeout=3) == before
    assert runtime.sample_head().commit_seq == before.commit_seq + 1


def test_explicit_close_drains_authority_holders_without_owning_boundary(runtime):
    from concurrent.futures import ThreadPoolExecutor

    _create(runtime)
    manager = runtime.manager
    with ThreadPoolExecutor(max_workers=1) as pool:
        with manager.writer_authority_guard(vault_root=runtime.root):
            close = pool.submit(manager.close)
            assert not manager.client.released.wait(0.15)
            with manager._mutation_coordinator_for(runtime.root).hold(timeout_seconds=0):
                pass
        close.result(timeout=5)
    assert manager.client.released.is_set()


@pytest.mark.parametrize("body_error", [False, True], ids=["normal-exit", "exceptional-exit"])
def test_close_waits_until_authority_grant_is_released(runtime, monkeypatch, body_error):
    from contextlib import contextmanager, nullcontext
    from queue import Queue

    _create(runtime)
    manager = runtime.manager
    guard_thread = threading.current_thread()
    grant_releasing, resume_grant = threading.Event(), threading.Event()
    boundary_available = threading.Event()
    observations = Queue()
    first_event, close_errors, handoff_errors = [], [], []
    original_claim = manager._replica_grant_claim
    original_wait = manager._store_condition.wait

    def close():
        try:
            manager.close()
        except BaseException as error:  # noqa: BLE001 - report every worker failure on the test thread
            close_errors.append(error)
            observations.put(("close-error", error))
        else:
            observations.put(("closed", None))

    close_worker = threading.Thread(target=close, name="close-authority-grant")

    @contextmanager
    def claim(*, exclusive=False):
        with original_claim(exclusive=exclusive) as available:
            if exclusive and threading.current_thread() is close_worker:
                observations.put(("exclusive-claim", available))
            try:
                yield available
            finally:
                if not exclusive and threading.current_thread() is guard_thread:
                    # Pause after the guard body exits, while the real shared grant remains held.
                    grant_releasing.set()
                    assert resume_grant.wait(5), "shared grant release was not resumed"

    def wait(timeout=None):
        if threading.current_thread() is close_worker:
            observations.put(("drain-wait", None))
        return original_wait(timeout)

    def handoff():
        try:
            assert grant_releasing.wait(5), "guard never reached shared grant release"
            close_worker.start()
            event = observations.get(timeout=5)
            first_event.append(event)
            if event == ("drain-wait", None):
                with manager._mutation_coordinator_for(runtime.root).hold(timeout_seconds=0):
                    boundary_available.set()
        except BaseException as error:  # noqa: BLE001 - report every worker failure on the test thread
            handoff_errors.append(error)
        finally:
            resume_grant.set()

    monkeypatch.setattr(manager, "_replica_grant_claim", claim)
    monkeypatch.setattr(manager._store_condition, "wait", wait)
    handoff_worker = threading.Thread(target=handoff, name="handoff-authority-grant")
    handoff_worker.start()
    try:
        body = pytest.raises(ValueError, match="guard body failed") if body_error else nullcontext()
        with body, manager.writer_authority_guard(vault_root=runtime.root):
            if body_error:
                raise ValueError("guard body failed")
    finally:
        resume_grant.set()
        handoff_worker.join(5)
        if close_worker.ident is not None:
            close_worker.join(5)

    diagnostic = (f"first close event={first_event!r}; close errors={close_errors!r}; "
                  f"handoff errors={handoff_errors!r}")
    assert not handoff_worker.is_alive(), diagnostic
    assert not close_worker.is_alive(), diagnostic
    assert first_event == [("drain-wait", None)], diagnostic
    assert not handoff_errors, diagnostic
    assert not close_errors, diagnostic
    assert boundary_available.is_set(), diagnostic
    assert manager.client.released.is_set()
    assert manager.client.release_head == runtime.sample_head()


def test_close_drains_a_guard_rejected_after_shared_grant_acquisition(runtime, monkeypatch):
    from contextlib import contextmanager
    from contextvars import copy_context
    from queue import Queue

    _create(runtime)
    manager = runtime.manager
    guard_thread = threading.current_thread()
    grant_acquired, resume_acquisition = threading.Event(), threading.Event()
    boundary_available, late_refused, late_claimed = (
        threading.Event(), threading.Event(), threading.Event()
    )
    observations = Queue()
    first_event, close_errors, handoff_errors = [], [], []
    original_claim = manager._replica_grant_claim
    original_wait = manager._store_condition.wait

    def close():
        try:
            manager.close()
        except BaseException as error:  # noqa: BLE001 - report every worker failure on the test thread
            close_errors.append(error)
            observations.put(("close-error", error))
        else:
            observations.put(("closed", None))

    close_worker = threading.Thread(target=close, name="close-acquiring-authority")

    @contextmanager
    def claim(*, exclusive=False):
        with original_claim(exclusive=exclusive) as available:
            if exclusive and threading.current_thread() is close_worker:
                observations.put(("exclusive-claim", available))
            elif not exclusive and threading.current_thread() is guard_thread:
                grant_acquired.set()
                assert resume_acquisition.wait(5), "shared grant acquisition was not resumed"
            elif not exclusive and threading.current_thread() is handoff_worker:
                late_claimed.set()
            yield available

    def wait(timeout=None):
        if threading.current_thread() is close_worker:
            observations.put(("drain-wait", None))
        return original_wait(timeout)

    def handoff():
        try:
            assert grant_acquired.wait(5), "guard never acquired its shared grant"
            close_worker.start()
            event = observations.get(timeout=5)
            first_event.append(event)
            if event == ("drain-wait", None):
                with manager._mutation_coordinator_for(runtime.root).hold(timeout_seconds=0):
                    boundary_available.set()
                with pytest.raises(OpError, match="COLLECTION_STORE_BUSY"):
                    with manager.writer_authority_guard(vault_root=runtime.root):
                        pytest.fail("a guard entered after handoff began")
                late_refused.set()
        except BaseException as error:  # noqa: BLE001 - report every worker failure on the test thread
            handoff_errors.append(error)
        finally:
            resume_acquisition.set()

    monkeypatch.setattr(manager, "_replica_grant_claim", claim)
    monkeypatch.setattr(manager._store_condition, "wait", wait)
    handoff_worker = threading.Thread(
        target=copy_context().run, args=(handoff,), name="handoff-acquiring-authority"
    )
    handoff_worker.start()
    try:
        with pytest.raises(OpError, match="COLLECTION_STORE_BUSY"):
            with manager.writer_authority_guard(vault_root=runtime.root):
                pytest.fail("a guard entered after its acquisition overlapped handoff")
    finally:
        resume_acquisition.set()
        handoff_worker.join(5)
        if close_worker.ident is not None:
            close_worker.join(5)

    diagnostic = (f"first close event={first_event!r}; close errors={close_errors!r}; "
                  f"handoff errors={handoff_errors!r}")
    assert not handoff_worker.is_alive(), diagnostic
    assert not close_worker.is_alive(), diagnostic
    assert first_event == [("drain-wait", None)], diagnostic
    assert not handoff_errors, diagnostic
    assert not close_errors, diagnostic
    assert boundary_available.is_set(), diagnostic
    assert late_refused.is_set(), diagnostic
    assert not late_claimed.is_set(), diagnostic
    assert manager.client.released.is_set()
    assert manager.client.release_head == runtime.sample_head()


def test_authority_grant_acquisition_timeout_does_not_block_close(runtime, monkeypatch):
    _create(runtime)
    manager = runtime.manager
    with manager._replica_grant_claim(exclusive=True) as acquired:
        assert acquired
        with monkeypatch.context() as patch:
            patch.setattr(manager, "_mutation_timeout_seconds", 0)
            with pytest.raises(OpError, match="MUTATION_BUSY"):
                with manager.writer_authority_guard(vault_root=runtime.root):
                    pytest.fail("an authority guard entered while an exclusive grant was held")

    manager.close()
    assert manager.client.released.is_set()
    assert manager.client.release_head == runtime.sample_head()


def test_saved_adapter_cannot_read_after_checkout(runtime):
    from exomem import record_formats

    _create(runtime)
    with preview_store(runtime.root, runtime) as writer, writer.read_collection(CID) as manifest:
        adapter = record_formats.load_adapter(runtime.root, manifest)
        assert adapter.read().records == ()
    with pytest.raises(connection.CollectionStoreError, match="CHECKOUT_CLOSED"):
        adapter.read()


def test_cancellation_during_real_publication_leaves_release_pending(runtime, monkeypatch):
    _create(runtime)
    cancelled = threading.Event()
    original = replica._Publisher.flush

    def cancel(publisher):
        cancelled.set()
        return original(publisher)

    with monkeypatch.context() as patch:
        patch.setattr(replica._Publisher, "flush", cancel)
        with pytest.raises(connection.CollectionStoreError, match="FLUSH_PENDING"):
            runtime.manager.close(cancelled=cancelled.is_set)
    assert not runtime.manager.client.released.is_set()
    assert runtime.manager._fencing_token == 1
    assert runtime.manager._stop.is_set()
    runtime.manager.close()
    assert runtime.manager.client.release_head == runtime.sample_head()


def test_idle_boundary_contention_defers_publication_and_keeps_warm_handle(runtime):
    from concurrent.futures import ThreadPoolExecutor

    _create(runtime)
    manager = runtime.manager
    manager._mutation_timeout_seconds = 0.1
    manager._last_activity_monotonic -= 10
    with manager.consistency_guard(runtime.root), ThreadPoolExecutor(max_workers=1) as pool:
        assert not pool.submit(manager._maybe_idle_release, 1).result(timeout=2)
    assert not manager.client.released.is_set()
    assert manager._fencing_token == 1
    with preview_store(runtime.root, runtime) as writer:
        assert writer.inspect_collection(CID)


def test_unavailable_head_does_not_kill_renewer_or_report_null(runtime, monkeypatch):
    _create(runtime)
    attempted, recovered = threading.Event(), threading.Event()
    sample = runtime.sample_head

    def unavailable_once():
        if not attempted.is_set():
            attempted.set()
            raise connection.CollectionStoreError("COLLECTION_STORE_ABSENT", "unavailable")
        recovered.set()
        return sample()

    monkeypatch.setattr(runtime, "sample_head", unavailable_once)
    # Keep the idle path out of this renewal-specific failure.
    runtime.manager._last_activity_monotonic = time.monotonic() + 60
    runtime.manager.start_renewer()
    assert attempted.wait(2)
    assert recovered.wait(2)
    assert runtime.manager._renewer.is_alive()
    runtime.manager._stop.set()
    runtime.manager._renewer.join(2)
    assert all(head is not None for head in runtime.manager.client.heads)


def test_retained_inspection_provenance_cannot_expose_a_live_connection(runtime):
    from concurrent.futures import ThreadPoolExecutor

    from exomem.governance import egress

    _create(runtime)
    with preview_store(runtime.root, runtime) as writer:
        result = writer.inspect_collection(CID)
        assert egress.postfilter("record_memory", result, runtime.root) == result
    assert not runtime.manager._store_borrowers
    evidence = result._canonical_metadata_evidence
    assert getattr(evidence.handle, "connection", None) is None
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(getattr, evidence.handle, "connection", None).result(timeout=2) is None


def test_retained_inspection_cache_cannot_mutate_after_checkout(runtime):
    _create(runtime)
    with preview_store(runtime.root, runtime) as writer:
        writer.inspect_collection(CID)
        cache = writer.handle.release_cache.inspections
    with pytest.raises(connection.CollectionStoreError, match="CHECKOUT_CLOSED"):
        cache.clear()


def test_idle_boundary_wait_crossing_ttl_keeps_lease_until_publication(runtime, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    _create(runtime)
    manager, client = runtime.manager, runtime.manager.client
    expiry = [time.monotonic() + 3]
    started = threading.Event()
    original_renew, original_status = client.renew, client.status

    def status():
        if time.monotonic() >= expiry[0]:
            client.holder = None
        return original_status()

    def renew(token, **kwargs):
        status()
        if client.holder is not None:
            expiry[0] = time.monotonic() + 3
        result = original_renew(token, **kwargs)
        started.set()
        return result

    monkeypatch.setattr(client, "status", status)
    monkeypatch.setattr(client, "renew", renew)
    manager._last_activity_monotonic -= 10
    manager._mutation_timeout_seconds = 10
    with ThreadPoolExecutor(max_workers=1) as pool:
        with manager.consistency_guard(runtime.root):
            release = pool.submit(manager._maybe_idle_release, 1)
            assert started.wait(1)
            time.sleep(3.2)
        assert release.result(timeout=3)
    assert client.release_head == runtime.sample_head()
    assert time.monotonic() < expiry[0]


def test_warm_read_revalidates_authority_without_acquire_or_write_activity(runtime, monkeypatch):
    _create(runtime)
    manager, client = runtime.manager, runtime.manager.client
    manager._last_activity_monotonic -= 10
    activity = manager._last_activity_monotonic
    calls = []
    acquire, status = client.acquire, client.status

    def counted_acquire(**kwargs):
        calls.append("acquire")
        return acquire(**kwargs)

    def counted_status():
        calls.append("status")
        return status()

    monkeypatch.setattr(client, "acquire", counted_acquire)
    monkeypatch.setattr(client, "status", counted_status)
    with preview_store(runtime.root, runtime) as writer:
        assert writer.inspect_collection(CID)["coverage"]["committed"] == 0
    assert "status" in calls
    assert "acquire" not in calls
    assert manager._last_activity_monotonic == activity


def test_read_after_schema_fence_cut_rechecks_acquisition_compatibility(runtime, monkeypatch):
    _create(runtime)
    client = runtime.manager.client
    # The coordinator's schema CAS revokes the holder and advances its token.
    client.holder = None
    client.token += 1

    def incompatible_acquire(**kwargs):
        return LeaseRecord("local", time.time() + 30, client.token, True,
                           governance_enrolled=True, required_schema_version=999)

    monkeypatch.setattr(client, "acquire", incompatible_acquire)
    with pytest.raises(OpError, match="WRITER_SCHEMA_FENCE_MISMATCH"):
        with preview_store(runtime.root, runtime):
            pytest.fail("read borrowed a store after the schema fence cut")


def test_warm_read_propagates_coordinator_failure_without_acquisition(runtime, monkeypatch):
    _create(runtime)
    client = runtime.manager.client

    def unavailable_status():
        raise OpError("WRITER_COORDINATOR_UNAVAILABLE", "coordinator unavailable")

    def unexpected_acquire(**kwargs):
        pytest.fail("coordinator failure must not trigger acquisition")

    monkeypatch.setattr(client, "status", unavailable_status)
    monkeypatch.setattr(client, "acquire", unexpected_acquire)
    with pytest.raises(OpError, match="WRITER_COORDINATOR_UNAVAILABLE"):
        with preview_store(runtime.root, runtime):
            pytest.fail("read borrowed a store without current authority")


def test_expired_cached_writer_reopens_on_new_owner_thread(runtime):
    from concurrent.futures import ThreadPoolExecutor

    from exomem import structured_collections as collections
    from exomem.governance import egress
    from exomem.record_memory import record_memory

    _create(runtime)
    with preview_store(runtime.root, runtime) as writer:
        result = writer.inspect_collection(CID)
    old = runtime._handle
    runtime.manager.client.holder = None

    def reopen():
        with preview_store(runtime.root, runtime) as writer:
            assert writer.inspect_collection(CID) == result
            with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
                egress.postfilter("record_memory", result, runtime.root)
            record_memory(runtime.root, "append", collection=CID,
                          item={"title": "After expiry"}, item_key=KEY, why="capture")
        with preview_store(runtime.root, runtime) as writer:
            assert writer.inspect_collection(CID)["coverage"]["committed"] == 1

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(reopen).result(timeout=3)
    assert old._closed
    assert runtime._writer_token == runtime.manager._fencing_token == 2


def test_stale_cached_writer_is_not_retired_during_nested_snapshot(runtime):
    from concurrent.futures import ThreadPoolExecutor

    _create(runtime)
    with preview_store(runtime.root, runtime) as writer, writer.read_snapshot():
        old = runtime._handle
        runtime.manager.client.holder = None
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_BUSY"):
                pool.submit(preview_store(runtime.root, runtime).__enter__).result(timeout=2)
        with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_BUSY"):
            with preview_store(runtime.root, runtime):
                pass
        assert not old._closed
        assert writer.connection.in_transaction
    with preview_store(runtime.root, runtime) as writer:
        assert writer.inspect_collection(CID)
    assert old._closed


def test_stale_cached_writer_is_not_retired_with_an_unfinished_transaction(runtime):
    _create(runtime)
    with preview_store(runtime.root, runtime) as writer:
        writer.connection.execute("BEGIN")
    old = runtime._handle
    runtime.manager.client.holder = None
    with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_BUSY"):
        with preview_store(runtime.root, runtime):
            pass
    assert not old._closed
    old.connection.rollback()
    with preview_store(runtime.root, runtime) as writer:
        assert writer.inspect_collection(CID)
    assert old._closed


def test_standalone_preview_close_drains_without_claiming_or_closing_store(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    client = Client()
    manager = LeaseManager(LeaseConfig(
        url="https://lease.example", vault_id="vault", replica_id="local",
        state_dir=tmp_path / "lease",
    ), client=client)
    manager.ensure_writer()
    monkeypatch.setattr("exomem.writer_lease.active_manager", lambda: manager)
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        with ThreadPoolExecutor(max_workers=2) as pool:
            with preview_store(root, handle) as writer, writer.read_snapshot():
                close = pool.submit(manager.close, deadline=time.monotonic() + 2)
                assert not client.released.wait(0.15), "released during standalone preview read"
                with pytest.raises(OpError, match="COLLECTION_STORE_BUSY"):
                    pool.submit(preview_store(root, handle).__enter__).result(timeout=1)
                assert writer.connection.execute("SELECT 1").fetchone() == (1,)
            close.result(timeout=3)
        assert client.released.is_set()
        assert client.release_head is None
        assert handle.connection.execute("SELECT 1").fetchone() == (1,)
        with pytest.raises(OpError, match="COLLECTION_STORE_BUSY") as busy:
            with preview_store(root, handle):
                pass
        # The one shape every BUSY takes, so a caller retries instead of failing.
        assert busy.value.details == {"status": "retryable", "committed": False}
        manager.close()


def test_standalone_close_from_borrower_stays_pending(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    client = Client()
    manager = LeaseManager(LeaseConfig(
        url="https://lease.example", vault_id="vault", replica_id="local",
        state_dir=tmp_path / "lease",
    ), client=client)
    manager.ensure_writer()
    monkeypatch.setattr("exomem.writer_lease.active_manager", lambda: manager)
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        with preview_store(root, handle) as writer, writer.read_snapshot():
            with pytest.raises(OpError, match="COLLECTION_STORE_FLUSH_PENDING"):
                manager.close(deadline=time.monotonic() + 0.05)
            assert not client.released.is_set()
            assert manager._stop.is_set()
            assert writer.connection.execute("SELECT 1").fetchone() == (1,)
        manager.close()
        assert client.released.is_set()


class CoordinatorClient:
    """The coordinator's own SQLite store behind the lease client interface, counting the acquires it serves."""

    def __init__(self, store, config):
        self.store, self.config, self.acquires = store, config, 0

    def acquire(self, **kwargs):
        self.acquires += 1
        return LeaseRecord.from_json(self.store.acquire(
            self.config.vault_id, self.config.replica_id, self.config.ttl_seconds,
            schema_version=self.config.schema_version, **kwargs))

    def status(self):
        return LeaseRecord.from_json(self.store.status(self.config.vault_id))

    def renew(self, token, collection_store_head=None, **kwargs):
        # This vault enrols no store fence, so the coordinator has nowhere to record a head.
        return LeaseRecord.from_json(self.store.renew(
            self.config.vault_id, self.config.replica_id, token, self.config.ttl_seconds,
            schema_version=self.config.schema_version, **kwargs))


@pytest.fixture
def coordinated(tmp_path):
    from exomem.lease_coordinator import SQLiteLeaseStore

    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    config = LeaseConfig(
        url="https://lease.example", vault_id="vault", replica_id="local",
        state_dir=tmp_path / "lease", ttl_seconds=30,
    )
    store = SQLiteLeaseStore(tmp_path / "coordinator.sqlite")
    client = CoordinatorClient(store, config)
    manager = LeaseManager(config, client=client)
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True):
        pass
    runtime = CollectionStoreRuntime(root, manager, authority_check=lambda: True)
    yield runtime, store, client
    if runtime._handle is not None:
        runtime._handle.close()
    manager._stop.set()


def test_a_store_write_inside_the_operations_guard_asks_the_coordinator_for_one_grant(coordinated):
    from exomem.record_memory import record_memory

    runtime, _, client = coordinated
    _create(runtime)
    granted_before = client.acquires
    with runtime.manager.writer_authority_guard(vault_root=runtime.root):
        with preview_store(runtime.root, runtime):
            record_memory(runtime.root, "append", collection=CID,
                          item={"title": "One"}, item_key=KEY, why="capture")
    assert runtime.sample_head().commit_seq == 2
    assert client.acquires - granted_before == 1


def test_a_file_effect_nested_in_an_operation_is_refused_once_the_operations_grant_is_revoked(coordinated):
    from exomem.create_directory import create_directory

    runtime, store, _ = coordinated
    with runtime.manager.mutation_guard(runtime.root, operation="outer_operation"):
        # The cut revokes the holder and advances the token, as a takeover by another replica does.
        store.transition_schema_fence("vault", expected_generation=0, schema_version=3)
        with pytest.raises(OpError) as refused:
            create_directory(runtime.root, path="Notes/after-the-cut")
    assert refused.value.code == "WRITER_SCHEMA_FENCE_MISMATCH"
    assert not (runtime.root / "Notes").exists()


def test_a_schema_cut_after_the_operations_grant_fences_the_store_write(coordinated):
    from exomem.record_memory import record_memory

    runtime, store, _ = coordinated
    _create(runtime)
    committed = runtime.sample_head().commit_seq
    with runtime.manager.writer_authority_guard(vault_root=runtime.root):
        with preview_store(runtime.root, runtime):
            # The cut revokes the holder and advances the token; nothing renews before the write.
            store.transition_schema_fence("vault", expected_generation=0, schema_version=3)
            with pytest.raises(OpError) as fenced:
                record_memory(runtime.root, "append", collection=CID,
                              item={"title": "Late"}, item_key=KEY, why="after the cut")
    assert fenced.value.code == "WRITER_FENCED"
    assert runtime.sample_head().commit_seq == committed


# The reader pool: the serving handle lends read-only connections, so each test first shows that a
# second lend returns the same connection, then breaks the one thing that would make that unsafe.

@pytest.fixture
def handle(tmp_path):
    with connection.open_writer(tmp_path / "collections.sqlite", lease_check=lambda: True) as writer:
        yield writer


def _write_probe(handle, value):
    with handle.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO store_meta(key, value) VALUES('probe', ?)", (value,))


def _read_probe(conn):
    row = conn.execute("SELECT value FROM store_meta WHERE key='probe'").fetchone()
    return None if row is None else row[0]


def test_a_head_sample_sees_the_commit_made_after_an_earlier_sample_returned_its_reader(runtime):
    """A sample opens a transaction on its reader; returning the reader must not carry that snapshot."""
    from exomem.record_memory import record_memory

    _create(runtime)
    pool = runtime.reader_pool()
    first = runtime.sample_head()
    with preview_store(runtime.root, runtime):
        record_memory(runtime.root, "append", collection=CID, item={"title": "One"}, item_key=KEY, why="capture")
    with pool.lend(head=True, current=False) as warm:
        pass
    assert runtime.sample_head().commit_seq == first.commit_seq + 1
    with pool.lend(head=True, current=False) as again:
        assert again is warm


def test_a_reader_returned_with_a_half_read_cursor_is_not_lent_again(handle):
    """A half-read cursor pins its snapshot while the reader reports no transaction, so the next lend would read stale."""
    pool = handle.readers
    _write_probe(handle, "one")
    with pool.lend(head=False, current=True) as warm:
        pass
    with pool.lend(head=False, current=True) as lent:
        assert lent is warm
        cursor = lent.execute("SELECT key FROM store_meta")  # the borrower keeps it past the lend
        cursor.fetchone()
    _write_probe(handle, "two")
    with pool.lend(head=False, current=True) as next_lend:
        assert next_lend is not lent
        assert _read_probe(next_lend) == "two"


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses to replace a store file that a connection has open")
def test_a_pooled_reader_does_not_serve_a_store_file_that_was_replaced(runtime, tmp_path):
    _create(runtime)
    pool = runtime.reader_pool()
    runtime.sample_head()
    with pool.lend(head=True, current=False) as warm:
        pass
    with pool.lend(head=True, current=False) as again:
        assert again is warm
    donor = tmp_path / "donor.sqlite"
    connection.open_writer(donor, lease_check=lambda: True).close()
    with closing(sqlite3.connect(runtime.path)) as raw:
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    os.replace(donor, runtime.path)
    with pytest.raises(connection.CollectionStoreError, match="IDENTITY_CHANGED"):
        runtime.sample_head()


def test_retiring_the_handle_leaves_no_reader_open_on_the_store(runtime):
    _create(runtime)
    pool = runtime.reader_pool()
    with pool.lend(head=True, current=False) as warm:
        pass
    with pool.lend(head=True, current=False) as again:
        assert again is warm
    assert runtime.retire_idle_handle()
    with closing(sqlite3.connect(runtime.path, timeout=0)) as raw:
        assert raw.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    assert not runtime.path.with_name(runtime.path.name + "-wal").exists()


def test_closing_the_handle_during_a_lend_lets_the_read_finish_and_closes_that_reader_after(handle):
    pool = handle.readers
    with pool.lend(head=False, current=True) as warm:
        pass
    lent, release, outcome = threading.Event(), threading.Event(), {}

    def borrow():
        try:
            with pool.lend(head=False, current=True) as reader:
                outcome["pooled"] = reader is warm
                lent.set()
                assert release.wait(5)
                outcome["count"] = reader.execute("SELECT COUNT(*) FROM store_meta").fetchone()[0]
                outcome["reader"] = reader
        except BaseException as error:  # noqa: BLE001 - hand the failure to the asserting thread
            outcome["error"] = error

    thread = threading.Thread(target=borrow)
    thread.start()
    assert lent.wait(5)
    handle.close()
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome
    assert outcome["pooled"] and outcome["count"] > 0
    with pytest.raises(sqlite3.ProgrammingError):
        outcome["reader"].execute("SELECT 1")
    with pool.lend(head=False, current=True) as after:
        assert after is not outcome["reader"]


def test_a_nested_lend_gets_another_reader_and_leaves_the_outer_snapshot_alone(handle):
    pool = handle.readers
    _write_probe(handle, "one")
    with pool.lend(head=False, current=True) as warm:
        pass
    with pool.lend(head=False, current=True) as outer:
        assert outer is warm
        outer.execute("BEGIN")
        assert _read_probe(outer) == "one"
        _write_probe(handle, "two")
        with pool.lend(head=False, current=True) as inner:
            assert inner is not outer
            assert _read_probe(inner) == "two"
        assert _read_probe(outer) == "one"


def test_a_warm_reader_refuses_a_store_that_now_records_a_newer_schema(handle):
    pool = handle.readers
    with pool.lend(head=True, current=False) as warm:
        pass
    with pool.lend(head=True, current=False) as again:
        assert again is warm
    with closing(sqlite3.connect(handle.path)) as raw:
        raw.execute("UPDATE store_meta SET value=? WHERE key=?",
                    (str(schema.SCHEMA_VERSION + 1), schema.META_SCHEMA_VERSION))
        raw.commit()
    with pytest.raises(connection.CollectionStoreError, match="SCHEMA_NEWER"):
        with pool.lend(head=True, current=False):
            pytest.fail("a lend served a store that records a newer schema")


def test_a_warm_reader_refuses_a_current_lend_but_serves_a_head_lend_while_the_store_awaits_migration(
        handle, monkeypatch):
    pool = handle.readers
    with pool.lend(head=False, current=True):
        pass
    with pool.lend(head=True, current=False) as head:
        pass
    monkeypatch.setattr(schema, "SCHEMA_VERSION", schema.SCHEMA_VERSION + 1)
    with pytest.raises(connection.CollectionStoreError, match="SCHEMA_PENDING"):
        with pool.lend(head=False, current=True):
            pytest.fail("a current lend served a store that awaits its writer's migration")
    with pool.lend(head=True, current=False) as served:
        assert served is head


def test_a_disable_committed_while_the_checkout_borrows_refuses_it_from_a_warm_reader(runtime, monkeypatch):
    """The checkout rechecks the rollback after it borrows; a reader that answered the first check must not answer again."""
    _create(runtime)
    pool = runtime.reader_pool()
    with pool.lend(head=True, current=False) as warm:
        pass
    borrow = runtime.manager._collection_store_checkout

    @contextmanager
    def disabled_while_borrowing():
        with closing(sqlite3.connect(runtime.path)) as raw:
            raw.execute("INSERT OR IGNORE INTO store_meta(key, value) VALUES(?, '1')", (capability.DISABLED_KEY,))
            raw.commit()
        with borrow() as borrowed:
            yield borrowed

    monkeypatch.setattr(runtime.manager, "_collection_store_checkout", disabled_while_borrowing)
    with state_paths.resolution_scope(), pytest.raises(connection.CollectionStoreError, match="DISABLED"):
        with runtime.checkout():
            pytest.fail("a checkout began after the vault's rollback")
    with pool.lend(head=True, current=False) as again:
        assert again is warm


def test_only_the_cached_writers_own_store_gets_its_pool(runtime, tmp_path):
    assert runtime.reader_pool() is None
    _create(runtime)
    assert runtime.reader_pool() is runtime._handle.readers
    assert runtime.reader_pool(runtime.path) is runtime._handle.readers
    assert runtime.reader_pool(tmp_path / "another.sqlite") is None
