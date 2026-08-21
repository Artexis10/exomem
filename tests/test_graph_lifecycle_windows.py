"""Native Windows regressions for graph-aware lifecycle rollback durability."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from exomem import (
    delete_file,
    graph_sync,
    held_fs,
    recall_policy,
    recover_from_trash,
    reserved_paths,
)
from exomem.governance import lifecycle

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows lifecycle contract")


@pytest.fixture(autouse=True)
def writer_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "machine-state"))


def _note(vault: Path, name: str) -> tuple[str, Path]:
    relative = f"Knowledge Base/Notes/Insights/{name}"
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# transition\n", encoding="utf-8")
    return relative, path


def _govern(vault: Path, relative: str) -> None:
    governance = vault / "Knowledge Base" / "_Governance"
    scope = governance / "scopes" / "lifecycle.yaml"
    rule = governance / "rules" / "lifecycle.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    rule.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        f'paths: ["{relative.removeprefix("Knowledge Base/")}"]\n',
        encoding="utf-8",
    )
    rule.write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
        "audience: external\nceiling: 4\n",
        encoding="utf-8",
    )
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()


def _epoch_snapshot(vault: Path) -> tuple[bytes | None, bytes | None]:
    floor = graph_sync.floor_path(vault)
    checkpoint = graph_sync.checkpoint_path(vault)
    return (
        floor.read_bytes() if floor.exists() else None,
        checkpoint.read_bytes() if checkpoint.exists() else None,
    )


def _marker_snapshot(vault: Path) -> dict[str, bytes]:
    root = vault / "Knowledge Base" / "_Governance" / "deletion-tombstones"
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*.json"))
    } if root.exists() else {}


def _held_directory_probe(
    vault: Path,
    relative: str,
) -> tuple[type[held_fs.HeldFilesystem], held_fs.StableIdentity]:
    acquired = held_fs.acquire(vault)
    assert acquired.ok
    with acquired.require() as filesystem:
        retained = filesystem.parent(relative)
        assert retained.ok
        with retained.require() as directory:
            return type(filesystem), directory.identity


def test_windows_private_unlink_closes_delete_pending_handle_before_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = graph_sync.floor_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"floor")

    acquired = held_fs.acquire(tmp_path)
    assert acquired.ok
    with acquired.require() as filesystem:
        parent_result = filesystem.parent("Knowledge Base", access="mutate")
        assert parent_result.ok
        with parent_result.require() as parent:
            file_result = filesystem.file(parent, target.name, access="mutate")
            assert file_result.ok
            with file_result.require() as file:
                filesystem_type = type(filesystem)
                file_type = type(file)

    events: list[str] = []
    original_unlink = filesystem_type.unlink
    original_close = file_type.close
    original_flush = filesystem_type.flush_directory

    def observe_unlink(filesystem, file):  # noqa: ANN001
        events.append("unlink")
        return original_unlink(filesystem, file)

    def observe_close(file):  # noqa: ANN001
        events.append("close")
        return original_close(file)

    def observe_flush(filesystem, directory):  # noqa: ANN001
        events.append("flush")
        return original_flush(filesystem, directory)

    monkeypatch.setattr(filesystem_type, "unlink", observe_unlink)
    monkeypatch.setattr(file_type, "close", observe_close)
    monkeypatch.setattr(filesystem_type, "flush_directory", observe_flush)

    with reserved_paths._subsystem_authority_scope("graph_sync"):
        assert reserved_paths._remove_owner_file(
            tmp_path,
            target,
            "graph-handoff",
        )

    assert events == ["unlink", "close", "flush"]
    assert not target.exists()


def _absolute_move_paths(
    vault_root: Path,
    source: object,
    destination: object,
) -> tuple[Path, Path]:
    return (
        Path(vault_root) / Path(os.fspath(source)),
        Path(vault_root) / Path(os.fspath(destination)),
    )


@pytest.mark.parametrize("refusal", ["cross-device", "census-drift"])
def test_windows_epoch_abort_flushes_removed_floor_and_preserves_lifecycle_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """A successful Windows epoch rollback returns the original refusal unchanged."""
    relative, source = _note(tmp_path, f"epoch-{refusal}.md")
    _govern(tmp_path, relative)
    flushed: list[Path] = []
    armed = False
    original_open = graph_sync.os.open
    filesystem_type, kb_identity = _held_directory_probe(tmp_path, "Knowledge Base")
    original_flush = filesystem_type.flush_directory

    def reject_crt_epoch_directory(path, *args, **kwargs):  # noqa: ANN001
        if Path(path) == graph_sync.floor_path(tmp_path).parent:
            pytest.fail("graph epoch restore attempted CRT directory access")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(graph_sync.os, "open", reject_crt_epoch_directory)

    def flush_epoch_directory(filesystem, directory):  # noqa: ANN001
        result = original_flush(filesystem, directory)
        if armed and directory.identity == kb_identity:
            flushed.append(graph_sync.floor_path(tmp_path).parent)
        return result

    monkeypatch.setattr(filesystem_type, "flush_directory", flush_epoch_directory)
    if refusal == "cross-device":
        devices = iter((10, 11))

        def device(_path: Path) -> int:
            nonlocal armed
            value = next(devices)
            if value == 11:
                armed = True
            return value

        monkeypatch.setattr(lifecycle, "_device", device)
        expected = "CROSS_DEVICE_MOVE"
    else:
        original_checkpoint = lifecycle._checkpoint

        def drift(point: str) -> None:
            nonlocal armed
            if point == "deletion_tombstone":
                source.write_text("# changed after manifest\n", encoding="utf-8")
                armed = True
            original_checkpoint(point)

        monkeypatch.setattr(lifecycle, "_checkpoint", drift)
        expected = "LIFECYCLE_CENSUS_DRIFT"

    with pytest.raises(delete_file.DeleteFileError) as error:
        delete_file.delete_file(tmp_path, path=relative, confirm=True)

    assert error.value.code == expected
    assert source.exists()
    assert _epoch_snapshot(tmp_path) == (None, None)
    if refusal == "cross-device":
        assert _marker_snapshot(tmp_path) == {}
    else:
        assert source.read_text(encoding="utf-8") == "# changed after manifest\n"
        assert _marker_snapshot(tmp_path)
    assert graph_sync.floor_path(tmp_path).parent in flushed


def test_windows_epoch_restore_flush_failure_remains_graph_rollback_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing the epoch's post-unlink flush retains the staged epoch for reconcile."""
    relative, source = _note(tmp_path, "epoch-flush-failure.md")
    _govern(tmp_path, relative)
    flushed: list[Path] = []
    armed = False
    filesystem_type, kb_identity = _held_directory_probe(tmp_path, "Knowledge Base")
    original_flush = filesystem_type.flush_directory

    def refuse_epoch_flush(filesystem, directory):  # noqa: ANN001
        if armed and directory.identity == kb_identity:
            flushed.append(graph_sync.floor_path(tmp_path).parent)
            return held_fs.HeldResult(
                error=held_fs.HeldFsError(
                    "IO_REFUSED", "injected epoch flush refusal"
                )
            )
        return original_flush(filesystem, directory)

    devices = iter((10, 11))

    def device(_path: Path) -> int:
        nonlocal armed
        value = next(devices)
        if value == 11:
            armed = True
        return value

    monkeypatch.setattr(lifecycle, "_device", device)
    monkeypatch.setattr(filesystem_type, "flush_directory", refuse_epoch_flush)

    with pytest.raises(delete_file.DeleteFileError) as error:
        delete_file.delete_file(tmp_path, path=relative, confirm=True)

    assert error.value.code == "GRAPH_SYNC_DELETION_ROLLBACK_FAILED"
    assert isinstance(error.value.__cause__, graph_sync.GraphLifecycleRollbackError)
    assert source.exists()
    assert graph_sync.floor_path(tmp_path).exists() is False
    assert _marker_snapshot(tmp_path)
    assert flushed == [graph_sync.floor_path(tmp_path).parent]


