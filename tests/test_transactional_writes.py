from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from exomem import move_file as move_module
from exomem import vault as vault_module

_WORKSPACE_RE = re.compile(r"^\.exomem-batch-[0-9a-f]{32}$")


def _leaf(value: object) -> str:
    return Path(os.fspath(value)).name


def _workspaces(parent: Path) -> list[Path]:
    return sorted(parent.glob(".exomem-batch-*"))


def _residue_name(index: int) -> str:
    return f".exomem-batch-{index:032x}"


def _make_residue(
    parent: Path,
    index: int,
    *,
    children: tuple[str, ...] = (),
) -> Path:
    workspace = parent / _residue_name(index)
    workspace.mkdir(mode=0o700)
    os.chmod(workspace, 0o700)
    for name in children:
        (workspace / name).write_bytes(f"residue:{name}".encode())
    return workspace


def _replace_capability_member(
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    original: object,
    replacement: object | None,
) -> None:
    members = set(getattr(os, capability, set()))
    members.discard(original)
    if replacement is not None:
        members.add(replacement)
    monkeypatch.setattr(os, capability, members)


def _set_descriptor_xattr(path: Path, name: str, value: bytes) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.setxattr(descriptor, name, value)
        except (AttributeError, OSError) as error:
            pytest.skip(f"descriptor xattrs are unsupported: {error}")
    finally:
        os.close(descriptor)


def _get_descriptor_xattrs(path: Path) -> dict[str, bytes]:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        return {
            name: os.getxattr(descriptor, name)
            for name in os.listxattr(descriptor)
        }
    finally:
        os.close(descriptor)


def test_batch_atomic_write_uses_private_workspaces_and_fans_out_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "existing.md"
    created = tmp_path / "created.md"
    existing.write_bytes(b"old")
    workspace_mkdir_modes: list[int] = []
    backup_creations: list[str] = []
    flips: list[str] = []
    watcher_calls: list[tuple[Path, ...]] = []
    index_calls: list[tuple[Path, ...]] = []
    reports: list[object] = []
    report = object()
    real_mkdir = os.mkdir
    real_mkstemp = vault_module.tempfile.mkstemp
    real_replace = os.replace

    def observe_mkdir(path, mode=0o777, *args, **kwargs):
        if _WORKSPACE_RE.fullmatch(_leaf(path)):
            workspace_mkdir_modes.append(mode)
        return real_mkdir(path, mode, *args, **kwargs)

    def reject_named_backup(*args, **kwargs):
        if kwargs.get("suffix") == ".bak":
            backup_creations.append(str(kwargs))
        return real_mkstemp(*args, **kwargs)

    def observe_flip(src, dst, *args, **kwargs):
        if _leaf(src).startswith("stage-"):
            flips.append(_leaf(dst))
        return real_replace(src, dst, *args, **kwargs)

    def register(_root: Path, paths: list[Path]) -> None:
        watcher_calls.append(tuple(paths))

    def index(_root: Path, paths: list[Path]) -> object:
        index_calls.append(tuple(paths))
        return report

    monkeypatch.setattr(vault_module.os, "mkdir", observe_mkdir)
    monkeypatch.setattr(vault_module.tempfile, "mkstemp", reject_named_backup)
    monkeypatch.setattr(vault_module.os, "replace", observe_flip)
    monkeypatch.setattr("exomem.file_watcher.register_self_write", register)
    monkeypatch.setattr("exomem.index_sync.upsert_after_write", index)

    replaced = vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(existing, "superseded"),
            vault_module.PlannedWrite(created, "created\nexact"),
            vault_module.PlannedWrite(existing, "existing\nexact"),
        ],
        vault_root=tmp_path,
        index_reports=reports,
    )

    assert replaced == [existing, created]
    assert existing.read_bytes() == b"existing\nexact"
    assert created.read_bytes() == b"created\nexact"
    assert flips == ["existing.md", "created.md"]
    assert workspace_mkdir_modes == [0o700]
    assert backup_creations == []
    assert watcher_calls == [(existing, created)]
    assert index_calls == [(existing, created)]
    assert reports == [report]
    assert _workspaces(tmp_path) == []
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".bak")]


