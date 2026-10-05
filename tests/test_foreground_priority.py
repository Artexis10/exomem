"""Latency-bound requests run ahead of in-process bulk work.

Measured 2026-09-28 on the personal service: while a whole-vault graph rebuild
ran in the serving process, every activation stage ran 5-15x slower (server
total 0.3-0.8 s settled, 1.9-4.5 s during the rebuild). The rebuild is
CPU-bound Python; the request releases the GIL on every short SQLite call and
then waits for the rebuild thread to hand it back. These tests pin the
scheduling seam that removes that wait: bulk loops yield while a request is in
flight, boundedly.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import commands, epistemic_graph, foreground_priority


def test_yield_is_free_when_no_request_is_in_flight() -> None:
    started = time.monotonic()
    waited = foreground_priority.yield_to_foreground(max_wait=5.0)
    assert waited == 0.0
    assert time.monotonic() - started < 0.05


def test_worker_cancellation_preserves_per_pass_scheduling() -> None:
    """Daemon uptime must not become a whole-vault pass's fairness budget."""
    stop = threading.Event()
    items = [1, 2]
    with foreground_priority.cancellable(stop):
        assert foreground_priority.yielding_in_bulk(items) is items
        with foreground_priority.bulk():
            assert list(foreground_priority.yielding_in_bulk(items)) == items
            stop.set()
            with pytest.raises(foreground_priority.BulkCancelled):
                foreground_priority.check_cancelled()
    foreground_priority.check_cancelled()


def test_yield_waits_until_the_request_finishes() -> None:
    entered = threading.Event()
    release = threading.Event()

    def request() -> None:
        with foreground_priority.foreground():
            entered.set()
            release.wait(5.0)

    worker = threading.Thread(target=request)
    worker.start()
    assert entered.wait(5.0)
    threading.Timer(0.3, release.set).start()
    waited = foreground_priority.yield_to_foreground(max_wait=5.0)
    worker.join(5.0)
    assert 0.2 <= waited < 2.0
    assert foreground_priority.in_flight() == 0


def test_overlapping_requests_hold_bulk_work_until_the_last_one_ends() -> None:
    first_in = threading.Event()
    second_in = threading.Event()
    first_out = threading.Event()
    second_out = threading.Event()

    def request(entered: threading.Event, leave: threading.Event) -> None:
        with foreground_priority.foreground():
            entered.set()
            leave.wait(5.0)

    workers = [
        threading.Thread(target=request, args=(first_in, first_out)),
        threading.Thread(target=request, args=(second_in, second_out)),
    ]
    for worker in workers:
        worker.start()
    assert first_in.wait(5.0) and second_in.wait(5.0)
    assert foreground_priority.in_flight() == 2
    threading.Timer(0.1, first_out.set).start()
    threading.Timer(0.4, second_out.set).start()
    waited = foreground_priority.yield_to_foreground(max_wait=5.0)
    for worker in workers:
        worker.join(5.0)
    assert waited >= 0.3


def test_a_request_that_never_ends_cannot_stop_bulk_work() -> None:
    entered = threading.Event()
    release = threading.Event()

    def request() -> None:
        with foreground_priority.foreground():
            entered.set()
            release.wait(10.0)

    worker = threading.Thread(target=request)
    worker.start()
    try:
        assert entered.wait(5.0)
        started = time.monotonic()
        foreground_priority.yield_to_foreground(max_wait=0.2)
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        worker.join(5.0)


def test_a_request_never_waits_on_itself() -> None:
    """Bulk work run inline by a foreground request must not stall that request."""
    with foreground_priority.foreground():
        started = time.monotonic()
        assert foreground_priority.yield_to_foreground(max_wait=1.0) == 0.0
        assert time.monotonic() - started < 0.05


