from contextlib import closing

import pytest

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


def _published_note(vault):
    import uuid

    from exomem import epistemic_graph

    path = "Knowledge Base/Notes/Insights/stranded.md"
    page = vault / path
    page.parent.mkdir(parents=True)
    page.write_text(
        f"---\ntype: insight\nstatus: active\nexomem_id: {uuid.uuid4()}\n---\n"
        "A durable conclusion written while the graph was republishing.\n",
        encoding="utf-8",
    )
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return path


def _wait_for(predicate, *, seconds=30.0):
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_a_job_stranded_by_a_withdrawn_graph_drains_after_the_next_publish(tmp_path, monkeypatch):
    """Graph churn strands a write's guidance; the next publish delivers it (1.3).

    No activation drain and no review call reaches the job: the watcher is
    already running with an empty queue when the write lands.
    """
    import threading

    from exomem import epistemic_graph, mutation_terminal, server_runtime, vocabulary_review

    path = _published_note(tmp_path)
    reviews = []
    monkeypatch.setattr(vocabulary_review, "review", lambda *a, **k: reviews.append(a) or {})
    recoveries = []
    real_recover = vocabulary_delivery.recover
    monkeypatch.setattr(
        vocabulary_delivery,
        "recover",
        lambda *a, **k: recoveries.append(a) or real_recover(*a, **k),
    )
    shutdown = threading.Event()
    watcher = threading.Thread(
        target=server_runtime.watch_vocabulary_recovery, args=(tmp_path, shutdown), daemon=True
    )
    watcher.start()
    try:
        # The activation drain finds an empty queue and makes one bounded pass.
        assert _wait_for(lambda: len(recoveries) == 1)
        assert vocabulary_recovery.publication_waiting(tmp_path)
        recoveries.clear()
        graph = epistemic_graph.EpistemicGraphIndex(tmp_path)
        graph.withdraw_availability()
        terminal = mutation_terminal.committed_terminal(
            {"path": path},
            request_id="stranded-write",
            receipt_id="receipt-stranded",
            idempotency_key="stranded-write",
        )
        with library_scope():
            written = vocabulary_delivery.after_commit(tmp_path, terminal)
            assert written["vocabulary_sync"]["reason"] == "graph_projection_unavailable"
            assert len(vocabulary_recovery.page(tmp_path, limit=4)) == 1
        assert recoveries == []

        with library_scope():
            graph.rebuild_all()

        def drained():
            with library_scope():
                return vocabulary_recovery.page(tmp_path, limit=4) == ()

        assert _wait_for(drained)
        assert reviews == []
    finally:
        shutdown.set()
        watcher.join(timeout=10)
    assert not watcher.is_alive()


def test_a_publish_after_the_activation_drain_redrains_nothing(tmp_path, monkeypatch):
    import threading

    from exomem import epistemic_graph, graph_sync, server_runtime

    with library_scope():
        vocabulary_recovery.enqueue(tmp_path, "0", "Knowledge Base/Notes/0.md")
    projected = []
    monkeypatch.setattr(vocabulary_delivery.vocabulary_review, "_visible", lambda root, item: True)
    monkeypatch.setattr(
        vocabulary_delivery,
        "_project",
        lambda root, path, continuation: projected.append(path)
        or {"sync": {"state": "current"}, "continuation": None, "items": []},
    )
    monkeypatch.setattr(graph_sync, "status", lambda root: {"state": "current"})
    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "available", lambda self: True)

    assert server_runtime.drain_vocabulary_recovery(tmp_path, threading.Event()) == 1
    assert server_runtime.redrain_after_publish(tmp_path, threading.Event()) is None
    assert projected == ["Knowledge Base/Notes/0.md"]


def test_a_publish_that_never_becomes_readable_leaves_the_job_queued(tmp_path, monkeypatch):
    import threading

    from exomem import epistemic_graph, server_runtime

    with library_scope():
        vocabulary_recovery.enqueue(tmp_path, "0", "Knowledge Base/Notes/0.md")
    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "available", lambda self: False)
    monkeypatch.setattr(
        vocabulary_delivery, "recover", lambda *a, **k: pytest.fail("an unreadable graph must not drain")
    )

    assert server_runtime.redrain_after_publish(tmp_path, threading.Event(), ready_seconds=0.05) == 0
    with library_scope():
        assert len(vocabulary_recovery.page(tmp_path, limit=4)) == 1


def test_publication_signal_is_a_non_blocking_no_op_without_a_watcher(tmp_path):
    vocabulary_recovery.note_graph_published(tmp_path)
    assert not vocabulary_recovery.publication_waiting(tmp_path)