def test_batch_atomic_write_restores_exact_bytes_and_supported_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"first-old\x00bytes")
    second.write_bytes(b"second-old")
    os.chmod(first, 0o640)
    first_times = (1_731_111_111_123_456_789, 1_731_222_222_987_654_321)
    second_times = (1_732_111_111_123_456_789, 1_732_222_222_987_654_321)
    os.utime(first, ns=first_times)
    os.utime(second, ns=second_times)
    _set_descriptor_xattr(first, "user.exomem-test", b"before")
    expected_xattrs = _get_descriptor_xattrs(first)
    real_replace = os.replace
    flips = 0

    def fail_second_flip(src, dst, *args, **kwargs):
        nonlocal flips
        if _leaf(src).startswith("stage-"):
            flips += 1
            if flips == 2:
                raise OSError("injected second flip failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_module.os, "replace", fail_second_flip)

    with pytest.raises(OSError, match="second flip failure"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ]
        )

    restored_info = first.stat()
    untouched_info = second.stat()
    assert first.read_bytes() == b"first-old\x00bytes"
    assert second.read_bytes() == b"second-old"
    assert stat.S_IMODE(restored_info.st_mode) == 0o640
    assert (restored_info.st_atime_ns, restored_info.st_mtime_ns) == first_times
    assert (untouched_info.st_atime_ns, untouched_info.st_mtime_ns) == second_times
    assert _get_descriptor_xattrs(first) == expected_xattrs
    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_uses_path_metadata_fallbacks_after_dir_fd_flip_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"first-old")
    second.write_bytes(b"second-old")
    os.chmod(first, 0o640)
    first_times = (1_711_111_111_123_456_789, 1_712_222_222_987_654_321)
    os.utime(first, ns=first_times)
    real_chmod = os.chmod
    real_replace = os.replace
    descriptor_relative_flips: list[str] = []
    flips = 0

    def fail_after_second_kernel_flip(src, dst, *args, **kwargs):
        nonlocal flips
        if _leaf(src).startswith("stage-"):
            flips += 1
            assert kwargs.get("src_dir_fd") is not None
            assert kwargs.get("dst_dir_fd") is not None
            descriptor_relative_flips.append(_leaf(dst))
            result = real_replace(src, dst, *args, **kwargs)
            if flips == 2:
                raise OSError("post-kernel descriptor-relative failure")
            return result
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.delattr(vault_module.os, "fchmod")
    _replace_capability_member(monkeypatch, "supports_fd", real_chmod, None)
    _replace_capability_member(monkeypatch, "supports_fd", os.utime, None)
    monkeypatch.setattr(vault_module.os, "replace", fail_after_second_kernel_flip)
    _replace_capability_member(
        monkeypatch,
        "supports_dir_fd",
        real_replace,
        fail_after_second_kernel_flip,
    )

    with pytest.raises(OSError, match="post-kernel descriptor-relative failure"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ]
        )

    first_info = first.stat()
    assert descriptor_relative_flips == ["first.md", "second.md"]
    assert first.read_bytes() == b"first-old"
    assert second.read_bytes() == b"second-old"
    assert stat.S_IMODE(first_info.st_mode) == 0o640
    assert (first_info.st_atime_ns, first_info.st_mtime_ns) == first_times
    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_detects_failed_path_mode_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"first-old")
    second.write_bytes(b"second-old")
    os.chmod(first, 0o640)
    real_chmod = os.chmod
    real_replace = os.replace
    flips = 0

    def ignore_restore_mode(path, mode, *args, **kwargs):
        if _leaf(path).startswith("restore-"):
            return None
        return real_chmod(path, mode, *args, **kwargs)

    def fail_second_flip(src, dst, *args, **kwargs):
        nonlocal flips
        if _leaf(src).startswith("stage-"):
            flips += 1
            if flips == 2:
                raise OSError("commit failed")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.delattr(vault_module.os, "fchmod")
    monkeypatch.setattr(vault_module.os, "chmod", ignore_restore_mode)
    _replace_capability_member(
        monkeypatch,
        "supports_fd",
        real_chmod,
        None,
    )
    _replace_capability_member(
        monkeypatch,
        "supports_dir_fd",
        real_chmod,
        ignore_restore_mode,
    )
    monkeypatch.setattr(vault_module.os, "replace", fail_second_flip)

    with pytest.raises(RuntimeError, match="rollback incomplete"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ]
        )

    assert first.read_bytes() == b"first-new"
    assert second.read_bytes() == b"second-old"
    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_removes_new_file_on_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = tmp_path / "created.md"
    existing = tmp_path / "existing.md"
    existing.write_bytes(b"old")
    real_replace = os.replace
    flips = 0

    def fail_second_flip(src, dst, *args, **kwargs):
        nonlocal flips
        if _leaf(src).startswith("stage-"):
            flips += 1
            if flips == 2:
                raise OSError("commit failed")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_module.os, "replace", fail_second_flip)

    with pytest.raises(OSError, match="commit failed"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(created, "new"),
                vault_module.PlannedWrite(existing, "changed"),
            ]
        )

    assert not created.exists()
    assert existing.read_bytes() == b"old"
    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_metadata_capture_error_precedes_every_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"first-old")
    second.write_bytes(b"second-old")
    flips: list[str] = []
    real_replace = os.replace

    def fail_xattr_capture(_descriptor: int) -> dict[str, bytes]:
        raise PermissionError("private capture detail")

    def observe_flip(src, dst, *args, **kwargs):
        if _leaf(src).startswith("stage-"):
            flips.append(_leaf(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(
        vault_module,
        "_capture_descriptor_xattrs",
        fail_xattr_capture,
        raising=False,
    )
    monkeypatch.setattr(vault_module.os, "replace", observe_flip)

    with pytest.raises(PermissionError, match="capture detail"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ]
        )

    assert flips == []
    assert first.read_bytes() == b"first-old"
    assert second.read_bytes() == b"second-old"
    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_restores_source_times_when_snapshot_capture_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.md"
    target.write_bytes(b"old")
    source_times = (1_701_111_111_123_456_789, 1_702_222_222_987_654_321)
    changed_atime = source_times[0] + 123_456_789
    os.utime(target, ns=source_times)
    real_descriptor_bytes = vault_module._descriptor_bytes
    real_replace = os.replace
    flips: list[str] = []

    def fail_after_read(descriptor: int, expected) -> bytes:
        real_descriptor_bytes(descriptor, expected)
        os.utime(descriptor, ns=(changed_atime, source_times[1]))
        raise PermissionError("snapshot capture failed after read")

    def observe_flip(src, dst, *args, **kwargs):
        if _leaf(src).startswith("stage-"):
            flips.append(_leaf(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_module, "_descriptor_bytes", fail_after_read)
    monkeypatch.setattr(vault_module.os, "replace", observe_flip)

    with pytest.raises(PermissionError, match="capture failed after read"):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, "new")]
        )

    target_info = target.stat()
    assert flips == []
    assert (target_info.st_atime_ns, target_info.st_mtime_ns) == source_times
    assert target.read_bytes() == b"old"
    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_retries_partial_and_interrupted_stage_and_restore_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first_old = b"first-old-with-more-than-three-bytes"
    first.write_bytes(first_old)
    second.write_bytes(b"second-old")
    real_write = os.write
    real_replace = os.replace
    write_calls: dict[int, int] = {}
    written_names: set[str] = set()
    flips = 0

    def partial_interrupted_write(descriptor: int, data) -> int:
        call = write_calls.get(descriptor, 0)
        write_calls[descriptor] = call + 1
        try:
            written_names.add(Path(os.readlink(f"/proc/self/fd/{descriptor}")).name)
        except OSError:
            pass
        if call == 0:
            raise InterruptedError
        return real_write(descriptor, bytes(data[:3]))

    def fail_second_flip(src, dst, *args, **kwargs):
        nonlocal flips
        if _leaf(src).startswith("stage-"):
            flips += 1
            if flips == 2:
                raise OSError("flip failed")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_module.os, "write", partial_interrupted_write)
    monkeypatch.setattr(vault_module.os, "replace", fail_second_flip)

    with pytest.raises(OSError, match="flip failed"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new-exact"),
                vault_module.PlannedWrite(second, "second-new-exact"),
            ]
        )

    assert first.read_bytes() == first_old
    assert second.read_bytes() == b"second-old"
    assert any(name.startswith("stage-") for name in written_names)
    assert any(name.startswith("restore-") for name in written_names)
    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_cleans_owned_workspace_when_fchmod_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fchmod = os.fchmod

    def fail_workspace_fchmod(descriptor: int, mode: int) -> None:
        try:
            name = Path(os.readlink(f"/proc/self/fd/{descriptor}")).name
        except OSError:
            name = ""
        if _WORKSPACE_RE.fullmatch(name):
            raise PermissionError("workspace fchmod failed")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(vault_module.os, "fchmod", fail_workspace_fchmod)

    with pytest.raises(PermissionError, match="workspace fchmod failed"):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(tmp_path / "target.md", "new")]
        )

    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_preserves_mkdir_failure_before_workspace_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir = os.mkdir

    def fail_workspace_mkdir(path, mode=0o777, *args, **kwargs):
        if _WORKSPACE_RE.fullmatch(_leaf(path)):
            raise PermissionError("workspace mkdir denied")
        return real_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(vault_module.os, "mkdir", fail_workspace_mkdir)

    with pytest.raises(PermissionError, match="workspace mkdir denied"):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(tmp_path / "target.md", "new")]
        )

    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_refreshes_workspace_identity_after_mode_hardening(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.md"
    previous_umask = os.umask(0o200)
    try:
        replaced = vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, "new exact content")]
        )
    finally:
        os.umask(previous_umask)

    assert replaced == [target]
    assert target.read_text(encoding="utf-8") == "new exact content"
    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_cleans_partially_initialized_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write
    stage_writes = 0

    def fail_after_partial_stage_write(descriptor: int, data) -> int:
        nonlocal stage_writes
        try:
            name = Path(os.readlink(f"/proc/self/fd/{descriptor}")).name
        except OSError:
            name = ""
        if name.startswith("stage-"):
            stage_writes += 1
            if stage_writes == 1:
                return real_write(descriptor, bytes(data[:3]))
            raise PermissionError("partial stage initialization failed")
        return real_write(descriptor, data)

    monkeypatch.setattr(vault_module.os, "write", fail_after_partial_stage_write)

    with pytest.raises(PermissionError, match="partial stage initialization failed"):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(tmp_path / "target.md", "new exact content")]
        )

    assert stage_writes == 2
    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_retains_fully_bound_stage_when_content_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.md"
    target.write_bytes(b"old")
    drifted = b"same-owner drift must not be deleted"

    def drift_stage_then_fail(_path: Path):
        workspace = _workspaces(tmp_path)[0]
        stage = next(workspace.glob("stage-*.tmp"))
        stage.write_bytes(drifted)
        raise PermissionError("snapshot failed after stage drift")

    monkeypatch.setattr(
        vault_module,
        "_capture_batch_snapshot",
        drift_stage_then_fail,
    )

    with pytest.raises(RuntimeError, match="cleanup retained changed artifacts") as retained:
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, "new exact content")]
        )

    assert isinstance(retained.value.__cause__, PermissionError)
    workspace = _workspaces(tmp_path)[0]
    stage = next(workspace.glob("stage-*.tmp"))
    assert stage.read_bytes() == drifted
    assert target.read_bytes() == b"old"


