"""Failure-window coverage for graph-aware lifecycle transitions."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    delete_directory,
    delete_file,
    graph_sync,
    held_fs,
    index_sync,
    recall_policy,
    recover_from_trash,
    state_paths,
    writer_lease,
)
from exomem.governance import lifecycle


def _note(vault: Path, name: str = "transition.md") -> tuple[str, Path]:
    relative = f"Knowledge Base/Notes/Insights/{name}"
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# transition\n", encoding="utf-8")
    return relative, path


def _synthetic_windows_path(*parts: str) -> str:
    return "\\".join(("C:", "example", *parts))


@pytest.fixture
def _isolated_graph_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Give graph artifacts and receipts separate machine-local test roots."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "external-state"))
    lease_state = tmp_path / "writer-lease-state"
    lease_state.mkdir()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(lease_state))
    writer_lease.reset_managers_for_tests()
    yield vault
    writer_lease.reset_managers_for_tests()


def test_relocated_deletion_epoch_uses_the_canonical_content_prefix(
    monkeypatch: pytest.MonkeyPatch,
    _isolated_graph_state: Path,
) -> None:
    relative, _source = _note(_isolated_graph_state, "relocated-delete.md")
    outside = "Machine State/relocated-delete.md"
    monkeypatch.setattr(recall_policy, "is_recall_candidate", lambda *_args: True)

    epoch = graph_sync.prepare_deletion_epoch(
        _isolated_graph_state, [outside, relative]
    )

    assert epoch is not None
    assert epoch.checkpoint.paths == ((relative, None),)
    graph_sync.restore_deletion_epoch(epoch)


def test_relocated_recovery_epoch_uses_the_canonical_content_prefix(
    _isolated_graph_state: Path,
) -> None:
    relative, _source = _note(_isolated_graph_state, "relocated-recovery.md")
    outside = "Machine State/relocated-recovery.md"

    epoch = graph_sync.prepare_recovery_epoch(
        _isolated_graph_state,
        [(outside, "b" * 64), (relative, "a" * 64)],
    )

    assert epoch is not None
    assert epoch.checkpoint.paths == ((relative, "a" * 64),)
    assert epoch.checkpoint.created_paths == (relative,)
    graph_sync.restore_deletion_epoch(epoch)


def test_initially_absent_epoch_rollback_durably_flushes_external_deletions(
    monkeypatch: pytest.MonkeyPatch,
    _isolated_graph_state: Path,
) -> None:
    relative, _source = _note(_isolated_graph_state, "external-rollback.md")
    flushed: list[Path] = []
    monkeypatch.setattr(
        held_fs,
        "flush_directory_path",
        lambda path: flushed.append(Path(path)) or held_fs.HeldResult(value=None),
        raising=False,
    )

    epoch = graph_sync.prepare_deletion_epoch(_isolated_graph_state, [relative])
    assert epoch is not None
    graph_sync.commit_deletion_epoch(epoch)
    assert graph_sync.checkpoint_path(_isolated_graph_state).is_file()
    assert graph_sync.floor_path(_isolated_graph_state).is_file()

    graph_sync.restore_deletion_epoch(epoch)

    state_dir = state_paths.vault_state_dir(_isolated_graph_state)
    assert graph_sync.checkpoint_path(_isolated_graph_state).exists() is False
    assert graph_sync.floor_path(_isolated_graph_state).exists() is False
    assert flushed == [state_dir, state_dir]


def test_lifecycle_epoch_staging_never_marks_an_active_mutation_committed(
    tmp_path: Path,
) -> None:
    relative, source = _note(tmp_path)
    trace = writer_lease._ACTIVE_MUTATION_TRACE.set(("request", "delete_file", "receipt"))
    committed = writer_lease._ACTIVE_MUTATION_COMMITTED.set(False)
    try:
        transition = graph_sync.begin_deletion_transition(
            tmp_path,
            source_rel=relative,
            trash_rel="Knowledge Base/_trash/transition.md",
            removed_rel_paths=[relative],
        )
        assert transition.epoch is not None
        assert writer_lease._ACTIVE_MUTATION_COMMITTED.get() is False
        transition.abort()
        assert source.exists()
    finally:
        writer_lease._ACTIVE_MUTATION_COMMITTED.reset(committed)
        writer_lease._ACTIVE_MUTATION_TRACE.reset(trace)


def test_lifecycle_checkpoints_bind_active_lease_claims_for_delete_and_recovery(
    tmp_path: Path,
) -> None:
    from exomem.epistemic_graph import EpistemicGraphIndex
    from exomem.writer_lease import LeaseConfig, LeaseManager

    relative, source = _note(tmp_path, "lease-claim.md")
    source.write_text(
        "---\ntype: insight\nstatus: draft\n---\n# lease claim\n",
        encoding="utf-8",
    )
    EpistemicGraphIndex(tmp_path).rebuild_all()
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    trash_paths: list[str] = []

    def delete(root: Path) -> dict:
        result = delete_file.delete_file(root, path=relative, confirm=True)
        trash_paths.append(result.trash_path)
        return result.as_dict()

    manager.invoke(
        SimpleNamespace(
            name="delete_file",
            read_only=False,
            leaf=delete,
        ),
        (tmp_path,),
        {},
        idempotency_key="lease-claim-delete",
    )
    delete_checkpoint = graph_sync.read_checkpoint(tmp_path)
    receipt_dir = graph_sync.graph_commit_receipt_path(tmp_path, "0" * 24).parent
    delete_receipts = list(receipt_dir.glob("*.json"))
    assert delete_checkpoint is not None
    assert len(delete_receipts) == 1
    delete_receipt = graph_sync.GraphCommitReceipt.parse(delete_receipts[0].read_bytes())
    assert delete_receipt is not None
    assert delete_checkpoint.mutation_id == delete_receipt.commit_token
    assert delete_receipt.checkpoint_generation == delete_checkpoint.generation
    assert delete_receipt.checkpoint_sha256 == delete_checkpoint.checkpoint_sha256

    manager.invoke(
        SimpleNamespace(
            name="recover_from_trash",
            read_only=False,
            leaf=lambda root: recover_from_trash.recover_from_trash(
                root, trash_path=trash_paths[0]
            ).as_dict(),
        ),
        (tmp_path,),
        {},
        idempotency_key="lease-claim-recovery",
    )
    recovery_checkpoint = graph_sync.read_checkpoint(tmp_path)
    recovery_receipts = list(receipt_dir.glob("*.json"))
    assert recovery_checkpoint is not None
    assert len(recovery_receipts) == 2
    recovery_receipt = max(
        (graph_sync.GraphCommitReceipt.parse(path.read_bytes()) for path in recovery_receipts),
        key=lambda receipt: receipt.checkpoint_generation if receipt is not None else -1,
    )
    assert recovery_receipt is not None
    assert recovery_checkpoint.mutation_id == recovery_receipt.commit_token
    assert recovery_receipt.checkpoint_generation == recovery_checkpoint.generation
    assert recovery_receipt.checkpoint_sha256 == recovery_checkpoint.checkpoint_sha256


def test_rename_failure_after_floor_restores_prior_epoch_and_aborts_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative, source = _note(tmp_path, "rename-failure.md")

    def fail_rename(*_args: object, **_kwargs: object) -> None:
        raise lifecycle.LifecycleError("ATOMIC_MOVE_FAILED", "injected rename failure")

    monkeypatch.setattr(lifecycle, "atomic_rename", fail_rename)

    with pytest.raises(delete_file.DeleteFileError) as error:
        delete_file.delete_file(tmp_path, path=relative, confirm=True)

    assert error.value.code == "ATOMIC_MOVE_FAILED"
    assert source.exists()
    assert graph_sync.floor_path(tmp_path).exists() is False
    assert graph_sync.checkpoint_path(tmp_path).exists() is False


def test_raw_post_rename_fsync_error_durably_inverses_before_restoring_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw POSIX fsync refusal cannot bypass graph lifecycle rollback."""
    relative, source = _note(tmp_path, "raw-fsync-refusal.md")
    flushed: list[Path] = []
    original_fsync = lifecycle._fsync_directory

    def fail_forward_fsync(path: Path) -> None:
        flushed.append(path)
        if len(flushed) == 1:
            raise OSError(_synthetic_windows_path("private", "post-rename-fsync"))
        original_fsync(path)

    monkeypatch.setattr(lifecycle, "_fsync_directory", fail_forward_fsync)

    with pytest.raises(delete_file.DeleteFileError) as error:
        delete_file.delete_file(tmp_path, path=relative, confirm=True)

    assert error.value.code == "LIFECYCLE_PATH_UNSAFE"
    assert _synthetic_windows_path("private", "post-rename-fsync") not in str(error.value)
    assert source.exists()
    assert not list((tmp_path / "Knowledge Base" / "_trash").rglob("*raw-fsync-refusal.md"))
    assert graph_sync.floor_path(tmp_path).exists() is False
    assert graph_sync.checkpoint_path(tmp_path).exists() is False
    assert len(flushed) == 3
    assert flushed[0] == source.parent
    assert flushed[-1] == source.parent


