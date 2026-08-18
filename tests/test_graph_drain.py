"""The daemon that decides *when* queued epistemic-graph repair runs."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from exomem import deferred_index, graph_drain, index_sync


@pytest.fixture(autouse=True)
def _stop_the_daemon():
    """No test may leave a drain thread running into the next one."""
    yield
    graph_drain.stop()
    graph_drain._DEBT.clear()


@pytest.fixture(autouse=True)
def _no_standing_barrier(monkeypatch: pytest.MonkeyPatch):
    """Default to a graph with no stopped rebuild to repair.

    `_pending` is the union of queued receipts and a persisted barrier, so a
    test about the queue has to say the barrier is absent or it is quietly
    testing both.
    """
    monkeypatch.setattr(graph_drain, "_barrier_pending", lambda _root: False)


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_a_queued_write_is_drained_without_waiting_for_the_reconcile_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: repair follows the write, not a five-minute clock.

    Before this daemon the only drain call sites were inside
    `file_watcher._reconcile_once`, which runs on `reconcile_interval_seconds` --
    300s by default. Between ticks the graph reported `recovery_required` and
    readers got `graph sidecar unavailable`, with the repair already queued.
    """
    drained = threading.Event()
    calls: list[int | None] = []

    def drain(_root: Path, *, limit: int | None = None) -> int:
        calls.append(limit)
        drained.set()
        return 1

    monkeypatch.setattr(index_sync, "drain_graph_work", drain)
    monkeypatch.setattr(graph_drain, "_queue_pending", lambda _root: not drained.is_set())
    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.01)

    graph_drain.start(tmp_path)

    assert drained.wait(timeout=5.0), "queued repair was never drained"
    assert calls == [graph_drain.DRAIN_LIMIT]


def test_the_debt_signal_wakes_a_sleeping_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enqueue must wake the worker rather than wait out its idle poll.

    The idle poll is a backstop for debt queued by another process. If a local
    enqueue had to wait for it, this would reintroduce the same latency in a
    smaller unit.
    """
    monkeypatch.setattr(graph_drain, "IDLE_POLL_SECONDS", 3600.0)
    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.01)
    pending = threading.Event()
    drained = threading.Event()
    monkeypatch.setattr(graph_drain, "_queue_pending", lambda _root: pending.is_set())

    def drain(_root: Path, *, limit: int | None = None) -> int:
        pending.clear()
        drained.set()
        return 1

    monkeypatch.setattr(index_sync, "drain_graph_work", drain)

    graph_drain.start(tmp_path)
    assert _wait_for(lambda: not graph_drain._DEBT.is_set())  # startup pass done
    drained.clear()

    pending.set()
    graph_drain.note_graph_debt()

    assert drained.wait(timeout=5.0), "the enqueue signal did not wake the drain"


def test_a_queue_that_cannot_drain_backs_off_instead_of_spinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-progress is the mid-batch case, so it retries -- but bounded.

    An epoch that is not settled refuses the drain and the next pass clears it,
    which is why this retries at all. The other cause is a queue that cannot
    drain, and that must not become a busy loop against the sidecar.
    """
    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(graph_drain, "RETRY_SECONDS", 0.01)
    monkeypatch.setattr(graph_drain, "MAX_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(graph_drain, "_queue_pending", lambda _root: True)
    attempts: list[float] = []

    def drain(_root: Path, *, limit: int | None = None) -> int:
        attempts.append(time.monotonic())
        return 0  # never any progress

    monkeypatch.setattr(index_sync, "drain_graph_work", drain)

    graph_drain.start(tmp_path)
    assert _wait_for(lambda: len(attempts) >= 4, timeout=5.0)
    graph_drain.stop()

    gaps = [b - a for a, b in zip(attempts, attempts[1:], strict=False)]
    assert gaps, "expected repeated attempts"
    assert max(gaps) <= 1.0, f"backoff exceeded its ceiling: {gaps}"
    assert gaps[-1] >= gaps[0], f"expected backoff to grow, got {gaps}"


def test_a_failing_drain_never_kills_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue is durable; one bad pass must not end scheduling forever."""
    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(graph_drain, "RETRY_SECONDS", 0.01)
    monkeypatch.setattr(graph_drain, "MAX_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(graph_drain, "_queue_pending", lambda _root: True)
    attempts: list[int] = []

    def explode(_root: Path, *, limit: int | None = None) -> int:
        attempts.append(1)
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(index_sync, "drain_graph_work", explode)

    thread = graph_drain.start(tmp_path)
    assert _wait_for(lambda: len(attempts) >= 3, timeout=5.0)
    assert thread is not None and thread.is_alive()


def test_an_unreadable_queue_is_not_pending_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queue-depth is a scheduling hint, not a contract."""

    def explode(_root: Path) -> int | None:
        raise RuntimeError("sidecar unreadable")

    monkeypatch.setattr(deferred_index, "graph_full_rebuild_pending", explode)

    assert graph_drain._queue_pending(tmp_path) is False


def test_a_whole_vault_marker_counts_as_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker is raised exactly when the changed scope is unknown.

    That is the case the drain most needs to hear about, and it carries no
    per-path receipts, so a depth-only check would call the queue empty.
    """
    monkeypatch.setattr(deferred_index, "graph_full_rebuild_pending", lambda _root: 7)
    monkeypatch.setattr(deferred_index, "graph_status", lambda _root: {"count": 0})

    assert graph_drain._queue_pending(tmp_path) is True


def test_start_is_idempotent_and_stop_ends_the_thread(tmp_path: Path) -> None:
    """Hosted quiesce/resume reuses the process; a second start must not stack."""
    first = graph_drain.start(tmp_path)
    second = graph_drain.start(tmp_path)

    assert first is not None and first is second

    graph_drain.stop()
    assert _wait_for(lambda: not first.is_alive())


def test_the_kill_switch_declines_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_DRAIN", "1")

    assert graph_drain.disabled() is True
    assert graph_drain.start(tmp_path) is None


def test_enqueuing_graph_work_signals_the_drain(tmp_path: Path) -> None:
    """The seam that makes repair prompt: `add_graph` wakes the daemon."""
    graph_drain._DEBT.clear()

    added = deferred_index.add_graph(tmp_path, ["Knowledge Base/Notes/a.md"])

    assert added
    assert graph_drain._DEBT.is_set()


def test_a_whole_vault_marker_signals_the_drain(tmp_path: Path) -> None:
    deferred_index.mark_graph_full_rebuild(tmp_path, generation=3)

    assert graph_drain._DEBT.is_set()


def test_a_stopped_rebuild_is_re_armed_without_the_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half of convergence the queue cannot express.

    A rebuild that stops is terminal -- `graph_sync` records the error, clears
    `_running` and returns -- so its debt lives in a persisted barrier, not in
    `graph_upserts`. A product E2E run showed the cost: the queue settled four
    times in seven seconds, the rebuild stopped at +7.3s, and the server then
    answered readiness polls for the remaining 120s without ever attempting
    another. The only lane that would have retried is the watcher's 300s
    reconcile, which that run disables outright.
    """
    from exomem import epistemic_graph

    barrier = threading.Event()
    barrier.set()
    recovered = threading.Event()

    def recover(_root: Path) -> bool:
        barrier.clear()
        recovered.set()
        return True

    monkeypatch.setattr(graph_drain, "_queue_pending", lambda _root: False)
    monkeypatch.setattr(graph_drain, "_barrier_pending", lambda _root: barrier.is_set())
    monkeypatch.setattr(epistemic_graph, "recover_suspended_graph", recover)
    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.01)

    graph_drain.start(tmp_path)

    assert recovered.wait(timeout=5.0), "the stopped rebuild was never re-armed"