def test_iterating_through_the_gate_yields_before_each_item() -> None:
    entered = threading.Event()
    release = threading.Event()

    def request() -> None:
        with foreground_priority.foreground():
            entered.set()
            release.wait(5.0)

    worker = threading.Thread(target=request)
    worker.start()
    assert entered.wait(5.0)
    threading.Timer(0.3, release.set).start()
    started = time.monotonic()
    first = next(iter(foreground_priority.yielding(["a", "b"])))
    worker.join(5.0)
    assert first == "a"
    assert time.monotonic() - started >= 0.2


def test_shared_helpers_yield_only_inside_a_bulk_pass() -> None:
    entered = threading.Event()
    release = threading.Event()

    def request() -> None:
        with foreground_priority.foreground():
            entered.set()
            release.wait(5.0)

    worker = threading.Thread(target=request)
    worker.start()
    try:
        assert entered.wait(5.0)
        started = time.monotonic()
        assert list(foreground_priority.yielding_in_bulk(["a", "b"])) == ["a", "b"]
        assert time.monotonic() - started < 0.05
        threading.Timer(0.3, release.set).start()
        with foreground_priority.bulk():
            started = time.monotonic()
            assert list(foreground_priority.yielding_in_bulk(["a"])) == ["a"]
            assert time.monotonic() - started >= 0.2
    finally:
        release.set()
        worker.join(5.0)


def test_a_request_that_raises_still_releases_bulk_work() -> None:
    with pytest.raises(RuntimeError):
        with foreground_priority.foreground():
            raise RuntimeError("request failed")
    assert foreground_priority.in_flight() == 0


def test_the_whole_vault_graph_pass_yields_between_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = tmp_path / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    for index in range(6):
        (notes / f"page-{index}.md").write_text(
            f"---\ntype: note\n---\n\n# Page {index}\n\nSee [[page-{(index + 1) % 6}]].\n",
            encoding="utf-8",
        )
    calls: list[float] = []

    def spy(*, max_wait: float = foreground_priority.MAX_YIELD_SECONDS) -> float:
        calls.append(max_wait)
        return 0.0

    monkeypatch.setattr(foreground_priority, "yield_to_foreground", spy)
    report = epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    assert report["indexed_files"] >= 6
    # One yield per page in the pass, and one per page in each of the two
    # direct-disk freshness walks that bracket it.
    assert len(calls) >= 3 * 6


def test_activation_runs_as_a_foreground_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def body(*_args, **_kwargs):
        seen.append(foreground_priority.in_flight())
        return {"abstained": True, "abstention": {"reason": "unresolved"}}

    monkeypatch.setattr(commands, "_op_activate_context_body", body)
    monkeypatch.setattr(commands, "_carry_thread_through_abstention", lambda *_a, **_k: None)
    monkeypatch.setattr(commands, "_withhold_vault_generation", lambda *_a, **_k: None)
    monkeypatch.setattr(commands.query_log, "log_activation_call", lambda *_a, **_k: None)
    commands.op_activate_context(tmp_path, turn="where were we?")
    assert seen == [1]
    assert foreground_priority.in_flight() == 0


def test_a_thread_holding_a_mutation_boundary_never_pauses(tmp_path: Path) -> None:
    """A pause under a boundary would queue every writer behind the request too."""
    from exomem import mutation_lock

    entered = threading.Event()
    release = threading.Event()

    def request() -> None:
        with foreground_priority.foreground():
            entered.set()
            release.wait(5.0)

    worker = threading.Thread(target=request)
    worker.start()
    (tmp_path / "state").mkdir()
    coordinator = mutation_lock.VaultMutationCoordinator(tmp_path / "state", tmp_path)
    try:
        assert entered.wait(5.0)
        with coordinator.hold(request_id="bulk", operation="bulk_probe"):
            with foreground_priority.bulk():
                started = time.monotonic()
                assert foreground_priority.yield_to_foreground(max_wait=1.0) == 0.0
                assert list(foreground_priority.yielding_in_bulk(["a"])) == ["a"]
                assert time.monotonic() - started < 0.2
    finally:
        release.set()
        worker.join(5.0)