def _fail_first_post_rename_flush(
    canonical_source: Path, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, Path | None]]:
    events: list[tuple[str, Path | None]] = []
    failed = False

    def flush(path: Path) -> None:
        nonlocal failed
        path = Path(path)
        events.append(("flush", path))
        if not failed and not canonical_source.exists():
            failed = True
            events.append(("forward_failure", path))
            raise lifecycle.LifecycleError(
                "LIFECYCLE_PATH_UNSAFE", "injected post-rename flush refusal"
            )

    monkeypatch.setattr(lifecycle, "_fsync_directory", flush)
    return events


def _assert_inverse_flushes_precede_epoch_and_marker_restore(
    events: list[tuple[str, Path | None]],
    *,
    inverse_source_parent: Path,
    inverse_destination_parent: Path,
) -> None:
    failure_index = next(
        index for index, item in enumerate(events) if item[0] == "forward_failure"
    )
    epoch_index = next(
        index for index, item in enumerate(events) if item[0] == "restore_epoch"
    )
    marker_index = next(
        index for index, item in enumerate(events) if item[0] == "restore_marker"
    )
    inverse_flushes = [
        path
        for kind, path in events[failure_index + 1 : epoch_index]
        if kind == "flush"
    ]

    expected = [inverse_source_parent]
    if inverse_destination_parent != inverse_source_parent:
        expected.append(inverse_destination_parent)
    assert inverse_flushes == expected
    assert failure_index < epoch_index < marker_index