def test_a_barrier_alone_is_enough_to_count_as_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty queue is not the same as a converged graph."""
    monkeypatch.setattr(graph_drain, "_queue_pending", lambda _root: False)
    monkeypatch.setattr(graph_drain, "_barrier_pending", lambda _root: True)

    assert graph_drain._pending(tmp_path) is True


def test_a_rebuild_that_cannot_publish_backs_off_like_a_queue_that_cannot_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Folding the barrier into `_pending` is what buys this.

    Recovery is a *whole-vault* rebuild. Retrying one every idle poll would cost
    far more than the queue thrash the backoff was written for, so a barrier
    that will not clear has to be as bounded as a queue that will not drain.
    """
    from exomem import epistemic_graph

    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(graph_drain, "RETRY_SECONDS", 0.01)
    monkeypatch.setattr(graph_drain, "MAX_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(graph_drain, "_queue_pending", lambda _root: False)
    monkeypatch.setattr(graph_drain, "_barrier_pending", lambda _root: True)
    attempts: list[float] = []

    def never_recovers(_root: Path) -> bool:
        attempts.append(time.monotonic())
        return False

    monkeypatch.setattr(epistemic_graph, "recover_suspended_graph", never_recovers)

    graph_drain.start(tmp_path)
    assert _wait_for(lambda: len(attempts) >= 4, timeout=5.0)
    graph_drain.stop()

    gaps = [b - a for a, b in zip(attempts, attempts[1:], strict=False)]
    assert gaps, "expected repeated attempts"
    assert max(gaps) <= 1.0, f"backoff exceeded its ceiling: {gaps}"


def test_a_raising_recovery_never_kills_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The barrier is persisted, so it survives to be retried -- the thread must too."""
    from exomem import epistemic_graph

    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(graph_drain, "RETRY_SECONDS", 0.01)
    monkeypatch.setattr(graph_drain, "MAX_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(graph_drain, "_queue_pending", lambda _root: False)
    monkeypatch.setattr(graph_drain, "_barrier_pending", lambda _root: True)
    attempts: list[int] = []

    def explode(_root: Path) -> bool:
        attempts.append(1)
        raise RuntimeError("recovery exploded")

    monkeypatch.setattr(epistemic_graph, "recover_suspended_graph", explode)

    thread = graph_drain.start(tmp_path)
    assert _wait_for(lambda: len(attempts) >= 3, timeout=5.0)
    assert thread is not None and thread.is_alive()


def test_the_queue_is_drained_before_a_barrier_is_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order matters: the proportional repair may clear the expensive one.

    Draining queued paths can publish the incremental marker the barrier was
    waiting on, which is cheaper than the whole-vault rebuild recovery runs.
    """
    from exomem import epistemic_graph, index_sync

    order: list[str] = []
    queued = threading.Event()
    queued.set()

    def drain(_root: Path, *, limit: int | None = None) -> int:
        order.append("drain")
        queued.clear()
        return 1

    def recover(_root: Path) -> bool:
        order.append("recover")
        return True

    monkeypatch.setattr(index_sync, "drain_graph_work", drain)
    monkeypatch.setattr(epistemic_graph, "recover_suspended_graph", recover)
    monkeypatch.setattr(graph_drain, "_queue_pending", lambda _root: queued.is_set())
    monkeypatch.setattr(graph_drain, "_barrier_pending", lambda _root: True)

    assert graph_drain._work_once(tmp_path) == 2
    assert order == ["drain", "recover"]
