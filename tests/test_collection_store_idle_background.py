"""A store the lease manager released on idle takes the lease again only for work it can prove."""

import threading
import time
from contextlib import closing

import pytest
from test_collection_store_s1_gate import LATER, ROW, _until, coordinator
from test_collection_store_service import ON, Service, _served_vault, call, offline_create
from test_collection_store_writer import CID, manifest_path

from exomem import graph_sync, writer_lease
from exomem.cli_ops import OpError
from exomem.collection_store import capability, connection, edit_back, runtime
from exomem.collection_store.writer import CollectionWriter


@pytest.fixture
def idle_service(tmp_path, monkeypatch):
    """The managed service as a non-preferred holder, which idle-releases, with views on and quick ticks."""
    root = _served_vault(tmp_path, monkeypatch)
    monkeypatch.delenv("EXOMEM_WRITER_LEASE_PREFERRED")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TTL", "3")  # the lease loop wakes every second
    monkeypatch.setattr(capability, "RELEASED", ON | {capability.COLLECTION_VIEWS_V1})
    monkeypatch.setattr(runtime, "IMPORT_IDLE_SECONDS", 0.05)
    monkeypatch.setattr(runtime, "IMPORT_RETRY_SECONDS", 0.2)
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 0.3)
    with coordinator(tmp_path / "coordinator.sqlite"):
        assert offline_create(root, tmp_path / "c.md", manifest_path(), why="new store collection") == 0
        started = Service(root, writer_lease.start_server_lifecycle())
        try:
            _until(lambda: started.manager.status().get("collection_store", {}).get("status") == "admitted")
            yield started
        finally:
            if not started.manager._stop.is_set():
                started.stop()


def rows(root, sql, parameters=()):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute(sql, parameters).fetchall()


def release_on_idle(manager):
    """Let the lease loop's own idle rule fire now, instead of waiting out the idle period.

    The clock is aged again whenever a last background write moved it, until the loop releases."""
    idle = manager.config.idle_release_seconds

    def released():
        if manager._fencing_token is not None and time.monotonic() - manager._last_activity_monotonic < idle:
            manager._last_activity_monotonic -= idle + 1
        return manager._fencing_token is None

    _until(released, seconds=30)


def graph_rebuilding():
    """Whether a graph rebuild thread is still running in this process."""
    return any(thread.name == graph_sync.GRAPH_REBUILD_THREAD_NAME and thread.is_alive()
               for thread in threading.enumerate())


def watching(monkeypatch, manager):
    """The lease grants the coordinator made, and how many views ticks ran, from here on."""
    grants, ticks = [], []
    acquire, tick = manager.client.acquire, runtime.StoreServer._views_tick

    def counted_acquire(*args, **kwargs):
        grants.append(args)
        return acquire(*args, **kwargs)

    def counted_tick(server):
        ticks.append(server)
        return tick(server)

    monkeypatch.setattr(manager.client, "acquire", counted_acquire)
    monkeypatch.setattr(runtime.StoreServer, "_views_tick", counted_tick)
    return grants, ticks


def test_a_released_store_takes_the_lease_only_for_an_edited_view_or_a_request(idle_service, monkeypatch):
    """Defect: the store's own ticks take the lease again right after an idle release, holding the vault
    boundary for a takeover of a store that has nothing to do; or a released store ignores an owner's edit
    of a view file, or a request for it."""
    root, manager = idle_service.root, idle_service.manager
    call(root, "record_memory", action="append", collection=CID, item={"title": "Canonical"},
         item_key=ROW, why="first row")
    view = root / "Knowledge Base/Records/Work/_collection.md"  # a summary collection's editable view
    _until(lambda: rows(root, "SELECT count(*) FROM projection_state WHERE state!='current'") == [(0,)])
    # The writes' graph rebuilds take the lease to publish; only the store's own ticks are under test.
    _until(lambda: not graph_rebuilding(), seconds=30)
    grants, ticks = watching(monkeypatch, manager)
    release_on_idle(manager)
    granted, head = len(grants), rows(root, "SELECT value FROM store_meta WHERE key='commit_seq'")
    seen = len(ticks)
    _until(lambda: len(ticks) >= seen + 40)  # a second of ticks, several sweeps of the view files
    assert manager._fencing_token is None and len(grants) == granted
    assert rows(root, "SELECT value FROM store_meta WHERE key='commit_seq'") == head
    assert rows(root, "SELECT count(*) FROM projection_state WHERE state='pending'") == [(0,)]
    # The owner edits the view file; the watcher is off, so only the tick's own look can find it.
    view.write_bytes(view.read_bytes().replace(b"title: Work", b"title: Revised work"))
    _until(lambda: "title: Revised work" in rows(
        root, "SELECT manifest_text FROM collection_manifests ORDER BY manifest_version DESC LIMIT 1")[0][0])
    assert len(grants) > granted
    release_on_idle(manager)
    granted = len(grants)
    assert call(root, "record_memory", action="append", collection=CID, item={"title": "Later"},
                item_key=LATER, why="after the release")["outcome"] == "committed"
    assert len(grants) > granted and manager._fencing_token is not None


