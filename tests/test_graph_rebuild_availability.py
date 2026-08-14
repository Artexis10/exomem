"""Contract coverage for durable graph rebuild handoff and publication."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import freshness, graph_sync, runtime_readiness
from exomem import mutation_lock as mutation_lock_module
from exomem import reconcile as reconcile_module
from exomem import vault as vault_module


def _crash_after_canonical_terminal(database: str, marker: str) -> None:
    from exomem.writer_lease import IdempotencyStore

    def operation() -> dict[str, object]:
        Path(marker).write_text("committed", encoding="utf-8")
        return {"state": "committed", "graph_sync": "pending"}

    def crash(_result: object) -> object:
        os._exit(0)

    IdempotencyStore(Path(database)).run(
        "crash-window", "digest", operation, after_operation_guard=crash
    )


def _hold_cross_process_rebuild_lock(
    vault_root: str, temporary: str, ready, release  # noqa: ANN001
) -> None:
    from exomem import graph_sync as graph_sync_module

    assert graph_sync_module.claim_rebuild_owner(Path(vault_root), Path(temporary))
    ready.set()
    assert release.wait(10)
    graph_sync_module.release_rebuild_owner(Path(vault_root), Path(temporary))


def _attempt_cross_process_rebuild_lock(vault_root: str, temporary: str, result) -> None:  # noqa: ANN001
    from exomem import graph_sync as graph_sync_module

    claimed = graph_sync_module.claim_rebuild_owner(Path(vault_root), Path(temporary))
    try:
        result.put(claimed)
    finally:
        if claimed:
            graph_sync_module.release_rebuild_owner(Path(vault_root), Path(temporary))


def _forked_graph_lock_state(vault_root: str, temporary: str, result) -> None:  # noqa: ANN001
    from exomem import graph_sync as graph_sync_module

    inherited_registry = bool(graph_sync_module._REBUILD_LOCK_HANDLES)
    claimed = graph_sync_module.claim_rebuild_owner(Path(vault_root), Path(temporary))
    try:
        result.put((inherited_registry, claimed))
    finally:
        if claimed:
            graph_sync_module.release_rebuild_owner(Path(vault_root), Path(temporary))


def _checkpoint(generation: int) -> graph_sync.GraphSyncCheckpoint:
    return graph_sync.GraphSyncCheckpoint.create(
        generation=generation,
        mutation_id=f"{generation:024x}",
        paths=(("Knowledge Base/Notes/example.md", "d" * 64),),
        created_paths=("Knowledge Base/Notes/example.md",),
    )


def test_checkpoint_is_closed_domain_separated_and_projects_paths() -> None:
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="0123456789abcdef01234567",
        paths=(("Knowledge Base/Notes/example.md", "d" * 64),),
        created_paths=("Knowledge Base/Notes/example.md",),
    )

    assert checkpoint.as_dict() == {
        "version": 1,
        "generation": 1,
        "mutation_id": "0123456789abcdef01234567",
        "scope": "paths",
        "paths": [["Knowledge Base/Notes/example.md", "d" * 64]],
        "created_paths": ["Knowledge Base/Notes/example.md"],
        "checkpoint_sha256": "941d8a67ae715b6795daade34607f445d4c5b5726b9dc5e4ac095c9946c6d877",
    }
    assert graph_sync.GraphSyncCheckpoint.parse(checkpoint.render()) == checkpoint
    assert graph_sync.GraphSyncCheckpoint.parse('{"generation":1}') is None
    projected = graph_sync.GraphSyncCheckpoint.create(
        generation=2,
        mutation_id="fedcba9876543210fedcba98",
        paths=(
            ("Knowledge Base/Notes/z.md", None),
            ("Knowledge Base/Notes/a.md", "a" * 64),
        ),
        created_paths=("Knowledge Base/Notes/a.md",),
    )
    assert projected.paths == (
        ("Knowledge Base/Notes/a.md", "a" * 64),
        ("Knowledge Base/Notes/z.md", None),
    )
    assert projected.created_paths == (
        "Knowledge Base/Notes/a.md",
    )


def test_generation_floor_is_closed_and_rejects_noncanonical_mutation_ids() -> None:
    floor = graph_sync.GraphSyncGenerationFloor.create(1)

    assert floor.as_dict() == {
        "version": 1,
        "generation": 1,
        "floor_sha256": "a1d6d04d75a81af9abb1a6c8ea88214a520fdde62d2a03fb99eda5934d3ad829",
    }
    assert graph_sync.GraphSyncGenerationFloor.parse(floor.render()) == floor
    assert graph_sync.GraphSyncGenerationFloor.parse('{"generation":1}') is None
    with pytest.raises(ValueError, match="lowercase 24-hex"):
        graph_sync.GraphSyncCheckpoint.create(
            generation=1,
            mutation_id="0123456789ABCDEF01234567",
            paths=(),
            created_paths=(),
        )


def test_checkpoint_generation_is_monotonic_and_full_scope_is_bounded() -> None:
    prior = _checkpoint(4)
    checkpoint = graph_sync.next_checkpoint(
        current=prior,
        acknowledged_generation=7,
        mutation_id="f" * 24,
        paths=[(f"Knowledge Base/Notes/{index}.md", "a" * 64) for index in range(1001)],
        created_paths=[],
    )

    assert checkpoint.generation == 8
    assert checkpoint.scope == "full"
    assert checkpoint.paths == ()
    assert checkpoint.created_paths == ()


def test_graph_relevant_batch_stages_checkpoint_with_canonical_write(tmp_path: Path) -> None:
    note = tmp_path / "Knowledge Base/Notes/Insights/example.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Example\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )

    checkpoint = graph_sync.read_checkpoint(tmp_path)
    assert checkpoint is not None
    assert checkpoint.generation == 1
    assert checkpoint.paths == (("Knowledge Base/Notes/Insights/example.md", vault_module.content_hash("# Example\n")),)
    assert not graph_sync.is_graph_input_path("Knowledge Base/.graph-sync.json")


def test_interrupted_floor_is_recovered_by_the_next_caller_batch_at_full_scope(
    tmp_path: Path,
) -> None:
    first = tmp_path / "Knowledge Base/Notes/Insights/first.md"
    second = tmp_path / "Knowledge Base/Notes/Insights/second.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(first, "# First\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    graph_sync.checkpoint_path(tmp_path).unlink()

    replaced = vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(second, "# Second\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )

    checkpoint = graph_sync.read_checkpoint(tmp_path)
    assert replaced == [second]
    assert checkpoint is not None
    assert checkpoint.generation == 2
    assert checkpoint.scope == "full"
    assert checkpoint.paths == ()
    assert checkpoint.created_paths == ()
    assert graph_sync.read_floor(tmp_path) == graph_sync.GraphSyncGenerationFloor.create(2)


def test_malformed_checkpoint_after_a_valid_floor_is_recovered_by_the_next_batch(
    tmp_path: Path,
) -> None:
    first = tmp_path / "Knowledge Base/Notes/Insights/first.md"
    second = tmp_path / "Knowledge Base/Notes/Insights/second.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(first, "# First\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    graph_sync.checkpoint_path(tmp_path).write_text("{broken", encoding="utf-8")

    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(second, "# Second\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )

    checkpoint = graph_sync.read_checkpoint(tmp_path)
    assert checkpoint is not None
    assert checkpoint.generation == 2
    assert checkpoint.scope == "full"


def test_recovery_epoch_preparation_admits_a_fresh_floor_without_checkpoint(
    tmp_path: Path,
) -> None:
    restored = "Knowledge Base/Notes/Insights/restored.md"
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(1))

    epoch = graph_sync.prepare_recovery_epoch(tmp_path, [(restored, "a" * 64)])

    assert epoch is not None
    assert epoch.checkpoint.generation == 2
    assert epoch.checkpoint.scope == "full"
    assert epoch.checkpoint.paths == ()
    assert epoch.checkpoint.created_paths == ()
    assert graph_sync.read_floor(tmp_path) == graph_sync.GraphSyncGenerationFloor.create(2)


def test_records_write_does_not_issue_an_epoch_or_schedule_graph_work(tmp_path: Path) -> None:
    record = tmp_path / "Knowledge Base/Records/private.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(record, "# Private record\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )

    assert graph_sync.read_floor(tmp_path) is None
    assert graph_sync.read_checkpoint(tmp_path) is None


def test_caught_batch_failure_rolls_back_the_checkpoint_with_canonical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = tmp_path / "Knowledge Base/Notes/Insights/rollback.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Before\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    prior_checkpoint = graph_sync.checkpoint_path(tmp_path).read_bytes()
    replace = vault_module._BatchWorkspace.replace_artifact

    def fail_checkpoint(workspace, artifact, target):
        if target == graph_sync.checkpoint_path(tmp_path):
            raise PermissionError(13, "replacement denied", str(target))
        return replace(workspace, artifact, target)

    monkeypatch.setattr(vault_module._BatchWorkspace, "replace_artifact", fail_checkpoint)
    with pytest.raises(PermissionError):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(note, "# After\n")],
            vault_root=tmp_path,
            post_commit_fanout=False,
        )

    assert note.read_text(encoding="utf-8") == "# Before\n"
    assert graph_sync.checkpoint_path(tmp_path).read_bytes() == prior_checkpoint


def test_valid_floor_with_malformed_checkpoint_recovers_at_a_higher_full_epoch(
    tmp_path: Path,
) -> None:
    note = tmp_path / "Knowledge Base/Notes/Insights/recovery-floor.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Recovery\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    graph_sync.checkpoint_path(tmp_path).write_text("{broken", encoding="utf-8")

    recovered = graph_sync.recover_checkpoint(tmp_path)

    assert recovered is not None
    assert recovered.generation == 2
    assert recovered.scope == "full"
    assert graph_sync.read_floor(tmp_path).generation == 2


def test_full_rebuild_rejects_malformed_checkpoint_before_live_sidecar_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.epistemic_graph import EpistemicGraphIndex

    note = tmp_path / "Knowledge Base/Notes/Insights/publication-floor.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Before\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    index = EpistemicGraphIndex(tmp_path)
    index.rebuild_all()
    with index._connect() as conn:
        prior_rows = conn.execute("SELECT node_key, source_hash FROM graph_nodes").fetchall()
    graph_sync.checkpoint_path(tmp_path).write_text("{broken", encoding="utf-8")
    seam_reached = False

    def seam(_temporary: Path, _live: Path) -> None:
        nonlocal seam_reached
        seam_reached = True

    monkeypatch.setattr(index, "_before_publish_replacement", seam)

    with pytest.raises(graph_sync.GraphEpochIncoherent, match="coherent"):
        index.rebuild_all()

    assert seam_reached is False
    with index._connect() as conn:
        assert conn.execute("SELECT node_key, source_hash FROM graph_nodes").fetchall() == prior_rows
    assert graph_sync.status(tmp_path) == {"state": "recovery_required", "generation": 1}


def test_publication_epoch_retries_a_floor_checkpoint_commit_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _checkpoint(1)
    second = graph_sync.GraphSyncCheckpoint.create(
        generation=2,
        mutation_id="2" * 24,
        paths=(),
        created_paths=(),
        scope="full",
    )
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))
    graph_sync._write_checkpoint(tmp_path, first)
    real_checkpoint_state = graph_sync.checkpoint_state
    reads = 0

    def complete_after_read(root: Path):
        nonlocal reads
        reads += 1
        state = real_checkpoint_state(root)
        if reads == 1:
            graph_sync.checkpoint_path(root).write_text(second.render(), encoding="utf-8")
        return state

    monkeypatch.setattr(graph_sync, "checkpoint_state", complete_after_read)

    assert graph_sync.publication_epoch(tmp_path) == graph_sync.GraphPublicationEpoch(
        graph_sync.GraphSyncGenerationFloor.create(2), second, None
    )
    assert reads == 2


def test_rebuild_retries_an_epoch_window_that_outlives_inner_resampling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.epistemic_graph import EpistemicGraphIndex

    note = tmp_path / "Knowledge Base/Notes/epoch-window.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Epoch window\n", encoding="utf-8")
    freshness.seed(tmp_path, "vault", [(str(note), freshness.stat_signature(note))])
    first = _checkpoint(1)
    second = graph_sync.GraphSyncCheckpoint.create(
        generation=2,
        mutation_id="2" * 24,
        paths=(),
        created_paths=(),
        scope="full",
    )
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))
    graph_sync._write_checkpoint(tmp_path, first)
    real_checkpoint_state = graph_sync.checkpoint_state
    reads = 0

    def finish_on_outer_retry(root: Path):
        nonlocal reads
        reads += 1
        if reads == 3:
            graph_sync.checkpoint_path(root).write_text(second.render(), encoding="utf-8")
        return real_checkpoint_state(root)

    monkeypatch.setattr(graph_sync, "checkpoint_state", finish_on_outer_retry)

    EpistemicGraphIndex(tmp_path).rebuild_all()

    assert reads >= 3
    assert graph_sync.read_checkpoint(tmp_path) == second
    assert EpistemicGraphIndex(tmp_path).available()


def test_ahead_floor_never_accepts_or_reuses_an_old_checkpoint(tmp_path: Path) -> None:
    old = _checkpoint(1)
    graph_sync._write_checkpoint(tmp_path, old)
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))

    assert graph_sync.status(tmp_path) == {"state": "unavailable", "generation": 2}

    recovered = graph_sync.recover_checkpoint(tmp_path)

    assert recovered is not None
    assert recovered.scope == "full"
    assert recovered.generation == 3
    assert graph_sync.read_floor(tmp_path).generation == 3


def test_reconcile_report_defaults_to_no_graph_reset() -> None:
    report = reconcile_module.ReconcileReport()

    assert report.graph_rebuild_requested is False
    assert report.graph_rebuild_applicable is False
    assert report.graph_rebuild_status == "not_requested"
    assert report.graph_quarantine_id is None


def test_post_join_finalizer_clears_the_checkpoint_bound_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint(2)
    handoff = {
        "graph_status": "unavailable",
        "graph_refreshed": 0,
        "graph_rebuild_status": "quarantined",
        "graph_quarantine_id": "a" * 24,
        "_graph_rebuild_handoff": {
            "operation_id": "a" * 24,
            "checkpoint": checkpoint.as_dict(),
            "graph_refreshed": 1,
        },
    }
    monkeypatch.setattr(
        graph_sync, "wait_for_registered", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(graph_sync, "status", lambda _root: {"state": "current"})
    monkeypatch.setattr(
        graph_sync,
        "cleanup_published_graph_lineage_reset",
        lambda root, operation_id, required: (
            root == tmp_path and operation_id == "a" * 24 and required == checkpoint
        ),
    )
    monkeypatch.setattr(
        reconcile_module,
        "_graph_rebuild_is_current",
        lambda _root: True,
    )

    result = reconcile_module.finalize_graph_rebuild_handoff(tmp_path, handoff)

    assert result["graph_status"] == "refreshed"
    assert result["graph_refreshed"] == 1
    assert result["graph_rebuild_status"] == "cleared"
    assert result["graph_quarantine_id"] == "a" * 24
    assert "_graph_rebuild_handoff" not in result


def test_completed_dispatch_is_a_valid_checkpoint_bound_reset_handoff() -> None:
    checkpoint = _checkpoint(2)
    completed = SimpleNamespace(outcome="completed", checkpoint=checkpoint)

    assert reconcile_module._is_graph_rebuild_handoff(completed, checkpoint)


def test_rebuild_graph_dry_run_previews_unavailable_reset_without_mutating(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint(1)
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))
    graph_sync._write_checkpoint(tmp_path, checkpoint)
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"old graph")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in (graph_sync.floor_path(tmp_path), graph_sync.checkpoint_path(tmp_path), live)
    }

    report = reconcile_module.reconcile(tmp_path, dry_run=True, rebuild_graph=True)

    assert report.graph_rebuild_requested is True
    assert report.graph_rebuild_applicable is True
    assert report.graph_rebuild_status == "would_quarantine"
    assert report.graph_quarantine_id is None
    assert graph_sync.registered_checkpoint(tmp_path) is None
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in (graph_sync.floor_path(tmp_path), graph_sync.checkpoint_path(tmp_path), live)
    } == before


def test_dry_run_census_never_recovers_an_interrupted_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph_sync._write_checkpoint(tmp_path, _checkpoint(1))
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))
    kb = tmp_path / "Knowledge Base"
    (kb / ".graph.sqlite").write_bytes(b"graph")

    monkeypatch.setattr(
        graph_sync,
        "_recover_interrupted_reset",
        lambda _root: pytest.fail("dry-run must not recover a transaction"),
    )

    assert graph_sync.census_unavailable_graph_lineage(tmp_path) == (".graph.sqlite",)


def test_recovered_isolated_reset_requires_exact_quarantine_identity(
    tmp_path: Path,
) -> None:
    reset = graph_sync.GraphReset("a" * 24, (".graph.sqlite",), "isolated")
    kb = tmp_path / "Knowledge Base"
    kb.mkdir()
    quarantine = kb / f".graph-reset-{'a' * 24}"
    quarantine.mkdir()
    graph = quarantine / ".graph.sqlite"
    graph.write_bytes(b"quarantined")
    held = mutation_lock_module.retain_regular_file(graph)
    try:
        identities = {".graph.sqlite": held.identity}
    finally:
        held.close()
    (quarantine / ".manifest.json").write_bytes(graph_sync._reset_manifest_raw(reset, identities))
    graph.unlink()
    graph.write_bytes(b"replacement")

    with pytest.raises(graph_sync.GraphResetFailed):
        graph_sync._recover_interrupted_reset(tmp_path)


def test_unavailable_reset_quarantines_only_the_live_graph_set(tmp_path: Path) -> None:
    graph_sync._write_checkpoint(tmp_path, _checkpoint(1))
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))
    kb = tmp_path / "Knowledge Base"
    live = kb / ".graph.sqlite"
    companion = kb / ".graph.sqlite-wal"
    receipt = kb / ".graph-commit-receipts" / "receipt.json"
    note = kb / "Notes/unchanged.md"
    live.write_bytes(b"main")
    companion.write_bytes(b"wal")
    receipt.parent.mkdir()
    receipt.write_bytes(b"receipt")
    note.parent.mkdir()
    note.write_bytes(b"canonical")

    reset = graph_sync.isolate_unavailable_graph_lineage(tmp_path)

    assert reset is not None
    quarantine = kb / f".graph-reset-{reset.operation_id}"
    assert (quarantine / ".graph.sqlite").read_bytes() == b"main"
    assert (quarantine / ".graph.sqlite-wal").read_bytes() == b"wal"
    assert receipt.read_bytes() == b"receipt"
    assert note.read_bytes() == b"canonical"


def test_unavailable_companion_only_lineage_is_previewed_and_quarantined(tmp_path: Path) -> None:
    """A missing primary database does not make a safe retained companion invisible."""
    graph_sync._write_checkpoint(tmp_path, _checkpoint(1))
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))
    companion = tmp_path / "Knowledge Base/.graph.sqlite-wal"
    companion.parent.mkdir()
    companion.write_bytes(b"wal")

    dry_run = reconcile_module.reconcile(tmp_path, dry_run=True, rebuild_graph=True)

    assert dry_run.graph_rebuild_applicable is True
    assert dry_run.graph_rebuild_status == "would_quarantine"
    assert companion.exists()
    reset = graph_sync.isolate_unavailable_graph_lineage(tmp_path)
    assert reset is not None
    assert (companion.parent / f".graph-reset-{reset.operation_id}" / companion.name).exists()


def test_explicit_rebuild_adopts_an_isolated_reset_before_epoch_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-isolation/pre-checkpoint crash cut resumes even if live state looks current."""
    reset = graph_sync.GraphReset("a" * 24, (".graph.sqlite",), "isolated")
    checkpoint = _checkpoint(3)
    monkeypatch.setattr(
        graph_sync, "recover_isolated_graph_lineage_reset", lambda _root: reset
    )
    monkeypatch.setattr(
        graph_sync, "classify_epoch", lambda _root: pytest.fail("must adopt reset first")
    )
    monkeypatch.setattr(graph_sync, "status", lambda _root: {"state": "current", "generation": 3})
    monkeypatch.setattr(graph_sync, "reconcile_checkpoint", lambda _root: checkpoint)
    from exomem import epistemic_graph

    monkeypatch.setattr(
        epistemic_graph, "_registered_or_failure",
        lambda *_args: SimpleNamespace(outcome="registered", checkpoint=checkpoint),
    )

    report = reconcile_module.reconcile(tmp_path, rebuild_graph=True)

    assert report.graph_quarantine_id == reset.operation_id
    assert report._graph_rebuild_handoff == {
        "operation_id": reset.operation_id,
        "checkpoint": checkpoint.as_dict(),
        "graph_refreshed": 0,
    }


