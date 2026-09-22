"""The daemon that decides *when* queued epistemic-graph repair runs."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from exomem import deferred_index, epistemic_graph, freshness, graph_drain, graph_sync, index_sync
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex


@pytest.fixture(autouse=True)
def _stop_the_daemon():
    """No test may leave a drain thread running into the next one."""
    yield
    graph_drain.stop()
    graph_drain._DEBT.clear()


@pytest.fixture(autouse=True)
def _no_standing_barrier(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Default to a graph with no stopped rebuild to repair.

    `_pending` is the union of queued receipts and a persisted barrier, so a
    test about the queue has to say the barrier is absent or it is quietly
    testing both.
    """
    if "real_barrier" not in request.fixturenames:
        monkeypatch.setattr(graph_drain, "_barrier_pending", lambda _root: False)


@pytest.fixture
def real_barrier() -> None:
    """Opt into the persisted read-barrier probe for daemon recovery tests."""


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _page(title: str, body: str) -> str:
    return f"---\ntype: insight\nstatus: active\n---\n# {title}\n\n## Claim\n\n{body}\n"


def _published_vault(root: Path) -> Path:
    """Build a graph whose recall publication can be made cold independently."""
    vault = root / "vault"
    notes = vault / "Knowledge Base/Notes/Insights"
    notes.mkdir(parents=True)
    (notes / "a.md").write_text(_page("A", "A links to [[b]]."), encoding="utf-8")
    (notes / "b.md").write_text(_page("B", "B is present."), encoding="utf-8")
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(vault)),
    )
    freshness.seed(
        vault,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(vault / "Knowledge Base")
        ),
    )
    EpistemicGraphIndex(vault).rebuild_all()
    return vault


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
    attempts: list[int] = []
    scheduled: list[float | None] = []
    real_wait = graph_drain._DEBT.wait

    def record(timeout: float | None = None) -> bool:
        # Record the interval the loop *scheduled*, not the wall clock between
        # attempts. Measuring elapsed time asserted that a 10 ms sleep and a
        # 50 ms sleep are distinguishable on a shared runner: macOS CI
        # observed [74 ms, 68 ms, 48 ms] for a schedule that had already
        # reached its 50 ms ceiling by the second attempt, so every gap was
        # the same nominal sleep and the ordering was scheduler noise.
        scheduled.append(timeout)
        return real_wait(timeout=timeout)

    monkeypatch.setattr(graph_drain._DEBT, "wait", record)

    def drain(_root: Path, *, limit: int | None = None) -> int:
        attempts.append(1)
        return 0  # never any progress

    monkeypatch.setattr(index_sync, "drain_graph_work", drain)

    graph_drain.start(tmp_path)
    assert _wait_for(lambda: len(attempts) >= 4, timeout=5.0)
    graph_drain.stop()

    # The first wait is the idle poll the loop starts on; the backoff is what
    # follows it.
    backoff = [interval for interval in scheduled[1:] if interval is not None]
    assert len(backoff) >= 3, f"expected repeated attempts, got {scheduled}"
    assert max(backoff) <= graph_drain.MAX_RETRY_SECONDS, (
        f"backoff exceeded its ceiling: {backoff}"
    )
    assert min(backoff) >= graph_drain.RETRY_SECONDS, (
        f"backoff dropped below one retry interval: {backoff}"
    )
    assert backoff == sorted(backoff), f"expected backoff to grow, got {backoff}"


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


def test_a_barrier_with_an_external_mark_converges_through_the_full_marker(
    tmp_path: Path,
    real_barrier: None,
) -> None:
    """A declined barrier recovery is coverage debt, not a settled graph.

    The daemon sees an empty per-path queue here.  The external mark says its
    coverage is unknown, so it must use the existing full-marker route rather
    than baselining the current process and advertising the old graph.
    """
    vault = _published_vault(tmp_path)
    index = EpistemicGraphIndex(vault)
    index.suspend_reads()
    freshness.mark_external_pending(vault, paths=[vault / "Knowledge Base/Notes/Insights/a.md"])

    assert graph_drain._work_once(vault) == 1
    assert deferred_index.graph_full_rebuild_pending(vault) is not None
    assert freshness.external_pending(vault)
    assert not index.available()

    assert graph_drain._work_once(vault) == 1
    assert deferred_index.graph_full_rebuild_pending(vault) is None
    assert not freshness.external_pending(vault)
    assert EpistemicGraphIndex(vault).available()