def test_checkpoint_failure_after_rename_restores_exact_prior_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative, source = _note(tmp_path, "checkpoint-failure.md")
    prior = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="a" * 24,
        paths=((relative, "a" * 64),),
        created_paths=(relative,),
    )
    graph_sync._write_floor(tmp_path, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(tmp_path, prior)
    prior_floor = graph_sync.floor_path(tmp_path).read_bytes()
    prior_checkpoint = graph_sync.checkpoint_path(tmp_path).read_bytes()

    real_write = graph_sync._write_checkpoint

    def write_then_fail(root: Path, checkpoint: graph_sync.GraphSyncCheckpoint) -> None:
        real_write(root, checkpoint)
        raise PermissionError("injected checkpoint failure")

    monkeypatch.setattr(graph_sync, "_write_checkpoint", write_then_fail)

    with pytest.raises(delete_file.DeleteFileError) as error:
        delete_file.delete_file(tmp_path, path=relative, confirm=True)

    assert error.value.code == "GRAPH_SYNC_CHECKPOINT_FAILED"
    assert source.exists()
    assert graph_sync.floor_path(tmp_path).read_bytes() == prior_floor
    assert graph_sync.checkpoint_path(tmp_path).read_bytes() == prior_checkpoint


def test_recovery_epoch_setup_failure_aborts_the_staged_floor_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative, source = _note(tmp_path, "recovery-setup.md")
    source.write_text(
        "---\ntype: insight\nstatus: draft\n---\n# transition\n",
        encoding="utf-8",
    )
    deleted = delete_file.delete_file(tmp_path, path=relative, confirm=True)
    prior_floor = graph_sync.floor_path(tmp_path).read_bytes()
    prior_checkpoint = graph_sync.checkpoint_path(tmp_path).read_bytes()
    real_prepare = graph_sync.prepare_recovery_epoch

    def stage_then_fail(
        root: Path, restored: list[tuple[str, str]]
    ) -> graph_sync.GraphDeletionEpoch | None:
        real_prepare(root, restored)
        raise RuntimeError("injected recovery epoch setup failure")

    monkeypatch.setattr(graph_sync, "prepare_recovery_epoch", stage_then_fail)

    with pytest.raises(recover_from_trash.RecoverError) as error:
        recover_from_trash.recover_from_trash(tmp_path, trash_path=deleted.trash_path)

    assert error.value.code == "GRAPH_SYNC_EPOCH_FAILED"
    assert source.exists() is False
    assert (tmp_path / deleted.trash_path).exists()
    assert graph_sync.floor_path(tmp_path).read_bytes() == prior_floor
    assert graph_sync.checkpoint_path(tmp_path).read_bytes() == prior_checkpoint


@pytest.mark.parametrize("artifact", ["floor", "checkpoint"])
def test_oversized_prior_artifact_aborts_deletion_marker_before_epoch_staging(
    tmp_path: Path, artifact: str
) -> None:
    relative, _source = _note(tmp_path, f"oversized-{artifact}.md")
    _govern(tmp_path, relative)
    path = graph_sync.floor_path(tmp_path) if artifact == "floor" else graph_sync.checkpoint_path(tmp_path)
    limit = graph_sync._FLOOR_READ_LIMIT if artifact == "floor" else graph_sync._CHECKPOINT_READ_LIMIT
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(limit + 1)

    with pytest.raises(graph_sync.GraphLifecycleEpochSetupError):
        graph_sync.begin_deletion_transition(
            tmp_path,
            source_rel=relative,
            trash_rel="Knowledge Base/_trash/oversized.md",
            removed_rel_paths=[relative],
        )

    assert not list(
        (tmp_path / "Knowledge Base/_Governance/deletion-tombstones").glob("*.json")
    )


@pytest.mark.parametrize("artifact", ["floor", "checkpoint"])
def test_oversized_prior_artifact_aborts_recovery_marker_before_epoch_staging(
    tmp_path: Path, artifact: str
) -> None:
    relative, source = _note(tmp_path, f"oversized-recovery-{artifact}.md")
    source.write_text("---\ntype: insight\nstatus: draft\n---\n# transition\n", encoding="utf-8")
    _govern(tmp_path, relative)
    deleted = delete_file.delete_file(tmp_path, path=relative, confirm=True)
    path = graph_sync.floor_path(tmp_path) if artifact == "floor" else graph_sync.checkpoint_path(tmp_path)
    limit = graph_sync._FLOOR_READ_LIMIT if artifact == "floor" else graph_sync._CHECKPOINT_READ_LIMIT
    with path.open("wb") as stream:
        stream.truncate(limit + 1)

    with pytest.raises(graph_sync.GraphLifecycleEpochSetupError):
        graph_sync.begin_recovery_transition(
            tmp_path,
            trash_rel=deleted.trash_path,
            source_rel=relative,
            restored_paths=[(relative, "a" * 64)],
        )

    assert not list(
        (tmp_path / "Knowledge Base/_Governance/deletion-tombstones/recovery").glob("*.json")
    )


@pytest.mark.parametrize("artifact", ["floor", "checkpoint"])
def test_malformed_non_utf8_prior_artifact_aborts_deletion_before_rename(
    tmp_path: Path, artifact: str
) -> None:
    relative, source = _note(tmp_path, f"malformed-delete-{artifact}.md")
    _govern(tmp_path, relative)
    path = graph_sync.floor_path(tmp_path) if artifact == "floor" else graph_sync.checkpoint_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior_bytes = b"\xffmalformed epoch artifact"
    path.write_bytes(prior_bytes)

    with pytest.raises(graph_sync.GraphLifecycleEpochSetupError):
        graph_sync.begin_deletion_transition(
            tmp_path,
            source_rel=relative,
            trash_rel="Knowledge Base/_trash/malformed-delete.md",
            removed_rel_paths=[relative],
        )

    assert source.exists()
    assert path.read_bytes() == prior_bytes
    assert not list(
        (tmp_path / "Knowledge Base/_Governance/deletion-tombstones").glob("*.json")
    )


@pytest.mark.parametrize("artifact", ["floor", "checkpoint"])
def test_malformed_non_utf8_prior_artifact_aborts_recovery_before_rename(
    tmp_path: Path, artifact: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative, source = _note(tmp_path, f"malformed-recovery-{artifact}.md")
    source.write_text("---\ntype: insight\nstatus: draft\n---\n# transition\n", encoding="utf-8")
    _govern(tmp_path, relative)
    deleted = delete_file.delete_file(tmp_path, path=relative, confirm=True)
    path = graph_sync.floor_path(tmp_path) if artifact == "floor" else graph_sync.checkpoint_path(tmp_path)
    prior_bytes = b"\xffmalformed epoch artifact"
    path.write_bytes(prior_bytes)
    if artifact == "checkpoint":
        graph_sync.floor_path(tmp_path).unlink()
    monkeypatch.setattr(recall_policy, "is_recall_candidate", lambda *_args: True)

    with pytest.raises(graph_sync.GraphLifecycleEpochSetupError):
        graph_sync.begin_recovery_transition(
            tmp_path,
            trash_rel=deleted.trash_path,
            source_rel=relative,
            restored_paths=[(relative, "a" * 64)],
        )

    assert source.exists() is False
    assert (tmp_path / deleted.trash_path).exists()
    assert path.read_bytes() == prior_bytes
    assert not list(
        (tmp_path / "Knowledge Base/_Governance/deletion-tombstones/recovery").glob("*.json")
    )


def test_rollback_failure_preserves_durable_recovery_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative, source = _note(tmp_path, "rollback-failure.md")
    real_rename = lifecycle.atomic_rename

    def fail_only_inverse(
        operation: lifecycle.LifecycleOperation,
        *,
        source: Path,
        destination: Path,
        recovery: bool = False,
    ) -> None:
        if "_trash" in source.parts:
            raise lifecycle.LifecycleError("ATOMIC_MOVE_FAILED", "injected rollback failure")
        real_rename(operation, source=source, destination=destination, recovery=recovery)

    monkeypatch.setattr(lifecycle, "atomic_rename", fail_only_inverse)
    monkeypatch.setattr(
        graph_sync,
        "commit_deletion_epoch",
        lambda _epoch: (_ for _ in ()).throw(PermissionError("injected checkpoint failure")),
    )

    with pytest.raises(delete_file.DeleteFileError) as error:
        delete_file.delete_file(tmp_path, path=relative, confirm=True)

    assert error.value.code == "GRAPH_SYNC_DELETION_ROLLBACK_FAILED"
    assert source.exists() is False
    assert graph_sync.floor_path(tmp_path).exists()
    assert list((tmp_path / "Knowledge Base/_trash").rglob("*rollback-failure.md"))


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ("failed", "graph_handle_failed"),
        ("deferred", "deferred_durable"),
        ("unverified", "graph_outcome_unverified"),
    ],
)
def test_lifecycle_terminal_gate_rejects_nonexact_graph_outcomes(
    outcome: str, code: str
) -> None:
    report = {
        "paths_truncated": False,
        "reconcile_required": outcome == "failed",
        "components": [
            {"component": "epistemic_graph", "outcome": outcome, "code": code}
        ],
    }

    assert lifecycle._index_report_is_exact(report) is False


