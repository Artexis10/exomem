"""Cached preview operations and real replica handoff share one lifetime."""

import threading
import time

import pytest
from test_collection_store_writer import CID, KEY, manifest_path, manifest_text

from exomem.cli_ops import OpError
from exomem.collection_store import connection, replica, schema
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
    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
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
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
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

    from exomem.record_memory import record_memory

    _create(runtime)
    before = runtime.sample_head()
    metadata_read, resume = threading.Event(), threading.Event()
    original_open = connection.open_reader

    class PausedReader:
        def __init__(self, reader):
            self.reader = reader

        def execute(self, statement):
            if "FROM txns" in statement:
                metadata_read.set()
                assert resume.wait(3)
            return self.reader.execute(statement)

        def close(self):
            self.reader.close()

    def open_reader(*args, **kwargs):
        reader = original_open(*args, **kwargs)
        return PausedReader(reader) if threading.current_thread().name.startswith("snapshot") else reader

    monkeypatch.setattr(connection, "open_reader", open_reader)
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
    evidence = result._canonical_inspection_evidence
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

    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
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
    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
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