def test_windows_deletion_post_rename_flush_refusal_durably_inverse_restores_prior_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deletion flush refusal after rename returns only after durable inverse rollback."""
    relative, source = _note(tmp_path, "delete-post-rename.md")
    _govern(tmp_path, relative)
    prior_epoch = _epoch_snapshot(tmp_path)
    prior_markers = _marker_snapshot(tmp_path)
    events = _fail_first_post_rename_flush(source, monkeypatch)
    rename_calls: list[tuple[Path, Path]] = []
    original_move = reserved_paths.move_generic_path
    original_restore = graph_sync.restore_deletion_epoch
    original_abort = lifecycle.abort_deletion

    def rename(vault_root, source_path, destination_path, **kwargs):  # noqa: ANN001
        rename_calls.append(
            _absolute_move_paths(Path(vault_root), source_path, destination_path)
        )
        return original_move(vault_root, source_path, destination_path, **kwargs)

    def restore(epoch: graph_sync.GraphDeletionEpoch) -> None:
        events.append(("restore_epoch", None))
        original_restore(epoch)

    def abort(operation: lifecycle.LifecycleOperation) -> None:
        events.append(("restore_marker", None))
        original_abort(operation)

    monkeypatch.setattr(reserved_paths, "move_generic_path", rename)
    monkeypatch.setattr(graph_sync, "restore_deletion_epoch", restore)
    monkeypatch.setattr(lifecycle, "abort_deletion", abort)

    with pytest.raises(delete_file.DeleteFileError) as error:
        delete_file.delete_file(
            tmp_path,
            path=relative,
            confirm=True,
            now=dt.datetime(2026, 8, 14, 12, 34, 56),
        )

    assert error.value.code == "LIFECYCLE_PATH_UNSAFE"
    assert source.exists()
    assert not list((tmp_path / "Knowledge Base" / "_trash").rglob("delete-post-rename.md"))
    assert _epoch_snapshot(tmp_path) == prior_epoch
    assert _marker_snapshot(tmp_path) == prior_markers
    assert len(rename_calls) == 2
    forward_source, forward_destination = rename_calls[0]
    assert forward_source == source
    assert rename_calls[1] == (forward_destination, source)
    _assert_inverse_flushes_precede_epoch_and_marker_restore(
        events,
        inverse_source_parent=forward_destination.parent,
        inverse_destination_parent=source.parent,
    )


def test_windows_deletion_inverse_move_failure_retains_epoch_and_marker_for_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inverse deletion move failure must not erase the staged graph evidence."""
    relative, source = _note(tmp_path, "delete-inverse-failure.md")
    _govern(tmp_path, relative)
    prior_floor, _prior_checkpoint = _epoch_snapshot(tmp_path)
    _fail_first_post_rename_flush(source, monkeypatch)
    original_move = reserved_paths.move_generic_path
    rename_calls: list[tuple[Path, Path]] = []

    def refuse_inverse(vault_root, source_path, destination_path, **kwargs):  # noqa: ANN001
        rename_calls.append(
            _absolute_move_paths(Path(vault_root), source_path, destination_path)
        )
        if len(rename_calls) == 2:
            raise reserved_paths.ReservedPathLeafError("IO_REFUSED")
        return original_move(vault_root, source_path, destination_path, **kwargs)

    monkeypatch.setattr(reserved_paths, "move_generic_path", refuse_inverse)

    with pytest.raises(delete_file.DeleteFileError) as error:
        delete_file.delete_file(
            tmp_path,
            path=relative,
            confirm=True,
            now=dt.datetime(2026, 8, 14, 12, 34, 56),
        )

    assert error.value.code == "GRAPH_SYNC_DELETION_ROLLBACK_FAILED"
    assert len(rename_calls) == 2
    assert source.exists() is False
    assert list((tmp_path / "Knowledge Base" / "_trash").rglob("*delete-inverse-failure.md"))
    assert graph_sync.floor_path(tmp_path).read_bytes() != prior_floor
    assert _marker_snapshot(tmp_path)