def test_taking_the_lease_again_queues_no_history_page_but_a_changed_row_still_renders_its_page(
        idle_service, monkeypatch):
    """Defect: every takeover of an unchanged store marks all its history pages pending, so each one is
    rendered again; or the change that keeps that quiet also keeps a changed row's page from rendering.

    The first half asserts that no page was queued, which is a mechanism: a page rendered again without
    need keeps its inode, mtime and projection rows, so no behaviour-level artifact exists to assert.
    The second half renders a changed row and does assert the behaviour."""
    root, manager = idle_service.root, idle_service.manager
    appended = call(root, "record_memory", action="append", collection=CID, item={"title": "Initial", "count": 1},
                    item_key=ROW, why="capture")
    update = {"action": "update", "collection": CID, "item_key": ROW}
    corrected = call(root, "record_memory", **update, changes={"count": 2}, why="first correction",
                     expected_container_hash=appended["after_container_hash"],
                     expected_item_version=appended["after_item_hash"])
    page = root / "Knowledge Base/Records/Work/_history.md"
    _until(lambda: page.exists() and "first correction" in page.read_text())
    server = runtime._server(root)
    _until(lambda: server._history_policy is not None)
    queued, invalidate = [], CollectionWriter.invalidate_history_pages

    def counted(writer):
        queued.append(writer)
        return invalidate(writer)

    monkeypatch.setattr(CollectionWriter, "invalidate_history_pages", counted)
    grants, ticks = watching(monkeypatch, manager)
    for _ in range(2):  # a release, then a read that takes the lease again and lets the ticks run
        release_on_idle(manager)
        granted = len(grants)
        call(root, "record_memory", action="query", collection=CID)
        assert len(grants) > granted
        seen = len(ticks)
        _until(lambda seen=seen: len(ticks) >= seen + 20)
    assert queued == [] and rows(root, "SELECT count(*) FROM projection_state WHERE state='pending'") == [(0,)]
    call(root, "record_memory", **update, changes={"count": 3}, why="second correction",
         expected_container_hash=corrected["after_container_hash"], expected_item_version=corrected["after_item_hash"])
    _until(lambda: "second correction" in page.read_text())


def test_an_edit_whose_wake_could_not_take_the_lease_is_absorbed_by_a_later_sweep(idle_service, monkeypatch):
    """Defect: the sweep counts an edited view as handled when it wakes the tick, so a wake whose lease
    request fails leaves the edit unabsorbed, with the watcher off, until some request takes the lease."""
    root, manager = idle_service.root, idle_service.manager
    call(root, "record_memory", action="append", collection=CID, item={"title": "Canonical"},
         item_key=ROW, why="first row")
    view = root / "Knowledge Base/Records/Work/_collection.md"
    _until(lambda: rows(root, "SELECT count(*) FROM projection_state WHERE state!='current'") == [(0,)])
    release_on_idle(manager)
    store, refused, acquire = runtime._server(root).thread, [], manager.client.acquire

    def refusing_once(*args, **kwargs):
        if threading.current_thread() is store and not refused:  # only the store thread's first wake
            refused.append(args)
            raise OpError("WRITER_COORDINATOR_UNAVAILABLE", "the coordinator did not answer")
        return acquire(*args, **kwargs)

    monkeypatch.setattr(manager.client, "acquire", refusing_once)
    view.write_bytes(view.read_bytes().replace(b"title: Work", b"title: Revised work"))
    _until(lambda: refused)
    _until(lambda: "title: Revised work" in rows(
        root, "SELECT manifest_text FROM collection_manifests ORDER BY manifest_version DESC LIMIT 1")[0][0])


def test_a_view_deleted_again_after_the_store_restored_it_is_restored_again(idle_service):
    """Defect: the sweep remembers a deleted view by its missing file alone, so the same deletion after
    the store restored the view and released the lease again is taken for the one already handled."""
    root, manager = idle_service.root, idle_service.manager
    call(root, "record_memory", action="append", collection=CID, item={"title": "Canonical"},
         item_key=ROW, why="first row")
    view = root / "Knowledge Base/Records/Work/_collection.md"
    _until(lambda: rows(root, "SELECT count(*) FROM projection_state WHERE state!='current'") == [(0,)])
    for _ in range(2):
        release_on_idle(manager)
        view.unlink()
        _until(view.exists)
