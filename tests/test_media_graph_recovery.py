"""Crash-gap recovery coverage for deferred media graph completion."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import (
    deferred_index,
    epistemic_graph,
    extract,
    file_watcher,
    graph_sync,
    index_sync,
    mode,
)
from exomem import vault as vault_module


def _prime_current_graph(vault: Path) -> None:
    """Create a real graph sidecar with a fully acknowledged baseline epoch."""
    prior = vault / "Knowledge Base" / "Notes" / "prior.md"
    prior.parent.mkdir(parents=True, exist_ok=True)
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(prior, "# prior\n")],
        vault_root=vault,
        post_commit_fanout=False,
    )
    graph = epistemic_graph.EpistemicGraphIndex(vault)
    graph.rebuild_all()
    assert graph.available()
    assert graph_sync.status(vault)["state"] == "current"


def _commit_floor_ahead_sidecar(vault: Path, name: str) -> tuple[Path, int]:
    """Commit canonical Markdown through media's deferred graph boundary."""
    sidecar = vault / "Knowledge Base" / "Notes" / name
    handoff = vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(
                sidecar,
                "---\ntype: source\nmedia_type: audio\nextracted_by: external-asr\n"
                "processing_state: completed\n---\n\n## Extracted text\n\n"
                "[0:00] A committed transcript must never be extracted again.\n",
            )
        ],
        vault_root=vault,
        post_commit_fanout=False,
        defer_graph_completion=True,
    )

    assert isinstance(handoff, vault_module.DeferredGraphCompletion)
    assert graph_sync.classify_epoch(vault).kind == "recoverable"
    return sidecar, handoff.checkpoint.generation


def test_full_receipt_drain_recovers_floor_ahead_graph_and_cas_clears_completed_work(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real full drain repairs the crash gap before its normal graph fan-out."""
    _prime_current_graph(vault)
    sidecar, issued_generation = _commit_floor_ahead_sidecar(vault, "drain-recovery.wav.md")
    rel = sidecar.relative_to(vault).as_posix()
    [admitted] = deferred_index.add_full_receipts(vault, [rel])
    extracted: list[object] = []
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extracted.append(object())
        or pytest.fail("full-receipt recovery must not extract a committed transcript"),
    )

    real_graph_upsert = epistemic_graph.upsert_after_write
    graph_states_before_graph_work: list[dict[str, int | str]] = []

    def observe_graph_work(root: Path, paths: list[Path], **kwargs):  # noqa: ANN001
        graph_states_before_graph_work.append(graph_sync.status(root))
        return real_graph_upsert(root, paths, **kwargs)

    monkeypatch.setattr(epistemic_graph, "upsert_after_write", observe_graph_work)

    real_upsert = index_sync.upsert_after_write

    def readd_after_completed_fanout(root: Path, paths: list[Path], **kwargs):  # noqa: ANN001
        report = real_upsert(root, paths, **kwargs)
        newer_receipts.extend(deferred_index.add_full_receipts(root, [rel]))
        return report

    monkeypatch.setattr(index_sync, "upsert_after_write", readd_after_completed_fanout)
    newer_receipts: list[deferred_index.DeferredReceipt] = []
    clear_calls: list[list[deferred_index.DeferredReceipt]] = []
    real_clear = deferred_index.clear_full_receipts

    def clear_only_completed_graph_work(root: Path, receipts):  # noqa: ANN001
        captured = list(receipts)
        clear_calls.append(captured)
        assert captured == [admitted]
        assert newer_receipts == [deferred_index.DeferredReceipt(rel, admitted.revision + 1)]
        assert deferred_index.snapshot_full(root) == newer_receipts
        graph = epistemic_graph.EpistemicGraphIndex(root)
        assert graph.available()
        assert graph.indexed_paths([rel]) == {rel}
        return real_clear(root, receipts)

    monkeypatch.setattr(deferred_index, "clear_full_receipts", clear_only_completed_graph_work)

    assert index_sync.drain_deferred_work(vault, limit=1) == 0
    assert graph_states_before_graph_work == [
        {"state": "current", "generation": issued_generation + 1}
    ]
    assert graph_sync.status(vault) == {
        "state": "current",
        "generation": issued_generation + 1,
    }
    assert epistemic_graph.EpistemicGraphIndex(vault).available()
    assert clear_calls == [[admitted]]
    assert deferred_index.snapshot_full(vault) == newer_receipts
    assert extracted == []


def test_full_receipt_drain_skips_all_receipts_when_vault_recovery_fails(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failed vault-wide rebuild leaves every receipt for a later pass."""
    _prime_current_graph(vault)
    sidecar, _generation = _commit_floor_ahead_sidecar(vault, "failed-recovery.wav.md")
    other = vault / "Knowledge Base" / "Notes" / "also-deferred.md"
    other.write_text("# deferred\n", encoding="utf-8")
    admitted = deferred_index.add_full_receipts(
        vault,
        [
            sidecar.relative_to(vault).as_posix(),
            other.relative_to(vault).as_posix(),
        ],
    )
    rebuild_attempts: list[object] = []
    dispatches: list[list[Path]] = []

    def fail_rebuild(_index: epistemic_graph.EpistemicGraphIndex):
        rebuild_attempts.append(object())
        raise RuntimeError("rebuild unavailable")

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_rebuild_all_off_boundary",
        fail_rebuild,
    )
    monkeypatch.setattr(
        index_sync,
        "upsert_after_write",
        lambda _root, paths: dispatches.append(paths),
    )

    # The subject is the *full* queue, and `snapshot_full` below says so
    # exactly. The drain's aggregate return counts every queue it serves, and
    # since the graph queue joined it (converge-graph-incrementally) a nonzero
    # total can mean "graph paths repaired", which is unrelated to whether this
    # vault's full receipts survived a failed recovery. `dispatches == []` is
    # the assertion that no full work was replayed.
    index_sync.drain_deferred_work(vault, limit=2)
    assert len(rebuild_attempts) == 1
    assert dispatches == []
    assert deferred_index.snapshot_full(vault) == admitted
    assert graph_sync.status(vault)["state"] == "recovery_required"

    index_sync.drain_deferred_work(vault, limit=2)
    assert len(rebuild_attempts) == 2
    assert dispatches == []
    assert deferred_index.snapshot_full(vault) == admitted