def test_cold_recovery_rebuilds_unqueued_recall_content_before_advertising_current(
    tmp_path: Path,
    real_barrier: None,
) -> None:
    """A cold process cannot bless a partial event scope as a complete graph."""
    vault = _published_vault(tmp_path)
    queued = vault / "Knowledge Base/Notes/Insights/c.md"
    unqueued = vault / "Knowledge Base/Notes/Insights/other.md"
    queued.write_text(_page("C", "C is queued."), encoding="utf-8")
    unqueued.write_text(_page("Other", "Other was not in the event scope."), encoding="utf-8")
    freshness.invalidate(vault)
    EpistemicGraphIndex(vault).suspend_reads()
    freshness.mark_external_pending(vault, paths=[queued])

    assert graph_drain._work_once(vault) == 1
    assert graph_drain._work_once(vault) == 1

    connection = EpistemicGraphIndex(vault)._open_read_snapshot()
    assert connection is not None
    try:
        assert connection.execute(
            "SELECT path FROM graph_nodes WHERE path = ?", (str(unqueued.relative_to(vault)),)
        ).fetchone() is not None
    finally:
        connection.close()


def test_a_newer_external_epoch_stays_pending_when_reconcile_retires_the_older_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_barrier: None,
) -> None:
    """Clear-through may retire only the sampled epoch, never a newer mark."""
    vault = _published_vault(tmp_path)
    EpistemicGraphIndex(vault).suspend_reads()
    freshness.mark_external_pending(vault, paths=[vault / "Knowledge Base/Notes/Insights/a.md"])
    assert graph_drain._work_once(vault) == 1

    real_reconcile = freshness.reconcile
    real_clear = freshness.clear_external_pending
    after_old_clear: list[tuple[bool, bool]] = []
    evictions: list[str] = []
    marked_newer = False

    real_evict_resolver = find_module.evict_resolver_caches
    real_evict_inbound = vault_module.evict_inbound_index

    def evict_resolver(root: Path) -> None:
        evictions.append("resolver")
        real_evict_resolver(root)

    def evict_inbound(root: Path) -> None:
        evictions.append("inbound")
        real_evict_inbound(root)

    def reconcile_with_newer_mark(*args, **kwargs):
        nonlocal marked_newer
        result = real_reconcile(*args, **kwargs)
        if not marked_newer:
            marked_newer = True
            freshness.mark_external_pending(
                vault, paths=[vault / "Knowledge Base/Notes/Insights/b.md"]
            )
        return result

    def observe_clear(root: Path, *, through: int) -> None:
        assert evictions[:2] == ["resolver", "inbound"]
        real_clear(root, through=through)
        after_old_clear.append(
            (freshness.external_pending(vault), EpistemicGraphIndex(vault).available())
        )

    monkeypatch.setattr(freshness, "reconcile", reconcile_with_newer_mark)
    monkeypatch.setattr(freshness, "clear_external_pending", observe_clear)
    monkeypatch.setattr(find_module, "evict_resolver_caches", evict_resolver)
    monkeypatch.setattr(vault_module, "evict_inbound_index", evict_inbound)

    assert graph_drain._work_once(vault) == 1
    assert after_old_clear[0] == (True, False)


def test_refusal_backoff_and_an_active_owner_do_not_duplicate_recovery_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_barrier: None,
) -> None:
    """The durable marker is one retry handle, subject to the existing backoff."""
    vault = _published_vault(tmp_path)
    EpistemicGraphIndex(vault).suspend_reads()
    freshness.mark_external_pending(vault, paths=[vault / "Knowledge Base/Notes/Insights/a.md"])
    epistemic_graph.note_publication_refusal(vault)

    assert graph_drain._work_once(vault) == 0
    assert deferred_index.graph_full_rebuild_pending(vault) is None

    epistemic_graph.clear_publication_refusal(vault)
    assert graph_drain._work_once(vault) == 1
    marker = deferred_index.graph_full_rebuild_pending(vault)
    assert marker is not None

    monkeypatch.setattr(graph_sync, "claim_rebuild_owner", lambda *_args, **_kwargs: False)

    assert graph_drain._work_once(vault) == 0
    assert deferred_index.graph_full_rebuild_pending(vault) == marker
    assert graph_drain._work_once(vault) == 0
    assert deferred_index.graph_full_rebuild_pending(vault) == marker