def test_post_publication_cleanup_requires_a_current_covered_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a covered rebuild may remove its exact isolated reset evidence."""
    graph_sync._write_checkpoint(tmp_path, _checkpoint(1))
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"old")

    reset = graph_sync.isolate_unavailable_graph_lineage(tmp_path)

    assert reset is not None
    quarantine = live.parent / f".graph-reset-{reset.operation_id}"
    covered = _checkpoint(3)
    monkeypatch.setattr(graph_sync, "read_checkpoint", lambda _root: covered)
    monkeypatch.setattr(graph_sync, "status", lambda _root: {"state": "unavailable"})

    assert not graph_sync.cleanup_published_graph_lineage_reset(
        tmp_path, reset.operation_id, covered
    )
    assert quarantine.exists()

    monkeypatch.setattr(graph_sync, "status", lambda _root: {"state": "current"})

    assert graph_sync.cleanup_published_graph_lineage_reset(
        tmp_path, reset.operation_id, covered
    )
    assert not quarantine.exists()


def test_unavailable_reset_rolls_back_a_partial_move(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import mutation_lock

    graph_sync._write_checkpoint(tmp_path, _checkpoint(1))
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))
    kb = tmp_path / "Knowledge Base"
    (kb / ".graph.sqlite").write_bytes(b"main")
    (kb / ".graph.sqlite-wal").write_bytes(b"wal")
    original = mutation_lock.rename_retained_regular_file
    calls = 0

    def fail_second(source, destination):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("move denied")
        return original(source, destination)

    monkeypatch.setattr(mutation_lock, "rename_retained_regular_file", fail_second)
    with pytest.raises(graph_sync.GraphResetFailed, match="GRAPH_SYNC_RESET_REFUSED"):
        graph_sync.isolate_unavailable_graph_lineage(tmp_path)

    assert (kb / ".graph.sqlite").read_bytes() == b"main"
    assert (kb / ".graph.sqlite-wal").read_bytes() == b"wal"


def test_nonlegacy_malformed_floor_cannot_be_overwritten_by_a_new_write(tmp_path: Path) -> None:
    note = tmp_path / "Knowledge Base/Notes/Insights/malformed-floor.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Before\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    graph_sync.floor_path(tmp_path).write_text("{broken", encoding="utf-8")

    with pytest.raises(graph_sync.GraphEpochIncoherent, match="floor"):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(note, "# After\n")],
            vault_root=tmp_path,
            post_commit_fanout=False,
        )

    assert note.read_text(encoding="utf-8") == "# Before\n"
    assert graph_sync.floor_path(tmp_path).read_text(encoding="utf-8") == "{broken"


def test_deletion_epoch_restores_the_prior_floor_when_checkpoint_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = "Knowledge Base/Notes/Insights/deleted.md"
    source = tmp_path / path
    source.parent.mkdir(parents=True)
    source.write_text("# Deleted\n", encoding="utf-8")
    epoch = graph_sync.prepare_deletion_epoch(tmp_path, [path])
    assert epoch is not None
    assert graph_sync.read_floor(tmp_path).generation == 1

    def fail(*_args, **_kwargs):
        raise PermissionError("checkpoint denied")

    monkeypatch.setattr(graph_sync, "_write_checkpoint", fail)
    with pytest.raises(PermissionError, match="checkpoint denied"):
        graph_sync.commit_deletion_epoch(epoch)

    graph_sync.restore_deletion_epoch(epoch)
    assert graph_sync.read_floor(tmp_path) is None


def test_file_delete_checkpoint_failure_restores_the_source_and_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import delete_file

    rel = "Knowledge Base/Notes/Insights/rollback-file.md"
    source = tmp_path / rel
    source.parent.mkdir(parents=True)
    source.write_text("# Rollback\n", encoding="utf-8")

    monkeypatch.setattr(
        graph_sync,
        "commit_deletion_epoch",
        lambda _epoch: (_ for _ in ()).throw(PermissionError("checkpoint denied")),
    )
    with pytest.raises(delete_file.DeleteFileError) as error:
        delete_file.delete_file(tmp_path, path=rel, confirm=True)

    assert error.value.code == "GRAPH_SYNC_CHECKPOINT_FAILED"
    assert source.read_text(encoding="utf-8") == "# Rollback\n"
    assert graph_sync.read_floor(tmp_path) is None
    assert not any((tmp_path / "Knowledge Base/_trash").rglob("rollback-file.md"))


def test_recursive_delete_checkpoint_failure_restores_the_tree_and_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import delete_directory

    rel = "Knowledge Base/Notes/Insights/rollback-directory"
    source = tmp_path / rel / "nested.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Rollback\n", encoding="utf-8")

    monkeypatch.setattr(
        graph_sync,
        "commit_deletion_epoch",
        lambda _epoch: (_ for _ in ()).throw(PermissionError("checkpoint denied")),
    )
    with pytest.raises(delete_directory.DeleteDirectoryError) as error:
        delete_directory.delete_directory(tmp_path, path=rel, confirm=True, recursive=True)

    assert error.value.code == "GRAPH_SYNC_CHECKPOINT_FAILED"
    assert source.read_text(encoding="utf-8") == "# Rollback\n"
    assert graph_sync.read_floor(tmp_path) is None
    assert not any((tmp_path / "Knowledge Base/_trash").rglob("rollback-directory"))


def test_second_writer_commits_while_first_waits_outside_the_graph_hold(tmp_path: Path) -> None:
    coordinator = graph_sync.GraphRebuildCoordinator(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def build(_checkpoint: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        entered.set()
        assert release.wait(2)
        return graph_sync.GraphBuildOutcome.covering(_checkpoint)

    first = coordinator.start_or_join(_checkpoint(1), build)
    assert entered.wait(1)
    second = coordinator.start_or_join(_checkpoint(2), build)

    assert first.builder_started is True
    assert second.builder_started is False
    assert coordinator.writer_hold_count == 0
    release.set()
    assert first.wait(2).covers(_checkpoint(1))
    assert second.wait(2).covers(_checkpoint(2))


def test_registered_rebuild_enters_only_after_post_guard_wait(tmp_path: Path) -> None:
    from exomem.epistemic_graph import EpistemicGraphIndex

    entered = threading.Event()
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="1" * 24,
        paths=(("Knowledge Base/Notes/escaped.md", vault_module.content_hash("# Escaped\n")),),
        created_paths=("Knowledge Base/Notes/escaped.md",),
    )

    def build(required: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        entered.set()
        return graph_sync.GraphBuildOutcome.covering(required)

    coordinator = EpistemicGraphIndex(tmp_path)._mutation_coordinator
    with coordinator.hold(operation="canonical-write", holder_kind="command"):
        graph_sync.register_rebuild(tmp_path, checkpoint, build)
        assert entered.is_set() is False

    assert entered.is_set() is False
    assert graph_sync.wait_for_registered(tmp_path, timeout=1).covers(checkpoint)
    assert entered.is_set() is True


def test_standalone_upsert_joins_registered_rebuild_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.epistemic_graph import EpistemicGraphIndex, upsert_after_write

    note = tmp_path / "Knowledge Base/Notes/Insights/standalone-join.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Standalone join\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    entered = threading.Event()
    release = threading.Event()
    original = EpistemicGraphIndex._rebuild_all_locked

    def slow_rebuild(index: EpistemicGraphIndex) -> dict[str, int]:
        assert index.path.name.startswith(".graph-rebuild-")
        entered.set()
        assert release.wait(1)
        return original(index)

    def release_build() -> None:
        assert entered.wait(1)
        time.sleep(0.1)
        release.set()

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_locked", slow_rebuild)
    releaser = threading.Thread(target=release_build)
    releaser.start()
    try:
        result = upsert_after_write(tmp_path, [note], created_paths=[note])
    finally:
        release.set()
        releaser.join(timeout=1)

    assert result.outcome == "completed"
    assert not list(note.parent.glob(".graph-rebuild-*.sqlite"))


@pytest.mark.parametrize("as_string", [False, True])
def test_direct_mutation_guard_starts_registered_graph_work_after_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_string: bool
) -> None:
    """A direct leaf cannot wait for graph work while it owns the boundary."""
    from exomem import mutation_lock
    from exomem.writer_lease import LeaseConfig, LeaseManager

    note = tmp_path / "Knowledge Base/Notes/Insights/direct-guard.md"
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    calls: list[str] = []

    def start_registered(*_args, **_kwargs) -> None:
        calls.append(f"start:{mutation_lock.active_mutation_snapshot()['state']}")

    def wait_for_registered(*_args, **_kwargs) -> None:
        calls.append(f"wait:{mutation_lock.active_mutation_snapshot()['state']}")

    monkeypatch.setattr(graph_sync, "start_registered", start_registered)
    monkeypatch.setattr(graph_sync, "wait_for_registered", wait_for_registered)

    root = str(tmp_path) if as_string else tmp_path
    with manager.mutation_guard(root, operation="direct_leaf"):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(note, "# Direct guard\n")],
            vault_root=tmp_path,
        )
        assert calls == []

    assert calls == ["start:free", "wait:free"]


def test_direct_mutation_guard_keeps_a_durable_graph_failure_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct leaves retain a graph failure handle instead of raising after commit."""
    from exomem.writer_lease import LeaseConfig, LeaseManager

    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    note = tmp_path / "Knowledge Base/Notes/Insights/direct-deferred.md"
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))

    with manager.mutation_guard(tmp_path, operation="direct_leaf"):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(note, "# Direct deferred\n")],
            vault_root=tmp_path,
        )

    assert graph_sync.status(tmp_path)["state"] == "recovery_required"


