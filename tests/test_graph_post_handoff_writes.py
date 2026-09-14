"""A replacement worker's first writes must stay incremental (phase 1 of
`seamless-managed-worker-handoff`).

The production shape: a managed upgrade replaces the worker child, the new
process starts with an empty `file_watcher._SELF_UPSERTS` table, and every
inotify event it cannot attribute to itself -- the old worker's last writes, the
offline migrator, ordinary vault traffic -- re-arms `freshness.external_pending`.
`EpistemicGraphIndex._open_read_snapshot` consulted that flag *before* the
predecessor probe, so `_graph_sync_predecessor_state` answered
`graph_sync_predecessor_unreadable`, and `upsert_after_write` collapsed that
into the same branch as a real lineage gap: a whole-vault rebuild, once per
write. Measured on a 446-file vault: 0.17 s per write clean, 3.5-4.0 s per write
with the flag armed, every one of them a rebuild.

The reproduction is `lane-debug/scripts/exp5.py external`; this is that shape as
a regression test. The assertions are the three facts that separate the repair
from a faster rebuild: no whole-vault pass runs at all, the acknowledgement
stays inside the incremental bound, and the durable repair queue drains to zero
rather than growing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from exomem import (
    deferred_index,
    epistemic_graph,
    file_watcher,
    freshness,
    graph_sync,
    index_sync,
    mode,
    mutation_lock,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

GENERATED = "Knowledge Base/Notes/Generated"
#: Large enough that a whole-vault pass is an order of magnitude more work than
#: one incremental write, small enough to seed inside a test.
NOTE_COUNT = 200
WRITE_COUNT = 10
#: Small enough that the watcher's own repair of each unattributed edit lands
#: inside the settle window, which is what makes a live-watcher test finish.
LIVE_NOTE_COUNT = 60
#: The reproduction's own count for the live-watcher shape. Every write there
#: waits for the watcher to finish with an edit, so a ten-write run is minutes
#: on a loaded host and dies on the suite's per-test timeout; the deterministic
#: test below keeps the full count, with no watcher to wait for.
LIVE_WRITE_COUNT = 6
#: Measured on this fixture (246 markdown files): an incremental governed write
#: acknowledges in 0.36-0.76 s, and 1.08 s worst observed on a host running other
#: suites at the same time. A whole-vault rebuild of the same fixture costs
#: 1.4-4.7 s. The wall-clock bound is deliberately loose, because the assertion
#: that carries the weight is the rebuild counter; this one exists so a repair
#: that stopped rebuilding but started doing something else expensive still fails.
ACK_BOUND_SECONDS = 2.0
#: The typical write, held to the measured range rather than the safety margin.
ACK_MEDIAN_BOUND_SECONDS = 1.0
#: The live-watcher shape has a second writer: the watcher's own repair of the
#: unattributed edits contends for the same mutation boundary, and a write that
#: lands on it waits for it. Measured there: 0.21-0.47 s typical, 3.1 s worst
#: when a repair was in flight. The median is what separates this from the
#: defect, where every write waited out a whole-vault rebuild (3.4-7.9 s).
LIVE_ACK_BOUND_SECONDS = 5.0


def _note(index: int, links: list[str]) -> str:
    body = [
        "---",
        "type: pattern",
        "status: active",
        "created: 2026-01-12",
        "updated: 2026-06-10",
        "sources: []",
        "pattern_type: architectural",
        f"tags: [generated, batch{index % 7}]",
        "---",
        "",
        f"# Generated note {index}",
        "",
        "## Problem",
        f"Synthetic body {index} for post-handoff dispatch measurement. " * 4,
        "",
        "## Connections",
    ]
    body.extend(f"- [[{GENERATED}/{link}]]" for link in links)
    return "\n".join(body) + "\n"


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )


def _build_vault(vault: Path, note_count: int) -> Iterator[Path]:
    generated = vault / GENERATED
    generated.mkdir(parents=True, exist_ok=True)
    names = [f"generated-note-{i:04d}" for i in range(note_count)]
    for i, name in enumerate(names):
        links = [names[(i + offset) % note_count] for offset in (1, 7, 23)]
        (generated / f"{name}.md").write_text(_note(i, links), encoding="utf-8")
    _seed_live_freshness(vault)
    EpistemicGraphIndex(vault).rebuild_all()
    epistemic_graph.clear_publication_memos()
    yield vault
    graph_sync.drain_active_rebuilds(timeout=30.0)
    epistemic_graph.clear_publication_memos()


@pytest.fixture
def handoff_vault(vault: Path) -> Iterator[Path]:
    """A seeded vault with a published graph, as a replacement worker inherits it."""
    yield from _build_vault(vault, NOTE_COUNT)


@pytest.fixture
def live_handoff_vault(vault: Path) -> Iterator[Path]:
    """The same vault, sized for a test that waits on a real watcher.

    A running watcher repairs each unattributed edit itself, and that repair
    costs what a whole-vault pass costs; at 246 files a six-write run spends
    minutes waiting for it and dies on the suite's per-test timeout. The live
    test's assertions are about which reasons appear and what the write costs,
    neither of which needs a large corpus -- the tests that do need one (the
    rebuild counts) have no second writer and keep the full fixture.
    """
    yield from _build_vault(vault, LIVE_NOTE_COUNT)


class _RebuildSpy:
    """Count whole-vault graph passes.

    `_rebuild_all_off_boundary` is the single funnel: `rebuild_all`, the
    registered rebuild captured by `_registered_or_failure`, the background
    warming rebuild and the post-release fallback all bottom out here.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.passes: list[float] = []
        real = EpistemicGraphIndex._rebuild_all_off_boundary

        def counted(inner_self: EpistemicGraphIndex, **kwargs: object) -> dict[str, int]:
            started = time.monotonic()
            try:
                return real(inner_self, **kwargs)
            finally:
                self.passes.append(time.monotonic() - started)

        monkeypatch.setattr(
            EpistemicGraphIndex, "_rebuild_all_off_boundary", counted, raising=True
        )

    @property
    def count(self) -> int:
        return len(self.passes)


def _external_edit(watcher: file_watcher.FileWatcher, path: Path, marker: str) -> None:
    """One edit the process did not author, observed exactly as watchdog delivers it."""
    path.write_text(path.read_text(encoding="utf-8") + f"\n- {marker}\n", encoding="utf-8")
    watcher._record(path, deleted=False)


def _governed_write(root: Path, path: Path, marker: str) -> float:
    text = path.read_text(encoding="utf-8") + f"\n- {marker}\n"
    started = time.monotonic()
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(path=path, content=text)], vault_root=root
    )
    return time.monotonic() - started


def _watcher_settle_budget(watcher: file_watcher.FileWatcher) -> float:
    """What one cycle may spend: the debounce window plus one lock acquisition.

    Taken from the watcher rather than chosen. A cycle is the debounce window
    plus the drain's compare-and-ack, and the drain spends the mutation
    coordinator's own timeout waiting for the lock. It deliberately excludes
    `GRAPH_WITHDRAWAL_RETRY_SECONDS`: that budget is only consulted once an
    attempt has already been refused, and `file_watcher` documents a boundary
    that stays busy that long as sustained contention -- a different condition,
    which it reports separately. Measured cycles on this fixture are 1.96-3.08 s,
    so this is both the declared bound for the step and about twice its cost.

    Applied per edit, not as one pot for the loop, so a slow first cycle cannot
    spend the whole allowance and hand every cycle after it unsettled status.
    """
    return watcher._debounce_seconds() + mutation_lock._DEFAULT_TIMEOUT_SECONDS


def _watcher_loop_ceiling(watcher: file_watcher.FileWatcher) -> float:
    """What the whole loop may spend, so no host can run it past the suite's timeout.

    The watcher's full declared worst case for the step that stalls when SQLite
    is contended ("sidecar WAL pragmas failed (database is locked)" in the shard
    that flaked): `GRAPH_WITHDRAWAL_RETRY_SECONDS` plus one coordinator timeout,
    which `file_watcher` states as about 20 s at today's defaults. The loop as a
    whole may spend one exhaustion budget; no single edit may. Six measured
    cycles cost 13.7 s of it, so this is headroom rather than the common path,
    and the fixed five-second window it replaces was shorter than the bound the
    code itself declares -- which is why a contended shard saw cycles that were
    legitimately still in flight and called them missing.
    """
    return (
        watcher._debounce_seconds()
        + file_watcher.GRAPH_WITHDRAWAL_RETRY_SECONDS
        + mutation_lock._DEFAULT_TIMEOUT_SECONDS
    )


def _fence_lines(caplog: pytest.LogCaptureFixture) -> int:
    """Deferral lines emitted so far -- NOT a count of fenced writes.

    One fenced write can emit this line more than once: the write's own dispatch
    and the fan-out behind it each defer on the same mark, and how many of those
    run depends on what else the watcher has in flight. So the ratio is not
    fixed and a line count must never be compared against a write count. The
    loop below reads this as a delta across one write and counts writes.
    """
    return sum(
        "reason=external_event_covers_these_paths" in record.getMessage()
        for record in caplog.records
    )


def _observe_and_settle(
    root: Path, watcher: file_watcher.FileWatcher, *, deadline: float
) -> tuple[bool, bool]:
    """Wait for the observer to deliver an unattributed edit and the watcher to ack it.

    Returns `(observed, settled)`. The reproduction spaces its governed writes
    the same way (`exp5.py` sleeps 0.6 s after each external edit). Settling is
    not a workaround: on a defective tree the mark never clears, because the
    drain's fan-out finds the graph unavailable and re-arms it -- so "the mark
    cleared" is itself part of what this test is asserting, and a write that
    lands mid-drain would measure the watcher's own lineage races instead of the
    fence.

    The two halves are reported separately because they fail for different
    reasons: an edit that was never *observed* means the watcher is not running
    the cycle at all, which is the defect this test exists for; an observed edit
    that has not settled inside the budget means the host is contended, which is
    not. So observation keeps a floor of two debounce windows even when the
    loop's ceiling is reached -- an exhausted budget must not turn "never waited"
    into "never observed" -- while settling gets only what the deadline allows.
    """
    observed = False
    delivery_floor = time.monotonic() + watcher._debounce_seconds() * 2
    while True:
        now = time.monotonic()
        if now >= (max(deadline, delivery_floor) if not observed else deadline):
            break
        if freshness.external_pending(root):
            observed = True
        elif observed and EpistemicGraphIndex(root).available():
            # The mark is retired *and* the fan-out republished the graph: the
            # watcher is done with this edit. Writing before that measures a
            # lineage race between two writers of the same vault, which is a
            # different question from the fence this test is about.
            return (True, True)
        time.sleep(0.02)
    return (observed, False)


#: Fewest acknowledgements a median may be taken over. A median of one sample
#: is that sample, so applying this helper to a single write turns a latency
#: BUDGET into a per-request timing assertion -- exactly the premise class that
#: has produced flakes here all along, and it produced one more: the first
#: write after a promotion at 1.14 s on a loaded CI runner, failing a 1.0 s
#: median bound it was never meant to be measured against. The guarantee this
#: helper encodes is about a SERIES of writes staying incremental; a single
#: write that legitimately pays first-request costs belongs under a structural
#: assertion, or under a budget wide enough to be a budget.
_MIN_LATENCY_SAMPLES = 3