def test_batch_atomic_write_never_uses_preexisting_user_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    user_backup = tmp_path / "first.md.bak"
    first.write_bytes(b"first-old")
    second.write_bytes(b"second-old")
    user_backup.write_bytes(b"user-owned")
    real_open = os.open
    real_replace = os.replace
    touched_backups: list[str] = []
    flips = 0

    def observe_open(path, *args, **kwargs):
        if _leaf(path).endswith(".bak"):
            touched_backups.append(_leaf(path))
        return real_open(path, *args, **kwargs)

    def fail_second_flip(src, dst, *args, **kwargs):
        nonlocal flips
        if _leaf(src).endswith(".bak") or _leaf(dst).endswith(".bak"):
            touched_backups.extend([_leaf(src), _leaf(dst)])
        if _leaf(src).startswith("stage-"):
            flips += 1
            if flips == 2:
                raise OSError("flip failed")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_module.os, "open", observe_open)
    monkeypatch.setattr(vault_module.os, "replace", fail_second_flip)

    with pytest.raises(OSError, match="flip failed"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ]
        )

    assert user_backup.read_bytes() == b"user-owned"
    assert touched_backups == []
    assert first.read_bytes() == b"first-old"
    assert second.read_bytes() == b"second-old"


def test_batch_atomic_write_preserves_create_only_conflict(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_bytes(b"existing")

    with pytest.raises(vault_module.CreateOnlyConflict):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, "new", create_only=True)],
            vault_root=tmp_path,
        )

    assert target.read_bytes() == b"existing"
    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_blocks_drifted_census_rollback_but_continues_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    census = vault_module.DirectoryCensusGuard.capture(
        tmp_path, "guarded", max_entries=4
    )
    safe = tmp_path / "safe.md"
    guarded_target = guarded / "guarded.md"
    pending = tmp_path / "pending.md"
    safe.write_bytes(b"safe-old")
    guarded_target.write_bytes(b"guarded-old")
    pending.write_bytes(b"pending-old")
    real_replace = os.replace
    flips = 0
    restore_targets: list[str] = []
    concurrent = guarded / "concurrent"

    def inject_census_change(src, dst, *args, **kwargs):
        nonlocal flips
        result = real_replace(src, dst, *args, **kwargs)
        if _leaf(src).startswith("stage-"):
            flips += 1
            if flips == 2:
                concurrent.write_bytes(b"concurrent-owned")
        elif _leaf(src).startswith("restore-"):
            restore_targets.append(_leaf(dst))
        return result

    monkeypatch.setattr(vault_module.os, "replace", inject_census_change)
    with pytest.raises(RuntimeError, match="rollback incomplete") as incomplete:
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(safe, "safe-new"),
                vault_module.PlannedWrite(guarded_target, "guarded-new"),
                vault_module.PlannedWrite(pending, "pending-new"),
            ],
            vault_root=tmp_path,
            required_guards=(census,),
        )

    assert isinstance(incomplete.value.__cause__, vault_module.PathGuardError)
    assert incomplete.value.__cause__.code == "PATH_GUARD_CHANGED"
    assert restore_targets == ["safe.md"]
    assert safe.read_bytes() == b"safe-old"
    assert guarded_target.read_bytes() == b"guarded-new"
    assert pending.read_bytes() == b"pending-old"
    assert concurrent.read_bytes() == b"concurrent-owned"
    assert _workspaces(tmp_path) == []
    assert _workspaces(guarded) == []