def test_a_pending_recovery_marker_does_not_bypass_publication_refusal_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_barrier: None,
) -> None:
    """A queued marker remains debt; it is not authority to retry a refusal."""
    vault = _published_vault(tmp_path)
    EpistemicGraphIndex(vault).suspend_reads()
    freshness.mark_external_pending(vault, paths=[vault / "Knowledge Base/Notes/Insights/a.md"])
    assert graph_drain._work_once(vault) == 1
    marker = deferred_index.graph_full_rebuild_pending(vault)
    assert marker is not None

    attempts: list[Path] = []
    real_converge = epistemic_graph.converge_full_graph_marker

    def record_converge(root: Path):
        attempts.append(root)
        return real_converge(root)

    epistemic_graph.note_publication_refusal(vault)
    monkeypatch.setattr(epistemic_graph, "converge_full_graph_marker", record_converge)

    assert graph_drain._work_once(vault) == 0
    assert attempts == []
    assert deferred_index.graph_full_rebuild_pending(vault) == marker
    assert graph_drain._work_once(vault) == 0
    assert deferred_index.graph_full_rebuild_pending(vault) == marker


def test_recovery_eviction_prevents_an_inflight_inbound_build_from_republishing_stale_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_barrier: None,
) -> None:
    """An old full walk may finish for its caller but cannot repopulate the cache."""
    vault = _published_vault(tmp_path)
    source = vault / "Knowledge Base/Notes/Insights/a.md"
    target = "Knowledge Base/Notes/Insights/b.md"
    old_signature = freshness.stat_signature(source)
    real_signature = freshness.stat_signature
    real_build = vault_module._build_inbound_index
    read_old = threading.Event()
    release_old = threading.Event()
    failures: list[BaseException] = []

    def paused_build(root: Path):
        data = real_build(root)
        if threading.current_thread().name == "stale-inbound-builder":
            read_old.set()
            assert release_old.wait(20)
        return data

    def old_reader() -> None:
        try:
            vault_module.find_inbound_wikilinks(vault, target)
        except BaseException as error:  # noqa: BLE001 - thread failure reaches the assertion
            failures.append(error)

    monkeypatch.setattr(vault_module, "_build_inbound_index", paused_build)
    thread = threading.Thread(target=old_reader, name="stale-inbound-builder")
    thread.start()
    try:
        assert read_old.wait(10), "the inbound builder never captured old source bytes"
        source.write_text(_page("A", "A has no links."), encoding="utf-8")
        monkeypatch.setattr(
            freshness,
            "stat_signature",
            lambda path: old_signature if path == source else real_signature(path),
        )
        EpistemicGraphIndex(vault).suspend_reads()
        freshness.mark_external_pending(vault, paths=[source])
        assert graph_drain._work_once(vault) == 1
        assert graph_drain._work_once(vault) == 1
    finally:
        release_old.set()
        thread.join(10)

    assert failures == []
    assert not thread.is_alive()
    assert vault_module.find_inbound_wikilinks(vault, target) == []