def test_immediate_checkpoint_successor_refreshes_without_full_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import file_watcher, freshness
    from exomem import find as find_module
    from exomem.epistemic_graph import EpistemicGraphIndex, upsert_after_write

    note = tmp_path / "Knowledge Base/Notes/Insights/incremental.md"
    companion = tmp_path / "Knowledge Base/Notes/Insights/companion.md"
    vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(
                note, "---\ntype: insight\nstatus: active\n---\n# Before\n"
            ),
            vault_module.PlannedWrite(
                companion, "---\ntype: insight\nstatus: active\n---\n# Companion\n"
            ),
        ],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    freshness.seed(
        tmp_path,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(tmp_path)),
    )
    freshness.seed(
        tmp_path,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(tmp_path / "Knowledge Base")
        ),
    )
    index = EpistemicGraphIndex(tmp_path)
    index.rebuild_all()
    rebuild_calls = 0
    real_rebuild = EpistemicGraphIndex._rebuild_all_locked

    def count_rebuild(self, *args, **kwargs):
        nonlocal rebuild_calls
        rebuild_calls += 1
        return real_rebuild(self, *args, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_locked", count_rebuild)

    vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(
                note, "---\ntype: insight\nstatus: active\n---\n# After\n"
            )
        ],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    file_watcher._publish_registry_change(tmp_path, [note], [])
    upsert_after_write(tmp_path, [note])

    assert rebuild_calls == 0
    assert graph_sync.status(tmp_path)["state"] == "current"


