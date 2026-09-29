"""An inherited graph sidecar is proved once, not forever (#1454).

A restart leaves the process with a sidecar stamped by an earlier registry
instance. `_open_read_snapshot` takes its cheap path only at the exact live
checkpoint, and an inherited checkpoint never is one -- adoption makes it a
delta origin, not the current checkpoint -- so every `available()` call paid
the O(corpus) source-bytes proof again. Three callers did it in a loop: the
graph drain's availability arm, the readiness/coordination probe, and graph
recall. On a 9,300-file vault the process never went idle and `ask_memory`
with the graph lane never returned.

Each test builds the graph in one process lineage and opens it from a fresh
one (`freshness.clear()` mints a new instance id, the signal a real restart
gives), then counts the proof.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from exomem import (
    deferred_index,
    epistemic_graph,
    freshness,
    graph_drain,
    graph_sync,
    index_sync,
    lexstore,
    writer_lease,
)
from exomem import find as find_module
from exomem import runtime_readiness as readiness_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

GENERATED = "Knowledge Base/Notes/Generated"
NOTE_COUNT = 40
#: Graph recall must answer well inside this while a proof is held open. The
#: held proof waits far longer, so a recall that waits on it cannot pass.
RECALL_BOUND_SECONDS = 5.0
HELD_PROOF_SECONDS = 8.0


def _note(index: int, links: list[str]) -> str:
    body = [
        "---",
        "type: pattern",
        "status: active",
        "created: 2026-01-12",
        "updated: 2026-06-10",
        "sources: []",
        "pattern_type: architectural",
        f"tags: [generated, batch{index % 5}]",
        "---",
        "",
        f"# Generated note {index}",
        "",
        "## Problem",
        f"Synthetic inherited sidecar body {index}. " * 3,
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


def _restart(root: Path) -> None:
    """The new process: a fresh registry instance whose watcher seeds live."""
    freshness.clear()
    _seed_live_freshness(root)


@pytest.fixture
def inherited_vault(vault: Path) -> Iterator[Path]:
    generated = vault / GENERATED
    generated.mkdir(parents=True, exist_ok=True)
    names = [f"generated-note-{i:04d}" for i in range(NOTE_COUNT)]
    for i, name in enumerate(names):
        links = [names[(i + offset) % NOTE_COUNT] for offset in (1, 3, 11)]
        (generated / f"{name}.md").write_text(_note(i, links), encoding="utf-8")
    _seed_live_freshness(vault)
    EpistemicGraphIndex(vault).rebuild_all()
    graph_sync.drain_active_rebuilds(timeout=30.0)
    epistemic_graph.clear_publication_memos()
    _restart(vault)
    yield vault
    graph_sync.drain_active_rebuilds(timeout=30.0)
    epistemic_graph.clear_publication_memos()


def _set_stored_source_hash(root: Path, rel: str, source_hash: str) -> str:
    """Rewrite one file row's source hash in place; return the previous one."""
    conn = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        (previous,) = conn.execute(
            "SELECT source_hash FROM graph_nodes WHERE kind = 'file' AND path = ?", (rel,)
        ).fetchone()
        conn.execute(
            "UPDATE graph_nodes SET source_hash = ? WHERE kind = 'file' AND path = ?",
            (source_hash, rel),
        )
        conn.commit()
    finally:
        conn.close()
    return str(previous)


STALE_REL = f"{GENERATED}/generated-note-0009.md"


@pytest.fixture
def stale_inherited_vault(vault: Path) -> Iterator[Path]:
    """The same inheritance, with a sidecar whose rows disagree with the bytes.

    Only a sidecar whose stored projection identity still matches disk reaches
    the proof at all -- a moved identity declines on a metadata compare -- so
    the stale row is written into the sidecar rather than into the Markdown.
    """
    generated = vault / GENERATED
    generated.mkdir(parents=True, exist_ok=True)
    names = [f"generated-note-{i:04d}" for i in range(NOTE_COUNT)]
    for i, name in enumerate(names):
        links = [names[(i + offset) % NOTE_COUNT] for offset in (1, 3, 11)]
        (generated / f"{name}.md").write_text(_note(i, links), encoding="utf-8")
    _seed_live_freshness(vault)
    EpistemicGraphIndex(vault).rebuild_all()
    graph_sync.drain_active_rebuilds(timeout=30.0)
    epistemic_graph.clear_publication_memos()
    vault.joinpath(".stale-hash").write_text(
        _set_stored_source_hash(vault, STALE_REL, "0" * 64), encoding="utf-8"
    )
    _restart(vault)
    yield vault
    graph_sync.drain_active_rebuilds(timeout=30.0)
    epistemic_graph.clear_publication_memos()