def test_recovery_eviction_prevents_an_inflight_recall_build_from_republishing_stale_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_barrier: None,
) -> None:
    """The single-flight leader may return its old snapshot but cannot publish it."""
    vault = _published_vault(tmp_path)
    real_from_entries = vault_module.WikilinkResolver.from_entries
    read_old = threading.Event()
    release_old = threading.Event()
    failures: list[BaseException] = []

    monkeypatch.setenv("EXOMEM_DISABLE_RESOLVER_WARM", "1")
    find_module.evict_resolver_caches(vault)

    def paused_from_entries(root: Path, entries):  # noqa: ANN001, ANN202
        if threading.current_thread().name == "stale-recall-builder":
            read_old.set()
            assert release_old.wait(20)
        return real_from_entries(root, entries)

    def old_reader() -> None:
        try:
            find_module.recall_resolver_snapshot(vault)
        except BaseException as error:  # noqa: BLE001 - thread failure reaches the assertion
            failures.append(error)

    monkeypatch.setattr(vault_module.WikilinkResolver, "from_entries", paused_from_entries)
    thread = threading.Thread(target=old_reader, name="stale-recall-builder")
    thread.start()
    try:
        assert read_old.wait(10), "the recall builder never captured old resolver entries"
        find_module.evict_resolver_caches(vault)
    finally:
        release_old.set()
        thread.join(10)

    assert failures == []
    assert not thread.is_alive()
    assert Path(vault) not in find_module._RECALL_RESOLVER_CACHE


def test_inbound_event_patch_cannot_reinsert_data_after_eviction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event patch publishes only if the cached generation still owns it."""
    vault = _published_vault(tmp_path)
    source = vault / "Knowledge Base/Notes/Insights/a.md"
    target = "Knowledge Base/Notes/Insights/b.md"
    vault_module.find_inbound_wikilinks(vault, target)
    source.write_text(_page("A", "A has no links."), encoding="utf-8")
    changed = source.relative_to(vault).as_posix()
    real_patch = vault_module._InboundIndexData.on_files_changed
    patched = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def paused_patch(self, root: Path, changed_rels, deleted_rels):  # noqa: ANN001
        real_patch(self, root, changed_rels, deleted_rels)
        patched.set()
        assert release.wait(20)

    def patcher() -> None:
        try:
            vault_module.on_inbound_files_changed(vault, [changed], [])
        except BaseException as error:  # noqa: BLE001 - thread failure reaches the assertion
            failures.append(error)

    monkeypatch.setattr(vault_module._InboundIndexData, "on_files_changed", paused_patch)
    thread = threading.Thread(target=patcher, name="stale-inbound-patch")
    thread.start()
    try:
        assert patched.wait(10), "the inbound event patch never captured its data"
        vault_module.evict_inbound_index(vault)
    finally:
        release.set()
        thread.join(10)

    assert failures == []
    assert not thread.is_alive()
    assert str(vault.resolve()) not in vault_module._INBOUND_INDEX


def test_clearing_inbound_cache_revokes_an_inflight_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test clear hook cannot reset an old builder's publication authority."""
    vault = _published_vault(tmp_path)
    target = "Knowledge Base/Notes/Insights/b.md"
    real_build = vault_module._build_inbound_index
    building = threading.Event()
    release = threading.Event()

    def paused_build(root: Path):
        data = real_build(root)
        building.set()
        assert release.wait(20)
        return data

    monkeypatch.setattr(vault_module, "_build_inbound_index", paused_build)
    thread = threading.Thread(
        target=lambda: vault_module.find_inbound_wikilinks(vault, target),
        name="cleared-inbound-builder",
    )
    thread.start()
    try:
        assert building.wait(10), "the inbound builder never started"
        vault_module.clear_inbound_index()
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert str(vault.resolve()) not in vault_module._INBOUND_INDEX


def test_unloading_resolver_caches_revokes_an_inflight_recall_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory release cannot let an old resolver return to the shared cache."""
    vault = _published_vault(tmp_path)
    real_from_entries = vault_module.WikilinkResolver.from_entries
    building = threading.Event()
    release = threading.Event()

    monkeypatch.setenv("EXOMEM_DISABLE_RESOLVER_WARM", "1")
    find_module.evict_resolver_caches(vault)

    def paused_from_entries(root: Path, entries):  # noqa: ANN001, ANN202
        building.set()
        assert release.wait(20)
        return real_from_entries(root, entries)

    monkeypatch.setattr(vault_module.WikilinkResolver, "from_entries", paused_from_entries)
    thread = threading.Thread(
        target=lambda: find_module.recall_resolver_snapshot(vault),
        name="unloaded-recall-builder",
    )
    thread.start()
    try:
        assert building.wait(10), "the recall builder never started"
        find_module.unload_ram_caches()
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert Path(vault) not in find_module._RECALL_RESOLVER_CACHE


# --- Whole-vault repair waits out a write burst instead of spinning into it ---


def _signal_debt_for(seconds: float, every: float = 0.02) -> None:
    """Stand in for a burst of writes: each one enqueues graph debt."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        graph_drain.note_graph_debt()
        time.sleep(every)