def test_failed_incremental_proof_registers_off_boundary_work_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.epistemic_graph import EpistemicGraphIndex

    note = tmp_path / "Knowledge Base/Notes/Insights/deferred.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Deferred\n", encoding="utf-8")
    index = EpistemicGraphIndex(tmp_path)
    required = _checkpoint(1)
    graph_sync._write_checkpoint(tmp_path, required)
    rebuilds = 0
    registrations: list[graph_sync.GraphSyncCheckpoint] = []

    def rebuild(*_args, **_kwargs):
        nonlocal rebuilds
        rebuilds += 1
        raise AssertionError("incremental proof fallback rebuilt under writer authority")

    monkeypatch.setattr(index, "_rebuild_all_locked", rebuild)
    monkeypatch.setattr(
        graph_sync,
        "register_rebuild",
        lambda _root, checkpoint, _builder, **_kwargs: registrations.append(checkpoint),
    )

    report = index._refresh_paths_locked([note], graph_checkpoint=required)

    assert report["deferred"] == 1
    assert rebuilds == 0
    assert registrations == [required]


def test_no_checkpoint_or_paths_skips_graph_fanout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem.epistemic_graph import EpistemicGraphIndex, upsert_after_write

    calls = 0

    def refresh(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("Records-only fanout must not touch the graph")

    monkeypatch.setattr(EpistemicGraphIndex, "refresh_paths", refresh)

    upsert_after_write(tmp_path, [])

    assert calls == 0


def test_rollback_compatibility_disables_new_scheduling_but_keeps_epoch_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.epistemic_graph import EpistemicGraphIndex, upsert_after_write

    note = tmp_path / "Knowledge Base/Notes/Insights/compatibility.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Compatibility\n", encoding="utf-8")
    checkpoint = _checkpoint(1)
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(tmp_path, checkpoint)
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    calls = 0

    def scheduled(*_args, **_kwargs) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(
        EpistemicGraphIndex,
        "refresh_paths",
        scheduled,
    )
    monkeypatch.setattr(
        graph_sync,
        "register_rebuild",
        scheduled,
    )

    upsert_after_write(tmp_path, [note])

    assert calls == 0
    assert graph_sync.read_checkpoint(tmp_path) == checkpoint
    assert graph_sync.recover_checkpoint(tmp_path) == checkpoint


def test_incremental_graph_ack_rollback_keeps_prior_rows_and_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import file_watcher, freshness
    from exomem import find as find_module
    from exomem.epistemic_graph import EpistemicGraphIndex

    note = tmp_path / "Knowledge Base/Notes/Insights/atomic.md"
    vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(
                note, "---\ntype: insight\nstatus: active\n---\n# Before\n"
            )
        ],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    freshness.seed(
        tmp_path,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(tmp_path)),
    )
    freshness.seed(
        tmp_path,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(tmp_path / "Knowledge Base")
        ),
    )
    index = EpistemicGraphIndex(tmp_path)
    index.rebuild_all()
    prior_checkpoint = graph_sync.read_checkpoint(tmp_path)
    assert prior_checkpoint is not None
    prior_hash = vault_module.content_hash(note.read_text(encoding="utf-8"))

    vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(
                note, "---\ntype: insight\nstatus: active\n---\n# After\n"
            )
        ],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    current_checkpoint = graph_sync.read_checkpoint(tmp_path)
    assert current_checkpoint is not None
    assert current_checkpoint.generation == prior_checkpoint.generation + 1
    file_watcher._publish_registry_change(tmp_path, [note], [])

    def cut_publication(*_args, **_kwargs) -> None:
        raise RuntimeError("cut before incremental commit")

    monkeypatch.setattr(index, "_publish_available_marker_in_transaction", cut_publication)
    with pytest.raises(RuntimeError, match="cut before incremental commit"):
        index.refresh_paths([note], graph_checkpoint=current_checkpoint)

    with index._connect() as conn:
        row = conn.execute(
            "SELECT source_hash FROM graph_nodes WHERE node_key = ?",
            ("file:Knowledge Base/Notes/Insights/atomic.md",),
        ).fetchone()
        meta = dict(
            conn.execute(
                "SELECT key, value FROM graph_meta WHERE key IN "
                "('graph_sync_generation', 'graph_sync_digest')"
            )
        )
    assert row == (prior_hash,)
    assert meta == {
        "graph_sync_generation": str(prior_checkpoint.generation),
        "graph_sync_digest": prior_checkpoint.checkpoint_sha256,
    }


def test_incremental_predecessor_requires_a_valid_checkpoint_lineage(tmp_path: Path) -> None:
    from exomem import file_watcher, freshness
    from exomem import find as find_module
    from exomem.epistemic_graph import EpistemicGraphIndex

    note = tmp_path / "Knowledge Base/Notes/Insights/lineage.md"
    vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(
                note, "---\ntype: insight\nstatus: active\n---\n# Before\n"
            )
        ],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    freshness.seed(
        tmp_path,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(tmp_path)),
    )
    freshness.seed(
        tmp_path,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(tmp_path / "Knowledge Base")
        ),
    )
    index = EpistemicGraphIndex(tmp_path)
    index.rebuild_all()
    vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(
                note, "---\ntype: insight\nstatus: active\n---\n# After\n"
            )
        ],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    current_checkpoint = graph_sync.read_checkpoint(tmp_path)
    assert current_checkpoint is not None
    file_watcher._publish_registry_change(tmp_path, [note], [])

    with index._connect() as conn:
        conn.execute(
            "UPDATE graph_meta SET value = ? WHERE key = 'graph_sync_checkpoint'",
            ("{malformed",),
        )

    assert index._graph_sync_predecessor_available(current_checkpoint) is False