def test_lifecycle_terminal_gate_allows_a_registered_graph_handle() -> None:
    report = {
        "paths_truncated": False,
        "reconcile_required": False,
        "components": [
            {
                "component": "epistemic_graph",
                "outcome": "registered",
                "code": "rebuild_registered",
            }
        ],
    }

    assert lifecycle._index_report_is_exact(report) is True


def test_lifecycle_terminal_gate_requires_an_explicit_derived_report() -> None:
    assert lifecycle._index_report_is_exact(None) is False


def _govern(vault: Path, relative: str) -> None:
    governance = vault / "Knowledge Base/_Governance"
    scope = governance / "scopes/lifecycle.yaml"
    rule = governance / "rules/lifecycle.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    rule.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        f'paths: ["{relative.removeprefix("Knowledge Base/")}"]\n',
        encoding="utf-8",
    )
    rule.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
        "audience: external\nceiling: 4\n",
        encoding="utf-8",
    )
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()


def test_lease_graph_wait_does_not_finalize_a_degraded_governed_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative, _source = _note(tmp_path, "mixed-component.md")
    _govern(tmp_path, relative)
    real_delete = index_sync.delete_after_remove

    def mixed_delete(root: Path, paths: list[str]) -> index_sync.IndexSyncReport:
        return index_sync.with_component(
            real_delete(root, paths),
            index_sync.IndexComponentOutcome("embeddings", "degraded", "injected_failure"),
        )

    monkeypatch.setattr(index_sync, "delete_after_remove", mixed_delete)
    calls: list[int] = []

    def leaf(root: Path) -> dict:
        calls.append(1)
        return delete_file.delete_file(root, path=relative, confirm=True).as_dict()

    result = _lease_manager(tmp_path).invoke(
        SimpleNamespace(name="delete_file", read_only=False, leaf=leaf),
        (tmp_path,),
        {"response_detail": "full"},
        idempotency_key="mixed-component-delete",
    )

    tombstones = list(
        (tmp_path / "Knowledge Base/_Governance/deletion-tombstones").glob("*.json")
    )
    assert calls == [1]
    assert result["status"] == "committed"
    # The write no longer waits on derived graph work (#576/#588), so it reports
    # `pending`; the graph's own outcome is asserted after joining the flight.
    assert result["graph_sync"] == "pending"
    graph_sync.await_active_rebuild(tmp_path, state_root=tmp_path / "state")
    assert any("remains tombstoned until reconcile" in warning for warning in result["diagnostics"]["warnings"])
    assert len(tombstones) == 1
    assert lifecycle._read_json(tmp_path, tombstones[0])["state"] == "pending"