def _assert_incremental_latency(
    acknowledgements: list[float], *, bound: float = ACK_BOUND_SECONDS
) -> None:
    assert len(acknowledgements) >= _MIN_LATENCY_SAMPLES, (
        f"this asserts the shape of a series, not one request: "
        f"{len(acknowledgements)} sample(s) given, {_MIN_LATENCY_SAMPLES} needed"
    )
    rendered = [round(seconds, 2) for seconds in acknowledgements]
    slowest = max(acknowledgements)
    assert slowest < bound, (
        f"slowest acknowledgement {slowest:.2f}s exceeds the incremental bound "
        f"{bound}s: {rendered}"
    )
    median = sorted(acknowledgements)[len(acknowledgements) // 2]
    assert median < ACK_MEDIAN_BOUND_SECONDS, (
        f"median acknowledgement {median:.2f}s is outside the measured incremental "
        f"range: {rendered}"
    )


#: Every dispatch outcome that claims durable per-path coverage, taken from the
#: module that declares it rather than hand-listed here. A hand list drifts: it
#: omitted `graph_repair_cold_resolver`, which is one of the five, and a run
#: whose resolver went cold at dispatch then failed on an outcome that is
#: exactly as covered as the three the list happened to name. Deriving it keeps
#: the test asserting the contract -- "this write's repair is durably queued" --
#: instead of asserting which doors happened to fire the day it was written.
_QUEUED_COVERAGE_CODES = frozenset(index_sync._GRAPH_COVERAGE_CODES) | {
    # Not a coverage claim: nothing was deferred, so there is nothing to cover.
    "incremental_completed",
}


_REBUILD_LINE = "graph dispatch registered a whole-vault rebuild"
_UNCOVERED_LINE = "graph lineage gap is not covered by durable receipts"


def _messages_from_this_thread(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    """Records matching `needle` that the calling thread itself emitted.

    `caplog` captures every thread in the process, and these tests run a real
    watcher. A watcher dispatch is a standalone library caller, so
    `_caller_can_carry_pending` is False for it and a deferral it makes must
    converge before it returns -- it registers a rebuild of its own and says
    `reason=incremental_refresh_queued_for_a_caller_that_must_converge`. That
    is by design and is not what any oracle here is about; it was also making
    the fence test fail about one full-file run in three, purely on when the
    watcher's loop landed inside a per-write window.

    The governed write dispatches synchronously on the thread that called it,
    so the emitting thread is exact attribution rather than a heuristic.
    """
    current = threading.current_thread().name
    return [
        record.getMessage()
        for record in caplog.records
        if record.threadName == current and needle in record.getMessage()
    ]


def _rebuild_reasons_this_write(root: Path, caplog: pytest.LogCaptureFixture) -> list[str]:
    """This write's own rebuild reasons, each proven to be a genuinely uncovered gap.

    A rebuild is admitted here only as the best-effort enqueue the canonical
    batch is allowed to lose ("a lost enqueue costs a reconcile; a refused
    write costs the user their edit"). `graph_sync_predecessor_mismatch` IS
    still admitted -- it is the only reason a lost enqueue can produce -- but
    never unconditionally, which is the door task 1.13 closed and which an open
    allowlist would reopen for a regression producing one mismatch rebuild in
    six. So when the probe declines, read back the durable record it declined
    on and require the generations it named to have no debt recorded.

    Read after the write rather than during it, which is sound because absence
    is monotone across that window: the generations named are strictly older
    than this checkpoint, and nothing records debt for a generation older than
    the one it is owed for.
    """
    reasons = [
        message.split("reason=", 1)[1].split(" ", 1)[0]
        for message in _messages_from_this_thread(caplog, _REBUILD_LINE)
    ]
    if "graph_sync_predecessor_mismatch" not in reasons:
        return reasons
    declined = _messages_from_this_thread(caplog, _UNCOVERED_LINE)
    assert declined, (
        "a write registered a whole-vault rebuild for a mismatch without the "
        f"probe naming the generations it could not prove covered: {reasons}"
    )
    for message in declined:
        missing = [
            int(token)
            for token in message.split("missing=", 1)[1].split(" ", 1)[0].split(",")
            if token
        ]
        assert missing, f"the probe declined but named no missing generation: {message}"
        recorded, _available = deferred_index.graph_debt_generations_recorded(
            root, missing
        )
        assert not (set(missing) & recorded), (
            f"generations {sorted(set(missing) & recorded)} have their debt "
            "durably recorded, so the gap was covered and the rebuild is the "
            f"regression task 1.13 closed, not a lost enqueue: {message}"
        )
    return reasons


def _drain_repair_queue(root: Path, watcher: file_watcher.FileWatcher) -> int:
    """Publish the observed external events, then drain the durable graph queue."""
    watcher._flush()
    for _ in range(12):
        if not deferred_index.list_graph_paths(root):
            return 0
        index_sync.drain_graph_work(root, limit=64)
    return len(deferred_index.list_graph_paths(root))


def _drain_graph_queue(root: Path) -> int:
    """Drain the durable graph queue without a watcher to publish events first."""
    for _ in range(12):
        if not deferred_index.list_graph_paths(root):
            return 0
        index_sync.drain_graph_work(root, limit=64)
    return len(deferred_index.list_graph_paths(root))


@pytest.fixture
def live_watcher(live_handoff_vault: Path) -> Iterator[file_watcher.FileWatcher]:
    """A watcher running for real: observer, dispatch loop and fan-out recovery.

    Driving `_record` on an unstarted watcher tests the debounce logic and
    nothing else. The defect this suite exists for is produced *by the running
    watcher*: its fan-out recovery raises an external mark between writes, and a
    mark raised there with no scope fences the next governed write into a
    whole-vault rebuild, whose deferral queues more paths, which leaves the next
    fan-out incomplete. Only a started watcher can close that loop, so the
    regression test runs one.
    """
    watcher = file_watcher.FileWatcher(live_handoff_vault, debounce_seconds=0.2)
    started = watcher.start()
    assert started, "the reproduction requires a running watcher"
    # The seed pass publishes the registry before events are dispatched.
    watcher._seed_complete.wait(30)
    try:
        yield watcher
    finally:
        watcher.stop()


def test_writes_after_a_worker_replacement_stay_incremental(
    live_handoff_vault: Path,
    live_watcher: file_watcher.FileWatcher,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`exp5.py external`, as a test: six writes, each behind one unattributed edit.

    The oracle is the reproduction's, and it is about the *write path*: every
    governed write's own dispatch schedules no whole-vault rebuild, every write
    the fence catches reports durable queued coverage, every acknowledgement
    stays inside the incremental bound (3.4-7.9 s per write on the defective
    tree, because each one waited out a whole-vault rebuild it had been fenced
    into), and the repair queue drains to zero.

    HOW MANY writes were fenced is deliberately not asserted. It used to be,
    as `fenced_writes <= 1 + unsettled`, and that bound is a statement about
    the WATCHER's catch-up: a write dispatched before the watcher has retired
    its mark is fenced by design, so the bound measures how much of its cycle a
    second, independent actor had finished. On a contended host it fires while
    the write path is perfectly healthy -- observed at 3 of 6 fenced with
    acknowledgements of 0.29-0.52 s, and 15/15 green on the same commit once
    the host was quiet. A premise a third party can satisfy or break on timing
    is not a premise.

    What replaced it is the guarantee itself, from this test's own evidence.
    Being fenced is harmless *because* the fence queues the paths it covers
    (task 1.12): before that it returned a bare deferral, dispatch read it as a
    missing rebuild, and "every write fenced" and "the loop" were the same
    statement. So the assertions are that no deferral escaped without queue
    coverage, that every outcome the governed writes reported is one of the
    declared coverage codes, and that their own dispatches registered no
    whole-vault pass at all. The fenced count is still computed and reported in
    the failure messages, as the diagnostic it always was.

    Counting rebuilds is possible here now and was not when this was written.
    A running watcher repairs the unattributed edits itself and legitimately
    bails to a full pass on a fixture this size; process-wide counting would
    measure that rather than what the governed writes cost. Attributing by
    emitting thread separates the two -- the governed write dispatches
    synchronously on the calling thread -- so the rebuild count can be asserted
    on the writes this test is actually about.
    """
    root = live_handoff_vault
    generated = root / GENERATED
    caplog.set_level("INFO", logger="exomem.epistemic_graph")

    outcomes: list[epistemic_graph.GraphDispatchResult] = []
    real_dispatch = epistemic_graph.upsert_after_write
    dispatching_thread = threading.current_thread().name

    def recorded(vault_root: Path, paths: list[Path], **kwargs: object):
        result = real_dispatch(vault_root, paths, **kwargs)
        if threading.current_thread().name == dispatching_thread:
            # The patch is module-global, so the running watcher's own repairs
            # arrive here too. They are a different caller with a different
            # contract; this oracle is about what the GOVERNED write chose.
            outcomes.append(result)
        return result

    monkeypatch.setattr(epistemic_graph, "upsert_after_write", recorded, raising=True)

    acknowledgements: list[float] = []
    rebuild_reasons: list[str] = []
    observed_marks = 0
    unsettled = 0
    fenced_writes = 0
    # Both deadlines come from the watcher: one cycle's own wait per edit, under
    # a ceiling for the loop so no host can run this past the suite's timeout.
    settle_budget = _watcher_settle_budget(live_watcher)
    loop_ceiling = time.monotonic() + _watcher_loop_ceiling(live_watcher)
    for i in range(LIVE_WRITE_COUNT):
        # Disjoint from the written notes, and inside this fixture's range.
        external = generated / f"generated-note-{LIVE_NOTE_COUNT // 2 + i:04d}.md"
        external.write_text(
            external.read_text(encoding="utf-8") + f"\n- external editor touch {i}\n",
            encoding="utf-8",
        )
        # Let the observer deliver the event and the watcher finish with it, the
        # way the reproduction spaces its writes.
        observed, settled = _observe_and_settle(
            root,
            live_watcher,
            deadline=min(loop_ceiling, time.monotonic() + settle_budget),
        )
        observed_marks += int(observed)
        unsettled += int(not settled)
        deferrals_before = _fence_lines(caplog)
        rebuilds_before = len(_messages_from_this_thread(caplog, _REBUILD_LINE))
        acknowledgements.append(
            _governed_write(root, generated / f"generated-note-{i:04d}.md", f"governed {i}")
        )
        fenced_writes += int(_fence_lines(caplog) > deferrals_before)
        rebuild_reasons.extend(
            message.split("reason=", 1)[1].split(" ", 1)[0]
            for message in _messages_from_this_thread(caplog, _REBUILD_LINE)[rebuilds_before:]
        )

    rendered = [round(seconds, 2) for seconds in acknowledgements]
    assert observed_marks == LIVE_WRITE_COUNT, (
        "the running watcher must observe every unattributed edit; only "
        f"{observed_marks} of {LIVE_WRITE_COUNT} were observed at all, which "
        "means the observer or the dispatch loop is not running"
    )
    assert "graph_sync_predecessor_unreadable" not in caplog.text, (
        "a governed write could not read a sidecar whose lineage was intact, which "
        f"is the defect itself: {rendered}"
    )
    # How many writes were fenced is a diagnostic, not a bound: see the
    # docstring. It is carried into every message below so a failure still says
    # what the watcher was doing at the time.
    context = (
        f"{fenced_writes} of {LIVE_WRITE_COUNT} writes fenced, "
        f"{unsettled} watcher cycle(s) in flight, acknowledgements {rendered}"
    )
    # What makes being fenced harmless, and the thing that has to stay true
    # however many writes were fenced: a path-scoped fence queues its own paths
    # (task 1.12). Before that it returned a bare deferral, dispatch read it as
    # a missing rebuild, and the loop followed.
    assert "incremental_refresh_deferred_without_queue_coverage" not in caplog.text, (
        f"a governed write deferred without queue coverage: {context}"
    )
    codes = [result.code for result in outcomes]
    assert codes, "no governed dispatch was recorded, so this proves nothing"
    assert set(codes) <= _QUEUED_COVERAGE_CODES, (
        f"a governed write reported an outcome that claims no durable coverage: "
        f"{codes}; {context}"
    )
    assert rebuild_reasons == [], (
        "the governed writes' own dispatches scheduled whole-vault rebuilds "
        f"for {rebuild_reasons}; {context}. A running watcher may legitimately "
        "run one of its own, which is why this counts only the thread that "
        "dispatched the write."
    )
    _assert_incremental_latency(acknowledgements, bound=LIVE_ACK_BOUND_SECONDS)
    assert _drain_repair_queue(root, live_watcher) == 0, (
        f"the graph repair queue never drained; {context}"
    )


def test_a_governed_write_attributes_time_to_the_graph_incremental_pass(
    handoff_vault: Path,
) -> None:
    """`graph.refresh_paths` has to be on `refresh_paths`, not merely in the file.

    Both existing pins grep the module for the decorator literal, so a decorator
    that had drifted onto the function inserted beneath it still passed them
    while the span measured the wrong thing -- an enqueue on the deferral path,
    and nothing at all on the ordinary one. A span is a measurement, so the pin
    has to be a measurement.
    """
    from exomem import call_spans

    root = handoff_vault
    call_spans.reset()
    handle = call_spans.MCP_CALL_TOKEN.set("graph-span-token")
    try:
        _governed_write(root, root / GENERATED / "generated-note-0011.md", "span probe")
        spans = {row["name"]: row for row in call_spans.pop_call_spans("graph-span-token")}
    finally:
        call_spans.MCP_CALL_TOKEN.reset(handle)
        call_spans.reset()

    assert "graph.refresh_paths" in spans, (
        f"a governed write recorded no graph pass at all: {sorted(spans)}"
    )
    assert spans["graph.refresh_paths"]["ms"] > 0, (
        f"the graph pass measured no time: {spans['graph.refresh_paths']}"
    )


def _strand_the_marker(root: Path) -> None:
    """Withdraw availability and leave no barrier behind.

    The state `_availability_pending` exists for and documents: a rebuild that
    exhausts its publication attempts leaves the graph unreadable with no
    barrier at all. Constructed here rather than raced for, because the drain's
    `elif` branch is only reachable without a barrier -- with one standing, the
    recovery arm takes the pass.
    """
    import sqlite3

    index = EpistemicGraphIndex(root)
    index.withdraw_availability()
    connection = sqlite3.connect(index.path)
    try:
        with connection:
            connection.execute("DELETE FROM graph_meta WHERE key = 'read_barrier'")
    finally:
        connection.close()


def test_the_drain_daemon_republishes_a_stranded_marker_before_asking_for_a_rebuild(
    handoff_vault: Path,
) -> None:
    """The daemon's own route to the stranded state, which the drain never reaches.

    `_work_once` only drains when the durable queue owes work, so a withdrawn
    marker with an empty queue skips the drain -- and the republication inside
    it -- entirely, and falls to the branch that requests a whole-vault rebuild.
    Spending a full pass on a sidecar whose rows may already match disk is the
    expensive answer to a question the cheap proof can settle.
    """
    from exomem import graph_drain

    root = handoff_vault
    assert EpistemicGraphIndex(root).available() is True
    _strand_the_marker(root)

    assert graph_drain._queue_pending(root) is False
    assert graph_drain._barrier_pending(root) is False
    assert graph_drain._availability_pending(root) is True

    processed = graph_drain._work_once(root)

    assert EpistemicGraphIndex(root).available() is True, (
        "the daemon left the marker stranded and answered with a rebuild request"
    )
    assert deferred_index.graph_full_rebuild_pending(root) is None, (
        "a whole-vault rebuild was requested for a graph whose rows match disk"
    )
    assert processed == 1


def test_a_refused_availability_proof_backs_off_instead_of_running_every_tick(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof is O(corpus); the release gate drains twice a second.

    Measured by the reviewer at 599-673 ms a tick on 400 pages, paid on every
    one of six ticks. A refusal is the ordinary case while repair is genuinely
    owed, so it has to cost one proof per backoff window rather than one per
    drain.
    """
    root = handoff_vault
    epistemic_graph.reset_republish_backoff()
    _strand_the_marker(root)
    # Residue: bytes on disk the sidecar has not indexed, so the proof refuses.
    stale = root / GENERATED / "generated-note-0009.md"
    stale.write_text(stale.read_text(encoding="utf-8") + "\n- unindexed\n", encoding="utf-8")

    proofs: list[int] = []
    real_proof = EpistemicGraphIndex._snapshot_sources_match_disk

    def counted(inner_self: EpistemicGraphIndex, conn: object, **kwargs: object):
        proofs.append(1)
        return real_proof(inner_self, conn, **kwargs)

    monkeypatch.setattr(
        EpistemicGraphIndex, "_snapshot_sources_match_disk", counted, raising=True
    )

    for _ in range(6):
        index_sync.drain_graph_work(root, limit=64)

    assert len(proofs) == 1, (
        f"the refused proof ran {len(proofs)} times across six drain ticks; the "
        "backoff exists so a residue that cannot clear costs one proof a window"
    )
    assert EpistemicGraphIndex(root).available() is False


def test_the_availability_proof_defers_to_the_watcher_while_a_mark_is_unrepaired(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrepaired external mark is the watcher's repair, and it republishes."""
    root = handoff_vault
    _strand_the_marker(root)
    freshness.mark_external_pending(root, paths=(root / GENERATED / "generated-note-0008.md",))

    proofs: list[int] = []
    real_proof = EpistemicGraphIndex._snapshot_sources_match_disk

    def counted(inner_self: EpistemicGraphIndex, conn: object, **kwargs: object):
        proofs.append(1)
        return real_proof(inner_self, conn, **kwargs)

    monkeypatch.setattr(
        EpistemicGraphIndex, "_snapshot_sources_match_disk", counted, raising=True
    )

    for _ in range(4):
        index_sync.drain_graph_work(root, limit=64)

    assert proofs == [], (
        "the corpus proof ran while an external mark was unrepaired; the watcher "
        "owns that repair and republishes through its own route"
    )


def test_the_graph_converges_readable_under_a_concurrent_writer(
    handoff_vault: Path,
) -> None:
    """`scripts/graph_concurrent_convergence.py` in miniature, for its drift check.

    The harness failed the 0.84.1 evidence run with `graph_state=current
    queue_remaining=0 drift=1` -- everything converged except the availability
    marker, and `graph_drift` reads through a public snapshot the marker gates,
    so an intact sidecar audited as missing or drifted. It reproduced once in
    eleven local harness runs, which is why the oracle here is the state rather
    than the harness: a writer running against periodic drains, then convergence,
    then the three properties that must hold together.

    A pending majority is NOT part of the oracle. The green baseline
    (`f1212e6f`) reports more pending than completed too: deferral to the queue
    is the design, and convergence is what makes it honest.

    This shape did not reproduce the race in three runs with the republication
    disabled, so it is a shape check, not the guard:
    `test_a_withdrawn_marker_is_republished_when_the_queue_drains_to_zero` is
    the deterministic one and fails every time without it.
    """
    root = handoff_vault
    generated = root / GENERATED
    stop = threading.Event()
    failures: list[BaseException] = []

    def write_forever() -> None:
        index = 0
        while not stop.is_set():
            try:
                _governed_write(
                    root, generated / f"generated-note-{index % 20:04d}.md", f"concurrent {index}"
                )
            except BaseException as error:  # noqa: BLE001 - reported, never swallowed
                failures.append(error)
                return
            index += 1

    writer = threading.Thread(target=write_forever, daemon=True)
    writer.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            index_sync.drain_graph_work(root, limit=64)
            time.sleep(0.3)
    finally:
        stop.set()
        writer.join(30)

    assert not failures, f"the concurrent writer failed: {failures[0]!r}"
    assert _drain_graph_queue(root) == 0, "the graph repair queue never converged"

    assert EpistemicGraphIndex(root).available() is True, (
        "the graph converged with an empty queue and stayed fenced, so every "
        "read refuses and the drift audit reports a sidecar that is intact"
    )
    assert epistemic_graph.graph_drift(root) == [], (
        "a converged graph whose rows match disk must audit as clean"
    )


def test_a_withdrawn_marker_is_republished_when_the_queue_drains_to_zero(
    handoff_vault: Path,
) -> None:
    """A withdrawal with nothing queued against it has nobody left to earn it back.

    Every route that withdraws pairs the withdrawal with durable repair, and the
    drain republishes when it repairs those paths. What nothing covered is the
    last withdrawal: a write that defers *after* the final drain leaves the
    marker withdrawn with an empty queue, and no later drain has work to
    republish it with. `graph_drift` reads through a public snapshot, which the
    marker gates, so it then reports the sidecar as missing or drifted -- which
    is what the convergence harness saw on the 0.84.1 evidence run:
    `graph_state=current queue_remaining=0 drift=1`.
    """
    root = handoff_vault
    index = EpistemicGraphIndex(root)
    assert index.available() is True
    assert epistemic_graph.graph_drift(root) == []

    index._mark_unavailable()
    assert EpistemicGraphIndex(root).available() is False
    assert deferred_index.list_graph_paths(root) == [], (
        "the stuck state is a withdrawal with NOTHING queued; a queued path "
        "would be repaired by the ordinary drain and prove nothing here"
    )

    index_sync.drain_graph_work(root, limit=64)

    assert EpistemicGraphIndex(root).available() is True, (
        "the drain left the graph fenced with an empty queue, so nothing will "
        "ever republish it: reads refuse and the drift audit reports a sidecar "
        "that is actually intact"
    )
    assert epistemic_graph.graph_drift(root) == [], (
        "a graph whose rows match disk must not read as drifted"
    )


def test_a_write_under_a_mark_on_its_own_paths_is_queued_repair(
    live_handoff_vault: Path,
    live_watcher: file_watcher.FileWatcher,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fence that covers a write's own paths owes the queue those paths.

    Found by a stress probe on the cold-resolver change: with the resolver
    forced to miss on every write, per-write rebuilds came back `[1, 0, 1, 0,
    1, 0]`. Every write that reached `_refresh_paths_locked` deferred correctly
    and queued; the three rebuilds entered through a different, older door. The
    path-scoped external-pending fence sits *before* that function, returns
    `{deferred: 1}` with no `queued`, and dispatch reads a deferral without
    queue coverage as a missing rebuild and schedules the vault.

    The mark is correct -- this process cannot trust its view of those paths --
    but it describes a bounded, known set, which is the definition of work the
    durable queue owns. The drain repairs it through `drain_paths`, which does
    not pass this fence, so convergence does not depend on the watcher's own
    repair landing first.

    Process-wide whole-vault passes are deliberately not the oracle here, for
    the reason the six-write test above gives: a running watcher may legitimately
    run one of its own. What is asserted is what dispatch chose, which is what
    the probe measured.

    The graph drain daemon runs between writes here because it does in
    production -- it fires within a second of the write that queued the debt.
    It is load-bearing, and honestly so: a deferral publishes nothing, so until
    the drain converges the queued paths the graph's acknowledgement stays one
    generation behind and the NEXT write's predecessor probe reports a proven
    gap. That is D2's own door, not this one. Measured on this shape: without a
    drain between writes the fence door contributes zero rebuilds (it was
    seven) and the predecessor door contributes six; with the drain, one, and
    that one is `graph_sync_predecessor_present_at_genesis` on the first write
    of a freshly built fixture.
    """
    root = live_handoff_vault
    generated = root / GENERATED
    caplog.set_level("INFO", logger="exomem.epistemic_graph")

    outcomes: list[epistemic_graph.GraphDispatchResult] = []
    real_dispatch = epistemic_graph.upsert_after_write
    dispatching_thread = threading.current_thread().name

    def recorded(vault_root: Path, paths: list[Path], **kwargs: object):
        result = real_dispatch(vault_root, paths, **kwargs)
        if threading.current_thread().name == dispatching_thread:
            # The patch is module-global, so the running watcher's own
            # dispatches arrive here too. They are a different caller with a
            # different contract (see `_messages_from_this_thread`), and the
            # outcome oracle below is about what the GOVERNED write chose.
            outcomes.append(result)
        return result

    monkeypatch.setattr(epistemic_graph, "upsert_after_write", recorded, raising=True)

    # One warm-up write, for the reason the convergence harness runs two: this
    # fixture publishes its graph through `rebuild_all`, which leaves no
    # `graph_sync` lineage, so the first write to take the predecessor probe
    # answers `graph_sync_predecessor_present_at_genesis` -- a proven verdict
    # about a fixture, not about the door under test. Consuming it here keeps
    # the per-write oracle below exact instead of carrying an exception.
    _governed_write(root, generated / "generated-note-0050.md", "warm-up")
    index_sync.drain_graph_work(root, limit=64)
    outcomes.clear()  # the warm-up's own outcome is fixture setup, not evidence

    acknowledgements: list[float] = []
    queued_after_write: list[bool] = []
    per_write_rebuilds: list[int] = []
    registered: list[str] = []
    captured: list[str] = []
    for i in range(LIVE_WRITE_COUNT):
        target = generated / f"generated-note-{i:04d}.md"
        # An unattributed edit to the path this write is about to touch, and
        # deliberately NOT waited out: the mark has to still be up when the
        # write dispatches, which is the shape the probe found.
        _external_edit(live_watcher, target, f"external editor touch {i}")
        assert freshness.external_pending_for(root, [target]), (
            "the probe's premise is a mark on the write's own path at dispatch"
        )
        # Cleared per write, and read back per write through
        # `_rebuild_reasons_this_write`, which attributes by emitting thread:
        # the window bounds *when*, the thread proves *who*. A running watcher
        # registers rebuilds of its own on the same vault, and counting those
        # against the governed write is what made this test order-dependent.
        # The aggregate assertions below still read the whole log, deliberately
        # -- `uncovered` and `fenced` are about the vault, not about a caller.
        caplog.clear()
        acknowledgements.append(_governed_write(root, target, f"governed {i}"))
        # Before the drain, so the receipt proof inside reads the queue this
        # write's own dispatch decided against.
        reasons = _rebuild_reasons_this_write(root, caplog)
        registered.extend(reasons)
        per_write_rebuilds.append(len(reasons))
        queued_after_write.append(bool(deferred_index.list_graph_paths(root)))
        # What the graph drain daemon does in production, on the same cadence.
        index_sync.drain_graph_work(root, limit=64)
        captured.append(caplog.text)

    log_text = "".join(captured)
    rendered = [round(seconds, 2) for seconds in acknowledgements]
    # The structural oracle for "a rebuild per write", which is what the median
    # latency bound below can only infer. Timing says a write was cheap; this
    # says a whole-vault pass was not scheduled on each one's account.
    #
    # Not zero, and deliberately: the canonical batch's own debt enqueue is
    # best-effort by construction ("a lost enqueue costs a reconcile; a refused
    # write costs the user their edit"), so under load one generation can go
    # unqueued and its gap is then genuinely uncovered. That rebuild is
    # correct -- and `_rebuild_reasons_this_write` has already proved it was
    # that: the reason is admitted, but only after reading back the generations
    # the probe named and finding no durable debt recorded for them.
    assert sum(per_write_rebuilds) <= 1, (
        f"whole-vault rebuilds registered per write: {per_write_rebuilds} for "
        f"reasons {registered}, acknowledgements {rendered}"
    )
    fenced = log_text.count("reason=external_event_covers_these_paths")
    assert fenced >= 1, (
        "the probe's premise never held: no write dispatched under a mark on "
        "its own paths, so this proves nothing"
    )
    uncovered = log_text.count("incremental_refresh_deferred_without_queue_coverage")
    assert uncovered == 0, (
        f"{uncovered} governed writes deferred without queue coverage and "
        f"registered a whole-vault rebuild: {rendered}. The fence describes this "
        "write's own paths, which the durable queue can own."
    )
    assert set(registered) <= {
        "graph_sync_predecessor_present_at_genesis",
        "graph_sync_predecessor_mismatch",
        "graph_sync_acknowledgement_absent",
    }, (
        "a fenced write registered a whole-vault rebuild for a reason this "
        f"change claims to have removed: {registered}"
    )
    assert "incremental_refresh_deferred_without_queue_coverage" not in registered
    codes = [result.code for result in outcomes]
    assert "graph_repair_external_pending" in codes, (
        f"no fenced write reported the fence's own pending outcome: {codes}"
    )
    assert set(codes) <= _QUEUED_COVERAGE_CODES, (
        f"a fenced write reported something other than queued coverage: {codes}"
    )
    assert any(queued_after_write), (
        "the fence must leave its own paths on the durable queue, or the "
        "pending outcome is a claim nothing backs"
    )
    _assert_incremental_latency(acknowledgements, bound=LIVE_ACK_BOUND_SECONDS)
    assert _drain_repair_queue(root, live_watcher) == 0, "the graph repair queue never drained"


def test_writes_faster_than_the_drain_stay_incremental_on_a_covered_gap(
    live_handoff_vault: Path,
    live_watcher: file_watcher.FileWatcher,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The rate the drain mitigation could not cover, which task 1.13 closes.

    A deferral publishes nothing, so the acknowledgement stays where it was and
    the next write's predecessor probe sees a gap. With a drain between writes
    the gap is closed before the next one dispatches; at a batch-ingest rate it
    is not, and the gap returned per write -- measured at six whole-vault
    rebuilds across six writes when nothing drained between them.

    The gap is real. What was not real is the claim that nothing is converging
    it: the canonical batch enqueued its own paths under the generation it
    committed, so the queue holds every generation the sidecar skipped. The
    probe proves that from the durable artifact and routes to the incremental
    path, which defers and queues without ever advancing the acknowledgement.
    """
    root = live_handoff_vault
    generated = root / GENERATED
    caplog.set_level("INFO", logger="exomem.epistemic_graph")

    # The fixture publishes through `rebuild_all` and leaves no graph_sync
    # lineage, so the first write to take the probe answers at genesis. Consume
    # it here, as the convergence harness consumes its own warm-up writes.
    _governed_write(root, generated / "generated-note-0050.md", "warm-up")
    index_sync.drain_graph_work(root, limit=64)

    outcomes: list[epistemic_graph.GraphDispatchResult] = []
    real_dispatch = epistemic_graph.upsert_after_write
    dispatching_thread = threading.current_thread().name

    def recorded(vault_root: Path, paths: list[Path], **kwargs: object):
        result = real_dispatch(vault_root, paths, **kwargs)
        if threading.current_thread().name == dispatching_thread:
            # The patch is module-global, so the running watcher's own
            # dispatches arrive here too. They are a different caller with a
            # different contract (see `_messages_from_this_thread`), and the
            # outcome oracle below is about what the GOVERNED write chose.
            outcomes.append(result)
        return result

    monkeypatch.setattr(epistemic_graph, "upsert_after_write", recorded, raising=True)

    acknowledgements: list[float] = []
    per_write_rebuilds: list[int] = []
    registered: list[str] = []
    captured: list[str] = []
    for i in range(LIVE_WRITE_COUNT):
        target = generated / f"generated-note-{i:04d}.md"
        _external_edit(live_watcher, target, f"external editor touch {i}")
        caplog.clear()
        acknowledgements.append(_governed_write(root, target, f"governed {i}"))
        reasons = _rebuild_reasons_this_write(root, caplog)
        registered.extend(reasons)
        per_write_rebuilds.append(len(reasons))
        captured.append(caplog.text)
        # Deliberately no drain: this is the rate the mitigation does not reach.

    log_text = "".join(captured)
    rendered = [round(seconds, 2) for seconds in acknowledgements]
    # Same oracle as the fenced test above, and for the same reason. Zero is
    # what 1.13 buys and what this measured, but the canonical batch's debt
    # enqueue is best-effort by construction, so one lost enqueue in six writes
    # leaves one gap genuinely uncovered and one correct rebuild -- observed at
    # about one isolated run in three. `_rebuild_reasons_this_write` has
    # already read the durable record back and required the generations the
    # probe named to have no debt recorded, so a rebuild admitted here is a
    # proven lost enqueue rather than the divergence 1.13 closed.
    assert sum(per_write_rebuilds) <= 1, (
        f"whole-vault rebuilds registered per write: {per_write_rebuilds} for "
        f"reasons {registered}, acknowledgements {rendered}"
    )
    assert set(registered) <= {"graph_sync_predecessor_mismatch"}, (
        "a write at batch-ingest rate registered a whole-vault rebuild for a "
        f"reason other than the lost enqueue this admits: {registered}"
    )
    assert "graph lineage gap is covered by durable receipts" in log_text, (
        "no write proved its gap covered, so this shape never reached the door "
        f"it is about: {[result.code for result in outcomes]}"
    )
    codes = [result.code for result in outcomes]
    covered_gap_writes = codes.count("graph_repair_covered_gap")
    assert set(codes) <= _QUEUED_COVERAGE_CODES, (
        f"a write reported something other than queued coverage: {codes}"
    )
    _assert_incremental_latency(acknowledgements, bound=LIVE_ACK_BOUND_SECONDS)
    assert _drain_repair_queue(root, live_watcher) == 0, "the graph repair queue never drained"
    # The premise -- that the writes outran repair -- is asserted above, from
    # this test's own evidence: nothing drains between the writes, and the
    # covered-gap line only appears when a write dispatched with the
    # acknowledgement genuinely behind its checkpoint. That is the rate shape,
    # stated directly.
    #
    # It used to be asserted here instead, as "an unattributed edit is still
    # outstanding at the end". That measured the WATCHER's catch-up, not this
    # test's rate: the watcher is an independent actor, and under load it
    # legitimately finishes retiring every mark before the loop ends -- which
    # failed the run about two times in ten while the write path it is about
    # behaved perfectly. A premise check that a third party can satisfy or
    # break on timing is not a premise check.
    assert covered_gap_writes >= 1, (
        "no write took the covered-gap door, so this shape never reached the "
        f"mechanism it is about: {[result.code for result in outcomes]}"
    )


def test_a_stale_receipt_does_not_bless_a_real_divergence(handoff_vault: Path) -> None:
    """Coverage is per generation, and an older one is repair for older bytes."""
    root = handoff_vault
    index = EpistemicGraphIndex(root)
    deferred_index.clear_graph(root)
    deferred_index.add_graph_receipts(
        root, [f"{GENERATED}/generated-note-0001.md"], generation=4
    )

    assert index._lineage_gap_is_receipt_covered(4, 8) is False, (
        "a receipt at generation 4 cannot vouch for generations 5, 6 and 7"
    )
    for generation in (5, 6, 7):
        deferred_index.add_graph_receipts(
            root, [f"{GENERATED}/generated-note-{generation:04d}.md"], generation=generation
        )
    assert index._lineage_gap_is_receipt_covered(4, 8) is True


def test_a_requeue_of_the_same_path_does_not_erase_an_older_generation(
    handoff_vault: Path,
) -> None:
    """The defect that made the covered-gap proof depend on timing.

    `graph_upserts` is keyed by rel_path and carries ONE generation column, and
    a re-queue of the same path moves it forward (`max(new, old)`) -- correct
    for a queue, because the row does still owe the newer repair, and fatal for
    a ledger. Measured under load on the six-write batch-ingest shape: the
    receipts held generations `{2, 3, 4, 5}` before a write and `{6}` after it,
    because the running watcher re-queued those same paths at its own
    checkpoint. The next write's predecessor probe then read a proven
    divergence over generations whose repair was fully queued, and bought a
    whole-vault rebuild -- about two writes in six, only under contention,
    which is the exact batch-ingest condition task 1.13 exists for.

    The proof now lives in its own append-only per-generation record, written
    in the same durable step as the receipts. Nothing overwrites it.
    """
    root = handoff_vault
    index = EpistemicGraphIndex(root)
    deferred_index.clear_graph(root)
    page = f"{GENERATED}/generated-note-0001.md"

    for generation in (2, 3, 4):
        deferred_index.add_graph_receipts(root, [page], generation=generation)

    assert index._lineage_gap_is_receipt_covered(1, 5) is True, (
        "the debt for generations 2, 3 and 4 was recorded, so the gap is covered"
    )

    # What the watcher does between two governed writes: the same path, queued
    # again under a newer checkpoint. The receipts collapse onto it by design.
    deferred_index.add_graph_receipts(root, [page], generation=9)
    receipt_generations, _unknown = deferred_index.graph_receipt_generations(root)
    assert receipt_generations == frozenset({9}), (
        "the premise is that the receipts themselves keep only the newest "
        f"generation per path: {sorted(receipt_generations)}"
    )

    assert index._lineage_gap_is_receipt_covered(1, 5) is True, (
        "a re-queue of the same path erased the evidence that generations 2, 3 "
        "and 4 were ever recorded, so a gap whose repair is queued reads as a "
        "proven divergence and buys a whole-vault rebuild"
    )
    assert index._lineage_gap_is_receipt_covered(1, 7) is False, (
        "generations 5 and 6 were never recorded, and nothing about a re-queue "
        "at 9 says otherwise"
    )


def test_clearing_the_whole_graph_queue_drops_the_debt_record_with_it(
    handoff_vault: Path,
) -> None:
    """A proof that outlived the queue it describes would bless an empty vault."""
    root = handoff_vault
    index = EpistemicGraphIndex(root)
    deferred_index.clear_graph(root)
    deferred_index.add_graph_receipts(
        root, [f"{GENERATED}/generated-note-0002.md"], generation=3
    )
    assert index._lineage_gap_is_receipt_covered(2, 4) is True

    deferred_index.clear_graph(root)

    assert index._lineage_gap_is_receipt_covered(2, 4) is False, (
        "the whole queue was discarded, so nothing is converging generation 3"
    )


def test_an_unknown_generation_receipt_does_not_bless_a_gap(handoff_vault: Path) -> None:
    """A receipt that cannot say what it owes is not evidence about anything."""
    root = handoff_vault
    index = EpistemicGraphIndex(root)
    deferred_index.clear_graph(root)
    for generation in (5, 6):
        deferred_index.add_graph_receipts(
            root, [f"{GENERATED}/generated-note-{generation:04d}.md"], generation=generation
        )
    assert index._lineage_gap_is_receipt_covered(4, 7) is True

    deferred_index.add_graph_receipts(root, [f"{GENERATED}/generated-note-0030.md"])
    assert index._lineage_gap_is_receipt_covered(4, 7) is False, (
        "one row with no generation makes the whole queue unusable as evidence"
    )


def test_an_unscoped_external_mark_is_not_coverable_by_a_path_queue(
    handoff_vault: Path,
) -> None:
    """An unknown affected set is the one thing a path-keyed queue cannot cover."""
    root = handoff_vault
    index = EpistemicGraphIndex(root)
    deferred_index.clear_graph(root)
    for generation in (5, 6):
        deferred_index.add_graph_receipts(
            root, [f"{GENERATED}/generated-note-{generation:04d}.md"], generation=generation
        )
    assert index._lineage_gap_is_receipt_covered(4, 7) is True

    freshness.mark_external_pending(root)
    assert index._lineage_gap_is_receipt_covered(4, 7) is False


def test_a_write_dispatched_behind_an_unrepaired_mark_stays_incremental(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same shape with the mark still outstanding at dispatch, deterministically.

    The live-watcher test above lets the watcher retire each mark, which is the
    production cycle; this one never does, so every write dispatches while an
    unattributed event is unrepaired -- the exact condition that used to answer
    `graph_sync_predecessor_unreadable` and rebuild the vault.
    """
    root = handoff_vault
    generated = root / GENERATED
    watcher = file_watcher.FileWatcher(root, debounce_seconds=0.2)
    spy = _RebuildSpy(monkeypatch)

    acknowledgements: list[float] = []
    per_write_rebuilds: list[int] = []
    for i in range(WRITE_COUNT):
        _external_edit(watcher, generated / f"generated-note-{100 + i:04d}.md", f"external {i}")
        assert freshness.external_pending(root) is True
        before = spy.count
        acknowledgements.append(
            _governed_write(root, generated / f"generated-note-{i:04d}.md", f"governed {i}")
        )
        per_write_rebuilds.append(spy.count - before)

    assert spy.count == 0, (
        f"{spy.count} whole-vault rebuild(s) ran for {WRITE_COUNT} governed writes: "
        f"per_write={per_write_rebuilds} "
        f"acknowledgements={[round(seconds, 2) for seconds in acknowledgements]}"
    )
    _assert_incremental_latency(acknowledgements)
    assert _drain_repair_queue(root, watcher) == 0, "the graph repair queue never drained"


def test_an_unscoped_fan_out_mark_is_the_only_one_that_fences_a_write(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watcher's fan-out recovery marks the scope it can prove, not the vault.

    This branch fires on every drain that leaves the graph behind. An unscoped
    mark here fenced the *next* governed write into a whole-vault rebuild, whose
    deferral queued more paths, which left the next fan-out incomplete: a loop
    that sustains itself for as long as the vault is written to.
    """
    root = handoff_vault
    batch = [f"{GENERATED}/generated-note-{i:04d}.md" for i in range(3)]
    # The durable queue is a backlog, not an observation: a scope taken from it
    # grows for as long as repair is outstanding, which is how a narrower mark
    # still ends up fencing the next write.
    deferred_index.add_graph(root, [f"{GENERATED}/generated-note-{i:04d}.md" for i in range(50, 60)])

    scope = file_watcher._graph_incompleteness_scope(root, batch)

    assert scope is not None, "a batch with a known path list is not a whole-vault scope"
    assert {path.name for path in scope} == {Path(rel).name for rel in batch}

    freshness.mark_external_pending(root, paths=scope)
    assert freshness.external_pending(root) is True
    assert (
        freshness.external_pending_for(root, [root / GENERATED / "generated-note-0100.md"])
        is False
    ), "a write outside the queued repair must not be fenced by it"

    spy = _RebuildSpy(monkeypatch)
    elapsed = _governed_write(root, root / GENERATED / "generated-note-0100.md", "unfenced")

    assert spy.count == 0
    assert elapsed < ACK_BOUND_SECONDS


def test_a_fan_out_scope_that_cannot_be_established_still_fences_everything(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: an unknown scope is a whole-vault scope."""
    root = handoff_vault
    batch = [f"{GENERATED}/generated-note-{i:04d}.md" for i in range(3)]
    deferred_index.mark_graph_full_rebuild(root, generation=1)

    assert file_watcher._graph_incompleteness_scope(root, batch) is None, (
        "whole-vault repair outstanding is a whole-vault scope"
    )

    deferred_index.clear_graph_full_rebuild(root)
    assert file_watcher._graph_incompleteness_scope(root, ()) is None, (
        "a drain that can name no path has not established a scope"
    )

    monkeypatch.setattr(
        deferred_index,
        "graph_full_rebuild_pending",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("marker unreadable")),
    )
    assert file_watcher._graph_incompleteness_scope(root, batch) is None, (
        "an unreadable marker is an unknown scope"
    )


def test_an_unrelated_external_edit_does_not_fence_a_write(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unattributed edit on another path; the write still takes the incremental path.

    Reads that require a current projection keep refusing until the edited path
    is repaired -- the fence narrows for writes only.
    """
    root = handoff_vault
    generated = root / GENERATED
    watcher = file_watcher.FileWatcher(root, debounce_seconds=0.2)
    spy = _RebuildSpy(monkeypatch)

    _external_edit(watcher, generated / "generated-note-0150.md", "unrelated external")
    assert freshness.external_pending(root) is True

    elapsed = _governed_write(root, generated / "generated-note-0001.md", "governed alongside")

    assert spy.count == 0, "an unrelated external edit scheduled a whole-vault rebuild"
    assert elapsed < ACK_BOUND_SECONDS
    assert EpistemicGraphIndex(root).available() is False, (
        "reads that require a current projection must still refuse while an "
        "external path is unrepaired"
    )
    assert _drain_repair_queue(root, watcher) == 0


def test_a_withdrawn_availability_marker_is_repaired_incrementally(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state every fence leaves behind is repaired by the next write, not rebuilt.

    Reached through the production call the watcher's drain makes before its
    compare-and-ack (`_suspend_reads_for_acknowledgement` ->
    `withdraw_availability`), not by editing the sidecar, because a state that
    only hand SQL can produce proves nothing about the path that produces it.

    The withdrawal used to drop `schema_version` and the stored recall
    checkpoint along with the marker. A per-path deferral thereby took a
    vault-wide action: the sidecar read as "not this build's" to the maintenance
    readers, and with no stored checkpoint the bounded repair had no lineage to
    advance, so the next governed write on any path bailed out on
    `recall_checkpoint_absent_or_registry_not_live` and rebuilt the whole vault.
    """
    root = handoff_vault
    watcher = file_watcher.FileWatcher(root, debounce_seconds=0.2)
    index = EpistemicGraphIndex(root)
    assert index.available() is True

    index.withdraw_availability()

    assert index.available() is False, "the withdrawal must fence public readers"
    assert index._declined_snapshot_state() == "graph_sync_predecessor_unreadable", (
        "a withdrawn marker is a fenced sidecar, not an unusable one"
    )

    spy = _RebuildSpy(monkeypatch)
    elapsed = _governed_write(root, root / GENERATED / "generated-note-0003.md", "after withdrawal")

    assert spy.count == 0, "the write after a withdrawal scheduled a whole-vault rebuild"
    assert elapsed < ACK_BOUND_SECONDS
    assert EpistemicGraphIndex(root).available() is True, (
        "the incremental pass must republish the availability marker"
    )
    assert _drain_repair_queue(root, watcher) == 0


def test_an_unreadable_predecessor_is_queued_repair_not_a_lineage_gap(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decline that proves nothing takes the queue, and says so in its own code.

    A sidecar held by a publication, or momentarily locked, declines the probe
    without proving anything about its lineage. That used to be spelled the same
    way as a proven lineage gap and earned the same whole-vault rebuild.
    """
    root = handoff_vault
    spy = _RebuildSpy(monkeypatch)
    monkeypatch.setattr(
        EpistemicGraphIndex, "available", lambda inner_self: False, raising=True
    )
    monkeypatch.setattr(
        EpistemicGraphIndex,
        "_graph_sync_predecessor_state",
        lambda inner_self, checkpoint: "graph_sync_predecessor_unreadable",
        raising=True,
    )
    monkeypatch.setattr(
        EpistemicGraphIndex,
        "_refresh_paths_locked",
        lambda inner_self, paths, **kwargs: {
            "indexed_files": 0,
            "nodes": 0,
            "edges": 0,
            "deferred": 1,
            "queued": 1,
        },
        raising=True,
    )

    target = root / GENERATED / "generated-note-0005.md"
    outcomes: list[epistemic_graph.GraphDispatchResult] = []
    real_dispatch = epistemic_graph.upsert_after_write

    def recorded(vault_root: Path, paths: list[Path], **kwargs: object):
        result = real_dispatch(vault_root, paths, **kwargs)
        outcomes.append(result)
        return result

    monkeypatch.setattr(epistemic_graph, "upsert_after_write", recorded, raising=True)

    _governed_write(root, target, "unreadable predecessor")

    assert spy.count == 0, "an unproven decline scheduled a whole-vault rebuild"
    assert [result.code for result in outcomes] == ["graph_repair_unreadable_predecessor"], (
        f"expected the distinct pending code, got {[result.code for result in outcomes]}"
    )
    assert {result.outcome for result in outcomes} == {"deferred"}


def test_a_cold_resolver_is_queued_repair_not_a_whole_vault_rebuild(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolver cache miss is a cold cache, not an unbounded graph.

    Measured on the 0.83.1 deploy: the replacement worker had never built a
    recall resolver, so `recall_resolver_snapshot_at_checkpoint` missed on every
    governed write. That bail-out's disposition was "rebuild", which enqueues
    nothing, so dispatch saw `deferred` without `queued`, reported
    `incremental_refresh_deferred_without_queue_coverage`, and registered a
    whole-vault rebuild the write then waited on.

    Everything the pass needs to bound the damage is already proven when this
    fires: the durable checkpoint matched, the acknowledgement was the
    predecessor, and the recall delta came back complete. What is missing is the
    pre-delta topology needed to widen the set, and the queue is exactly the
    mechanism for repair whose scope is known but whose proof is not.
    """
    root = handoff_vault
    spy = _RebuildSpy(monkeypatch)
    monkeypatch.setattr(
        find_module,
        "recall_resolver_snapshot_at_checkpoint",
        lambda *_args, **_kwargs: None,
        raising=True,
    )

    target = root / GENERATED / "generated-note-0007.md"
    outcomes: list[epistemic_graph.GraphDispatchResult] = []
    real_dispatch = epistemic_graph.upsert_after_write

    def recorded(vault_root: Path, paths: list[Path], **kwargs: object):
        result = real_dispatch(vault_root, paths, **kwargs)
        outcomes.append(result)
        return result

    monkeypatch.setattr(epistemic_graph, "upsert_after_write", recorded, raising=True)

    elapsed = _governed_write(root, target, "cold resolver")

    assert spy.count == 0, (
        "a cold resolver cache scheduled a whole-vault rebuild, which is the "
        "164.8 s write the 0.83.1 deploy measured"
    )
    assert [result.code for result in outcomes] == ["graph_repair_cold_resolver"], (
        f"expected the resolver's own pending code, got "
        f"{[result.code for result in outcomes]}"
    )
    assert {result.outcome for result in outcomes} == {"deferred"}
    assert deferred_index.list_graph_paths(root), (
        "the deferral must leave the affected paths on the durable queue, or "
        "the pending code is a claim nothing backs"
    )
    assert elapsed < ACK_BOUND_SECONDS, f"the write was not incremental: {elapsed:.2f}s"

    monkeypatch.undo()
    assert _drain_graph_queue(root) == 0, "the graph repair queue never drained"


def test_startup_graph_validation_waits_out_a_governed_write(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A held mutation boundary at seed time is contention, not incoherence.

    Measured on the 0.83.1 deploy: the watcher's seed-time validation met the
    first governed write holding `semantic_existing_edit_commit` for 10.6 s,
    `suspend_reads()` was refused MUTATION_BUSY inside its one coordinator
    timeout, and startup validation aborted -- leaving the graph unreadable with
    no persisted barrier, which is the state the drain then rebuilt from.

    The hold here outlasts one coordinator timeout on purpose: that is the
    entire difference between a one-shot attempt and a bounded retry.
    """
    root = handoff_vault
    watcher = file_watcher.FileWatcher(root, debounce_seconds=0.2)
    graph = EpistemicGraphIndex(root)
    caplog.set_level("INFO", logger="exomem.file_watcher")

    # Force the cheap durable proof to decline so the expensive branch -- the
    # one that needs the boundary -- is the branch under test.
    monkeypatch.setattr(
        EpistemicGraphIndex,
        "durable_checkpoint_is_coherent",
        lambda inner_self: False,
        raising=True,
    )

    hold_seconds = 6.5
    holding = threading.Event()
    released = threading.Event()

    def hold_the_boundary() -> None:
        try:
            with graph._mutation_coordinator.hold(
                operation="semantic_existing_edit_commit", holder_kind="command"
            ):
                holding.set()
                time.sleep(hold_seconds)
        finally:
            holding.set()
            released.set()

    writer = threading.Thread(target=hold_the_boundary, daemon=True)
    writer.start()
    assert holding.wait(10), "the simulated governed write never took the boundary"

    admitted = watcher._validate_existing_graph_on_seed()

    assert released.wait(30), "the simulated write never released the boundary"
    writer.join(10)

    assert admitted, (
        "startup validation must wait out a governed write rather than abort; "
        "aborting is what left the graph unreadable with no barrier"
    )
    assert "startup graph validation failed" not in caplog.text, caplog.text


def test_an_unusable_snapshot_still_rebuilds(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registry drift is a proven verdict, and a whole-vault pass is its repair."""
    import sqlite3

    root = handoff_vault
    index = EpistemicGraphIndex(root)
    connection = sqlite3.connect(index.path)
    try:
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_meta(key, value) VALUES "
                "('extension_registry_hash', 'drifted')"
            )
    finally:
        connection.close()

    spy = _RebuildSpy(monkeypatch)
    states: list[str] = []
    real_state = EpistemicGraphIndex._graph_sync_predecessor_state

    def observed(
        inner_self: EpistemicGraphIndex, checkpoint: graph_sync.GraphSyncCheckpoint
    ) -> str:
        state = real_state(inner_self, checkpoint)
        states.append(state)
        return state

    monkeypatch.setattr(
        EpistemicGraphIndex, "_graph_sync_predecessor_state", observed, raising=True
    )

    _governed_write(root, root / GENERATED / "generated-note-0004.md", "registry drift")
    graph_sync.drain_active_rebuilds(timeout=60.0)

    assert states == ["graph_sync_snapshot_unusable"], (
        f"expected the proven-unusable decline, got {states}"
    )
    assert spy.count >= 1, "an unusable snapshot must still schedule a whole-vault rebuild"


def test_a_real_lineage_gap_still_rebuilds(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot that cannot be advanced incrementally still schedules the vault."""
    root = handoff_vault
    generated = root / GENERATED
    spy = _RebuildSpy(monkeypatch)
    states: list[str] = []

    real_state = EpistemicGraphIndex._graph_sync_predecessor_state

    def mismatched(
        inner_self: EpistemicGraphIndex, checkpoint: graph_sync.GraphSyncCheckpoint
    ) -> str:
        real_state(inner_self, checkpoint)
        states.append("graph_sync_predecessor_mismatch")
        return "graph_sync_predecessor_mismatch"

    monkeypatch.setattr(
        EpistemicGraphIndex, "_graph_sync_predecessor_state", mismatched, raising=True
    )
    monkeypatch.setattr(
        EpistemicGraphIndex, "available", lambda inner_self: False, raising=True
    )

    _governed_write(root, generated / "generated-note-0002.md", "lineage gap")
    graph_sync.drain_active_rebuilds(timeout=60.0)

    assert states, "the dispatch must consult the predecessor state"
    assert spy.count >= 1, "a proven lineage gap must still schedule a whole-vault rebuild"


#: Run in a *fresh interpreter*, which is the point: `file_watcher._SELF_UPSERTS`
#: and the whole `freshness` registry are process-local, so a replacement worker
#: inherits neither. Phase 1 is the outgoing worker, phase 2 the replacement that
#: starts against the same vault, the same state root and the same published
#: graph, sees the outgoing worker's last writes as events it cannot attribute,
#: and then serves ten governed writes of its own.
_HANDOFF_CHILD = '''
import json, os, sys, time
from pathlib import Path

sys.path.insert(0, {tests_dir!r})
from test_graph_post_handoff_writes import (  # noqa: E402
    GENERATED,
    WRITE_COUNT,
    _governed_write,
    _seed_live_freshness,
)

from exomem import deferred_index, epistemic_graph, file_watcher, freshness  # noqa: E402
from exomem.epistemic_graph import EpistemicGraphIndex  # noqa: E402

root = Path(sys.argv[2])
phase = sys.argv[1]
generated = root / GENERATED

if phase in {{"outgoing", "outgoing_fenced"}}:
    _seed_live_freshness(root)
    EpistemicGraphIndex(root).rebuild_all()
    for index in range(3):
        _governed_write(root, generated / f"generated-note-{{index + 60:04d}}.md", "outgoing")
    if phase == "outgoing_fenced":
        # What a mid-traffic handoff leaves behind: a page whose canonical bytes
        # the graph never took, and a withdrawn availability marker, because the
        # write that deferred withdrew it and the repair did not land before the
        # worker stopped.
        deferred = generated / "generated-note-0090.md"
        deferred.write_text(
            deferred.read_text(encoding="utf-8") + chr(10) + "- deferred, unrepaired" + chr(10),
            encoding="utf-8",
        )
        EpistemicGraphIndex(root).withdraw_availability()
    print(json.dumps({{"available": EpistemicGraphIndex(root).available()}}))
    raise SystemExit(0)

victim = generated / "generated-note-0042.md"
if phase == "racing_edit":
    # An unattributed event published by the watcher *while the adoption proof is
    # walking the corpus*: a touch, which is the shape that passes the proof and
    # still advances the registry -- a content edit is caught by the proof's own
    # re-read and declines it. The origin must not be recorded past the event.
    real_proof = EpistemicGraphIndex._snapshot_sources_match_disk

    def racing(inner_self, conn, **kwargs):
        if not racing.fired:
            racing.fired = True
            victim.write_text(victim.read_text(encoding="utf-8"), encoding="utf-8")
            os.utime(victim, (time.time() + 1, time.time() + 1))
            freshness.on_files_changed(root, [victim], [])
        return real_proof(inner_self, conn, **kwargs)

    racing.fired = False
    EpistemicGraphIndex._snapshot_sources_match_disk = racing

passes = []
real = EpistemicGraphIndex._rebuild_all_off_boundary


def counted(self, **kwargs):
    started = time.monotonic()
    try:
        return real(self, **kwargs)
    finally:
        passes.append(time.monotonic() - started)


EpistemicGraphIndex._rebuild_all_off_boundary = counted
_seed_live_freshness(root)
# What the worker runtime's warm-up does for a replacement process, and what a
# standby does before promotion: prove the inherited snapshot and adopt its
# checkpoint as this process's delta origin.
# The warm window a replacement worker actually starts in, and the write that
# arrives during it. Everything a writer used to wait on is ready; only the
# handoff is not, which is exactly the 0.84.1 shape -- the corpus build finished
# 36 s before adoption ran, so the gate let the first governed write through
# into a process with no delta origin.
from types import SimpleNamespace  # noqa: E402

from exomem import readiness  # noqa: E402
from exomem.cli_ops import OpError  # noqa: E402
from exomem.writer_lease import LeaseConfig, LeaseManager  # noqa: E402

_manager = LeaseManager(LeaseConfig(state_dir=root / ".exomem" / "lease-state"))
_admitted = []
_early = SimpleNamespace(
    name="mutate", read_only=False, leaf=lambda: _admitted.append("ran")
)
readiness.begin_warm()
for component in readiness.COMPONENTS:
    if component != "graph_handoff":
        readiness.mark_ready(component)
early_refusal = None
try:
    _manager.invoke(_early, (), {{}}, mutation_request_id="early-write")
except OpError as error:
    early_refusal = {{
        "code": error.code,
        "component": (error.details or {{}}).get("warming_component"),
    }}

stored_before = None
index_for_adoption = EpistemicGraphIndex(root)
opened = index_for_adoption._open_read_snapshot(require_current_projection=False)
if opened is not None:
    stored_before = index_for_adoption._stored_recall_checkpoint(opened)
    opened.close()
adoption = index_for_adoption.adopt_published_snapshot()
adopted = bool(adoption)
# The handoff has settled; the gate opens, and the write that was refused above
# is the first one this process serves.
readiness.mark_ready("graph_handoff")
_manager.invoke(_early, (), {{}}, mutation_request_id="early-write-retry")
readiness.finish_warm()
early_admitted = _admitted == ["ran"]
victim_in_delta = bool(
    stored_before is not None
    and str(victim)
    in freshness.recall_delta_since(root, "vault", stored_before).changed
)
watcher = file_watcher.FileWatcher(root, debounce_seconds=0.2)
for index in range(3):
    watcher._record(generated / f"generated-note-{{index + 60:04d}}.md", deleted=False)
acknowledgements = []
per_write_rebuilds = []
for index in range(WRITE_COUNT):
    before = len(passes)
    acknowledgements.append(
        _governed_write(root, generated / f"generated-note-{{index:04d}}.md", "replacement")
    )
    per_write_rebuilds.append(len(passes) - before)
# The watcher's own compare-and-ack, run here because this child has no watcher
# thread: `graph_drift` reads a public snapshot, and an unrepaired external mark
# fences public reads, which would report the fence rather than any drift.
from exomem import index_sync  # noqa: E402

for _ in range(12):
    if not deferred_index.list_graph_paths(root):
        break
    index_sync.drain_graph_work(root, limit=64)
queue_after_drain = len(deferred_index.list_graph_paths(root))
pending_epoch = freshness.external_pending_epoch(root)
if pending_epoch is not None:
    freshness.clear_external_pending(root, through=pending_epoch)
drift = epistemic_graph.graph_drift(root)
print(
    json.dumps(
        {{
            "adopted": adopted,
            "early_refusal": early_refusal,
            "early_admitted": early_admitted,
            "residue": list(adoption.residue),
            "adoption_reason": adoption.reason,
            "queue_after_drain": queue_after_drain,
            "drift": [entry["path"] for entry in drift],
            "victim_in_delta": victim_in_delta,
            "rebuilds": len(passes),
            "per_write_rebuilds": per_write_rebuilds,
            "acknowledgements": acknowledgements,
            "available": EpistemicGraphIndex(root).available(),
        }}
    )
)
'''


def test_a_replacement_process_serves_its_first_writes_incrementally(
    handoff_vault: Path, tmp_path: Path
) -> None:
    """The handoff itself, across two real interpreters.

    `test_writes_after_a_worker_replacement_stay_incremental` drives the same
    shape in one process because the deterministic part of a replacement is the
    unattributed event, not the fork. This one pays for a second interpreter to
    prove the premise it rests on: the incoming worker really does start with an
    empty self-attribution table and an empty freshness registry.

    It also pins the adoption: a cold process cannot compute a delta from a
    checkpoint another process instance published, so before
    `adopt_published_snapshot` its first write rebuilt the whole vault purely to
    obtain a lineage it could advance. The child proves the inherited snapshot
    exactly as the worker runtime's warm-up does, and then rebuilds nothing at
    all.
    """
    import json
    import os
    import subprocess
    import sys

    script = tmp_path / "handoff_child.py"
    script.write_text(
        _HANDOFF_CHILD.format(tests_dir=str(Path(__file__).parent)), encoding="utf-8"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1] / "src"), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    outgoing = subprocess.run(
        [sys.executable, str(script), "outgoing", str(handoff_vault)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
        check=False,
    )
    assert outgoing.returncode == 0, outgoing.stderr[-4000:]
    assert json.loads(outgoing.stdout.strip().splitlines()[-1])["available"] is True

    replacement = subprocess.run(
        [sys.executable, str(script), "replacement", str(handoff_vault)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
        check=False,
    )
    assert replacement.returncode == 0, replacement.stderr[-4000:]
    report = json.loads(replacement.stdout.strip().splitlines()[-1])

    assert report["adopted"] is True, (
        "the replacement process must be able to prove the snapshot it inherited"
    )
    assert report["early_refusal"] == {
        "code": "MUTATION_WARMING",
        "component": "graph_handoff",
    }, (
        "a governed write arriving before adoption had run was admitted. That "
        "is the 0.84.1 failure: the gate waited only on the semantic corpus, "
        "the corpus finished 36 s before the handoff did, and the write it let "
        "through had no lineage to advance -- so it registered the whole-vault "
        f"rebuild adoption exists to remove. Got {report['early_refusal']!r}"
    )
    assert report["early_admitted"] is True, (
        "the refusal must be retryable and must clear once the handoff settles; "
        "a write held past adoption is a gate that never opens"
    )
    per_write = report["per_write_rebuilds"]
    assert per_write == [0] * WRITE_COUNT, (
        "a replacement process that adopted its inherited snapshot rebuilds "
        f"nothing; rebuilds per write were {per_write}"
    )
    _assert_incremental_latency(report["acknowledgements"][1:])


def test_an_edit_during_the_adoption_proof_is_still_indexed(
    handoff_vault: Path, tmp_path: Path
) -> None:
    """The adoption window, end to end, in a process that really did not publish.

    The proof walks the whole corpus, so the watcher publishes unattributed
    edits while it runs. An origin recorded at the generation the proof ended on
    declares those edits accounted for, and the bounded repair then advances a
    snapshot that never indexed the edited page -- drift that nothing reports,
    because every later delta starts after it.
    """
    import json
    import os
    import subprocess
    import sys

    script = tmp_path / "handoff_child.py"
    script.write_text(
        _HANDOFF_CHILD.format(tests_dir=str(Path(__file__).parent)), encoding="utf-8"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1] / "src"), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    outgoing = subprocess.run(
        [sys.executable, str(script), "outgoing", str(handoff_vault)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
        check=False,
    )
    assert outgoing.returncode == 0, outgoing.stderr[-4000:]

    replacement = subprocess.run(
        [sys.executable, str(script), "racing_edit", str(handoff_vault)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
        check=False,
    )
    assert replacement.returncode == 0, replacement.stderr[-4000:]
    report = json.loads(replacement.stdout.strip().splitlines()[-1])

    assert report["adopted"] is True
    assert report["victim_in_delta"] is True, (
        "an edit published while the proof was walking must remain in the delta "
        "the adopted origin opens"
    )
    assert report["drift"] == [], (
        f"the adopted snapshot advanced without indexing {report['drift']}"
    )


def _run_child(
    vault: Path, tmp_path: Path, phases: list[str]
) -> dict[str, object]:
    import json
    import os
    import subprocess
    import sys

    script = tmp_path / "handoff_child.py"
    script.write_text(
        _HANDOFF_CHILD.format(tests_dir=str(Path(__file__).parent)), encoding="utf-8"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1] / "src"), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    report: dict[str, object] = {}
    for phase in phases:
        completed = subprocess.run(
            [sys.executable, str(script), phase, str(vault)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=300,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-4000:]
        report = json.loads(completed.stdout.strip().splitlines()[-1])
    return report


def test_a_handoff_that_left_deferred_work_is_still_adopted(
    handoff_vault: Path, tmp_path: Path
) -> None:
    """The state a real mid-traffic handoff leaves, and the one adoption refused.

    A write that defers withdraws the availability marker, and the background
    repair does not always republish it before the old worker stops. Adoption
    ran the *public* proof, so it failed in 23 ms on exactly the vaults that
    needed it, and the promoted worker paid the whole-vault pass anyway.

    Now the proof reports that residue and, while it fits inside one drain pass,
    the adoption takes it on: the checkpoint becomes the delta origin, the
    residue paths are queued for incremental repair, and the marker stays
    withdrawn so reads keep refusing until that repair lands.
    """
    report = _run_child(handoff_vault, tmp_path, ["outgoing_fenced", "replacement"])

    assert report["adopted"] is True, (
        f"adoption refused a bounded residue: {report['adoption_reason']}"
    )
    assert len(report["residue"]) >= 1, (
        "the outgoing process left a page the graph never took; adoption must say so"
    )
    assert report["per_write_rebuilds"] == [0] * WRITE_COUNT, (
        f"the promoted process rebuilt: {report['per_write_rebuilds']}"
    )
    assert report["queue_after_drain"] == 0, "the residue never drained"
    assert report["drift"] == [], f"the residue was adopted but never repaired: {report['drift']}"


def test_an_unbounded_residue_still_refuses_adoption(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A residue larger than one drain pass is a different corpus, not a repair."""
    from exomem import graph_drain

    root = handoff_vault
    generated = root / GENERATED
    for i in range(3):
        page = generated / f"generated-note-{80 + i:04d}.md"
        page.write_text(
            page.read_text(encoding="utf-8") + "\n- unattributed\n", encoding="utf-8"
        )
    monkeypatch.setattr(graph_drain, "DRAIN_LIMIT", 2, raising=True)

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot()

    assert adoption.adopted is False
    assert adoption.reason == "residue_exceeds_drain_limit"
    assert len(adoption.residue) == 3


def test_a_residue_adoption_keeps_reads_refusing_until_the_repair_lands(
    handoff_vault: Path,
) -> None:
    """The marker is one value for the whole projection, so it stays withdrawn."""
    from exomem import index_sync

    root = handoff_vault
    page = root / GENERATED / "generated-note-0091.md"
    page.write_text(page.read_text(encoding="utf-8") + "\n- unattributed\n", encoding="utf-8")

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot()

    assert adoption.adopted is True
    assert list(adoption.residue) == [f"{GENERATED}/generated-note-0091.md"]
    assert EpistemicGraphIndex(root).available() is False, (
        "a snapshot that owes repair must not answer reads that require currency"
    )
    assert set(deferred_index.list_graph_paths(root)) == set(adoption.residue)

    for _ in range(12):
        if not deferred_index.list_graph_paths(root):
            break
        index_sync.drain_graph_work(root, limit=64)

    assert deferred_index.list_graph_paths(root) == []
    assert EpistemicGraphIndex(root).available() is True, (
        "the repair must republish the marker it kept withdrawn"
    )
    assert epistemic_graph.graph_drift(root) == []


#: Every reason `_snapshot_sources_match_disk` may decline with, and what an
#: operator is supposed to do about it. The 0.84.1 deploy is why this table
#: exists: the adoption line said `reason=snapshot_proof_declined`, which is a
#: restatement of `adopted=False` and told nobody whether the corpus had moved,
#: the projection was mid-repair, or a concurrent whole-vault rebuild was
#: rewriting the sidecar out from under the proof. Diagnosing it took a service
#: log correlated by hand across three minutes.
_DECLINE_REASONS = {
    "recall_membership_unreadable": "the recall scope could not be read at all",
    "source_unparseable": "a page the snapshot indexes no longer parses",
    "indexed_membership_differs": "a different corpus, not a bounded repair",
    "indexed_sources_differ": "same, for a caller that cannot carry a residue",
    "resolver_topology_mismatch": "the link topology is not the one published",
    "projection_moved_during_proof": "the projection changed under the proof",
    "proof_raised": "the proof itself failed; treat as unproven",
}


def test_every_adoption_decline_has_a_name_of_its_own() -> None:
    """`snapshot_proof_declined` is not a diagnosis, so no arm may return it."""
    import ast

    source = Path(epistemic_graph.__file__).read_text(encoding="utf-8")
    proof = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_snapshot_sources_match_disk"
    )
    reasons = {
        literal.value
        # Every string literal handed to `declined`, including the ones inside a
        # conditional expression: an arm that picks between two names still owes
        # both of them a declaration.
        for call in ast.walk(proof)
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "declined"
        for literal in ast.walk(call)
        if isinstance(literal, ast.Constant) and isinstance(literal.value, str)
    }
    assert reasons == set(_DECLINE_REASONS), (
        "an adoption decline arm appeared, moved or lost its name. Every arm "
        "reports a distinct condition an operator acts on differently; "
        f"declared {sorted(_DECLINE_REASONS)}, found {sorted(reasons)}"
    )
    assert "snapshot_proof_declined" not in reasons, (
        "the generic reason is the fallback for a proof that returned False "
        "without naming anything, never something an arm chooses"
    )


def test_a_declined_adoption_names_the_condition_it_declined_on(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reasons reachable without racing the proof, each pinned to its cause.

    Not every arm is deterministic from outside -- `projection_moved_during_proof`
    is a race by definition -- so the arms are enumerated structurally by the
    test above and the causes that can be produced are pinned here.
    """
    root = handoff_vault
    caplog.set_level("INFO", logger="exomem.warmup")

    def raises(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the indexed membership read failed")

    cases = {
        "recall_membership_unreadable": lambda mp: mp.setattr(
            EpistemicGraphIndex, "_recall_membership", lambda _self: None, raising=True
        ),
        "resolver_topology_mismatch": lambda mp: mp.setattr(
            epistemic_graph,
            "_resolver_topology_fingerprint",
            lambda _resolver: "not-the-published-topology",
            raising=True,
        ),
        "proof_raised": lambda mp: mp.setattr(
            EpistemicGraphIndex, "_indexed_recall_membership", raises, raising=True
        ),
    }
    for reason, arrange in cases.items():
        with monkeypatch.context() as patched:
            arrange(patched)
            adoption = EpistemicGraphIndex(root).adopt_published_snapshot()
        assert adoption.adopted is False
        assert adoption.reason == reason, (
            f"a decline on {reason} reported {adoption.reason!r} instead"
        )


def test_the_warm_up_reports_the_reason_an_adoption_declined(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The operator reads the warm-up line, not the return value."""
    from exomem import warmup

    caplog.set_level("INFO", logger="exomem.warmup")
    monkeypatch.setattr(warmup, "warmup_enabled", lambda: True, raising=True)
    monkeypatch.setattr(
        EpistemicGraphIndex, "_recall_membership", lambda _self: None, raising=True
    )

    warmup.warm_graph_handoff(handoff_vault)

    assert "reason=recall_membership_unreadable" in caplog.text, (
        "the adoption line has to carry the specific decline, or a deploy is "
        f"diagnosed by source read again: {caplog.text[-500:]}"
    )


def test_the_warm_up_adopts_after_the_seed_and_the_resolver(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warm-up order is load-bearing, so it is pinned rather than commented.

    `adopt_recall_origin` refuses a cold scope, and the watcher's seed replaces
    the registry maps wholesale -- an adoption recorded before that seed is
    dropped with the map it described. The graph step therefore has to run after
    the seed, and after the resolver step whose cache it reuses. Both now live
    on the unconditional start-up path rather than inside `warm_caches`, so this
    pins the order in `warm_graph_handoff`.
    """
    from exomem import warmup

    root = handoff_vault
    order: list[str] = []
    live_at_call: list[bool] = []

    real_resolver = find_module.recall_resolver_snapshot

    def traced_resolver(*args: object, **kwargs: object) -> object:
        order.append("resolver")
        return real_resolver(*args, **kwargs)

    real_adopt = EpistemicGraphIndex.adopt_published_snapshot

    def traced_adopt(inner_self: EpistemicGraphIndex) -> object:
        order.append("graph_snapshot")
        live_at_call.append(freshness.recall_is_live(inner_self.vault_root, "vault"))
        return real_adopt(inner_self)

    monkeypatch.setattr(warmup, "warmup_enabled", lambda: True, raising=True)
    monkeypatch.setattr(find_module, "recall_resolver_snapshot", traced_resolver, raising=True)
    monkeypatch.setattr(
        EpistemicGraphIndex, "adopt_published_snapshot", traced_adopt, raising=True
    )

    durations = warmup.warm_graph_handoff(root)

    assert "graph_snapshot" in durations, "the warm-up must adopt the inherited snapshot"
    steps = list(durations)
    assert steps.index("resolver") < steps.index("graph_snapshot"), (
        f"the graph step must follow the resolver step: {steps}"
    )
    assert order.index("resolver") < order.index("graph_snapshot"), order
    assert live_at_call == [True], (
        "the graph step must run against a seeded registry, or the origin it "
        "adopts is refused"
    )


def test_start_up_adopts_the_snapshot_when_the_resource_mode_skips_cpu_caches(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Adoption is not a cache, so the cache-preload policy must not decide it.

    Measured on the 0.83.1 deploy: the personal service runs `mode=normal`,
    which leaves `preload_cpu_caches` False, so `warm_caches` returned at its
    first gate and the adoption step inside it never ran. Warm complete listed
    only `retrieval_catalog` and `semantic_corpus`, there was no adoption line,
    and the replacement worker's first governed writes rebuilt the whole vault
    (164.8 s, then 46.7 s and 46.7 s).
    """
    from exomem import warmup

    root = handoff_vault
    caplog.set_level("INFO", logger="exomem.warmup")

    monkeypatch.setattr(warmup, "warmup_enabled", lambda: True, raising=True)
    monkeypatch.setattr(warmup, "warm_retrieval_catalog", lambda _root: True, raising=True)
    monkeypatch.setattr(warmup, "model_preload_allowed", lambda *_a: False, raising=True)
    monkeypatch.setattr(mode, "preload_cpu_caches", lambda: False, raising=True)

    durations = warmup.warm_all(root)

    assert "graph_snapshot" in durations, (
        "start-up must adopt the inherited snapshot even when the resource mode "
        f"skips CPU cache preloading: {sorted(durations)}"
    )
    assert "graph_snapshot_residue" in durations, (
        f"the adoption's residue must be recorded for the operator: {sorted(durations)}"
    )
    assert "graph snapshot adoption adopted=" in caplog.text, (
        "the adoption line is how a deploy is read back; without it the 0.83.1 "
        "diagnosis needed a source read instead of a log read"
    )
    assert root in find_module._RECALL_RESOLVER_CACHE, (
        "the resolver primer must run beside adoption: a process that never "
        "built one leaves every write bailing on resolver_snapshot_unavailable"
    )


def test_the_warm_up_adopts_before_it_builds_the_semantic_corpus(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writers are admitted on the corpus, so adoption cannot be behind it.

    Measured on the 0.84.1 personal service: `warm complete` listed the corpus
    build at 30-36 s on 4209 pages, and the graph handoff ran *after* it. The
    gate that admits governed writes waits on `semantic_corpus`, so the first
    write of the replacement worker was admitted into a process with no adopted
    lineage -- it fell back on `recall_delta_incomplete` and registered a
    whole-vault rebuild, which was still in flight 36 s later when adoption
    finally ran and declined its proof against the sidecar that rebuild was
    rewriting underneath it.

    Adoption is handoff correctness, not a cache, and it is sub-second to a few
    seconds. It belongs in the first seconds of the warm, ahead of everything a
    writer waits on, and the order is pinned here rather than commented.
    """
    from exomem import readiness, semantic_contract, warmup

    root = handoff_vault
    order: list[str] = []

    real_adopt = EpistemicGraphIndex.adopt_published_snapshot

    def traced_adopt(inner_self: EpistemicGraphIndex) -> object:
        order.append("graph_snapshot")
        return real_adopt(inner_self)

    def traced_corpus(*_args: object, **_kwargs: object) -> None:
        order.append("semantic_corpus")

    marked: list[str] = []
    real_mark = readiness.mark_ready

    def traced_mark(component: str) -> list:
        marked.append(component)
        return real_mark(component)

    monkeypatch.setattr(warmup, "warmup_enabled", lambda: True, raising=True)
    monkeypatch.setattr(warmup, "warm_retrieval_catalog", lambda _root: True, raising=True)
    monkeypatch.setattr(warmup, "model_preload_allowed", lambda *_a: False, raising=True)
    monkeypatch.setattr(warmup, "warm_caches", lambda *_a, **_kw: {}, raising=True)
    monkeypatch.setattr(mode, "preload_cpu_caches", lambda: False, raising=True)
    monkeypatch.setattr(
        EpistemicGraphIndex, "adopt_published_snapshot", traced_adopt, raising=True
    )
    monkeypatch.setattr(
        semantic_contract, "build_corpus_context", traced_corpus, raising=True
    )
    monkeypatch.setattr(readiness, "mark_ready", traced_mark, raising=True)

    warmup.warm_all(root)

    assert order == ["graph_snapshot", "semantic_corpus"], (
        "the graph handoff must run before the corpus build that admits "
        f"writers, not after it: {order}"
    )
    assert marked.index("graph_handoff") < marked.index("semantic_corpus"), (
        "the component a writer waits on must not be marked ready before the "
        f"one that gives its write a delta origin: {marked}"
    )


def test_start_up_adopts_the_snapshot_while_the_retrieval_catalog_is_repaired(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third gate that could skip adoption, and the reason it must not.

    `catalog_ready` exists so the disposable recall caches do not publish beside
    a detached repair owner rebuilding the *lexical* catalogue. The graph
    handoff is not that: it builds a process-local resolver and proves the graph
    sidecar, neither of which is the artifact under repair. Leaving adoption
    behind that gate means a worker that starts during a catalog repair admits
    writes with no delta origin -- the 0.84.1 failure, reached by a different
    door.
    """
    from exomem import warmup

    monkeypatch.setattr(warmup, "warmup_enabled", lambda: True, raising=True)
    monkeypatch.setattr(warmup, "warm_retrieval_catalog", lambda _root: False, raising=True)
    monkeypatch.setattr(warmup, "model_preload_allowed", lambda *_a: False, raising=True)
    monkeypatch.setattr(mode, "preload_cpu_caches", lambda: False, raising=True)

    durations = warmup.warm_all(handoff_vault)

    assert "graph_snapshot" in durations, (
        "a worker that starts while the retrieval catalog is under repair still "
        f"has to adopt the snapshot it inherited: {sorted(durations)}"
    )


def test_the_cache_preload_gate_still_skips_the_disposable_caches(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hoisting adoption out of the gate must not drag the caches out with it."""
    from exomem import warmup

    monkeypatch.setattr(warmup, "warmup_enabled", lambda: True, raising=True)
    assert warmup.warm_caches(handoff_vault, preload_cpu_caches=False) == {}


def test_a_rebuild_retarget_keeps_the_recall_resolver(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuild that retargets must not leave the next write without a resolver.

    A whole-vault rebuild whose projection moves under it drops every
    rebuildable cache and retries. Dropping the *recall* resolver there bought
    nothing -- every read of it revalidates the projection identity, and the
    graph's bounded repair revalidates the exact live checkpoint, so a stale
    entry can only miss -- while costing the next governed write a whole-vault
    rebuild of its own: `recall_resolver_snapshot_at_checkpoint` refuses to
    build on a miss by design. Measured in the reproduction: one coalesced
    follow-up rebuild running under later writes turned the next standalone
    write into a 14.7 s join.
    """
    root = handoff_vault
    find_module.recall_resolver_snapshot(root)
    assert root in find_module._RECALL_RESOLVER_CACHE, "the resolver must start warm"

    # Observed at the moment of the eviction. The rebuild's next attempt warms
    # the resolver again at the top of its loop, so a check after `rebuild_all`
    # returns would pass either way -- what costs the next governed write is the
    # window in between, which is where a concurrent write lands.
    survived: list[bool] = []
    real_unload = find_module.unload_ram_caches

    def observed_unload(*args: object, **kwargs: object) -> object:
        result = real_unload(*args, **kwargs)
        survived.append(root in find_module._RECALL_RESOLVER_CACHE)
        return result

    monkeypatch.setattr(find_module, "unload_ram_caches", observed_unload, raising=True)

    calls: list[int] = []
    real = EpistemicGraphIndex._resolver_source_versions

    def retarget_once(
        inner_self: EpistemicGraphIndex, resolver: object, membership: object, **kwargs: object
    ):
        calls.append(1)
        if len(calls) == 1:
            # Class C, first admitted cause: the supplied freshness identity did
            # not name the resolver bytes.
            return None
        return real(inner_self, resolver, membership, **kwargs)

    monkeypatch.setattr(
        EpistemicGraphIndex, "_resolver_source_versions", retarget_once, raising=True
    )

    EpistemicGraphIndex(root).rebuild_all()

    assert len(calls) >= 2, "the rebuild must have retargeted and retried"
    assert survived and all(survived), (
        "the retarget evicted the recall resolver, which sends the next governed "
        f"write down the whole-vault path (survived={survived})"
    )


# --- The promoted standby does not repeat its own warm (task 2.8) ------------


def _trace_warm(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    """Record which warm steps ran and which readiness components were marked."""
    from exomem import readiness, semantic_contract, warmup

    order: list[str] = []
    marked: list[str] = []
    real_mark = readiness.mark_ready

    def traced_corpus(*_args: object, **_kwargs: object) -> None:
        order.append("semantic_corpus")

    def traced_caches(*_args: object, **_kwargs: object) -> dict:
        order.append("lexical")
        return {}

    def traced_mark(component: str) -> list:
        marked.append(component)
        return real_mark(component)

    monkeypatch.setattr(warmup, "warmup_enabled", lambda: True, raising=True)
    monkeypatch.setattr(warmup, "warm_retrieval_catalog", lambda _root: True, raising=True)
    monkeypatch.setattr(warmup, "model_preload_allowed", lambda *_a: False, raising=True)
    monkeypatch.setattr(warmup, "warm_caches", traced_caches, raising=True)
    monkeypatch.setattr(mode, "preload_cpu_caches", lambda: False, raising=True)
    monkeypatch.setattr(
        warmup,
        "warm_graph_handoff",
        lambda _root: (order.append("graph_snapshot"), {})[1],
        raising=True,
    )
    monkeypatch.setattr(semantic_contract, "build_corpus_context", traced_corpus, raising=True)
    monkeypatch.setattr(readiness, "mark_ready", traced_mark, raising=True)
    return order, marked


def test_the_incremental_latency_oracle_refuses_a_single_sample() -> None:
    """The helper must not be usable as a per-request timing assertion.

    A median over one sample is that sample, so calling this with one write
    silently converts a series-shaped budget into the flakiest thing a test can
    assert. It reached CI that way once: the first write after a promotion at
    1.14 s against a 1.0 s median bound, on a write that was admitted and
    incremental exactly as designed. Refusing the call is cheaper than noticing
    the next one in a failing run.
    """
    with pytest.raises(AssertionError, match="shape of a series"):
        _assert_incremental_latency([0.01])


def test_a_cold_warm_carries_nothing_and_runs_every_step(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control. Subtracting a carried warm must not change a cold start."""
    from exomem import service_standby, warmup

    service_standby.reset_for_tests()
    order, marked = _trace_warm(monkeypatch)

    durations = warmup.warm_all(handoff_vault)

    assert order == ["graph_snapshot", "semantic_corpus", "lexical"]
    assert durations["carried_from_standby"] == 0.0
    for component in ("graph_handoff", "semantic_corpus", "lexical"):
        assert component in marked


def test_a_promoted_warm_repeats_neither_the_adoption_nor_the_corpus_build(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 0.85.0 defect, as a test.

    A cutover measured at 1672.9 ms by the supervisor handed back a worker that
    then spent ~30 s refusing governed writes `MUTATION_WARMING
    warming_component=semantic_corpus`, because its own `warm_all` repeated in
    full the warm its standby had already finished in the same process: a second
    `graph snapshot adoption adopted=True residue=0` at 10:23:03 after the
    standby's at 10:22:13, and a 9935.2 ms corpus build after the standby's
    8516.1 ms one. Promotion re-validated the snapshot (`snapshot: 'current'`)
    46.8 ms before that, so nothing about the second adoption could have
    returned a different answer.
    """
    from exomem import service_standby, warmup

    monkeypatch.setattr(
        service_standby,
        "carried_warm_components",
        lambda: frozenset({"graph_handoff", "semantic_corpus", "lexical"}),
        raising=True,
    )
    order, marked = _trace_warm(monkeypatch)

    durations = warmup.warm_all(handoff_vault)

    assert order == [], f"the promoted worker repeated a step it already ran: {order}"
    assert durations["carried_from_standby"] == 3.0
    # Carried is not the same as skipped: every component a writer waits on is
    # still marked, or the gate would never open at all.
    for component in ("graph_handoff", "semantic_corpus", "lexical"):
        assert component in marked


def test_an_uncarried_component_is_still_warmed_after_promotion(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial carry subtracts exactly what it names and nothing else."""
    from exomem import service_standby, warmup

    monkeypatch.setattr(
        service_standby,
        "carried_warm_components",
        lambda: frozenset({"semantic_corpus"}),
        raising=True,
    )
    order, _marked = _trace_warm(monkeypatch)

    warmup.warm_all(handoff_vault)

    assert order == ["graph_snapshot", "lexical"], (
        "a promotion whose snapshot verdict was not `current` has to adopt "
        f"again, and only the corpus is carried: {order}"
    )


def test_the_carried_components_are_ready_before_the_warm_thread_starts(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`begin_warm` clears every event, which is what refused the 10:23 writes.

    The admission gate waits on `graph_handoff` and `semantic_corpus`. Marking
    them on the warm thread would reopen the same window the carry exists to
    close, so `start_background` marks them synchronously and this pins that: by
    the time it returns -- before any request can be served -- the gate is open.
    """
    from exomem import readiness, service_standby, warmup

    monkeypatch.setattr(
        service_standby,
        "carried_warm_components",
        lambda: frozenset({"graph_handoff", "semantic_corpus"}),
        raising=True,
    )
    # A warm body that never finishes, so the only thing that can have marked
    # the components is the synchronous carry-forward.
    started = threading.Event()
    release = threading.Event()

    def _blocked(_root: Path) -> dict:
        started.set()
        release.wait(timeout=30)
        return {}

    monkeypatch.setattr(warmup, "warm_all", _blocked, raising=True)
    readiness.reset()
    try:
        thread = warmup.start_background(handoff_vault)
        assert readiness.is_ready("graph_handoff") is True
        assert readiness.is_ready("semantic_corpus") is True
        assert started.wait(timeout=30) is True
    finally:
        release.set()
        thread.join(timeout=30)
        readiness.reset()


def test_a_cold_start_marks_nothing_ready_before_its_warm_runs(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the above: carrying nothing must not open the gate early."""
    from exomem import readiness, service_standby, warmup

    service_standby.reset_for_tests()
    started = threading.Event()
    release = threading.Event()

    def _blocked(_root: Path) -> dict:
        started.set()
        release.wait(timeout=30)
        return {}

    monkeypatch.setattr(warmup, "warm_all", _blocked, raising=True)
    readiness.reset()
    try:
        thread = warmup.start_background(handoff_vault)
        assert readiness.is_ready("graph_handoff") is False
        assert readiness.is_ready("semantic_corpus") is False
        assert started.wait(timeout=30) is True
    finally:
        release.set()
        thread.join(timeout=30)
        readiness.reset()