def test_single_flight_retries_for_new_checkpoint_and_never_releases_stale_waiter(
    tmp_path: Path,
) -> None:
    coordinator = graph_sync.GraphRebuildCoordinator(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    observed: list[int] = []

    def build(checkpoint: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        observed.append(checkpoint.generation)
        entered.set()
        if checkpoint.generation == 1:
            assert release.wait(2)
        return graph_sync.GraphBuildOutcome.covering(checkpoint)

    first = coordinator.start_or_join(_checkpoint(1), build)
    assert entered.wait(1)
    second = coordinator.start_or_join(_checkpoint(2), build)
    release.set()

    assert first.wait(2).covers(_checkpoint(1))
    assert second.wait(2).covers(_checkpoint(2))
    assert observed == [1, 2]


@pytest.mark.parametrize(
    ("state", "expected_remediation"),
    [
        (
            "current",
            "Retry the same mutation identity or run reconcile to recover the derived graph.",
        ),
        (
            "recovery_required",
            "Retry the same mutation identity or run reconcile to recover the derived graph.",
        ),
        (
            "unavailable",
            "Run maintain_memory(mode=\"reconcile\", dry_run=false, rebuild_graph=true) "
            "to recover the derived graph.",
        ),
    ],
)
def test_registered_builder_failure_logs_and_chains_a_content_free_state_aware_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    state: str,
    expected_remediation: str,
) -> None:
    """Arbitrary builder failures retain diagnostics without leaking into terminals."""
    coordinator = graph_sync.GraphRebuildCoordinator(tmp_path)
    checkpoint = _checkpoint(1)
    sentinel = "private builder path C:\\vault\\Knowledge Base\\secret.md"
    failure = RuntimeError(sentinel)

    monkeypatch.setattr(
        graph_sync,
        "status",
        lambda _root: {"state": state, "generation": checkpoint.generation},
    )

    def fail(_checkpoint: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        raise failure

    caplog.set_level("ERROR")
    waiter = coordinator.start_or_join(checkpoint, fail)
    with pytest.raises(graph_sync.GraphRebuildStopped) as stopped:
        waiter.wait(1)

    assert stopped.value.__cause__ is failure
    assert stopped.value.code == "GRAPH_SYNC_REBUILD_STOPPED"
    assert stopped.value.remediation == expected_remediation
    assert sentinel not in str(stopped.value)
    assert sentinel in caplog.text
    assert checkpoint.checkpoint_sha256 in caplog.text


def test_registered_unavailable_lineage_failure_projects_explicit_reset_without_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unavailable typed lineage failure keeps diagnostics out of the terminal."""
    coordinator = graph_sync.GraphRebuildCoordinator(tmp_path)
    checkpoint = _checkpoint(1)
    sentinel = "private lineage path C:\\vault\\Knowledge Base\\secret.md"
    failure = graph_sync.GraphEpochIncoherent(sentinel)

    monkeypatch.setattr(
        graph_sync,
        "status",
        lambda _root: {"state": "unavailable", "generation": checkpoint.generation},
    )

    def fail(_checkpoint: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        raise failure

    caplog.set_level("ERROR")
    waiter = coordinator.start_or_join(checkpoint, fail)
    with pytest.raises(graph_sync.GraphRebuildRegistrationError) as stopped:
        waiter.wait(1)

    assert type(stopped.value) is graph_sync.GraphRebuildRegistrationError
    assert stopped.value.__cause__ is failure
    assert stopped.value.code == "GRAPH_SYNC_LINEAGE_CONFLICT"
    assert stopped.value.remediation == (
        "Run maintain_memory(mode=\"reconcile\", dry_run=false, rebuild_graph=true) "
        "to recover the derived graph."
    )
    assert sentinel not in str(stopped.value)
    assert sentinel in caplog.text
    assert checkpoint.checkpoint_sha256 in caplog.text


def test_same_generation_with_a_different_digest_does_not_cover_a_waiter() -> None:
    first = _checkpoint(1)
    replacement = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="f" * 24,
        paths=first.paths,
        created_paths=first.created_paths,
    )

    assert graph_sync.GraphBuildOutcome.covering(first).covers(replacement) is False


def test_same_generation_split_flight_fails_original_waiter_promptly(tmp_path: Path) -> None:
    coordinator = graph_sync.GraphRebuildCoordinator(tmp_path)
    first_checkpoint = _checkpoint(1)
    replacement = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="f" * 24,
        paths=first_checkpoint.paths,
        created_paths=first_checkpoint.created_paths,
    )
    entered = threading.Event()
    release = threading.Event()

    def build(checkpoint: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        entered.set()
        assert release.wait(2)
        return graph_sync.GraphBuildOutcome.covering(checkpoint)

    first = coordinator.start_or_join(first_checkpoint, build)
    assert entered.wait(1)
    second = coordinator.start_or_join(replacement, build)

    with pytest.raises(graph_sync.GraphEpochIncoherent, match="same-generation"):
        first.wait(1)
    with pytest.raises(graph_sync.GraphEpochIncoherent, match="same-generation"):
        second.wait(1)
    release.set()


def test_original_index_publication_seam_rechecks_freshness_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.epistemic_graph import EpistemicGraphIndex

    note = tmp_path / "Knowledge Base/Notes/Insights/publication-race.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Before\n", encoding="utf-8")
    required = _checkpoint(1)
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(tmp_path, required)
    index = EpistemicGraphIndex(tmp_path)
    raced = False

    def race(_temporary: Path, _live: Path) -> None:
        nonlocal raced
        if not raced:
            raced = True
            note.write_text("# After\n", encoding="utf-8")
            freshness.mark_external_pending(tmp_path)

    monkeypatch.setattr(index, "_before_publish_replacement", race)

    index.rebuild_all()

    with index._connect() as conn:
        source_hash = conn.execute(
            "SELECT source_hash FROM graph_nodes WHERE node_key = ?",
            ("file:Knowledge Base/Notes/Insights/publication-race.md",),
        ).fetchone()
    assert raced is True
    assert source_hash == (vault_module.content_hash("# After\n"),)


def test_publication_hook_exception_discards_ticket_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.epistemic_graph import EpistemicGraphIndex

    note = tmp_path / "Knowledge Base/Notes/hook-retry.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Hook retry\n", encoding="utf-8")
    freshness.seed(tmp_path, "vault", [(str(note), freshness.stat_signature(note))])
    index = EpistemicGraphIndex(tmp_path)
    calls = 0

    def fail_once(_temporary: Path, _live: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("hook failed once")

    monkeypatch.setattr(index, "_before_publish_replacement", fail_once)

    index.rebuild_all()

    assert calls == 2
    assert index.available()


def test_persistent_publication_hook_failure_is_exact_stabilization_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    note = tmp_path / "Knowledge Base/Notes/hook-failure.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Before\n", encoding="utf-8")
    freshness.seed(tmp_path, "vault", [(str(note), freshness.stat_signature(note))])
    index = epistemic_graph.EpistemicGraphIndex(tmp_path)
    index.rebuild_all()
    old_live = index.path.read_bytes()

    def fail(_temporary: Path, _live: Path) -> None:
        raise RuntimeError("persistent hook failure")

    monkeypatch.setattr(index, "_before_publish_replacement", fail)

    with pytest.raises(graph_sync.GraphRebuildRegistrationError) as raised:
        index.rebuild_all()

    assert raised.value.code == "GRAPH_SYNC_STABILIZATION_EXHAUSTED"
    assert f"after {epistemic_graph.REBUILD_PUBLICATION_ATTEMPTS} attempts" in str(raised.value)
    assert index.path.read_bytes() == old_live


def test_canonical_publication_epoch_uses_only_floor_and_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint(1)
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(tmp_path, checkpoint)
    monkeypatch.setattr(
        graph_sync,
        "acknowledgement_state",
        lambda *_args: pytest.fail("canonical publication epoch must not read SQLite acknowledgement"),
    )

    assert graph_sync.canonical_publication_epoch(tmp_path) == graph_sync.GraphPublicationEpoch(
        graph_sync.GraphSyncGenerationFloor.create(1), checkpoint, None
    )


def test_rebuild_publication_hold_runs_no_disk_or_sqlite_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/availability.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Availability\n", encoding="utf-8")
    freshness.seed(vault, "vault", [(str(note), freshness.stat_signature(note))])
    index = epistemic_graph.EpistemicGraphIndex(vault)
    state_root = tmp_path / "state"
    state_root.mkdir()
    holding = False

    class Coordinator:
        def __init__(self, root: Path) -> None:
            self.state_root = root

        @contextmanager
        def hold(self, **_kwargs):
            nonlocal holding
            holding = True
            try:
                yield
            finally:
                holding = False

    index._mutation_coordinator = Coordinator(state_root)
    real_freshness = epistemic_graph._disk_vault_freshness
    real_connect = epistemic_graph.sqlite3.connect

    def no_disk_work(root: Path):
        assert not holding, "vault walk ran under publication hold"
        return real_freshness(root)

    def no_sqlite_work(*args, **kwargs):
        assert not holding, "SQLite opened under publication hold"
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(epistemic_graph, "_disk_vault_freshness", no_disk_work)
    monkeypatch.setattr(epistemic_graph.sqlite3, "connect", no_sqlite_work)

    index.rebuild_all()


def test_rebuild_stops_after_bounded_cold_recall_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    index = epistemic_graph.EpistemicGraphIndex(tmp_path)
    preparations = 0
    reconciles = 0

    def unavailable(*_args, **_kwargs):
        nonlocal preparations
        preparations += 1
        return None

    def reconcile() -> None:
        nonlocal reconciles
        reconciles += 1

    monkeypatch.setattr(freshness, "prepare_recall_publication", unavailable)
    monkeypatch.setattr(index, "_reconcile_recall_publication", reconcile)

    with pytest.raises(graph_sync.GraphRebuildRegistrationError) as raised:
        index.rebuild_all()

    assert raised.value.code == "GRAPH_SYNC_STABILIZATION_EXHAUSTED"
    assert f"after {epistemic_graph.REBUILD_PUBLICATION_ATTEMPTS} attempts" in str(raised.value)
    assert preparations == epistemic_graph.REBUILD_PUBLICATION_ATTEMPTS
    assert reconciles == epistemic_graph.REBUILD_PUBLICATION_ATTEMPTS


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement contract")
def test_publication_rejects_a_hook_swapped_temp_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/symlink-race.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Safe\n", encoding="utf-8")
    freshness.seed(vault, "vault", [(str(note), freshness.stat_signature(note))])
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    old_live = index.path.read_bytes()
    target = tmp_path / "attacker.sqlite"
    target.write_bytes(b"attacker")

    def swap(temporary: Path, _live: Path) -> None:
        temporary.unlink()
        temporary.symlink_to(target)

    monkeypatch.setattr(index, "_before_publish_replacement", swap)

    with pytest.raises(RuntimeError, match="did not stabilize"):
        index.rebuild_all()

    assert index.path.read_bytes() == old_live


@pytest.mark.skipif(os.name != "nt", reason="native Windows reparse contract")
def test_publication_rejects_a_hook_swapped_temp_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/reparse-race.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Safe\n", encoding="utf-8")
    freshness.seed(vault, "vault", [(str(note), freshness.stat_signature(note))])
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    old_live = index.path.read_bytes()
    target = tmp_path / "attacker.sqlite"
    target.write_bytes(b"attacker")
    probe = tmp_path / "reparse-probe.sqlite"
    created = subprocess.run(
        ["cmd", "/c", "mklink", str(probe), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode:
        pytest.skip("Windows file reparse creation is unavailable")
    probe.unlink()

    def swap(temporary: Path, _live: Path) -> None:
        temporary.unlink()
        linked = subprocess.run(
            ["cmd", "/c", "mklink", str(temporary), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert linked.returncode == 0

    monkeypatch.setattr(index, "_before_publish_replacement", swap)

    with pytest.raises(RuntimeError, match="did not stabilize"):
        index.rebuild_all()

    assert index.path.read_bytes() == old_live


def test_temporary_identity_rejects_a_windows_reparse_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, mutation_lock

    temporary = tmp_path / "temporary.sqlite"
    temporary.write_bytes(b"sqlite")
    info = temporary.lstat()
    monkeypatch.setattr(
        mutation_lock.os,
        "lstat",
        lambda _path: SimpleNamespace(
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_mode=info.st_mode,
            st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns,
            st_file_attributes=0x400,
        ),
    )

    with pytest.raises(OSError, match="no-follow"):
        epistemic_graph.EpistemicGraphIndex._temporary_identity(temporary)


def test_private_ticket_handles_special_character_vault_paths(tmp_path: Path) -> None:
    from exomem import epistemic_graph

    vault = tmp_path / "vault?#"
    note = vault / "Knowledge Base/Notes/escaped.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Escaped\n", encoding="utf-8")
    freshness.seed(vault, "vault", [(str(note), freshness.stat_signature(note))])
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="1" * 24,
        paths=(("Knowledge Base/Notes/escaped.md", vault_module.content_hash("# Escaped\n")),),
        created_paths=("Knowledge Base/Notes/escaped.md",),
    )
    graph_sync._write_floor(vault, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(vault, checkpoint)

    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()

    assert index.available()


def test_publication_retries_when_access_policy_changes_at_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/controlled.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Controlled\n", encoding="utf-8")
    freshness.seed(vault, "vault", [(str(note), freshness.stat_signature(note))])
    index = epistemic_graph.EpistemicGraphIndex(vault)
    changed = False

    def change_policy(_temporary: Path, _live: Path) -> None:
        nonlocal changed
        if not changed:
            changed = True
            (vault / "Knowledge Base/_access.yaml").write_text(
                "excluded:\n  - Notes\n", encoding="utf-8"
            )

    monkeypatch.setattr(index, "_before_publish_replacement", change_policy)

    index.rebuild_all()

    assert changed
    assert index.available()
    assert index.nodes(path="Knowledge Base/Notes/controlled.md") == []


def test_rebuild_refuses_a_malformed_live_acknowledgement(tmp_path: Path) -> None:
    from exomem import epistemic_graph

    note = tmp_path / "Knowledge Base/Notes/ack.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Ack\n", encoding="utf-8")
    freshness.seed(tmp_path, "vault", [(str(note), freshness.stat_signature(note))])
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="1" * 24,
        paths=(("Knowledge Base/Notes/ack.md", vault_module.content_hash("# Ack\n")),),
        created_paths=("Knowledge Base/Notes/ack.md",),
    )
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(tmp_path, checkpoint)
    index = epistemic_graph.EpistemicGraphIndex(tmp_path)
    index.rebuild_all()
    with index._connect() as conn:
        conn.execute(
            "UPDATE graph_meta SET value = ? WHERE key = 'graph_sync_digest'", ("f" * 64,)
        )

    with pytest.raises(graph_sync.GraphEpochIncoherent):
        index.rebuild_all()


def test_higher_full_recovery_replaces_an_older_malformed_acknowledgement(
    tmp_path: Path,
) -> None:
    from exomem import epistemic_graph

    note = tmp_path / "Knowledge Base/Notes/ack-recovery.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Ack recovery\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    index = epistemic_graph.EpistemicGraphIndex(tmp_path)
    index.rebuild_all()
    with index._connect() as conn:
        conn.execute(
            "UPDATE graph_meta SET value = ? WHERE key = 'graph_sync_digest'", ("f" * 64,)
        )
    recovery = graph_sync.GraphSyncCheckpoint.create(
        generation=2,
        mutation_id="2" * 24,
        paths=(),
        created_paths=(),
        scope="full",
    )
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))
    graph_sync._write_checkpoint(tmp_path, recovery)

    index.rebuild_all()

    assert graph_sync.acknowledged_checkpoint(tmp_path) == graph_sync.GraphBuildOutcome.covering(
        recovery
    )
    assert index.available()


def test_rebuild_replaces_an_exact_current_acknowledgement(tmp_path: Path) -> None:
    from exomem import epistemic_graph

    note = tmp_path / "Knowledge Base/Notes/current-ack.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Current acknowledgement\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    index = epistemic_graph.EpistemicGraphIndex(tmp_path)
    index.rebuild_all()
    required = graph_sync.read_checkpoint(tmp_path)

    index.rebuild_all()

    assert required is not None
    assert graph_sync.acknowledged_checkpoint(tmp_path) == graph_sync.GraphBuildOutcome.covering(
        required
    )
    assert index.available()


def test_single_flight_caps_waiter_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph_sync, "MAX_GRAPH_REBUILD_WAITERS", 1)
    coordinator = graph_sync.GraphRebuildCoordinator(tmp_path)
    release = threading.Event()

    def build(checkpoint: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        assert release.wait(2)
        return graph_sync.GraphBuildOutcome.covering(checkpoint)

    first = coordinator.start_or_join(_checkpoint(1), build)
    rejected = coordinator.start_or_join(_checkpoint(1), build)
    with pytest.raises(graph_sync.GraphWaiterCapacityError):
        rejected.wait()
    release.set()
    assert first.wait(2).covers(_checkpoint(1))


def test_temp_sidecar_is_private_until_atomic_publication_and_reconcile_sweeps_abandoned(
    tmp_path: Path,
) -> None:
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"old")
    temporary = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    temporary.write_bytes(b"new")

    assert graph_sync.readable_sidecar(live) == live
    assert graph_sync.readable_sidecar(temporary) is None
    assert graph_sync.sweep_abandoned_temporaries(tmp_path, live, live_paths=set()) == [temporary]
    assert live.read_bytes() == b"old"


def test_temp_sweep_is_proof_scoped_to_well_formed_graph_rebuild_artifacts(tmp_path: Path) -> None:
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    abandoned = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    unrelated = live.with_name(".graph-rebuild-user-copy.sqlite")
    abandoned.write_bytes(b"abandoned")
    unrelated.write_bytes(b"user")

    assert graph_sync.sweep_abandoned_temporaries(tmp_path, live, live_paths=set()) == [abandoned]
    assert unrelated.read_bytes() == b"user"


def test_temp_sweep_preserves_a_live_owner_from_another_process(tmp_path: Path) -> None:
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    temporary = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    temporary.write_bytes(b"building")
    assert graph_sync.claim_rebuild_owner(tmp_path, temporary) is True
    try:
        assert graph_sync.sweep_abandoned_temporaries(tmp_path, live, live_paths=set()) == []
        assert temporary.exists()
    finally:
        graph_sync.release_rebuild_owner(tmp_path, temporary)


def test_cross_process_rebuild_lock_rejects_a_second_builder(tmp_path: Path) -> None:
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    first = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    second = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    owner = context.Process(
        target=_hold_cross_process_rebuild_lock,
        args=(str(tmp_path), str(first), ready, release),
    )
    owner.start()
    try:
        assert ready.wait(5)
        assert graph_sync.claim_rebuild_owner(tmp_path, second) is False
        assert not (tmp_path / "Knowledge Base/.graph-rebuild.owner").exists()
    finally:
        release.set()
        owner.join(10)
    assert owner.exitcode == 0


def test_standalone_write_joins_rebuild_and_exits_without_live_temporary(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    started = tmp_path / "rebuild-started"
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path

        from exomem import graph_sync, vault
        from exomem.epistemic_graph import EpistemicGraphIndex

        root = Path(sys.argv[1])
        started = Path(sys.argv[2])
        original = EpistemicGraphIndex._rebuild_all_locked

        def rebuild(self):
            started.write_text("started", encoding="utf-8")
            return original(self)

        EpistemicGraphIndex._rebuild_all_locked = rebuild
        vault.batch_atomic_write(
            [vault.PlannedWrite(root / "Knowledge Base/Notes/standalone.md", "# Standalone\\n")],
            vault_root=root,
        )
        assert started.exists(), "direct rebuild never started"
        assert graph_sync.read_checkpoint(root) is not None
        assert graph_sync.status(root)["state"] == "current"
        assert EpistemicGraphIndex(root).available()
        assert not graph_sync._REBUILD_LOCK_HANDLES
        assert not list((root / "Knowledge Base").glob(".graph-rebuild-*.sqlite"))
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script, str(vault_root), str(started)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Exception ignored" not in completed.stderr
    assert "PytestUnraisableExceptionWarning" not in completed.stderr
    assert "ResourceWarning" not in completed.stderr
    checkpoint = graph_sync.read_checkpoint(vault_root)
    assert checkpoint is not None
    assert graph_sync.recover_checkpoint(vault_root) == checkpoint


@pytest.mark.skipif(os.name == "nt", reason="fork descriptor inheritance is POSIX-only")
def test_fork_child_drops_inherited_graph_lock_without_unlocking_parent(tmp_path: Path) -> None:
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    parent_temporary = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    child_temporary = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    competitor_temporary = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    fork_context = multiprocessing.get_context("fork")
    result = fork_context.Queue()

    assert graph_sync.claim_rebuild_owner(tmp_path, parent_temporary) is True
    child = fork_context.Process(
        target=_forked_graph_lock_state,
        args=(str(tmp_path), str(child_temporary), result),
    )
    child.start()
    child.join(10)
    try:
        assert child.exitcode == 0
        assert result.get(timeout=2) == (False, False)

        spawn_context = multiprocessing.get_context("spawn")
        competitor_result = spawn_context.Queue()
        competitor = spawn_context.Process(
            target=_attempt_cross_process_rebuild_lock,
            args=(str(tmp_path), str(competitor_temporary), competitor_result),
        )
        competitor.start()
        competitor.join(10)
        assert competitor.exitcode == 0
        assert competitor_result.get(timeout=2) is False
    finally:
        graph_sync.release_rebuild_owner(tmp_path, parent_temporary)


def test_rebuild_lock_release_is_idempotent(tmp_path: Path) -> None:
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    temporary = graph_sync.temporary_sidecar_path(live, _checkpoint(1))

    assert graph_sync.claim_rebuild_owner(tmp_path, temporary) is True
    graph_sync.release_rebuild_owner(tmp_path, temporary)
    graph_sync.release_rebuild_owner(tmp_path, temporary)
    assert graph_sync.claim_rebuild_owner(tmp_path, temporary) is True
    graph_sync.release_rebuild_owner(tmp_path, temporary)


@pytest.mark.skipif(os.name != "nt", reason="native Windows lock contract")
def test_windows_rebuild_lock_claims_persists_and_refuses_parent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "runtime-state"))
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    temporary = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    lock = graph_sync._rebuild_lock_path(tmp_path)

    assert graph_sync.claim_rebuild_owner(tmp_path, temporary) is True
    try:
        assert lock.is_file()
        with pytest.raises(PermissionError):
            os.replace(lock.parent, lock.parent.with_name("moved-rebuild-locks"))
    finally:
        graph_sync.release_rebuild_owner(tmp_path, temporary)

    assert graph_sync.claim_rebuild_owner(tmp_path, temporary) is True
    graph_sync.release_rebuild_owner(tmp_path, temporary)


@pytest.mark.skipif(os.name != "nt", reason="native Windows lock contract")
def test_windows_rebuild_lock_rejects_a_reparse_lock_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "runtime-state"
    target = tmp_path / "reparse-target"
    target.mkdir()
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(state_root), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode:
        pytest.skip("junction creation is unavailable on this Windows host")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_root))

    with pytest.raises(graph_sync.GraphRebuildLockUnavailable):
        graph_sync.claim_rebuild_owner(tmp_path, tmp_path / "temporary.sqlite")


def test_runtime_readiness_projects_checkpoint_health_without_vault_paths() -> None:
    snapshot = runtime_readiness.build_runtime_readiness(
        coordination={
            "enabled": False,
            "role": "standalone",
            "replica_id": None,
            "coordinator_healthy": True,
            "graph_sync": {"state": "recovery_required", "generation": 2},
        },
        release="1.2.3",
        mcp_tool_surface_sha256="a" * 64,
    )

    assert snapshot["coordination"]["graph_sync"] == {
        "state": "recovery_required",
        "generation": 2,
    }
    assert "Knowledge Base" not in repr(snapshot)


def test_reconcile_recovers_an_unacknowledged_checkpoint_without_rewriting_markdown(
    tmp_path: Path,
) -> None:
    note = tmp_path / "Knowledge Base/Notes/Insights/recovery.md"
    body = "---\ntype: insight\nstatus: active\n---\n# Recovery\n"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, body)], vault_root=tmp_path, post_commit_fanout=False
    )

    report = reconcile_module.reconcile(tmp_path)

    assert note.read_text(encoding="utf-8") == body
    assert graph_sync.status(tmp_path)["state"] == "current"
    assert report.graph_status == "refreshed"


def test_manager_reconcile_releases_canonical_boundary_before_graph_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconcile graph rebuild must not retain the command mutation boundary."""
    from types import SimpleNamespace

    from exomem.commands import commands_for
    from exomem.epistemic_graph import EpistemicGraphIndex
    from exomem.writer_lease import LeaseConfig, LeaseManager

    note = tmp_path / "Knowledge Base/Notes/Insights/reconcile-boundary.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Reconcile boundary\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    rebuild_started = threading.Event()
    release_rebuild = threading.Event()
    second_admitted = threading.Event()
    original = EpistemicGraphIndex._rebuild_all_locked

    def slow_rebuild(index: EpistemicGraphIndex) -> dict[str, int]:
        rebuild_started.set()
        assert release_rebuild.wait(2)
        return original(index)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_locked", slow_rebuild)
    state_dir = tmp_path / "state"
    reconcile_manager = LeaseManager(
        LeaseConfig(state_dir=state_dir), mutation_timeout_seconds=1
    )
    mutation_manager = LeaseManager(
        LeaseConfig(state_dir=state_dir), mutation_timeout_seconds=0.1
    )
    reconcile_command = next(
        command
        for command in commands_for("mcp")
        if command.name == "reconcile"
    )

    def canonical_leaf(vault_root: Path) -> dict[str, str]:
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(
                    vault_root / "Knowledge Base/Records/concurrent.md",
                    "# Concurrent canonical mutation\n",
                )
            ],
            vault_root=vault_root,
            post_commit_fanout=False,
        )
        second_admitted.set()
        return {"status": "committed"}

    second_command = SimpleNamespace(
        name="canonical_mutation", read_only=False, leaf=canonical_leaf
    )
    reconcile_result: list[object] = []

    def run_reconcile() -> None:
        reconcile_result.append(
            reconcile_manager.invoke(
                reconcile_command, (tmp_path,), {"response_detail": "legacy"}
            )
        )

    reconcile_thread = threading.Thread(target=run_reconcile)
    reconcile_thread.start()
    assert rebuild_started.wait(1)
    try:
        mutation_manager.invoke(second_command, (tmp_path,), {})
        assert second_admitted.is_set()
    finally:
        release_rebuild.set()
        reconcile_thread.join(timeout=3)

    assert not reconcile_thread.is_alive()
    assert reconcile_result[0]["graph_status"] == "refreshed", reconcile_result
    assert reconcile_result[0]["graph_refreshed"] == 1
    assert graph_sync.status(tmp_path)["state"] == "current"