def test_governed_delete_stays_pending_for_an_unverified_legacy_fanout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative, _source = _note(tmp_path, "legacy-delete.md")
    _govern(tmp_path, relative)
    monkeypatch.setattr(index_sync, "delete_after_remove", lambda *_args: True)

    result = delete_file.delete_file(tmp_path, path=relative, confirm=True)

    tombstones = list(
        (tmp_path / "Knowledge Base/_Governance/deletion-tombstones").glob("*.json")
    )
    assert any("remains tombstoned until reconcile" in warning for warning in result.warnings)
    assert len(tombstones) == 1
    assert lifecycle._read_json(tmp_path, tombstones[0])["state"] == "pending"


def test_governed_recovery_stays_pending_for_an_unverified_legacy_fanout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative, source = _note(tmp_path, "legacy-recovery.md")
    source.write_text("---\ntype: insight\nstatus: draft\n---\n# transition\n", encoding="utf-8")
    _govern(tmp_path, relative)
    deleted = delete_file.delete_file(tmp_path, path=relative, confirm=True)
    monkeypatch.setattr(index_sync, "upsert_after_write", lambda *_args, **_kwargs: True)

    result = recover_from_trash.recover_from_trash(tmp_path, trash_path=deleted.trash_path)

    recovery = list(
        (tmp_path / "Knowledge Base/_Governance/deletion-tombstones/recovery").glob("*.json")
    )
    assert any("remains tombstoned until reconcile" in warning for warning in result.warnings)
    assert len(recovery) == 1
    assert lifecycle._read_json(tmp_path, recovery[0])["state"] == "staged"


