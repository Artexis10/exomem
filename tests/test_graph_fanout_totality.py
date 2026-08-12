"""Contract tests for exact post-commit graph fanout outcomes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import epistemic_graph, graph_sync, index_sync, vault


def _checkpoint() -> graph_sync.GraphSyncCheckpoint:
    return graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="1" * 24,
        paths=(("Knowledge Base/Notes/example.md", "a" * 64),),
        created_paths=("Knowledge Base/Notes/example.md",),
    )


def test_empty_graph_fanout_is_explicitly_not_required(tmp_path: Path) -> None:
    result = epistemic_graph.upsert_after_write(tmp_path, [])

    assert result == epistemic_graph.GraphDispatchResult.not_required()
    assert graph_sync.registered_checkpoint(tmp_path) is None


def test_registration_exception_installs_exact_failure_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = tmp_path / "Knowledge Base/Notes/example.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Example\n", encoding="utf-8")
    checkpoint = _checkpoint()
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(tmp_path, checkpoint)

    monkeypatch.setattr(
        graph_sync,
        "register_rebuild",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("registration failed")),
    )

    result = epistemic_graph.upsert_after_write(tmp_path, [note])

    assert result.outcome == "failed"
    assert result.checkpoint == checkpoint
    assert result.code == "GRAPH_SYNC_REGISTRATION_FAILED"
    assert graph_sync.registered_checkpoint(tmp_path) == checkpoint


def test_scheduling_disabled_keeps_exact_deferred_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = tmp_path / "Knowledge Base/Notes/example.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Example\n", encoding="utf-8")
    checkpoint = _checkpoint()
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(tmp_path, checkpoint)
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")

    result = epistemic_graph.upsert_after_write(tmp_path, [note])

    assert result.outcome == "deferred"
    assert result.checkpoint == checkpoint
    assert graph_sync.registered_checkpoint(tmp_path) == checkpoint


def test_canonical_batch_repairs_a_missing_graph_handoff_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = tmp_path / "Knowledge Base/Notes/example.md"

    def incomplete_report(_root: Path, _paths: list[Path], **_kwargs) -> index_sync.IndexSyncReport:
        return index_sync.IndexSyncReport(
            "upsert",
            ("Knowledge Base/Notes/example.md",),
            ("Knowledge Base/Notes/example.md",),
            (
                index_sync.IndexComponentOutcome(
                    "epistemic_graph", "registered", "graph_rebuild_registered"
                ),
            ),
        )

    monkeypatch.setattr(index_sync, "upsert_after_write", incomplete_report)
    reports: list[index_sync.IndexSyncReport] = []

    vault.batch_atomic_write(
        [vault.PlannedWrite(note, "# Example\n")], vault_root=tmp_path, index_reports=reports
    )

    checkpoint = graph_sync.read_checkpoint(tmp_path)
    assert checkpoint is not None
    assert graph_sync.registered_checkpoint(tmp_path) == checkpoint
    graph = next(item for item in reports[0].components if item.component == "epistemic_graph")
    assert (graph.outcome, graph.code) == ("failed", "GRAPH_SYNC_HANDOFF_MISSING")


def test_records_only_batch_performs_no_graph_dispatch_or_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "Knowledge Base/Records/Health/example.md"
    calls = 0

    def graph_dispatch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("Records-only fanout must not touch the graph")

    monkeypatch.setattr(epistemic_graph, "upsert_after_write", graph_dispatch)

    vault.batch_atomic_write([vault.PlannedWrite(record, "value: 1\n")], vault_root=tmp_path)

    assert calls == 0
    assert graph_sync.read_checkpoint(tmp_path) is None
    assert graph_sync.registered_checkpoint(tmp_path) is None


def test_constructor_failure_fallback_keeps_invoking_manager_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker must retain manager B rather than recover ambient manager A."""
    from exomem import writer_lease

    ambient_state = tmp_path / "ambient-state"
    custom_state = tmp_path / "custom-state"
    ambient = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=ambient_state))
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=custom_state))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: ambient)
    vault_root = tmp_path / "vault"
    note = vault_root / "Knowledge Base/Notes/example.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Example\n", encoding="utf-8")
    checkpoint = _checkpoint()
    graph_sync._write_floor(vault_root, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(vault_root, checkpoint)

    real_init = epistemic_graph.EpistemicGraphIndex.__init__
    initial_construction = True
    observed_roots: list[Path] = []

    def fail_initial_construction(self, root, **kwargs):  # noqa: ANN001
        nonlocal initial_construction
        if initial_construction:
            initial_construction = False
            raise RuntimeError("injected graph constructor failure")
        real_init(self, root, **kwargs)

    def capture_rebuild(self):  # noqa: ANN001
        observed_roots.append(self._canonical_mutation_coordinator().state_root)
        return {"indexed_files": 0, "nodes": 0, "edges": 0}

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "__init__", fail_initial_construction)
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex, "_rebuild_all_off_boundary", capture_rebuild
    )
    command = SimpleNamespace(
        name="graph-fallback-boundary",
        read_only=False,
        leaf=lambda root: epistemic_graph.upsert_after_write(root, [note]),
    )

    manager.invoke(command, (vault_root,), {})

    assert observed_roots == [custom_state]