def test_directory_census_ignores_exact_valid_residue_without_touching_it(
    tmp_path: Path,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    residue = _make_residue(
        guarded,
        1,
        children=("stage-0.tmp", "restore-000.tmp"),
    )
    expected: dict[str, tuple[bytes, int, int, int]] = {}
    for index, child in enumerate(sorted(residue.iterdir())):
        content = child.read_bytes()
        times = (
            1_711_111_111_123_456_789 + index,
            1_712_222_222_987_654_321 + index,
        )
        os.utime(child, ns=times)
        info = child.stat()
        expected[child.name] = (
            content,
            info.st_mode,
            info.st_atime_ns,
            info.st_mtime_ns,
        )
    workspace_times = (
        1_701_111_111_123_456_789,
        1_702_222_222_987_654_321,
    )
    os.utime(residue, ns=workspace_times)

    census = vault_module.DirectoryCensusGuard.capture(
        tmp_path,
        "guarded",
        max_entries=0,
    )
    census.recheck(tmp_path)

    assert census.entries == ()
    residue_info = residue.stat()
    assert (residue_info.st_atime_ns, residue_info.st_mtime_ns) == workspace_times
    actual: dict[str, tuple[bytes, int, int, int]] = {}
    for child in residue.iterdir():
        info = child.stat()
        actual[child.name] = (
            child.read_bytes(),
            info.st_mode,
            info.st_atime_ns,
            info.st_mtime_ns,
        )
    assert actual == expected


def test_directory_census_restores_residue_times_on_classifier_failure(
    tmp_path: Path,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    residue = _make_residue(guarded, 1, children=("unexpected.tmp",))
    workspace_times = (
        1_701_111_111_123_456_789,
        1_702_222_222_987_654_321,
    )
    os.utime(residue, ns=workspace_times)

    with pytest.raises(vault_module.PathGuardError) as unsafe:
        vault_module.DirectoryCensusGuard.capture(
            tmp_path,
            "guarded",
            max_entries=0,
        )

    residue_info = residue.stat()
    assert unsafe.value.code == "BATCH_RESIDUE_UNSAFE"
    assert (residue_info.st_atime_ns, residue_info.st_mtime_ns) == workspace_times


def test_directory_census_chains_classifier_failure_when_time_restore_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    _make_residue(guarded, 1, children=("unexpected.tmp",))
    real_utime = os.utime

    def fail_residue_time_restore(path, *args, **kwargs):
        raise PermissionError("injected residue timestamp failure")

    monkeypatch.setattr(vault_module.os, "utime", fail_residue_time_restore)
    _replace_capability_member(
        monkeypatch,
        "supports_fd",
        real_utime,
        fail_residue_time_restore,
    )

    with pytest.raises(vault_module.PathGuardError) as unsafe:
        vault_module.DirectoryCensusGuard.capture(
            tmp_path,
            "guarded",
            max_entries=0,
        )

    assert unsafe.value.code == "BATCH_RESIDUE_UNSAFE"
    assert isinstance(unsafe.value.__cause__, vault_module.PathGuardError)
    assert unsafe.value.__cause__.code == "BATCH_RESIDUE_UNSAFE"


@pytest.mark.parametrize(
    "case",
    [
        "malformed_name",
        "uppercase_hex",
        "malformed_child",
        "workspace_symlink",
        "workspace_file",
        "child_directory",
        "child_symlink",
        "permissive_mode",
    ],
)
def test_directory_census_rejects_unsafe_reserved_residue(
    tmp_path: Path,
    case: str,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    if case == "malformed_name":
        (guarded / ".exomem-batch-short").mkdir()
    elif case == "uppercase_hex":
        (guarded / f".exomem-batch-{'A' * 32}").mkdir()
    elif case == "malformed_child":
        residue = _make_residue(guarded, 1)
        (residue / "stage-١.tmp").write_bytes(b"unicode digit")
    elif case == "workspace_symlink":
        target = tmp_path / "workspace-target"
        target.mkdir()
        (guarded / _residue_name(1)).symlink_to(target, target_is_directory=True)
    elif case == "workspace_file":
        (guarded / _residue_name(1)).write_bytes(b"not a directory")
    elif case == "child_directory":
        residue = _make_residue(guarded, 1)
        (residue / "stage-0.tmp").mkdir()
    elif case == "child_symlink":
        target = tmp_path / "child-target"
        target.write_bytes(b"target")
        residue = _make_residue(guarded, 1)
        (residue / "restore-0.tmp").symlink_to(target)
    else:
        if os.name != "posix":
            pytest.skip("owner-only residue mode is a POSIX contract")
        residue = _make_residue(guarded, 1)
        os.chmod(residue, 0o755)

    with pytest.raises(vault_module.PathGuardError) as unsafe:
        vault_module.DirectoryCensusGuard.capture(
            tmp_path,
            "guarded",
            max_entries=0,
        )

    assert unsafe.value.code == "BATCH_RESIDUE_UNSAFE"
    assert unsafe.value.reason == "private batch residue is unsafe"


@pytest.mark.parametrize("mutation", ["add", "remove", "swap"])
def test_directory_census_rechecks_residue_children_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    residue = _make_residue(guarded, 1, children=("stage-0.tmp",))
    replacement = tmp_path / "replacement-child"
    if mutation == "swap":
        replacement.write_bytes(b"replacement identity")
    real_stat = os.stat
    mutated = False

    def mutate_after_child_stat(path, *args, **kwargs):
        nonlocal mutated
        info = real_stat(path, *args, **kwargs)
        if (
            not mutated
            and os.fspath(path) == "stage-0.tmp"
            and kwargs.get("dir_fd") is not None
        ):
            mutated = True
            child = residue / "stage-0.tmp"
            if mutation == "add":
                (residue / "stage-1.tmp").write_bytes(b"injected")
            elif mutation == "remove":
                child.unlink()
            else:
                os.replace(replacement, child)
        return info

    monkeypatch.setattr(vault_module.os, "stat", mutate_after_child_stat)
    _replace_capability_member(
        monkeypatch,
        "supports_dir_fd",
        real_stat,
        mutate_after_child_stat,
    )

    with pytest.raises(vault_module.PathGuardError) as unsafe:
        vault_module.DirectoryCensusGuard.capture(
            tmp_path,
            "guarded",
            max_entries=0,
        )

    assert mutated is True
    assert unsafe.value.code == "BATCH_RESIDUE_UNSAFE"


def test_directory_census_enforces_residue_workspace_cap_before_validation(
    tmp_path: Path,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    for index in range(64):
        _make_residue(guarded, index)

    census = vault_module.DirectoryCensusGuard.capture(
        tmp_path,
        "guarded",
        max_entries=0,
    )
    assert census.entries == ()

    sixty_fifth = _make_residue(guarded, 64)
    with pytest.raises(vault_module.PathGuardError) as limited:
        census.recheck(tmp_path)
    assert limited.value.code == "BATCH_RESIDUE_LIMIT"

    sixty_fifth.rmdir()
    (guarded / ".exomem-batch-malformed").mkdir()
    with pytest.raises(vault_module.PathGuardError) as precedence:
        census.recheck(tmp_path)
    assert precedence.value.code == "BATCH_RESIDUE_LIMIT"


def test_directory_census_enforces_residue_child_cap_before_validation(
    tmp_path: Path,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    residue = _make_residue(guarded, 1)
    for index in range(4_096):
        (residue / f"stage-{index}.tmp").touch()

    census = vault_module.DirectoryCensusGuard.capture(
        tmp_path,
        "guarded",
        max_entries=0,
    )
    assert census.entries == ()

    overflow = residue / "stage-4096.tmp"
    overflow.touch()
    with pytest.raises(vault_module.PathGuardError) as limited:
        census.recheck(tmp_path)
    assert limited.value.code == "BATCH_RESIDUE_LIMIT"

    overflow.unlink()
    (residue / "stage-0.tmp").rename(residue / "stage-١.tmp")
    with pytest.raises(vault_module.PathGuardError) as unsafe:
        census.recheck(tmp_path)
    assert unsafe.value.code == "BATCH_RESIDUE_UNSAFE"

    overflow.touch()
    with pytest.raises(vault_module.PathGuardError) as precedence:
        census.recheck(tmp_path)
    assert precedence.value.code == "BATCH_RESIDUE_LIMIT"


def test_directory_census_ignores_valid_residue_lifecycle_but_fails_unsafe(
    tmp_path: Path,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    census = vault_module.DirectoryCensusGuard.capture(
        tmp_path,
        "guarded",
        max_entries=0,
    )

    residue = _make_residue(guarded, 1, children=("stage-0.tmp",))
    census.recheck(tmp_path)
    (residue / "stage-0.tmp").unlink()
    residue.rmdir()
    census.recheck(tmp_path)

    residue = _make_residue(guarded, 2, children=("restore-0.tmp",))
    census.recheck(tmp_path)
    (residue / "unexpected.tmp").write_bytes(b"unsafe")
    with pytest.raises(vault_module.PathGuardError) as unsafe:
        census.recheck(tmp_path)
    assert unsafe.value.code == "BATCH_RESIDUE_UNSAFE"


def test_bounded_census_path_fallback_rejects_substituted_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    original_info = guarded.lstat()
    expected = vault_module._identity("guarded", original_info)
    displaced = tmp_path / "guarded-displaced"
    real_scandir = os.scandir
    substituted = False

    def substitute_before_path_scan(path):
        nonlocal substituted
        if not substituted and os.path.abspath(path) == os.path.abspath(guarded):
            substituted = True
            guarded.rename(displaced)
            guarded.mkdir()
        return real_scandir(path)

    monkeypatch.setattr(vault_module.os, "scandir", substitute_before_path_scan)
    _replace_capability_member(
        monkeypatch,
        "supports_fd",
        real_scandir,
        None,
    )

    with pytest.raises(vault_module.PathGuardError) as changed:
        vault_module._bounded_directory_entries(
            guarded,
            relative="guarded",
            expected=expected,
            max_entries=0,
        )

    assert substituted is True
    assert changed.value.code == "PATH_GUARD_CHANGED"


def test_directory_census_classifies_residue_through_path_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    residue = _make_residue(guarded, 1, children=("stage-0.tmp",))
    workspace_times = (
        1_701_111_111_123_456_789,
        1_702_222_222_987_654_321,
    )
    os.utime(residue, ns=workspace_times)

    _replace_capability_member(monkeypatch, "supports_dir_fd", os.open, None)
    _replace_capability_member(monkeypatch, "supports_dir_fd", os.stat, None)
    _replace_capability_member(monkeypatch, "supports_fd", os.scandir, None)
    _replace_capability_member(monkeypatch, "supports_fd", os.utime, None)
    _replace_capability_member(monkeypatch, "supports_dir_fd", os.utime, None)

    census = vault_module.DirectoryCensusGuard.capture(
        tmp_path,
        "guarded",
        max_entries=0,
    )
    census.recheck(tmp_path)

    residue_info = residue.stat()
    assert census.entries == ()
    assert (residue_info.st_atime_ns, residue_info.st_mtime_ns) == workspace_times
    assert (residue / "stage-0.tmp").read_bytes() == b"residue:stage-0.tmp"


def test_bounded_census_stops_retaining_ordinary_names_after_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    expected = vault_module._identity("guarded", guarded.lstat())
    real_scandir = os.scandir

    class TrackedName(str):
        live = 0

        def __new__(cls, value: str):
            instance = super().__new__(cls, value)
            cls.live += 1
            return instance

        def __del__(self) -> None:
            type(self).live -= 1

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class BoundedIterator:
        def __init__(self) -> None:
            self.index = 0

        def __iter__(self):
            return self

        def __next__(self):
            if TrackedName.live > 2:
                raise AssertionError("ordinary entry names accumulated past the bound")
            if self.index == 8:
                raise StopIteration
            name = TrackedName(f"ordinary-{self.index}")
            self.index += 1
            return Entry(name)

        def close(self) -> None:
            return None

    def bounded_scandir(path):
        if os.path.abspath(path) == os.path.abspath(guarded):
            return BoundedIterator()
        return real_scandir(path)

    monkeypatch.setattr(vault_module.os, "scandir", bounded_scandir)
    _replace_capability_member(
        monkeypatch,
        "supports_fd",
        real_scandir,
        None,
    )

    with pytest.raises(vault_module.PathGuardError) as limited:
        vault_module._bounded_directory_entries(
            guarded,
            relative="guarded",
            expected=expected,
            max_entries=0,
        )

    assert limited.value.code == "PATH_GUARD_LIMIT"


def test_batch_atomic_write_retries_with_fresh_workspace_beside_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    target = guarded / "target.md"
    target.write_bytes(b"old")
    stale = _make_residue(guarded, 1, children=("stage-0.tmp",))
    for index in range(2, 65):
        _make_residue(guarded, index)
    stale_bytes = (stale / "stage-0.tmp").read_bytes()
    census = vault_module.DirectoryCensusGuard.capture(
        tmp_path,
        "guarded",
        max_entries=1,
    )
    fresh_suffix = "f" * 32
    suffixes = iter((stale.name.removeprefix(".exomem-batch-"), fresh_suffix))
    written_workspaces: set[str] = set()
    real_write = os.write

    def fixed_token_hex(_size: int) -> str:
        return next(suffixes)

    def observe_write(descriptor: int, data) -> int:
        try:
            stage = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if stage.name.startswith("stage-"):
                written_workspaces.add(stage.parent.name)
        except OSError:
            pass
        return real_write(descriptor, data)

    monkeypatch.setattr(vault_module.secrets, "token_hex", fixed_token_hex)
    monkeypatch.setattr(vault_module.os, "write", observe_write)

    replaced = vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(target, "new")],
        vault_root=tmp_path,
        required_guards=(census,),
    )

    assert replaced == [target]
    assert target.read_bytes() == b"new"
    assert written_workspaces == {f".exomem-batch-{fresh_suffix}"}
    assert len(_workspaces(guarded)) == 64
    assert stale in _workspaces(guarded)
    assert (stale / "stage-0.tmp").read_bytes() == stale_bytes


@pytest.mark.parametrize("nested", [False, True])
def test_batch_atomic_write_rejects_reserved_logical_target_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
) -> None:
    reserved = (
        tmp_path / ".exomem-batch-user"
        if nested
        else tmp_path / _residue_name(99)
    )
    target = reserved / "target.md" if nested else reserved
    create_calls: list[Path] = []

    def reject_workspace_create(cls, parent: Path):
        create_calls.append(parent)
        raise AssertionError("workspace creation must not run")

    monkeypatch.setattr(
        vault_module._BatchWorkspace,
        "create",
        classmethod(reject_workspace_create),
    )

    with pytest.raises(vault_module.PathGuardError) as unsafe:
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, "must not write")]
        )

    assert unsafe.value.code == "BATCH_RESIDUE_UNSAFE"
    assert unsafe.value.reason == "private batch residue is unsafe"
    assert create_calls == []
    assert not os.path.lexists(reserved)