def _count_proofs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the thread each O(corpus) proof ran on."""
    calls: list[str] = []
    real_proof = EpistemicGraphIndex._snapshot_sources_match_disk

    def counted(inner_self: EpistemicGraphIndex, conn: object, **kwargs: object) -> bool:
        calls.append(threading.current_thread().name)
        return real_proof(inner_self, conn, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_snapshot_sources_match_disk", counted, raising=True)
    return calls


def test_repeated_availability_proves_an_inherited_sidecar_once(
    inherited_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = inherited_vault
    proofs = _count_proofs(monkeypatch)

    for _ in range(5):
        assert EpistemicGraphIndex(root).available() is True

    assert len(proofs) == 1, (
        f"an unchanged inherited sidecar was proved {len(proofs)} times; the "
        "verdict holds until the stored checkpoint or the freshness identity moves"
    )


def test_a_declined_proof_is_remembered_until_the_sidecar_moves(
    stale_inherited_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = stale_inherited_vault
    proofs = _count_proofs(monkeypatch)

    for _ in range(5):
        assert EpistemicGraphIndex(root).available() is False
    assert len(proofs) == 1, f"a stale sidecar was re-proved {len(proofs)} times"

    # Repairing the sidecar is a new question, and gets one new proof.
    original = root.joinpath(".stale-hash").read_text(encoding="utf-8")
    _set_stored_source_hash(root, STALE_REL, original)
    for _ in range(3):
        assert EpistemicGraphIndex(root).available() is True
    assert len(proofs) == 2


def test_a_remembered_verdict_never_outlives_a_moved_freshness_identity(
    inherited_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = inherited_vault
    _count_proofs(monkeypatch)
    assert EpistemicGraphIndex(root).available() is True

    moved = root / GENERATED / "generated-note-0010.md"
    moved.write_text(moved.read_text(encoding="utf-8") + "\nanother edit\n", encoding="utf-8")
    _seed_live_freshness(root)
    assert EpistemicGraphIndex(root).available() is False


def test_the_drain_schedules_one_rebuild_for_a_declined_inherited_sidecar(
    stale_inherited_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = stale_inherited_vault
    proofs = _count_proofs(monkeypatch)
    marks: list[int] = []
    real_mark = deferred_index.mark_graph_full_rebuild

    def counted_mark(vault_root: Path, **kwargs: object) -> object:
        marks.append(1)
        return real_mark(vault_root, **kwargs)

    monkeypatch.setattr(deferred_index, "mark_graph_full_rebuild", counted_mark)
    # Hold the rebuild itself: the marker stands, as it does while a
    # whole-vault pass waits for its quiet window or is refused.
    monkeypatch.setattr(index_sync, "drain_graph_work", lambda *_a, **_k: 0)

    for _ in range(5):
        assert graph_drain._pending(root) is True
        graph_drain._work_once(root)

    assert len(marks) == 1, f"the drain queued {len(marks)} rebuilds for one stale sidecar"
    assert deferred_index.graph_full_rebuild_pending(root) is not None
    assert len(proofs) <= 1, (
        f"the drain re-proved a declined sidecar {len(proofs)} times across five "
        "passes; the rebuild it scheduled is the repair, not another proof"
    )


def test_the_readiness_probe_never_runs_the_proof(
    inherited_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = inherited_vault
    proofs = _count_proofs(monkeypatch)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)

    generation = graph_sync.status(root)["generation"]
    assert manager.status(root)["graph_sync"] == {"state": "unproven", "generation": generation}
    readiness = readiness_module.runtime_readiness(mcp_tool_surface_sha256="a" * 64)
    assert readiness["coordination"]["graph_sync"] == {
        "state": "unproven",
        "generation": generation,
    }
    assert proofs == [], "the readiness probe ran the O(corpus) proof"

    # Once something has proved the sidecar, readiness reports that verdict.
    assert EpistemicGraphIndex(root).available() is True
    assert manager.status(root)["graph_sync"] == {"state": "current", "generation": generation}
    assert len(proofs) == 1


def test_the_readiness_probe_reports_a_remembered_decline_without_proving(
    stale_inherited_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = stale_inherited_vault
    proofs = _count_proofs(monkeypatch)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    assert EpistemicGraphIndex(root).available() is False
    assert len(proofs) == 1

    generation = graph_sync.status(root)["generation"]
    for _ in range(3):
        assert manager.status(root)["graph_sync"] == {
            "state": "unavailable",
            "generation": generation,
        }
    assert len(proofs) == 1


def test_a_cold_single_reader_recall_still_uses_the_graph_lane(
    inherited_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no proof running, recall proves inline once, like a one-shot CLI."""
    root = inherited_vault
    lexstore.ensure_fresh(root)
    proofs = _count_proofs(monkeypatch)

    for _ in range(3):
        degraded: list[str] = []
        hits = find_module.find(
            root, query="generated note", mode="hybrid", graph=True, degraded_out=degraded
        )
        assert hits
        assert "graph" not in degraded, "a lone cold reader lost the graph lane"
    assert len(proofs) == 1, f"one reader proved an unchanged sidecar {len(proofs)} times"


