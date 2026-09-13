from contextlib import closing

from exomem import vocabulary_delivery, vocabulary_recovery
from exomem.governance.principal import library_scope


def test_recovery_scans_a_fixed_window_and_rotates_hidden_work(tmp_path, monkeypatch):
    with library_scope():
        vocabulary_recovery.enqueue(tmp_path, "0", "Knowledge Base/Notes/0.md")
        with closing(vocabulary_recovery._connect(tmp_path)) as connection, connection:
            connection.executemany(
                "INSERT INTO vocabulary_recovery_jobs(job_id,path) VALUES (?,?)",
                [(str(index), f"Knowledge Base/Notes/{index}.md") for index in range(1, 1000)],
            )
    seen = []
    monkeypatch.setattr(vocabulary_delivery.vocabulary_review, "_visible", lambda root, item: seen.append(next(iter(item["target_versions"]))) or False)
    with library_scope():
        first = vocabulary_delivery.recover(tmp_path)
        second = vocabulary_delivery.recover(tmp_path)
    assert len(seen) == 8
    assert len(set(seen)) == 8
    assert first == second == {"state": "current", "processed": 0, "coverage": "bounded-pass"}
    assert "more" not in first


def test_recovery_cas_cannot_complete_a_newer_write(tmp_path):
    with library_scope():
        path = "Knowledge Base/Notes/example.md"
        vocabulary_recovery.enqueue(tmp_path, "old", path)
        old = vocabulary_recovery.page(tmp_path, limit=1)[0]
        assert vocabulary_recovery.claim(tmp_path, old)
        assert not vocabulary_recovery.claim(tmp_path, old)
        vocabulary_recovery.enqueue(tmp_path, "new", path)
        vocabulary_recovery.complete(tmp_path, old, continuation=None)
        jobs = vocabulary_recovery.page(tmp_path, limit=4)
    assert len(jobs) == 1
    assert jobs[0].key == "new"


def test_empty_recovery_does_not_create_a_store(tmp_path):
    with library_scope():
        assert vocabulary_recovery.page(tmp_path, limit=4) == ()
    assert not vocabulary_recovery.deferred_index.store_path(tmp_path).exists()


def test_activation_drains_queued_recovery_once_the_projection_is_current(tmp_path, monkeypatch):
    """Queued rows drain in the background, with no client review call (D12)."""
    import threading

    from exomem import graph_sync, server_runtime, vocabulary_review

    with library_scope():
        for index in range(9):
            vocabulary_recovery.enqueue(tmp_path, str(index), f"Knowledge Base/Notes/{index}.md")
        assert len(vocabulary_recovery.page(tmp_path, limit=4)) == 4

    reviews = []
    monkeypatch.setattr(
        vocabulary_review, "review", lambda *a, **k: reviews.append(a) or {}
    )
    monkeypatch.setattr(vocabulary_delivery.vocabulary_review, "_visible", lambda root, item: True)
    monkeypatch.setattr(
        vocabulary_delivery,
        "_project",
        lambda root, path, continuation: {
            "sync": {"state": "current"},
            "continuation": None,
            "items": [],
        },
    )
    monkeypatch.setattr(graph_sync, "status", lambda root: {"state": "current"})

    drained = server_runtime.drain_vocabulary_recovery(tmp_path, threading.Event())

    assert drained == 9
    assert reviews == []
    with library_scope():
        assert vocabulary_recovery.page(tmp_path, limit=4) == ()


def test_the_drain_gives_up_when_the_projection_never_becomes_current(tmp_path, monkeypatch):
    import threading

    from exomem import graph_sync, server_runtime

    with library_scope():
        vocabulary_recovery.enqueue(tmp_path, "0", "Knowledge Base/Notes/0.md")
    monkeypatch.setattr(graph_sync, "status", lambda root: {"state": "stale"})
    drained = server_runtime.drain_vocabulary_recovery(
        tmp_path, threading.Event(), wait_seconds=0.05
    )
    assert drained == 0
    with library_scope():
        assert len(vocabulary_recovery.page(tmp_path, limit=4)) == 1


def test_a_shutdown_stops_the_drain(tmp_path, monkeypatch):
    import threading

    from exomem import graph_sync, server_runtime

    monkeypatch.setattr(graph_sync, "status", lambda root: {"state": "current"})
    shutdown = threading.Event()
    shutdown.set()
    assert server_runtime.drain_vocabulary_recovery(tmp_path, shutdown) == 0
