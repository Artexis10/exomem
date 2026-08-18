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
    monkeypatch.setattr(graph_drain, "_pending", lambda _root: not drained.is_set())
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
    monkeypatch.setattr(graph_drain, "_pending", lambda _root: pending.is_set())

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
    monkeypatch.setattr(graph_drain, "_pending", lambda _root: True)
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
    monkeypatch.setattr(graph_drain, "_pending", lambda _root: True)
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

    assert graph_drain._pending(tmp_path) is False


def test_a_whole_vault_marker_counts_as_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker is raised exactly when the changed scope is unknown.

    That is the case the drain most needs to hear about, and it carries no
    per-path receipts, so a depth-only check would call the queue empty.
    """
    monkeypatch.setattr(deferred_index, "graph_full_rebuild_pending", lambda _root: 7)
    monkeypatch.setattr(deferred_index, "graph_status", lambda _root: {"count": 0})

    assert graph_drain._pending(tmp_path) is True


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
