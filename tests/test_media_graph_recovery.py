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

    def observe_startup_rebuild(index: epistemic_graph.EpistemicGraphIndex):
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
        {"state": "current", "generation": issued_generation + 1}
    ]
    assert graph_sync.status(vault) == {
        "state": "current",
        "generation": issued_generation + 1,
    }
    assert epistemic_graph.EpistemicGraphIndex(vault).available()
    assert clear_calls == [[admitted]]
    assert deferred_index.snapshot_full(vault) == newer_receipts
    assert sidecar.read_bytes() == committed_bytes