def test_write_signals_do_not_restart_whole_vault_repair_mid_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A burst of writes must not buy one whole-vault attempt per write.

    Every write enqueues graph debt and signals the drain. With a whole-vault
    marker standing, each of those passes either loses the canonical boundary
    to the next write (`graph_boundary_busy`) or starts a rebuild the next write
    invalidates, so answering every signal is the spin the live service showed:
    a convergence attempt every few seconds for as long as writes kept landing.
    The no-progress backoff has to hold against the signal, not only against
    the idle poll.
    """
    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(graph_drain, "RETRY_SECONDS", 0.2)
    monkeypatch.setattr(graph_drain, "MAX_RETRY_SECONDS", 0.4)
    monkeypatch.setattr(graph_drain, "WHOLE_VAULT_SETTLE_SECONDS", 0.3, raising=False)
    monkeypatch.setattr(graph_drain, "_pending", lambda _root: True)
    monkeypatch.setattr(graph_drain, "_whole_vault_pending", lambda _root: True, raising=False)
    attempts: list[float] = []

    def no_progress(_root: Path) -> int:
        attempts.append(time.monotonic())
        return 0

    monkeypatch.setattr(graph_drain, "_work_once", no_progress)

    graph_drain.start(tmp_path)
    burst_started = time.monotonic()
    _signal_debt_for(1.5)
    during_burst = [moment for moment in attempts if moment >= burst_started]

    # The ceiling is the backoff's own cadence, not one per write: ~75 signals
    # land in the burst, and at most one attempt per ceiling interval may start.
    assert len(during_burst) <= 5, (
        f"{len(during_burst)} whole-vault attempts during a 1.5 s write burst; "
        "debt signals are bypassing the no-progress backoff"
    )


def test_whole_vault_repair_runs_once_the_burst_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Waiting out a burst is deferral, never abandonment."""
    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(graph_drain, "RETRY_SECONDS", 0.2)
    monkeypatch.setattr(graph_drain, "MAX_RETRY_SECONDS", 0.4)
    monkeypatch.setattr(graph_drain, "WHOLE_VAULT_SETTLE_SECONDS", 0.3, raising=False)
    settled = threading.Event()
    monkeypatch.setattr(graph_drain, "_pending", lambda _root: not settled.is_set())
    monkeypatch.setattr(graph_drain, "_whole_vault_pending", lambda _root: True, raising=False)

    def converge(_root: Path) -> int:
        settled.set()
        return 1

    monkeypatch.setattr(graph_drain, "_work_once", converge)

    graph_drain.start(tmp_path)
    _signal_debt_for(0.5)
    assert _wait_for(settled.is_set, timeout=5.0), "whole-vault repair never ran after the burst"