def _raise_fanout(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("injected outer fanout failure")


def _lease_manager(tmp_path: Path) -> writer_lease.LeaseManager:
    return writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))


def _assert_outer_fanout_failure_terminal(
    manager: writer_lease.LeaseManager,
    command: SimpleNamespace,
    vault: Path,
    *,
    key: str,
    calls: list[int],
    observed: list[graph_sync.GraphSyncCheckpoint | None],
) -> None:
    result = manager.invoke(command, (vault,), {}, idempotency_key=key)
    replay = manager.invoke(command, (vault,), {}, idempotency_key=key)

    assert calls == [1]
    assert replay == result
    assert observed == [graph_sync.read_checkpoint(vault)]
    assert observed[0] is not None
    assert result["status"] == "committed"
    assert result["graph_sync"] == "failed"
    assert result["graph_sync_code"] == "GRAPH_SYNC_FANOUT_FAILED"
    assert result["graph_sync_checkpoint"] == observed[0].checkpoint_sha256


def test_lease_delete_file_outer_fanout_failure_keeps_an_exact_graph_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative, _source = _note(tmp_path, "outer-delete-file.md")
    monkeypatch.setattr(index_sync, "delete_after_remove", _raise_fanout)
    calls: list[int] = []
    observed: list[graph_sync.GraphSyncCheckpoint | None] = []

    def leaf(root: Path) -> dict:
        calls.append(1)
        result = delete_file.delete_file(root, path=relative, confirm=True)
        observed.append(graph_sync.registered_checkpoint(root))
        return result.as_dict()

    _assert_outer_fanout_failure_terminal(
        _lease_manager(tmp_path),
        SimpleNamespace(name="delete_file", read_only=False, leaf=leaf),
        tmp_path,
        key="outer-delete-file",
        calls=calls,
        observed=observed,
    )


