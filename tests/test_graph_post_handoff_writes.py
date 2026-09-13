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

from exomem import deferred_index, epistemic_graph, file_watcher, freshness, graph_sync, index_sync
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

GENERATED = "Knowledge Base/Notes/Generated"
#: Large enough that a whole-vault pass is an order of magnitude more work than
#: one incremental write, small enough to seed inside a test.
NOTE_COUNT = 200
WRITE_COUNT = 10
#: Measured on this fixture (246 markdown files): an incremental governed write
#: acknowledges in 0.36-0.76 s, and 1.08 s worst observed on a host running other
#: suites at the same time. A whole-vault rebuild of the same fixture costs
#: 1.4-4.7 s. The wall-clock bound is deliberately loose, because the assertion
#: that carries the weight is the rebuild counter; this one exists so a repair
#: that stopped rebuilding but started doing something else expensive still fails.
ACK_BOUND_SECONDS = 2.0
#: The typical write, held to the measured range rather than the safety margin.
ACK_MEDIAN_BOUND_SECONDS = 1.0


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


@pytest.fixture
def handoff_vault(vault: Path) -> Iterator[Path]:
    """A seeded vault with a published graph, as a replacement worker inherits it."""
    generated = vault / GENERATED
    generated.mkdir(parents=True, exist_ok=True)
    names = [f"generated-note-{i:04d}" for i in range(NOTE_COUNT)]
    for i, name in enumerate(names):
        links = [names[(i + offset) % NOTE_COUNT] for offset in (1, 7, 23)]
        (generated / f"{name}.md").write_text(_note(i, links), encoding="utf-8")
    _seed_live_freshness(vault)
    EpistemicGraphIndex(vault).rebuild_all()
    epistemic_graph.clear_publication_memos()
    yield vault
    graph_sync.drain_active_rebuilds(timeout=30.0)
    epistemic_graph.clear_publication_memos()


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


def _assert_incremental_latency(acknowledgements: list[float]) -> None:
    rendered = [round(seconds, 2) for seconds in acknowledgements]
    slowest = max(acknowledgements)
    assert slowest < ACK_BOUND_SECONDS, (
        f"slowest acknowledgement {slowest:.2f}s exceeds the incremental bound "
        f"{ACK_BOUND_SECONDS}s: {rendered}"
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


def test_writes_after_a_worker_replacement_stay_incremental(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ten governed writes, each preceded by one unattributed edit, rebuild nothing."""
    root = handoff_vault
    generated = root / GENERATED
    watcher = file_watcher.FileWatcher(root, debounce_seconds=0.2)
    spy = _RebuildSpy(monkeypatch)

    acknowledgements: list[float] = []
    fenced_at_dispatch = 0
    for i in range(WRITE_COUNT):
        _external_edit(watcher, generated / f"generated-note-{100 + i:04d}.md", f"external {i}")
        if freshness.external_pending(root):
            fenced_at_dispatch += 1
        acknowledgements.append(
            _governed_write(root, generated / f"generated-note-{i:04d}.md", f"governed {i}")
        )

    assert fenced_at_dispatch == WRITE_COUNT, (
        "the reproduction requires every write to dispatch with an unrepaired "
        "external event outstanding"
    )
    assert spy.count == 0, (
        f"{spy.count} whole-vault rebuild(s) ran for {WRITE_COUNT} governed writes: "
        f"passes={[round(seconds, 2) for seconds in spy.passes]} "
        f"acknowledgements={[round(seconds, 2) for seconds in acknowledgements]}"
    )
    _assert_incremental_latency(acknowledgements)
    assert _drain_repair_queue(root, watcher) == 0, "the graph repair queue never drained"


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
    """A sidecar whose rows and lineage are intact is advanced, not rebuilt.

    The availability marker is a reader's claim, and every deferral drops it.
    While the maintenance readers demanded it too, the only way to get it back
    was a whole-vault pass -- the incremental refresh that exists to republish
    it could not open the sidecar to do so.
    """
    import sqlite3

    root = handoff_vault
    index = EpistemicGraphIndex(root)
    connection = sqlite3.connect(index.path)
    try:
        with connection:
            connection.execute(
                "DELETE FROM graph_meta WHERE key = 'recall_projection_identity'"
            )
    finally:
        connection.close()
    assert index.available() is False, "the withdrawn marker must fence public readers"

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

    elapsed = _governed_write(root, root / GENERATED / "generated-note-0003.md", "withdrawn")

    assert states == ["available"], (
        "the maintenance probe must read a fenced-but-intact sidecar and prove its "
        f"lineage, got {states}"
    )
    assert spy.count == 0, "a withdrawn availability marker scheduled a whole-vault rebuild"
    assert elapsed < ACK_BOUND_SECONDS
    assert EpistemicGraphIndex(root).available() is True, (
        "the incremental pass must republish the availability marker"
    )


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
import json, sys, time
from pathlib import Path

sys.path.insert(0, {tests_dir!r})
from test_graph_post_handoff_writes import (  # noqa: E402
    GENERATED,
    WRITE_COUNT,
    _governed_write,
    _seed_live_freshness,
)

from exomem import epistemic_graph, file_watcher  # noqa: E402
from exomem.epistemic_graph import EpistemicGraphIndex  # noqa: E402

root = Path(sys.argv[2])
phase = sys.argv[1]
generated = root / GENERATED

if phase == "outgoing":
    _seed_live_freshness(root)
    EpistemicGraphIndex(root).rebuild_all()
    for index in range(3):
        _governed_write(root, generated / f"generated-note-{{index + 60:04d}}.md", "outgoing")
    print(json.dumps({{"available": EpistemicGraphIndex(root).available()}}))
    raise SystemExit(0)

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
print(
    json.dumps(
        {{
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

    It also names the one whole-vault pass phase 1 does *not* remove. A cold
    process cannot compute a delta from a checkpoint another process instance
    published, so its first write adopts the inherited snapshot the only way
    this tree can today -- `recall_delta_incomplete`, one whole-vault pass -- and
    every write after it is incremental. That cost is independent of this
    change: a replacement that observes no external event at all pays exactly
    the same one. Moving it off the serving path is `seamless-managed-worker-handoff`
    phase 2 (D7: the standby proves or adopts the snapshot while the old worker
    still serves). What phase 1 owns is that it happens *once*, not once per
    write, which is what this test pins.
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

    per_write = report["per_write_rebuilds"]
    assert sum(per_write[1:]) == 0, (
        "every write after the snapshot adoption must be incremental; rebuilds "
        f"per write were {per_write}"
    )
    assert report["rebuilds"] <= 1, (
        f"the replacement process rebuilt the whole vault {report['rebuilds']} time(s) "
        f"for {WRITE_COUNT} writes: {report['acknowledgements']}"
    )
    _assert_incremental_latency(report["acknowledgements"][1:])
