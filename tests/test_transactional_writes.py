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


def test_batch_atomic_write_rolls_back_when_required_directory_census_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    census = vault_module.DirectoryCensusGuard.capture(
        tmp_path, "guarded", max_entries=4
    )
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"first-old")
    second.write_bytes(b"second-old")
    real_replace = os.replace
    flips = 0

    def inject_census_change(src, dst, *args, **kwargs):
        nonlocal flips
        result = real_replace(src, dst, *args, **kwargs)
        if _leaf(src).startswith("stage-"):
            flips += 1
            if flips == 1:
                (guarded / "concurrent").write_bytes(b"new")
        return result

    monkeypatch.setattr(vault_module.os, "replace", inject_census_change)
    with pytest.raises(vault_module.PathGuardError) as changed:
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ],
            vault_root=tmp_path,
            required_guards=(census,),
        )

    assert changed.value.code == "PATH_GUARD_CHANGED"
    assert first.read_bytes() == b"first-old"
    assert second.read_bytes() == b"second-old"
    assert _workspaces(tmp_path) == []


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