def test_manager_rebuild_graph_finalizes_unavailable_reset_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manager sees the private handoff, clears once, and never returns it."""
    from exomem.commands import commands_for
    from exomem.writer_lease import LeaseConfig, LeaseManager

    note = tmp_path / "Knowledge Base/Notes/Insights/rebuild-finalizer.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Rebuild finalizer\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    graph_sync._write_checkpoint(tmp_path, _checkpoint(1))
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))
    stale_graph = tmp_path / "Knowledge Base/.graph.sqlite"
    stale_graph.write_bytes(b"unavailable graph")
    cleaned: list[tuple[Path, str, graph_sync.GraphSyncCheckpoint]] = []
    original_cleanup = graph_sync.cleanup_published_graph_lineage_reset

    def observe_cleanup(
        root: Path, operation_id: str, checkpoint: graph_sync.GraphSyncCheckpoint
    ) -> bool:
        cleaned.append((root, operation_id, checkpoint))
        return original_cleanup(root, operation_id, checkpoint)

    monkeypatch.setattr(
        graph_sync, "cleanup_published_graph_lineage_reset", observe_cleanup
    )
    command = next(command for command in commands_for("mcp") if command.name == "reconcile")
    result = LeaseManager(LeaseConfig(state_dir=tmp_path / "state")).invoke(
        command,
        (tmp_path,),
        {"rebuild_graph": True, "response_detail": "full"},
        idempotency_key="unavailable-reset-finalizer",
    )

    assert result["diagnostics"]["graph_rebuild_status"] == "cleared"
    assert len(cleaned) == 1
    assert result["diagnostics"]["graph_quarantine_id"] == cleaned[0][1]
    assert cleaned[0][0] == tmp_path
    assert cleaned[0][2] == graph_sync.read_checkpoint(tmp_path)
    assert "_graph_rebuild_handoff" not in repr(result)


def test_manager_rebuild_graph_resumes_canonical_handoff_after_restart_without_waiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canonical reset handoff survives the crash window and clears on replay."""
    import pickle

    from exomem.commands import commands_for
    from exomem.writer_lease import LeaseConfig, LeaseManager

    note = tmp_path / "Knowledge Base/Notes/Insights/rebuild-resume.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Rebuild resume\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    graph_sync._write_checkpoint(tmp_path, _checkpoint(1))
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(2))
    (tmp_path / "Knowledge Base/.graph.sqlite").write_bytes(b"unavailable graph")
    command = next(command for command in commands_for("mcp") if command.name == "reconcile")
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    original_wait = graph_sync.wait_for_registered

    def crash_before_finalization(*_args: object, **_kwargs: object) -> None:
        raise SystemExit("synthetic finalizer crash")

    monkeypatch.setattr(graph_sync, "wait_for_registered", crash_before_finalization)
    with pytest.raises(SystemExit, match="synthetic finalizer crash"):
        manager.invoke(
            command,
            (tmp_path,),
            {"rebuild_graph": True},
            idempotency_key="unavailable-reset-resume",
        )
    with manager.idempotency._connect() as connection:
        state, payload = connection.execute(
            "SELECT state, result FROM mutations"
        ).fetchone()
    canonical = pickle.loads(payload)
    assert state == "canonically_committed"
    assert "_graph_rebuild_handoff" in canonical["leaf_result"]

    graph_sync._PENDING_WAITERS.set(None)
    assert graph_sync.registered_checkpoint(tmp_path, state_root=tmp_path / "state") is None
    monkeypatch.setattr(graph_sync, "wait_for_registered", original_wait)
    restarted = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    resumed = restarted.invoke(
        command,
        (tmp_path,),
        {"rebuild_graph": True, "response_detail": "full"},
        idempotency_key="unavailable-reset-resume",
    )

    assert resumed["diagnostics"]["graph_rebuild_status"] == "cleared"
    assert "_graph_rebuild_handoff" not in repr(resumed)
    with restarted.idempotency._connect() as connection:
        state, payload = connection.execute(
            "SELECT state, result FROM mutations"
        ).fetchone()
    assert state == "completed"
    assert "_graph_rebuild_handoff" not in pickle.loads(payload)["leaf_result"]