def test_every_whole_vault_phase_yields_and_never_under_a_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured on a 4.6k-page vault: after the pass and its disk walks yield,
    ~10 s per rebuild still ran with no yield: three membership walks (6.4 s),
    source-version proofs (2.3 s), the recall reconcile (1.4 s) and the
    resolver's source versions (0.6 s)."""
    import traceback

    from exomem import mutation_lock

    notes = tmp_path / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    for index in range(4):
        (notes / f"page-{index}.md").write_text(
            f"---\ntype: note\n---\n\n# Page {index}\n\nSee [[page-{(index + 1) % 4}]].\n",
            encoding="utf-8",
        )
    callers: set[str] = set()
    held_while_yielding: list[str] = []
    real_holds = mutation_lock.current_thread_holds_boundary

    def spy(*, max_wait: float = foreground_priority.MAX_YIELD_SECONDS) -> float:
        frames = [frame.name for frame in traceback.extract_stack(limit=12)]
        callers.update(frames)
        if real_holds():
            held_while_yielding.append(frames[-2])
        return 0.0

    monkeypatch.setattr(foreground_priority, "yield_to_foreground", spy)
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    assert {"_recall_membership", "_source_versions_current", "_resolver_source_versions"} <= callers
    assert held_while_yielding == []


def test_the_lexical_repair_rebuild_yields_and_never_under_a_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background lexical repair rebuilds the whole catalogue in the
    serving process: two corpus walks, a tokenizing insert per page, and a
    second pair of walks for the source proof. Like the graph pass, it pauses
    for an in-flight activation, and never under its publication lock or a
    mutation boundary, where writers would wait out the request too."""
    import traceback

    from exomem import lexstore, mutation_lock, vault

    notes = tmp_path / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    for index in range(5):
        (notes / f"page-{index}.md").write_text(
            f"---\ntype: note\n---\n\n# Page {index}\n\nharbour lantern {index}\n",
            encoding="utf-8",
        )
    callers: set[str] = set()
    held_while_yielding: list[str] = []
    real_holds = mutation_lock.current_thread_holds_boundary

    def spy(*, max_wait: float = foreground_priority.MAX_YIELD_SECONDS) -> float:
        frames = [frame.name for frame in traceback.extract_stack(limit=12)]
        callers.update(frames)
        if real_holds() or getattr(vault._HELD_LOCKS, "keys", None):
            held_while_yielding.append(frames[-2])
        return 0.0

    monkeypatch.setattr(foreground_priority, "yield_to_foreground", spy)
    assert lexstore.get_store(tmp_path).rebuild_atomic() is True
    assert {"_walk_entries", "_materialize_catalog"} <= callers
    assert held_while_yielding == []


def _overlapping_requests(stop: threading.Event, *, hold: float = 0.4) -> list[threading.Thread]:
    """Two request streams offset by half a hold: `in_flight` never reaches 0."""

    def stream(offset: float) -> None:
        time.sleep(offset)
        while not stop.is_set():
            with foreground_priority.foreground():
                time.sleep(hold)

    threads = [
        threading.Thread(target=stream, args=(offset,), daemon=True)
        for offset in (0.0, hold / 2)
    ]
    for thread in threads:
        thread.start()
    return threads


def test_a_bulk_pass_keeps_progressing_under_overlapping_requests() -> None:
    """Review M1: with overlapping requests `in_flight` never reached zero, so
    every unit waited the full cap (3 units took 6 s). A pass now spends at
    most about half its elapsed time waiting, past a first grace wait."""
    units = 100
    unit_seconds = 0.01  # a sleep, so the unit costs time but not the GIL
    stop = threading.Event()
    streams = _overlapping_requests(stop)
    done = threading.Event()
    took: list[float] = []

    def bulk_pass() -> None:
        started = time.monotonic()
        with foreground_priority.bulk():
            for _unit in foreground_priority.yielding_in_bulk(range(units)):
                time.sleep(unit_seconds)
        took.append(time.monotonic() - started)
        done.set()

    time.sleep(0.1)
    assert foreground_priority.in_flight() >= 1
    worker = threading.Thread(target=bulk_pass, daemon=True)
    worker.start()
    try:
        # Unyielded ~1 s. Bound: the grace wait plus twice the work, with slack.
        finished = done.wait(timeout=foreground_priority.MAX_YIELD_SECONDS + 4 * units * unit_seconds + 2.0)
    finally:
        stop.set()
        for thread in streams:
            thread.join(5.0)
    assert finished, "the bulk pass starved behind overlapping foreground requests"
    assert took[0] < foreground_priority.MAX_YIELD_SECONDS + 4 * units * unit_seconds


def test_overlapping_requests_receive_only_one_bulk_grace_wait(monkeypatch) -> None:
    """Continuous foreground traffic cannot charge the initial grace twice."""
    clock = [0.0]

    def capped_wait(predicate, *, timeout):
        clock[0] += timeout
        return False

    monkeypatch.setattr(
        foreground_priority, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    monkeypatch.setattr(foreground_priority._condition, "wait_for", capped_wait)
    monkeypatch.setattr(foreground_priority, "_in_flight", 1)
    with foreground_priority.bulk():
        for _ in foreground_priority.yielding_in_bulk(range(100)):
            clock[0] += 0.01

    # One second of useful work permits one second of additional waiting at
    # the 50% share, after the single initial grace.
    assert clock[0] <= foreground_priority.MAX_YIELD_SECONDS + 2.0


def test_a_lone_request_mid_pass_still_holds_the_pass() -> None:
    """The floor must not cost a single request its priority: mid-pass, one
    request still holds the next unit until it ends."""
    entered = threading.Event()
    release = threading.Event()

    def request() -> None:
        with foreground_priority.foreground():
            entered.set()
            release.wait(5.0)

    with foreground_priority.bulk():
        for _unit in foreground_priority.yielding_in_bulk(range(20)):
            time.sleep(0.01)
        worker = threading.Thread(target=request)
        worker.start()
        assert entered.wait(5.0)
        threading.Timer(0.3, release.set).start()
        waited = foreground_priority.yield_to_foreground(max_wait=5.0)
        worker.join(5.0)
    assert 0.2 <= waited < 2.0


def test_costlier_bulk_units_cannot_extend_the_foreground_work_grant(monkeypatch) -> None:
    """A cheap walk must not buy an unbounded burst of later page parsing."""
    clock = [0.0]
    waits: list[float] = []

    def capped_wait(predicate, *, timeout):
        waits.append(timeout)
        clock[0] += timeout
        return False

    monkeypatch.setattr(
        foreground_priority, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    monkeypatch.setattr(foreground_priority._condition, "wait_for", capped_wait)
    monkeypatch.setattr(foreground_priority, "_in_flight", 0)
    with foreground_priority.bulk():
        for _ in range(100):
            foreground_priority.yield_to_foreground()
            clock[0] += 0.001
        monkeypatch.setattr(foreground_priority, "_in_flight", 1)
        assert foreground_priority.yield_to_foreground() > 0
        for _ in range(2):
            foreground_priority.yield_to_foreground()
            clock[0] += 0.03
        assert foreground_priority.yield_to_foreground() > 0
    assert len(waits) == 2


def test_nested_bulk_stop_interrupts_without_traffic_and_restores_scope() -> None:
    """Zero traffic/free scheduling units must not defeat lifecycle cancellation."""
    stop = threading.Event()
    completed = []
    with pytest.raises(foreground_priority.BulkCancelled):
        with foreground_priority.bulk(stop=stop):
            with foreground_priority.bulk():
                for item in foreground_priority.yielding_in_bulk(range(3)):
                    completed.append(item)
                    stop.set()
    assert completed == [0]
    with foreground_priority.bulk():
        assert list(foreground_priority.yielding_in_bulk(range(3))) == [0, 1, 2]
