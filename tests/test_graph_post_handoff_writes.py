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
#: Measured on this fixture: an incremental governed write acknowledges in
#: ~0.2 s, a whole-vault rebuild in seconds. The bound sits between them.
ACK_BOUND_SECONDS = 1.0


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
    slowest = max(acknowledgements)
    assert slowest < ACK_BOUND_SECONDS, (
        f"slowest acknowledgement {slowest:.2f}s exceeds the incremental bound "
        f"{ACK_BOUND_SECONDS}s: {[round(seconds, 2) for seconds in acknowledgements]}"
    )
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