def test_graph_recall_does_not_wait_on_another_readers_proof(
    inherited_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = inherited_vault
    lexstore.ensure_fresh(root)
    # Warm everything but the graph lane, so the timed call measures only it.
    find_module.find(root, query="generated note", mode="hybrid", graph=False)

    release = threading.Event()
    entered = threading.Event()
    callers: list[str] = []
    real_proof = EpistemicGraphIndex._snapshot_sources_match_disk

    def held(inner_self: EpistemicGraphIndex, conn: object, **kwargs: object) -> bool:
        callers.append(threading.current_thread().name)
        entered.set()
        release.wait(HELD_PROOF_SECONDS)
        return real_proof(inner_self, conn, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_snapshot_sources_match_disk", held, raising=True)

    # Another reader -- the drain, in a cell -- is already proving the sidecar.
    prover = threading.Thread(
        target=lambda: EpistemicGraphIndex(root).available(), name="other-reader-proof"
    )
    prover.start()
    assert entered.wait(10.0), "the other reader never started its proof"

    degraded: list[str] = []
    try:
        started = time.monotonic()
        hits = find_module.find(
            root, query="generated note", mode="hybrid", graph=True, degraded_out=degraded
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        prover.join(30.0)

    assert elapsed < RECALL_BOUND_SECONDS, f"graph recall waited {elapsed:.1f}s on the proof"
    assert hits, "recall must still answer without the graph lane"
    assert "graph" in degraded, "the skipped graph lane must be reported as degraded"
    assert callers == ["other-reader-proof"], f"the proof was stacked: {callers}"

    # Once the running proof lands, the graph lane participates again without
    # another proof.
    assert EpistemicGraphIndex(root).availability_state() == "available"
    degraded.clear()
    find_module.find(root, query="generated note", mode="hybrid", graph=True, degraded_out=degraded)
    assert "graph" not in degraded
    assert callers == ["other-reader-proof"]


def test_a_blocking_reader_shares_a_running_proof(
    inherited_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = inherited_vault
    release = threading.Event()
    entered = threading.Event()
    callers: list[str] = []
    real_proof = EpistemicGraphIndex._snapshot_sources_match_disk

    def held(inner_self: EpistemicGraphIndex, conn: object, **kwargs: object) -> bool:
        callers.append(threading.current_thread().name)
        entered.set()
        release.wait(HELD_PROOF_SECONDS)
        return real_proof(inner_self, conn, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_snapshot_sources_match_disk", held, raising=True)
    results: dict[str, bool] = {}

    def check(name: str) -> None:
        results[name] = EpistemicGraphIndex(root).available()

    first = threading.Thread(target=check, args=("first",), name="first")
    first.start()
    assert entered.wait(10.0)
    second = threading.Thread(target=check, args=("second",), name="second")
    second.start()
    time.sleep(0.2)
    release.set()
    first.join(30.0)
    second.join(30.0)

    assert results == {"first": True, "second": True}
    assert callers == ["first"], f"a second blocking reader re-proved: {callers}"
