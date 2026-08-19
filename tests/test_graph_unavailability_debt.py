"""A graph no reader can open is debt, even when nothing is holding a signal.

The drain recognised two kinds of debt: receipts in the durable queue, and a
persisted read barrier left by a rebuild that stopped. A rebuild that exhausts
its publication attempts leaves neither. The graph is unreadable, the queue is
empty, no barrier stands -- so the drain logged "graph settled" and went idle
against a graph every read answered `graph sidecar unavailable` for.

A product E2E run on `main` caught the whole sequence: generation 1 published,
generation 2 died Class C (`the recall projection identity moved across the
pass`), generation 6 ended `GRAPH_SYNC_STABILIZATION_EXHAUSTED`, and from there
the queue reported settling three times in five seconds while the reader saw
nothing, until the run failed on its 120-second convergence wait. That job also
fails on `main`, so it was gating merges rather than describing a branch.

These pin the third kind of debt and, as importantly, the two states that must
*not* be debt -- a graph that does not exist yet and one an operator switched
off both poll forever if they count.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem import deferred_index, epistemic_graph, graph_drain


@pytest.fixture(autouse=True)
def _stop_the_daemon():
    yield
    graph_drain.stop()
    graph_drain._DEBT.clear()


def _vault(tmp_path: Path) -> Path:
    notes = tmp_path / "Knowledge Base" / "Notes" / "Insights"
    notes.mkdir(parents=True)
    (notes / "lantern.md").write_text("# Lantern\n\nA body.\n", encoding="utf-8")
    return tmp_path


def _built(tmp_path: Path) -> Path:
    root = _vault(tmp_path)
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    return root


def _strand_the_projection(root: Path) -> None:
    """Leave the sidecar readable but not current, and leave no barrier.

    This is the shape a publication-exhausted rebuild leaves behind: the stored
    recall projection identity no longer matches the vault, so
    `_open_read_snapshot` refuses and `available()` is False, while
    `reads_suspended()` stays False because nothing wrote a barrier. Reaching it
    by writing the meta row directly is deliberate -- the race that produces it
    on CI is not reproducible on demand, and the state it produces is.
    """
    conn = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        with conn:
            conn.execute(
                "UPDATE graph_meta SET value = ? WHERE key = ?",
                ("stranded-projection", "recall_projection_identity"),
            )
    finally:
        conn.close()


def test_the_stranded_state_is_unreadable_with_no_barrier(tmp_path: Path) -> None:
    """The premise, asserted rather than assumed.

    If a barrier were standing here, recovery would already have handled it and
    none of the rest of this file would be about anything.
    """
    root = _built(tmp_path)
    _strand_the_projection(root)

    index = epistemic_graph.EpistemicGraphIndex(root)
    assert index.available() is False
    assert index.reads_suspended() is False


def test_an_unreadable_graph_is_debt(tmp_path: Path) -> None:
    """The fix, at the one place the daemon decides whether to do anything."""
    root = _built(tmp_path)
    _strand_the_projection(root)

    assert graph_drain._queue_pending(root) is False
    assert graph_drain._barrier_pending(root) is False
    assert graph_drain._availability_pending(root) is True
    assert graph_drain._pending(root) is True


def test_a_vault_with_no_graph_yet_is_not_debt(tmp_path: Path) -> None:
    """Nothing to repair is not the same as something to repair.

    Counting it would make every vault that has never built a graph poll for the
    life of the process, which is worse than the defect being fixed.
    """
    root = _vault(tmp_path)

    assert graph_drain._availability_pending(root) is False
    assert graph_drain._pending(root) is False


def test_a_disabled_graph_is_not_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator's switch is a decision, and the daemon does not argue."""
    root = _built(tmp_path)
    _strand_the_projection(root)
    monkeypatch.setattr(epistemic_graph, "graph_enabled", lambda: False)

    assert graph_drain._availability_pending(root) is False


def test_the_drain_queues_a_rebuild_and_the_graph_comes_back(tmp_path: Path) -> None:
    """End to end on the real state: from unreadable to readable, unattended.

    Two passes by construction -- the first states the debt durably, the second
    is an ordinary drain of it. That split is the point: the rebuild keeps
    running on the one path that runs rebuilds.
    """
    root = _built(tmp_path)
    _strand_the_projection(root)

    assert graph_drain._work_once(root) == 1
    assert deferred_index.graph_full_rebuild_pending(root) is not None
    assert epistemic_graph.EpistemicGraphIndex(root).available() is False

    graph_drain._work_once(root)

    assert epistemic_graph.EpistemicGraphIndex(root).available() is True
    assert graph_drain._pending(root) is False


def test_a_standing_marker_is_not_queued_a_second_time(tmp_path: Path) -> None:
    """The same debt counted twice would read as progress and skip the backoff."""
    root = _built(tmp_path)
    _strand_the_projection(root)
    assert graph_drain._request_full_rebuild(root) is True

    assert graph_drain._request_full_rebuild(root) is False


def test_a_standing_barrier_is_repaired_rather_than_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repair is cheaper and knows how to decline, so it keeps precedence.

    Queueing a whole-vault rebuild alongside it would pay for the most
    expensive operation in the system on exactly the signal that already has a
    handler.
    """
    root = _built(tmp_path)
    _strand_the_projection(root)
    requested: list[Path] = []
    monkeypatch.setattr(graph_drain, "_barrier_pending", lambda _root: True)
    monkeypatch.setattr(graph_drain, "_recover_once", lambda _root: True)
    monkeypatch.setattr(
        graph_drain, "_request_full_rebuild", lambda root: requested.append(root) or True
    )

    assert graph_drain._work_once(root) == 1
    assert requested == []