def test_directory_census_keeps_user_backup_in_ordinary_capacity(
    tmp_path: Path,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    _make_residue(guarded, 1)
    (guarded / "user-owned.bak").write_bytes(b"ordinary")

    with pytest.raises(vault_module.PathGuardError) as limited:
        vault_module.DirectoryCensusGuard.capture(
            tmp_path,
            "guarded",
            max_entries=0,
        )

    assert limited.value.code == "PATH_GUARD_LIMIT"


def test_directory_census_guard_enforces_its_raw_entry_bound(tmp_path: Path) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    for index in range(5):
        (guarded / f"{index}.txt").write_bytes(b"entry")

    with pytest.raises(vault_module.PathGuardError) as bounded:
        vault_module.DirectoryCensusGuard.capture(
            tmp_path, "guarded", max_entries=4
        )

    assert bounded.value.code == "PATH_GUARD_LIMIT"


def test_move_file_rolls_back_rename_when_link_batch_fails(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = vault / "Knowledge Base" / "Notes" / "Insights" / "old.md"
    inbound = vault / "Knowledge Base" / "Notes" / "Insights" / "inbound.md"
    old.write_text("# Old\n", encoding="utf-8")
    inbound.write_text("See [[Knowledge Base/Notes/Insights/old]].\n", encoding="utf-8")

    def fail_batch(*args, **kwargs):
        raise OSError("injected link batch failure")

    monkeypatch.setattr(move_module, "batch_atomic_write", fail_batch)
    with pytest.raises(OSError, match="link batch failure"):
        move_module.move_file(
            vault,
            old_path="Knowledge Base/Notes/Insights/old.md",
            new_path="Knowledge Base/Notes/Insights/new.md",
        )

    assert old.exists()
    assert not (old.parent / "new.md").exists()
    assert "[[Knowledge Base/Notes/Insights/old]]" in inbound.read_text(encoding="utf-8")