def test_a_publication_elsewhere_ends_the_drains_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once another owner publishes, the drain's backoff describes nothing.

    The coordinator's rebuild, not the drain, is usually what lands the graph
    after a burst. What is left then is ordinary per-path work, and a drain
    still asleep on a two-minute whole-vault backoff would leave it queued.
    """
    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(graph_drain, "RETRY_SECONDS", 30.0)
    monkeypatch.setattr(graph_drain, "MAX_RETRY_SECONDS", 60.0)
    monkeypatch.setattr(graph_drain, "WHOLE_VAULT_SETTLE_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(graph_drain, "_pending", lambda _root: True)
    monkeypatch.setattr(graph_drain, "_whole_vault_pending", lambda _root: True, raising=False)
    attempts: list[float] = []

    def no_progress(_root: Path) -> int:
        attempts.append(time.monotonic())
        return 0

    monkeypatch.setattr(graph_drain, "_work_once", no_progress)

    graph_drain.start(tmp_path)
    assert _wait_for(lambda: len(attempts) >= 1, timeout=5.0)
    graph_drain.note_graph_debt()
    time.sleep(0.3)
    assert len(attempts) == 1, "a write signal cut a 30 s whole-vault backoff short"

    graph_drain.note_graph_progress()

    assert _wait_for(lambda: len(attempts) >= 2, timeout=5.0), (
        "a publication by another owner did not wake the backed-off drain"
    )


def test_a_standing_marker_is_the_passs_only_whole_vault_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pass, one whole-vault attempt.

    With a whole-vault marker standing, the drain converges it first. When
    that attempt makes no progress, barrier recovery is another whole-vault
    rebuild of the same graph: the live service logged the pair on nearly
    every pass, a `graph_boundary_busy` convergence and then a recovery that
    failed on the same busy boundary. The marker's convergence covers the
    barrier too, so recovery waits for the next pass.
    """
    deferred_index.mark_graph_full_rebuild(tmp_path, generation=1)
    monkeypatch.setattr(graph_drain, "_drain_once", lambda _root: 0)
    monkeypatch.setattr(graph_drain, "_barrier_pending", lambda _root: True)
    recoveries: list[Path] = []

    def recover(root: Path) -> bool:
        recoveries.append(root)
        return False

    monkeypatch.setattr(graph_drain, "_recover_once", recover)

    assert graph_drain._work_once(tmp_path) == 0
    assert recoveries == [], "a pass paid for barrier recovery on top of the marker's attempt"
    assert deferred_index.graph_full_rebuild_pending(tmp_path) == 1


def test_the_quiet_window_is_one_whole_vault_pass_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gap shorter than one pass cannot publish one, so it must not start one.

    The live vault's whole-vault pass took ~45 s idle and minutes under load,
    while agents wrote every 10-60 s. A fixed few-second debounce starts a pass
    in nearly every gap, and the next write throws it away. The window the drain
    waits for is the duration of the last whole-vault pass it has seen.
    """
    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(graph_drain, "RETRY_SECONDS", 0.05)
    monkeypatch.setattr(graph_drain, "MAX_RETRY_SECONDS", 5.0)
    monkeypatch.setattr(graph_drain, "WHOLE_VAULT_SETTLE_SECONDS", 0.1, raising=False)
    monkeypatch.setattr(
        epistemic_graph, "last_whole_vault_pass_seconds", lambda _root: 0.6, raising=False
    )
    monkeypatch.setattr(graph_drain, "_pending", lambda _root: True)
    monkeypatch.setattr(graph_drain, "_whole_vault_pending", lambda _root: True, raising=False)
    attempts: list[float] = []

    def no_progress(_root: Path) -> int:
        attempts.append(time.monotonic())
        return 0

    monkeypatch.setattr(graph_drain, "_work_once", no_progress)

    graph_drain.start(tmp_path)
    # The startup pass runs at once (no burst yet) and makes no progress.
    assert _wait_for(lambda: len(attempts) >= 1, timeout=5.0)
    burst_started = time.monotonic()
    # Writes 0.2 s apart: every gap is longer than the floor, none as long as a pass.
    _signal_debt_for(1.5, every=0.2)
    burst_ended = time.monotonic()
    during_burst = [moment for moment in attempts if burst_started <= moment < burst_ended]
    assert during_burst == [], (
        f"{len(during_burst)} whole-vault attempt(s) started in gaps shorter than one pass"
    )
    assert _wait_for(lambda: any(moment >= burst_ended for moment in attempts), timeout=5.0), (
        "the pass never ran once the writes stopped"
    )


def _held_whole_vault_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    last_pass: float | None,
    floor: float,
    ceiling: float,
    gap: float,
    stream: float,
) -> tuple[float, list[float]]:
    """Run the real schedule against a stream of write signals; time each attempt.

    Whole-vault work stands throughout and no attempt makes progress, so every
    attempt the drain starts is visible and none ends the hold early. Returns the
    stream's start and the start time of every attempt.
    """
    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(graph_drain, "RETRY_SECONDS", 0.05)
    monkeypatch.setattr(graph_drain, "MAX_RETRY_SECONDS", ceiling)
    monkeypatch.setattr(graph_drain, "WHOLE_VAULT_SETTLE_SECONDS", floor)
    monkeypatch.setattr(epistemic_graph, "last_whole_vault_pass_seconds", lambda _root: last_pass)
    monkeypatch.setattr(graph_drain, "_pending", lambda _root: True)
    monkeypatch.setattr(graph_drain, "_whole_vault_pending", lambda _root: True)
    attempts: list[float] = []

    def no_progress(_root: Path) -> int:
        attempts.append(time.monotonic())
        return 0

    monkeypatch.setattr(graph_drain, "_work_once", no_progress)

    graph_drain.start(tmp_path)
    # The startup pass runs at once (no burst yet) and makes no progress.
    assert _wait_for(lambda: len(attempts) >= 1, timeout=5.0)
    started = time.monotonic()
    _signal_debt_for(stream, every=gap)
    return started, attempts


def test_a_held_whole_vault_attempt_runs_within_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quiet window is a preference, not a precondition.

    A stream whose gaps never reach one pass would otherwise hold whole-vault
    repair for as long as it lasts, and main, which starts a pass a debounce
    after each write, sometimes publishes in such a stream. An attempt held for
    a quiet window runs anyway once the first debt signal it was held for is
    `MAX_RETRY_SECONDS` old, and later attempts are held no longer than that.
    """
    started, attempts = _held_whole_vault_attempts(
        tmp_path, monkeypatch, last_pass=0.3, floor=0.1, ceiling=0.6, gap=0.1, stream=2.5
    )
    ended = time.monotonic()
    during = [moment for moment in attempts if started <= moment < ended]

    assert during, "a whole-vault attempt stayed held for the whole stream"
    assert during[0] - started <= 0.6 + 0.35, (
        f"the first held attempt ran {during[0] - started:.2f}s into the stream, "
        "past its ceiling"
    )
    gaps = [later - earlier for earlier, later in zip(during, during[1:], strict=False)]
    assert all(gap_ <= 0.6 + 0.35 for gap_ in gaps), f"an attempt outlived the ceiling: {gaps}"
    assert len(during) <= 6, f"{len(during)} attempts in 2.5 s: the ceiling became a spin"