def test_legacy_refresh_releases_canonical_boundary_before_missing_sidecar_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy refresh rebuilds privately after, not inside, graph authority."""
    from types import SimpleNamespace

    from exomem.epistemic_graph import EpistemicGraphIndex
    from exomem.writer_lease import LeaseConfig, LeaseManager

    note = tmp_path / "Knowledge Base/Notes/Insights/legacy-refresh.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Legacy refresh\n", encoding="utf-8")
    rebuild_started = threading.Event()
    release_rebuild = threading.Event()
    concurrent_writer_admitted = threading.Event()
    original = EpistemicGraphIndex._rebuild_all_locked

    def slow_rebuild(index: EpistemicGraphIndex) -> dict[str, int]:
        assert index.path.name.startswith(".graph-rebuild-")
        rebuild_started.set()
        assert release_rebuild.wait(2)
        return original(index)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_locked", slow_rebuild)
    state_dir = tmp_path / "state"
    refresh_manager = LeaseManager(LeaseConfig(state_dir=state_dir, mutation_timeout_seconds=1))
    writer_manager = LeaseManager(LeaseConfig(state_dir=state_dir, mutation_timeout_seconds=0.1))
    index = EpistemicGraphIndex(
        tmp_path,
        mutation_coordinator=refresh_manager._mutation_coordinator_for(tmp_path),
    )

    def canonical_leaf(vault_root: Path) -> dict[str, str]:
        record = vault_root / "Knowledge Base/Records/concurrent.md"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("# Concurrent canonical mutation\n", encoding="utf-8")
        concurrent_writer_admitted.set()
        return {"status": "committed"}

    writer_command = SimpleNamespace(
        name="canonical_mutation", read_only=False, leaf=canonical_leaf
    )
    reports: list[dict[str, int]] = []

    refresh_thread = threading.Thread(
        target=lambda: reports.append(index.refresh_paths([note]))
    )
    refresh_thread.start()
    assert rebuild_started.wait(1)
    try:
        writer_manager.invoke(writer_command, (tmp_path,), {})
        assert concurrent_writer_admitted.is_set()
    finally:
        release_rebuild.set()
        refresh_thread.join(timeout=3)

    assert not refresh_thread.is_alive()
    assert reports[0]["indexed_files"] == 1
    assert index.available() is True


def test_manager_maintain_memory_reconcile_projects_graph_repair_across_terminal_details(
    tmp_path: Path,
) -> None:
    from exomem.commands import product_commands_for
    from exomem.writer_lease import LeaseConfig, LeaseManager

    command = next(
        command
        for command in product_commands_for("mcp")
        if command.name == "maintain_memory"
    )

    def reconcile_detail(name: str, detail: str | None = None) -> object:
        vault_root = tmp_path / name
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(
                    vault_root / "Knowledge Base/Notes/Insights/reconcile-projection.md",
                    "# Reconcile projection\n",
                )
            ],
            vault_root=vault_root,
            post_commit_fanout=False,
        )
        kwargs = {"mode": "reconcile"}
        if detail is not None:
            kwargs["response_detail"] = detail
        return LeaseManager(LeaseConfig(state_dir=vault_root / "state")).invoke(
            command, (vault_root,), kwargs
        )

    compact = reconcile_detail("compact")
    assert compact["graph_sync"] == "completed"
    assert "diagnostics" not in compact

    full = reconcile_detail("full", "full")

    assert full["graph_sync"] == "completed"
    assert full["diagnostics"]["graph_status"] == "refreshed"
    assert full["diagnostics"]["graph_refreshed"] == 1
    assert "_graph_reconcile_registered" not in full["diagnostics"]

    legacy = reconcile_detail("legacy", "legacy")
    assert legacy["graph_status"] == "refreshed"
    assert legacy["graph_refreshed"] == 1


def test_malformed_checkpoint_refuses_an_acknowledged_graph_and_reconcile_keeps_markdown(
    tmp_path: Path,
) -> None:
    note = tmp_path / "Knowledge Base/Notes/Insights/malformed.md"
    body = "---\ntype: insight\nstatus: active\n---\n# Malformed\n"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, body)], vault_root=tmp_path, post_commit_fanout=False
    )
    from exomem.epistemic_graph import EpistemicGraphIndex

    EpistemicGraphIndex(tmp_path).rebuild_all()
    graph_sync.checkpoint_path(tmp_path).write_text("{not-json", encoding="utf-8")

    assert EpistemicGraphIndex(tmp_path).available() is False
    report = reconcile_module.reconcile(tmp_path)

    assert note.read_text(encoding="utf-8") == body
    assert graph_sync.status(tmp_path)["state"] == "current"
    assert EpistemicGraphIndex(tmp_path).available() is True
    assert report.graph_status == "refreshed"


def test_legacy_graph_without_a_checkpoint_remains_available(tmp_path: Path) -> None:
    note = tmp_path / "Knowledge Base/Notes/Insights/legacy.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\ntype: insight\nstatus: active\n---\n# Legacy\n", encoding="utf-8")
    from exomem.epistemic_graph import EpistemicGraphIndex

    index = EpistemicGraphIndex(tmp_path)
    index.rebuild_all()

    assert graph_sync.read_checkpoint(tmp_path) is None
    assert index.available() is True


def test_missing_checkpoint_after_acknowledgement_stays_recovery_required(tmp_path: Path) -> None:
    note = tmp_path / "Knowledge Base/Notes/Insights/missing.md"
    body = "---\ntype: insight\nstatus: active\n---\n# Missing\n"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, body)], vault_root=tmp_path, post_commit_fanout=False
    )
    from exomem.epistemic_graph import EpistemicGraphIndex

    index = EpistemicGraphIndex(tmp_path)
    index.rebuild_all()
    graph_sync.checkpoint_path(tmp_path).unlink()

    assert index.available() is False
    reconcile_module.reconcile(tmp_path)
    assert note.read_text(encoding="utf-8") == body
    assert graph_sync.status(tmp_path)["state"] == "current"
    assert EpistemicGraphIndex(tmp_path).available() is True


def test_reconcile_does_not_claim_current_when_the_generation_floor_is_malformed(
    tmp_path: Path,
) -> None:
    note = tmp_path / "Knowledge Base/Notes/Insights/malformed-floor.md"
    body = "---\ntype: insight\nstatus: active\n---\n# Malformed floor\n"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, body)], vault_root=tmp_path, post_commit_fanout=False
    )
    from exomem.epistemic_graph import EpistemicGraphIndex

    EpistemicGraphIndex(tmp_path).rebuild_all()
    graph_sync.floor_path(tmp_path).write_text("{not-json", encoding="utf-8")

    report = reconcile_module.reconcile(tmp_path)

    assert note.read_text(encoding="utf-8") == body
    assert graph_sync.status(tmp_path)["state"] == "unavailable"
    assert EpistemicGraphIndex(tmp_path).available() is False
    assert report.graph_status == "unavailable"


def test_reconcile_sweeps_only_abandoned_reserved_graph_temporaries(tmp_path: Path) -> None:
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    abandoned = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    abandoned.write_bytes(b"abandoned")

    reconcile_module.reconcile(tmp_path)

    assert not abandoned.exists()


def test_unacknowledged_checkpoint_requires_recovery_and_preserves_committed_terminal() -> None:
    required = _checkpoint(2)
    availability = graph_sync.availability(required, acknowledged=None)
    failure = graph_sync.committed_graph_failure(required)

    assert availability.available is False
    assert availability.reason == "checkpoint_unacknowledged"
    assert failure == {
        "graph_sync": "failed",
        "graph_sync_code": "GRAPH_SYNC_STABILIZATION_EXHAUSTED",
        "graph_sync_checkpoint": required.checkpoint_sha256,
        "graph_sync_remediation": "Run reconcile to recover the derived graph.",
    }


def test_committed_derived_failure_is_idempotently_replayed(tmp_path: Path) -> None:
    from exomem.writer_lease import IdempotencyStore

    store = IdempotencyStore(tmp_path / "idempotency.sqlite")
    required = _checkpoint(2)
    calls = 0

    def operation() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"mutated": True}

    def derived_failure(result: dict[str, object]) -> dict[str, object]:
        return {**result, **graph_sync.committed_graph_failure(required)}

    first = store.run(
        "same-request", "digest", operation, after_operation_guard=derived_failure
    )
    replay = store.run(
        "same-request", "digest", operation, after_operation_guard=derived_failure
    )

    assert calls == 1
    assert replay == first


def test_canonical_handoff_is_non_owned_until_off_boundary_graph_work_completes(
    tmp_path: Path,
) -> None:
    from exomem.writer_lease import IdempotencyStore

    store = IdempotencyStore(tmp_path / "idempotency.sqlite", wait_seconds=1)
    released_guard = threading.Event()
    release_graph = threading.Event()
    result: list[dict[str, object]] = []

    class Guard:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            released_guard.set()

    def run_owner() -> None:
        result.append(
            store.run(
                "graph-pending",
                "digest",
                lambda: {"state": "committed"},
                operation_guard=Guard,
                after_canonical_persisted=lambda value: value,
                after_operation_guard=lambda value: (
                    release_graph.wait(2) and {**value, "graph_sync": "completed"}
                ),
            )
        )

    owner = threading.Thread(target=run_owner)
    owner.start()
    assert released_guard.wait(1)
    with store._connect() as conn:
        assert conn.execute("SELECT state, owner FROM mutations").fetchone() == (
            "canonically_committed",
            None,
        )
    replay = threading.Thread(
        target=lambda: result.append(
            store.run(
                "graph-pending",
                "digest",
                lambda: (_ for _ in ()).throw(AssertionError("canonical leaf replayed")),
                resume_canonically_committed=lambda value: {**value, "graph_sync": "completed"},
            )
        )
    )
    replay.start()
    release_graph.set()
    owner.join(2)
    replay.join(2)

    assert result == [
        {"state": "committed", "graph_sync": "completed"},
        {"state": "committed", "graph_sync": "completed"},
    ]


def test_dead_graph_pending_owner_resumes_only_derived_graph_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pickle

    from exomem.writer_lease import IdempotencyStore

    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    store = IdempotencyStore(tmp_path / "idempotency.sqlite")
    vault = tmp_path / "vault"
    checkpoint = _checkpoint(1)
    graph_sync._write_floor(vault, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(vault, checkpoint)
    canonical = {
        "state": "committed",
        "graph_sync": "pending",
        "_graph_sync_checkpoint": checkpoint.as_dict(),
    }
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO mutations(key, digest, state, result, updated_at, owner) "
            "VALUES (?, ?, 'graph_pending', ?, ?, ?)",
            ("dead-graph", "digest", pickle.dumps(canonical), 0, "999999:deadowner00000002"),
        )

    resumed = store.run(
        "dead-graph",
        "digest",
        lambda: (_ for _ in ()).throw(AssertionError("canonical leaf replayed")),
        resume_canonically_committed=lambda result: {
            key: value
            for key, value in {**result, "graph_sync": "completed"}.items()
            if key != "_graph_sync_checkpoint"
        },
        commit_evidence=lambda: True,
        legacy_graph_pending_proof=lambda candidate: (
            graph_sync.read_checkpoint(vault) == candidate
            and graph_sync.classify_epoch(vault).kind == "coherent"
        ),
    )

    assert resumed == {"state": "committed", "graph_sync": "completed"}


def test_dead_pending_without_an_exact_receipt_is_outcome_unknown(tmp_path: Path) -> None:
    from exomem.writer_lease import IdempotencyStore, OpError

    store = IdempotencyStore(tmp_path / "idempotency.sqlite")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO mutations(key, digest, state, updated_at, owner) VALUES (?, ?, ?, ?, ?)",
            ("uncertain-cut", "digest", "pending", 0, "999999:deadowner00000003"),
        )

    with pytest.raises(OpError) as outcome:
        store.run(
            "uncertain-cut",
            "digest",
            lambda: (_ for _ in ()).throw(AssertionError("canonical leaf replayed")),
            commit_evidence=lambda: True,
        )
    assert outcome.value.code == "MUTATION_OUTCOME_UNKNOWN"


def test_crash_after_canonical_terminal_persistence_never_replays_the_write(tmp_path: Path) -> None:
    from exomem.writer_lease import IdempotencyStore

    database = tmp_path / "idempotency.sqlite"
    marker = tmp_path / "canonical-write"
    child = multiprocessing.get_context("spawn").Process(
        target=_crash_after_canonical_terminal, args=(str(database), str(marker))
    )
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 0
    assert marker.read_text(encoding="utf-8") == "committed"

    replayed = IdempotencyStore(database).run(
        "crash-window",
        "digest",
        lambda: (_ for _ in ()).throw(AssertionError("canonical write replayed")),
        resume_canonically_committed=lambda result: {**result, "graph_sync": "completed"},
    )
    assert replayed == {"state": "committed", "graph_sync": "completed"}


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses replacement while a reader is open")
def test_temporary_swap_keeps_an_open_reader_on_the_previous_sidecar(tmp_path: Path) -> None:
    import sqlite3

    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    with closing(sqlite3.connect(live)) as conn:
        conn.execute("CREATE TABLE value (item TEXT)")
        conn.execute("INSERT INTO value VALUES ('old')")
        conn.commit()
    temporary = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    with closing(sqlite3.connect(temporary)) as conn:
        conn.execute("CREATE TABLE value (item TEXT)")
        conn.execute("INSERT INTO value VALUES ('new')")
        conn.commit()

    reader = sqlite3.connect(live)
    try:
        assert reader.execute("SELECT item FROM value").fetchone() == ("old",)
        graph_sync.replace_sidecar(temporary, live)
        assert reader.execute("SELECT item FROM value").fetchone() == ("old",)
        with closing(sqlite3.connect(live)) as current:
            assert current.execute("SELECT item FROM value").fetchone() == ("new",)
    finally:
        reader.close()


def test_windows_replacement_refusal_keeps_the_previous_complete_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"old")
    temporary = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    temporary.write_bytes(b"new")
    monkeypatch.setattr(graph_sync.os, "replace", lambda *_args: (_ for _ in ()).throw(PermissionError()))

    with pytest.raises(graph_sync.GraphSidecarReplaceUnavailable):
        graph_sync.replace_sidecar(temporary, live)
    assert live.read_bytes() == b"old"
    assert temporary.read_bytes() == b"new"


def test_full_rebuild_replacement_refusal_retains_complete_temp_for_later_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from exomem.epistemic_graph import EpistemicGraphIndex

    note = tmp_path / "Knowledge Base/Notes/Insights/replacement-refusal.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Before\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    index = EpistemicGraphIndex(tmp_path)
    index.rebuild_all()
    old_live = index.path.read_bytes()
    note.write_text("# After\n", encoding="utf-8")
    retained: list[Path] = []
    original_replace = graph_sync.replace_sidecar

    def refuse_replacement(temporary: Path, _live: Path) -> None:
        retained.append(temporary)
        raise graph_sync.GraphSidecarReplaceUnavailable("live graph sidecar has an open reader")

    monkeypatch.setattr(graph_sync, "replace_sidecar", refuse_replacement)
    with pytest.raises(graph_sync.GraphSidecarReplaceUnavailable):
        index.rebuild_all()

    assert len(retained) == 1
    temporary = retained[0]
    assert index.path.read_bytes() == old_live
    assert temporary.exists()
    with closing(sqlite3.connect(temporary)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    monkeypatch.setattr(graph_sync, "replace_sidecar", original_replace)
    index.rebuild_all()
    assert index.path.read_bytes() != old_live
    assert temporary in graph_sync.sweep_abandoned_temporaries(
        tmp_path, index.path, live_paths=set()
    )
    assert not temporary.exists()


def test_temp_sweep_preserves_a_sharing_refused_abandoned_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    temporary = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    temporary.write_bytes(b"complete")
    original_unlink = Path.unlink

    def sharing_refused(path: Path, *, missing_ok: bool = False) -> None:
        if path == temporary:
            raise PermissionError("reader still holds the temporary")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", sharing_refused)

    assert graph_sync.sweep_abandoned_temporaries(tmp_path, live, live_paths=set()) == []
    assert temporary.read_bytes() == b"complete"


def test_temp_sweep_removes_abandoned_sqlite_companions_but_keeps_active_set(
    tmp_path: Path,
) -> None:
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    abandoned = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    active = graph_sync.temporary_sidecar_path(live, _checkpoint(1))
    abandoned_set = (
        abandoned,
        *(
            abandoned.with_name(f"{abandoned.name}{suffix}")
            for suffix in ("-journal", "-wal", "-shm")
        ),
    )
    active_set = (
        active,
        *(active.with_name(f"{active.name}{suffix}") for suffix in ("-journal", "-wal", "-shm")),
    )
    for path in (*abandoned_set, *active_set):
        path.write_bytes(b"sqlite artifact")

    removed = graph_sync.sweep_abandoned_temporaries(tmp_path, live, live_paths={active})

    assert set(removed) == set(abandoned_set)
    assert all(not path.exists() for path in abandoned_set)
    assert all(path.exists() for path in active_set)
