"""A busy graph sidecar is not a broken graph lineage.

`acknowledgement_state` reads the live `.graph.sqlite`, and it used to answer
`malformed` for every `sqlite3.Error` -- including `database is locked`. That
single conflation was enough to stop the graph converging under load, because
of what the callers do with the answer:

* `classify_epoch` turns a non-readable acknowledgement into `unavailable`;
* `_admit_epoch_inputs`, on the canonical write path, then raises
  `GRAPH_SYNC_LINEAGE_CONFLICT` -- "reconcile the graph epoch" -- and the write
  fails outright;
* `index_sync._drain_graph_work` refuses the whole deferred queue.

So a sidecar that some other thread was writing *this instant* -- the expected
state while a rebuild publishes -- was reported as damaged history and cost the
caller their write. A 400-page concurrent-write run reproduced it in under a
minute and left the queue refusing to drain afterwards.

The second half of the same failure needs no error at all. A canonical batch
installs its generation floor before its checkpoint, so any unsynchronised
sample taken inside a batch sees `floor.generation == checkpoint.generation + 1`
and classifies `recoverable`. Under a live writer that is most samples, and the
drain simply stopped: the same run measured the graph eleven generations behind
by the end, catching up only once writes stopped.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from exomem import epistemic_graph, graph_sync
from exomem.kbdir import kb_dirname


def _kb(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / kb_dirname()).mkdir(parents=True)
    return root


def _sidecar_with_meta(root: Path) -> Path:
    path = epistemic_graph.sidecar_path(root)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE graph_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    return path


def _install_epoch(root: Path, *, floor: int, checkpoint: int) -> graph_sync.GraphSyncCheckpoint:
    """Write a floor/checkpoint pair straight to disk, no batch involved."""
    committed = graph_sync.GraphSyncCheckpoint.create(
        generation=checkpoint,
        mutation_id="0123456789abcdef01234567",
        paths=(("Knowledge Base/Notes/example.md", "a" * 64),),
        created_paths=(),
    )
    graph_sync.floor_path(root).write_text(
        graph_sync.GraphSyncGenerationFloor.create(floor).render(), encoding="utf-8"
    )
    graph_sync.checkpoint_path(root).write_text(committed.render(), encoding="utf-8")
    return committed


def test_a_locked_sidecar_reads_busy_rather_than_malformed(tmp_path: Path) -> None:
    """The real thing: hold SQLite's write lock and read the acknowledgement.

    Deliberately a genuine lock rather than a raised sentinel. The whole defect
    lived in how one `sqlite3.OperationalError` was classified, and only a real
    contending transaction proves the message this code must recognise is the
    message SQLite actually produces on this platform.
    """
    root = _kb(tmp_path)
    path = _sidecar_with_meta(root)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        status, acknowledgement = graph_sync.acknowledgement_state(root)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert status == "busy"
    assert acknowledgement is None


def test_a_busy_acknowledgement_classifies_busy_not_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _kb(tmp_path)
    _install_epoch(root, floor=4, checkpoint=4)
    monkeypatch.setattr(graph_sync, "acknowledgement_state", lambda _root: ("busy", None))
    assert graph_sync.classify_epoch(root).kind == "busy"


def test_a_busy_acknowledgement_never_reports_a_lineage_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical write path admits the epoch instead of refusing the write.

    `GRAPH_SYNC_LINEAGE_CONFLICT` tells the caller to reconcile a damaged graph.
    Nothing here is damaged, so if this ever raises again it must at least raise
    the retryable code -- but the correct answer is not to raise at all.
    """
    root = _kb(tmp_path)
    _install_epoch(root, floor=4, checkpoint=4)
    monkeypatch.setattr(graph_sync, "acknowledgement_state", lambda _root: ("busy", None))
    epoch = graph_sync._admit_epoch_inputs(root)
    assert epoch.kind == "coherent"
    assert epoch.acknowledgement is None


def test_a_busy_acknowledgement_still_advances_the_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admitting without the ack is safe because the floor already dominates it.

    `next_checkpoint` folds the acknowledged generation into a `max` with the
    floor, and every published acknowledgement covers a checkpoint that
    installed a floor at its own generation. Dropping the term therefore cannot
    reissue a generation -- which is the only thing it was protecting.
    """
    root = _kb(tmp_path)
    _install_epoch(root, floor=9, checkpoint=9)
    monkeypatch.setattr(graph_sync, "acknowledgement_state", lambda _root: ("busy", None))
    epoch = graph_sync._admit_epoch_inputs(root)
    issued = graph_sync.next_checkpoint(
        current=epoch.checkpoint,
        acknowledged_generation=0,
        floor_generation=epoch.floor.generation if epoch.floor is not None else 0,
        mutation_id="0123456789abcdef01234568",
        paths=[("Knowledge Base/Notes/example.md", "b" * 64)],
        created_paths=[],
    )
    assert issued.generation == 10


def test_a_permanently_busy_epoch_raises_retry_not_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the floor itself is unusable, the busy read must not upgrade to a
    lineage verdict it did not earn."""
    root = _kb(tmp_path)
    graph_sync.floor_path(root).write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(graph_sync, "acknowledgement_state", lambda _root: ("busy", None))
    with pytest.raises(graph_sync.GraphEpochUnreadable) as raised:
        graph_sync._admit_epoch_inputs(root)
    assert raised.value.code == "GRAPH_SYNC_EPOCH_BUSY"
    assert not isinstance(raised.value, graph_sync.GraphEpochIncoherent)


def test_the_drain_re_reads_a_mid_batch_epoch_under_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A floor one ahead of its checkpoint is a batch in flight, not a fault.

    The first sample sees the batch's interior and classifies `recoverable`;
    acquiring the canonical boundary waits the batch out, so the second sample
    sees the settled epoch. Without the re-read the deferred queue stops
    draining for as long as anything is writing.
    """
    root = _kb(tmp_path)
    _install_epoch(root, floor=6, checkpoint=5)
    index = epistemic_graph.EpistemicGraphIndex(root)
    samples: list[str] = []

    def classify(vault_root: Path) -> graph_sync.GraphEpochState:
        samples.append("sampled")
        if len(samples) == 1:
            return graph_sync.GraphEpochState("recoverable", None, None, None)
        return graph_sync.GraphEpochState("coherent", None, None, None)

    monkeypatch.setattr(graph_sync, "classify_epoch", classify)
    assert index.epoch_admits_incremental_repair() is True
    assert len(samples) == 2


def test_the_drain_still_refuses_an_epoch_that_stays_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-read is a coalescing window, not a way past the guard."""
    root = _kb(tmp_path)
    _install_epoch(root, floor=6, checkpoint=5)
    index = epistemic_graph.EpistemicGraphIndex(root)
    monkeypatch.setattr(
        graph_sync,
        "classify_epoch",
        lambda _root: graph_sync.GraphEpochState("unavailable", None, None, None),
    )
    assert index.epoch_admits_incremental_repair() is False