def test_the_five_second_floor_applies_only_until_a_pass_is_timed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed pass shorter than the floor sets the window on its own.

    Gaps longer than one measured pass can publish one, so a floor that is
    longer than the pass must not turn them away.
    """
    started, attempts = _held_whole_vault_attempts(
        tmp_path, monkeypatch, last_pass=0.2, floor=0.6, ceiling=30.0, gap=0.4, stream=2.0
    )
    ended = time.monotonic()
    during = [moment for moment in attempts if started <= moment < ended]

    assert during, "a floor longer than the measured pass held every gap in the stream"


def test_an_unavailable_graph_alone_is_per_path_work_and_is_not_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary per-path fence must keep draining through a write burst.

    A deferred write withdraws availability and queues its own paths; the drain
    restores availability by repairing them. That is not a whole-vault pass, so
    the quiet window does not apply to it: holding it would leave the graph
    unreadable for the whole burst when proportional repair could have kept up.
    """
    monkeypatch.setattr(graph_drain, "DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(graph_drain, "WHOLE_VAULT_SETTLE_SECONDS", 60.0, raising=False)
    monkeypatch.setattr(graph_drain, "_queue_pending", lambda _root: True)
    monkeypatch.setattr(graph_drain, "_availability_pending", lambda _root: True)
    monkeypatch.setattr(deferred_index, "graph_full_rebuild_pending", lambda _root: None)
    drained: list[float] = []

    def drain(_root: Path, *, limit: int | None = None) -> int:
        drained.append(time.monotonic())
        return 1

    monkeypatch.setattr(index_sync, "drain_graph_work", drain)
    monkeypatch.setattr(graph_drain, "_republish_once", lambda _root: False)
    monkeypatch.setattr(graph_drain, "_request_full_rebuild", lambda _root: False)

    graph_drain.start(tmp_path)
    burst_started = time.monotonic()
    _signal_debt_for(0.5)

    assert any(moment >= burst_started for moment in drained), (
        "per-path repair was held behind the whole-vault quiet window"
    )