def test_lease_delete_directory_outer_fanout_failure_keeps_an_exact_graph_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative = "Knowledge Base/Notes/Insights/outer-delete-directory"
    source = tmp_path / relative / "nested.md"
    source.parent.mkdir(parents=True)
    source.write_text("# transition\n", encoding="utf-8")
    monkeypatch.setattr(index_sync, "delete_after_remove", _raise_fanout)
    calls: list[int] = []
    observed: list[graph_sync.GraphSyncCheckpoint | None] = []

    def leaf(root: Path) -> dict:
        calls.append(1)
        result = delete_directory.delete_directory(
            root, path=relative, confirm=True, recursive=True
        )
        observed.append(graph_sync.registered_checkpoint(root))
        return result.as_dict()

    _assert_outer_fanout_failure_terminal(
        _lease_manager(tmp_path),
        SimpleNamespace(name="delete_directory", read_only=False, leaf=leaf),
        tmp_path,
        key="outer-delete-directory",
        calls=calls,
        observed=observed,
    )


def test_lease_recovery_outer_fanout_failure_keeps_an_exact_graph_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative, source = _note(tmp_path, "outer-recovery.md")
    source.write_text(
        "---\ntype: insight\nstatus: draft\n---\n# transition\n",
        encoding="utf-8",
    )
    deleted = delete_file.delete_file(tmp_path, path=relative, confirm=True)
    monkeypatch.setattr(index_sync, "upsert_after_write", _raise_fanout)
    calls: list[int] = []
    observed: list[graph_sync.GraphSyncCheckpoint | None] = []

    def leaf(root: Path) -> dict:
        calls.append(1)
        result = recover_from_trash.recover_from_trash(root, trash_path=deleted.trash_path)
        observed.append(graph_sync.registered_checkpoint(root))
        return result.as_dict()

    _assert_outer_fanout_failure_terminal(
        _lease_manager(tmp_path),
        SimpleNamespace(name="recover_from_trash", read_only=False, leaf=leaf),
        tmp_path,
        key="outer-recovery",
        calls=calls,
        observed=observed,
    )