def _deleted_governed_note(vault: Path, name: str) -> tuple[str, Path, delete_file.DeleteFileResult]:
    relative, source = _note(vault, name)
    source.write_text(
        "---\ntype: insight\nstatus: draft\n---\n# transition\n",
        encoding="utf-8",
    )
    _govern(vault, relative)
    deleted = delete_file.delete_file(vault, path=relative, confirm=True)
    return relative, source, deleted


def test_windows_recovery_post_rename_flush_refusal_durably_inverse_restores_prior_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery uses the opposite durable inverse direction before returning refusal."""
    monkeypatch.setattr(recall_policy, "is_recall_candidate", lambda *_args: True)
    _relative, source, deleted = _deleted_governed_note(tmp_path, "recovery-post-rename.md")
    trash = tmp_path / deleted.trash_path
    prior_epoch = _epoch_snapshot(tmp_path)
    prior_markers = _marker_snapshot(tmp_path)
    events = _fail_first_post_rename_flush(trash, monkeypatch)
    rename_calls: list[tuple[Path, Path]] = []
    original_move = reserved_paths.move_generic_path
    original_restore = graph_sync.restore_deletion_epoch
    original_abort = lifecycle.abort_recovery

    def rename(vault_root, source_path, destination_path, **kwargs):  # noqa: ANN001
        rename_calls.append(
            _absolute_move_paths(Path(vault_root), source_path, destination_path)
        )
        return original_move(vault_root, source_path, destination_path, **kwargs)

    def restore(epoch: graph_sync.GraphDeletionEpoch) -> None:
        events.append(("restore_epoch", None))
        original_restore(epoch)

    def abort(operation: lifecycle.LifecycleOperation) -> None:
        events.append(("restore_marker", None))
        original_abort(operation)

    monkeypatch.setattr(reserved_paths, "move_generic_path", rename)
    monkeypatch.setattr(graph_sync, "restore_deletion_epoch", restore)
    monkeypatch.setattr(lifecycle, "abort_recovery", abort)

    with pytest.raises(recover_from_trash.RecoverError) as error:
        recover_from_trash.recover_from_trash(tmp_path, trash_path=deleted.trash_path)

    assert error.value.code == "LIFECYCLE_PATH_UNSAFE"
    assert source.exists() is False
    assert trash.exists()
    assert _epoch_snapshot(tmp_path) == prior_epoch
    assert _marker_snapshot(tmp_path) == prior_markers
    assert len(rename_calls) == 2
    forward_source, forward_destination = rename_calls[0]
    assert forward_source == trash
    assert rename_calls[1] == (forward_destination, trash)
    _assert_inverse_flushes_precede_epoch_and_marker_restore(
        events,
        inverse_source_parent=forward_destination.parent,
        inverse_destination_parent=trash.parent,
    )


def test_windows_recovery_inverse_move_failure_retains_epoch_and_marker_for_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed inverse recovery retains staged graph and lifecycle evidence."""
    monkeypatch.setattr(recall_policy, "is_recall_candidate", lambda *_args: True)
    _relative, source, deleted = _deleted_governed_note(tmp_path, "recovery-inverse-failure.md")
    trash = tmp_path / deleted.trash_path
    prior_floor, _prior_checkpoint = _epoch_snapshot(tmp_path)
    _fail_first_post_rename_flush(trash, monkeypatch)
    original_move = reserved_paths.move_generic_path
    rename_calls: list[tuple[Path, Path]] = []

    def refuse_inverse(vault_root, source_path, destination_path, **kwargs):  # noqa: ANN001
        rename_calls.append(
            _absolute_move_paths(Path(vault_root), source_path, destination_path)
        )
        if len(rename_calls) == 2:
            raise reserved_paths.ReservedPathLeafError("IO_REFUSED")
        return original_move(vault_root, source_path, destination_path, **kwargs)

    monkeypatch.setattr(reserved_paths, "move_generic_path", refuse_inverse)

    with pytest.raises(recover_from_trash.RecoverError) as error:
        recover_from_trash.recover_from_trash(tmp_path, trash_path=deleted.trash_path)

    assert error.value.code == "GRAPH_SYNC_RECOVERY_ROLLBACK_FAILED"
    assert len(rename_calls) == 2
    assert source.exists()
    assert trash.exists() is False
    assert graph_sync.floor_path(tmp_path).read_bytes() != prior_floor
    assert any("recovery/" in path for path in _marker_snapshot(tmp_path))
