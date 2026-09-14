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


def _watcher_cycle_budget(watcher: file_watcher.FileWatcher) -> float:
    """The watcher's own bound for the cycle this test waits on.

    Taken from the watcher rather than chosen: a cycle is the debounce window
    plus the drain's compare-and-ack, and `file_watcher` documents that step's
    worst case as `GRAPH_WITHDRAWAL_RETRY_SECONDS` plus one mutation-coordinator
    timeout -- about 20 s at today's defaults, and precisely the step that
    stalls when SQLite is contended ("sidecar WAL pragmas failed (database is
    locked)" in the shard that flaked). A fixed few-second window is shorter
    than the bound the code itself declares, so under load it counted cycles
    that were legitimately still in flight and called them missing.

    Spent as one budget across the whole loop rather than per edit, so a
    contended host cannot push this test past the suite's per-test timeout: the
    quiet case costs about a second an edit and never approaches it.
    """
    return (
        watcher._debounce_seconds()
        + file_watcher.GRAPH_WITHDRAWAL_RETRY_SECONDS
        + mutation_lock._DEFAULT_TIMEOUT_SECONDS
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
    not. So observation keeps a per-edit floor of two debounce windows even when
    the shared budget is spent -- an exhausted budget must not turn "never
    waited" into "never observed" -- while settling draws only on what is left.
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


def _assert_incremental_latency(
    acknowledgements: list[float], *, bound: float = ACK_BOUND_SECONDS
) -> None:
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


def _drain_repair_queue(root: Path, watcher: file_watcher.FileWatcher) -> int:
    """Publish the observed external events, then drain the durable graph queue."""
    watcher._flush()
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
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`exp5.py external`, as a test: six writes, each behind one unattributed edit.

    The oracle is the reproduction's, and it is about the *write path*: every
    acknowledgement stays inside the incremental bound (3.4-7.9 s per write on
    the defective tree, because each one waited out a whole-vault rebuild it had
    been fenced into), no write is fenced by an unattributed event beyond the
    echo race a running watcher cannot exclude, and the repair queue drains to
    zero.

    Whole-vault passes are deliberately NOT counted here. A running watcher
    repairs the unattributed edits itself, and on a fixture this size its own
    incremental pass legitimately bails to a full one; counting process-wide
    passes would measure the watcher's repair of the edits this test invents
    rather than what the governed writes cost. The rebuild *count* is pinned by
    `test_a_write_dispatched_behind_an_unrepaired_mark_stays_incremental` and by
    the cross-process test, which have no second writer.
    """
    root = live_handoff_vault
    generated = root / GENERATED
    caplog.set_level("INFO", logger="exomem.epistemic_graph")

    acknowledgements: list[float] = []
    observed_marks = 0
    unsettled = 0
    # One deadline for the whole loop, derived from the watcher's own bound.
    budget_expires = time.monotonic() + _watcher_cycle_budget(live_watcher)
    for i in range(LIVE_WRITE_COUNT):
        # Disjoint from the written notes, and inside this fixture's range.
        external = generated / f"generated-note-{LIVE_NOTE_COUNT // 2 + i:04d}.md"
        external.write_text(
            external.read_text(encoding="utf-8") + f"\n- external editor touch {i}\n",
            encoding="utf-8",
        )
        # Let the observer deliver the event and the watcher finish with it, the
        # way the reproduction spaces its writes.
        observed, settled = _observe_and_settle(root, live_watcher, deadline=budget_expires)
        observed_marks += int(observed)
        unsettled += int(not settled)
        acknowledgements.append(
            _governed_write(root, generated / f"generated-note-{i:04d}.md", f"governed {i}")
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
    fenced = caplog.text.count("reason=external_event_covers_these_paths")
    # One fence is the race this shape cannot exclude -- a running watcher can
    # deliver a write's own echo after its publication intent has closed, and a
    # mark on the path being written is then correct. Each cycle the watcher had
    # not finished with buys one more, because a write dispatched behind an
    # unretired mark is fenced by design; that is a contended host, not the
    # loop. Every write being fenced is the loop this change removed, and no
    # tolerance reaches that.
    assert fenced <= 1 + unsettled, (
        f"{fenced} of {LIVE_WRITE_COUNT} governed writes were fenced by an "
        f"unattributed event with {unsettled} watcher cycle(s) still in flight: "
        f"{rendered}"
    )
    assert fenced < LIVE_WRITE_COUNT, f"every governed write was fenced: {rendered}"
    _assert_incremental_latency(acknowledgements, bound=LIVE_ACK_BOUND_SECONDS)
    assert _drain_repair_queue(root, live_watcher) == 0, "the graph repair queue never drained"


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
stored_before = None
index_for_adoption = EpistemicGraphIndex(root)
opened = index_for_adoption._open_read_snapshot(require_current_projection=False)
if opened is not None:
    stored_before = index_for_adoption._stored_recall_checkpoint(opened)
    opened.close()
adoption = index_for_adoption.adopt_published_snapshot()
adopted = bool(adoption)
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


def test_the_warm_up_adopts_after_the_seed_and_the_resolver(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warm-up order is load-bearing, so it is pinned rather than commented.

    `adopt_recall_origin` refuses a cold scope, and the watcher's seed replaces
    the registry maps wholesale -- an adoption recorded before that seed is
    dropped with the map it described. The graph step therefore has to run after
    the seed, and after the resolver step whose cache it reuses.
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

    durations = warmup.warm_caches(root, preload_models=False, preload_cpu_caches=True)

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