def test_watcher_seed_recovers_floor_ahead_receipt_before_rebuild_without_extraction(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup recovers a committed transcript's graph handoff before rebuilding it."""
    _prime_current_graph(vault)
    sidecar, issued_generation = _commit_floor_ahead_sidecar(vault, "watcher-recovery.wav.md")
    rel = sidecar.relative_to(vault).as_posix()
    [admitted] = deferred_index.add_full_receipts(vault, [rel])
    committed_bytes = sidecar.read_bytes()
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: pytest.fail(
            "watcher startup must not re-extract a committed transcript"
        ),
    )
    real_rebuild = epistemic_graph.EpistemicGraphIndex.rebuild_all
    rebuild_epochs: list[dict[str, int | str]] = []
    completed_rebuilds: list[epistemic_graph.EpistemicGraphIndex] = []
    rebuild_calls = 0

    def observe_startup_rebuild(index: epistemic_graph.EpistemicGraphIndex):
        nonlocal rebuild_calls
        rebuild_calls += 1
        rebuild_epochs.append(graph_sync.status(index.vault_root))
        result = real_rebuild(index)
        assert index.available()
        completed_rebuilds.append(index)
        return result

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "rebuild_all",
        observe_startup_rebuild,
    )
    newer_receipts: list[deferred_index.DeferredReceipt] = []
    clear_calls: list[list[deferred_index.DeferredReceipt]] = []
    real_clear = deferred_index.clear_full_receipts

    def clear_only_after_startup_rebuild(root: Path, receipts):  # noqa: ANN001
        captured = list(receipts)
        clear_calls.append(captured)
        assert captured == [admitted]
        assert completed_rebuilds
        graph = epistemic_graph.EpistemicGraphIndex(root)
        assert graph.available()
        assert graph_sync.status(root) == {
            "state": "current",
            "generation": issued_generation + 1,
        }
        assert graph.indexed_paths([rel]) == {rel}
        newer_receipts.extend(deferred_index.add_full_receipts(root, [rel]))
        assert deferred_index.snapshot_full(root) == newer_receipts
        return real_clear(root, receipts)

    monkeypatch.setattr(deferred_index, "clear_full_receipts", clear_only_after_startup_rebuild)

    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)

    assert rebuild_epochs == [
        {"state": "recovery_required", "generation": issued_generation + 1}
    ]
    assert rebuild_calls == 1
    assert graph_sync.status(vault) == {
        "state": "current",
        "generation": issued_generation + 1,
    }
    assert epistemic_graph.EpistemicGraphIndex(vault).available()
    assert clear_calls == [[admitted]]
    assert deferred_index.snapshot_full(vault) == newer_receipts
    assert sidecar.read_bytes() == committed_bytes


def test_watcher_seed_caps_full_receipt_snapshot_before_targeted_drain(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup replay admits only its configured receipt cap from a large backlog."""
    rels = [f"Knowledge Base/Notes/backlog-{index:02d}.md" for index in range(24)]
    for rel in rels:
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# queued\n", encoding="utf-8")
    deferred_index.add_full(vault, rels)
    watcher = file_watcher.FileWatcher(vault)
    observed_limits: list[int | None] = []
    real_snapshot_full = deferred_index.snapshot_full
    drains: list[tuple[list[Path], int | None]] = []

    def observe_snapshot_full(root: Path, *, limit=None, paths=None):  # noqa: ANN001
        assert paths is None
        observed_limits.append(limit)
        return real_snapshot_full(root, limit=limit, paths=paths)

    monkeypatch.setattr(watcher, "_validate_existing_graph_on_seed", lambda: True)
    monkeypatch.setattr(
        watcher,
        "_watcher_policy",
        lambda: mode.WatcherPolicy(0.5, 300.0, 2, 2, False),
    )
    monkeypatch.setattr(deferred_index, "snapshot_full", observe_snapshot_full)
    monkeypatch.setattr(
        index_sync,
        "drain_deferred_work",
        lambda _root, *, paths, limit: drains.append((paths, limit)),
    )

    watcher._reconcile_once(seed=True)

    assert observed_limits == [2]
    assert drains == [([vault / rels[0], vault / rels[1]], 2)]
