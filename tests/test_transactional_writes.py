from __future__ import annotations

import json
import os
import pickle
import re
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from exomem import cli_ops, media_jobs
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


def _inject_residue_mutation_at_metadata_check(
    monkeypatch: pytest.MonkeyPatch,
    residue: Path,
    mutation: Callable[[], None],
) -> dict[str, int]:
    """Inject after the baseline census while masking coarse timestamp changes."""
    snapshot = residue.stat()
    real_fstat = os.fstat
    real_stat = os.stat
    real_scandir = os.scandir
    state = {
        "injected": 0,
        "workspace_fstats": 0,
        "masked_metadata_checks": 0,
        "post_injection_scans": 0,
    }
    workspace_descriptor: int | None = None

    def inject_before_metadata_fstat(descriptor: int):
        nonlocal workspace_descriptor
        if descriptor == workspace_descriptor:
            state["workspace_fstats"] += 1
            if state["workspace_fstats"] == 2:
                mutation()
                state["injected"] = 1
                state["masked_metadata_checks"] += 1
                return snapshot
        info = real_fstat(descriptor)
        if (
            workspace_descriptor is None
            and info.st_dev == snapshot.st_dev
            and info.st_ino == snapshot.st_ino
        ):
            workspace_descriptor = descriptor
            state["workspace_fstats"] = 1
        return info

    def preserve_workspace_path_metadata(path, *args, **kwargs):
        if (
            state["injected"]
            and os.fspath(path) == residue.name
            and kwargs.get("dir_fd") is not None
        ):
            state["masked_metadata_checks"] += 1
            return snapshot
        return real_stat(path, *args, **kwargs)

    def observe_post_injection_scan(path):
        if isinstance(path, int):
            info = real_fstat(path)
            if (
                state["injected"]
                and info.st_dev == snapshot.st_dev
                and info.st_ino == snapshot.st_ino
            ):
                state["post_injection_scans"] += 1
        return real_scandir(path)

    monkeypatch.setattr(vault_module.os, "fstat", inject_before_metadata_fstat)
    monkeypatch.setattr(vault_module.os, "stat", preserve_workspace_path_metadata)
    monkeypatch.setattr(vault_module.os, "scandir", observe_post_injection_scan)
    _replace_capability_member(
        monkeypatch,
        "supports_dir_fd",
        real_stat,
        preserve_workspace_path_metadata,
    )
    _replace_capability_member(
        monkeypatch,
        "supports_fd",
        real_scandir,
        observe_post_injection_scan,
    )
    return state


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
    assert workspace_mkdir_modes == [0o777 if os.name == "nt" else 0o700]
    assert backup_creations == []
    assert watcher_calls == [(existing, created)]
    assert index_calls == [(existing, created)]
    assert reports == [report]
    assert _workspaces(tmp_path) == []
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".bak")]


@pytest.mark.skipif(os.name != "nt", reason="Windows binary-read regression")
def test_batch_rollback_preserves_crlf_bytes_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    original = b"first\r\nold\r\n"
    first.write_bytes(original)
    second.write_bytes(b"second-old")
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

    with pytest.raises(OSError, match="injected second flip failure"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ]
        )

    assert first.read_bytes() == original


@pytest.mark.skipif(os.name != "nt", reason="Windows binary-descriptor regression")
def test_batch_pre_flip_failure_cleans_crlf_stage_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.md"
    real_replace = os.replace

    def fail_before_flip(src, dst, *args, **kwargs):
        if _leaf(src).startswith("stage-"):
            raise OSError("injected pre-flip failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_module.os, "replace", fail_before_flip)

    with pytest.raises(OSError, match="injected pre-flip failure"):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, "first\r\nsecond\r\n")]
        )

    assert not target.exists()
    assert _workspaces(tmp_path) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows directory-handle regression")
def test_batch_atomic_write_replaces_and_creates_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "Knowledge Base" / "Evidence"
    parent.mkdir(parents=True)
    existing = parent / "existing.md"
    created = parent / "created.md"
    existing.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr("exomem.file_watcher.register_self_write", lambda *_: None)
    monkeypatch.setattr("exomem.index_sync.upsert_after_write", lambda *_: None)

    replaced = vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(
                existing,
                "new\n",
                expected_hash=vault_module.content_hash("old\n"),
            ),
            vault_module.PlannedWrite(created, "created\n"),
        ],
        vault_root=tmp_path,
    )

    assert replaced == [existing, created]
    assert existing.read_text(encoding="utf-8") == "new\n"
    assert created.read_text(encoding="utf-8") == "created\n"
    assert _workspaces(parent) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows closed-stage cleanup regression")
def test_batch_atomic_write_cleans_closed_stage_after_windows_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first-old\n", encoding="utf-8")
    second.write_text("second-old\n", encoding="utf-8")
    real_replace = os.replace
    flips = 0

    def fail_second_flip(source, destination, *args, **kwargs):
        nonlocal flips
        if _leaf(source).startswith("stage-"):
            flips += 1
            if flips == 2:
                raise OSError("injected Windows replace failure")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(vault_module.os, "replace", fail_second_flip)

    with pytest.raises(OSError, match="injected Windows replace failure"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new\n"),
                vault_module.PlannedWrite(second, "second-new\n"),
            ]
        )

    assert first.read_text(encoding="utf-8") == "first-old\n"
    assert second.read_text(encoding="utf-8") == "second-old\n"
    assert _workspaces(tmp_path) == []


def test_batch_target_summary_is_bounded_safe_and_vault_relative(
    tmp_path: Path,
) -> None:
    safe = [vault_module.PlannedWrite(tmp_path / f"safe-{index}.md", "x") for index in range(18)]
    writes = [
        vault_module.PlannedWrite(tmp_path.parent / "outside.md", "x"),
        vault_module.PlannedWrite(tmp_path / "unsafe\0name.md", "x"),
        vault_module.PlannedWrite(tmp_path / f"{'x' * 1025}.md", "x"),
        vault_module.PlannedWrite(tmp_path / "bad-\udcff.md", "x"),
        *safe,
    ]

    summary = vault_module._summarize_batch_targets(writes, tmp_path)

    assert summary == vault_module.BatchTargetSummary(
        affected_count=22,
        targets=tuple(f"safe-{index}.md" for index in range(16)),
        omitted_target_count=6,
    )
    with pytest.raises(AttributeError):
        summary.affected_count = 0

    no_root = vault_module._summarize_batch_targets(safe[:2], None)
    assert no_root == vault_module.BatchTargetSummary(2, (), 2)


@pytest.mark.parametrize(
    ("code", "committed", "kind", "message", "remediation"),
    [
        (
            "BATCH_ROLLBACK_INCOMPLETE",
            False,
            "rollback_incomplete",
            "The batch could not be fully rolled back.",
            "Reconcile retained workspace state, then retry with fresh guards if the "
            "intended write is still needed.",
        ),
        (
            "BATCH_CLEANUP_INCOMPLETE",
            True,
            "cleanup_incomplete",
            "The batch workspace cleanup is incomplete.",
            "Do not retry the write; committed destinations are preserved. Reconcile "
            "retained workspace state.",
        ),
    ],
)
def test_batch_write_error_public_payload_and_pickle_are_sanitized(
    tmp_path: Path,
    code: str,
    committed: bool,
    kind: str,
    message: str,
    remediation: str,
) -> None:
    summary = vault_module.BatchTargetSummary(
        affected_count=18,
        targets=tuple(f"safe-{index}.md" for index in range(16)),
        omitted_target_count=2,
    )
    raw_text = (
        f"{tmp_path}/.exomem-batch-{'a' * 32}/stage-0.tmp: "
        "low-level permission detail"
    )
    raw = PermissionError(raw_text)
    error = vault_module.BatchWriteError(
        code,
        summary,
        committed=committed,
        diagnostics=(raw,),
    )
    try:
        raise error from raw
    except vault_module.BatchWriteError as raised:
        error = raised

    expected = {
        "code": code,
        "message": message,
        "remediation": remediation,
        "outcome": {
            "kind": kind,
            "committed": committed,
            "incomplete": True,
            "affected_count": 18,
            "targets": [f"safe-{index}.md" for index in range(16)],
            "omitted_target_count": 2,
        },
    }
    assert error.as_public_dict() == expected
    assert error.code == code
    assert error.outcome_kind == kind
    assert error.committed is committed
    assert error.incomplete is True
    assert str(error) == json.dumps(
        expected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert error.__cause__ is raw
    assert error._diagnostics == (raw,)
    for secret in (str(tmp_path), ".exomem-batch-", "stage-0.tmp", "permission detail"):
        assert secret not in str(error)
        assert secret not in json.dumps(error.as_public_dict())
        assert secret not in json.dumps(cli_ops.error_dict(error))

    serialized = pickle.dumps(error)
    for secret in (os.fsencode(tmp_path), b".exomem-batch-", b"stage-0.tmp", b"permission detail"):
        assert secret not in serialized
    restored = pickle.loads(serialized)
    assert restored.as_public_dict() == expected
    assert str(restored) == str(error)
    assert restored.__cause__ is None
    assert restored._diagnostics == ()


def test_batch_cleanup_outcome_summarizes_deduped_commit_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    raw = PermissionError("private workspace initialization failure")

    def fail_workspace_create(cls, parent: Path):
        raise raw

    monkeypatch.setattr(
        vault_module._BatchWorkspace,
        "create",
        classmethod(fail_workspace_create),
    )
    monkeypatch.setattr(
        vault_module,
        "_cleanup_batch_workspaces",
        lambda workspaces: True,
    )

    with pytest.raises(vault_module.BatchWriteError) as incomplete:
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first draft"),
                vault_module.PlannedWrite(second, "second"),
                vault_module.PlannedWrite(first, "first final"),
            ],
            vault_root=tmp_path,
        )

    assert incomplete.value.committed is False
    assert incomplete.value.as_public_dict()["outcome"] == {
        "kind": "cleanup_incomplete",
        "committed": False,
        "incomplete": True,
        "affected_count": 2,
        "targets": ["first.md", "second.md"],
        "omitted_target_count": 0,
    }
    assert incomplete.value.__cause__ is raw


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

    with pytest.raises(vault_module.BatchWriteError) as incomplete:
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ]
        )

    assert incomplete.value.code == "BATCH_ROLLBACK_INCOMPLETE"
    assert incomplete.value.committed is False
    assert first.read_bytes() == b"first-new"
    assert second.read_bytes() == b"second-old"
    retained = _workspaces(tmp_path)
    assert len(retained) == 1
    assert sorted(path.name for path in retained[0].iterdir()) == [
        "restore-0.tmp",
        "stage-1.tmp",
    ]


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
    raw_errors: list[PermissionError] = []

    def drift_stage_then_fail(_path: Path):
        workspace = _workspaces(tmp_path)[0]
        stage = next(workspace.glob("stage-*.tmp"))
        stage.write_bytes(drifted)
        error = PermissionError(
            f"{stage}: low-level snapshot failure after stage drift"
        )
        raw_errors.append(error)
        raise error

    monkeypatch.setattr(
        vault_module,
        "_capture_batch_snapshot",
        drift_stage_then_fail,
    )

    with pytest.raises(vault_module.BatchWriteError) as retained:
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, "new exact content")],
            vault_root=tmp_path,
        )

    assert retained.value.code == "BATCH_CLEANUP_INCOMPLETE"
    assert retained.value.committed is False
    assert retained.value.as_public_dict()["outcome"] == {
        "kind": "cleanup_incomplete",
        "committed": False,
        "incomplete": True,
        "affected_count": 1,
        "targets": ["target.md"],
        "omitted_target_count": 0,
    }
    assert retained.value.__cause__ is raw_errors[0]
    assert str(tmp_path) not in str(retained.value)
    assert ".exomem-batch-" not in str(retained.value)
    assert "stage-0.tmp" not in str(retained.value)
    assert "snapshot failure" not in str(retained.value)
    workspace = _workspaces(tmp_path)[0]
    stage = next(workspace.glob("stage-*.tmp"))
    assert stage.read_bytes() == drifted
    assert target.read_bytes() == b"old"


def test_clean_batch_cleanup_preserves_sharing_error_path_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sidecar.md"
    target.write_bytes(b"old")
    captured: list[PermissionError] = []
    real_replace = os.replace

    def deny_stage_replace(src, dst, *args, **kwargs):
        if _leaf(src).startswith("stage-"):
            workspace = _workspaces(tmp_path)[0]
            error = PermissionError(13, "Access is denied", str(workspace / _leaf(src)))
            error.winerror = 5
            error.filename2 = str(target)
            captured.append(error)
            raise error
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_module.os, "replace", deny_stage_replace)

    with pytest.raises(PermissionError) as raised:
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, "new")],
            vault_root=tmp_path,
        )

    assert raised.value is captured[0]
    assert raised.value.filename2 == str(target)
    assert media_jobs.is_guarded_sidecar_sharing_violation(raised.value, target)
    assert target.read_bytes() == b"old"
    assert _workspaces(tmp_path) == []


def test_batch_atomic_write_reports_cleanup_incomplete_after_complete_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"first-old")
    second.write_bytes(b"second-old")
    real_replace = os.replace
    real_cleanup = vault_module._BatchWorkspace.cleanup
    stage_flips = 0
    cleanup_calls = 0
    replacements: list[tuple[str, str]] = []

    def fail_second_stage(src, dst, *args, **kwargs):
        nonlocal stage_flips
        source = _leaf(src)
        if source.startswith("stage-"):
            stage_flips += 1
            if stage_flips == 2:
                raise OSError("raw commit failure")
        replacements.append((source, _leaf(dst)))
        return real_replace(src, dst, *args, **kwargs)

    def retain_workspace_during_cleanup(self):
        nonlocal cleanup_calls
        cleanup_calls += 1
        (self.path / "unexpected.tmp").write_bytes(b"retain for reconcile")
        return real_cleanup(self)

    monkeypatch.setattr(vault_module.os, "replace", fail_second_stage)
    monkeypatch.setattr(
        vault_module._BatchWorkspace,
        "cleanup",
        retain_workspace_during_cleanup,
    )

    with pytest.raises(vault_module.BatchWriteError) as incomplete:
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ],
            vault_root=tmp_path,
        )

    assert incomplete.value.code == "BATCH_CLEANUP_INCOMPLETE"
    assert incomplete.value.committed is False
    assert isinstance(incomplete.value.__cause__, OSError)
    assert first.read_bytes() == b"first-old"
    assert second.read_bytes() == b"second-old"
    assert cleanup_calls == 1
    assert replacements == [("stage-0.tmp", "first.md"), ("restore-0.tmp", "first.md")]
    workspace = _workspaces(tmp_path)[0]
    assert (workspace / "unexpected.tmp").read_bytes() == b"retain for reconcile"


def test_batch_atomic_write_fans_out_once_before_committed_cleanup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"first-old")
    second.write_bytes(b"second-old")
    real_replace = os.replace
    real_cleanup = vault_module._BatchWorkspace.cleanup
    cleanup_calls = 0
    replacements: list[tuple[str, str]] = []
    watcher_calls: list[tuple[Path, ...]] = []
    index_calls: list[tuple[Path, ...]] = []
    reports: list[object] = []
    report = object()

    def observe_replace(src, dst, *args, **kwargs):
        replacements.append((_leaf(src), _leaf(dst)))
        return real_replace(src, dst, *args, **kwargs)

    def retain_workspace_during_cleanup(self):
        nonlocal cleanup_calls
        cleanup_calls += 1
        (self.path / "unexpected.tmp").write_bytes(b"post-commit residue")
        return real_cleanup(self)

    def register(_root: Path, paths: list[Path]) -> None:
        watcher_calls.append(tuple(paths))

    def index(_root: Path, paths: list[Path]) -> object:
        index_calls.append(tuple(paths))
        return report

    monkeypatch.setattr(vault_module.os, "replace", observe_replace)
    monkeypatch.setattr(
        vault_module._BatchWorkspace,
        "cleanup",
        retain_workspace_during_cleanup,
    )
    monkeypatch.setattr("exomem.file_watcher.register_self_write", register)
    monkeypatch.setattr("exomem.index_sync.upsert_after_write", index)

    with pytest.raises(vault_module.BatchWriteError) as incomplete:
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ],
            vault_root=tmp_path,
            index_reports=reports,
        )

    assert incomplete.value.code == "BATCH_CLEANUP_INCOMPLETE"
    assert incomplete.value.committed is True
    assert incomplete.value.__cause__ is None
    assert first.read_bytes() == b"first-new"
    assert second.read_bytes() == b"second-new"
    assert cleanup_calls == 1
    assert replacements == [("stage-0.tmp", "first.md"), ("stage-1.tmp", "second.md")]
    assert watcher_calls == [(first, second)]
    assert index_calls == [(first, second)]
    assert reports == [report]
    workspace = _workspaces(tmp_path)[0]
    assert (workspace / "unexpected.tmp").read_bytes() == b"post-commit residue"


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
    with pytest.raises(vault_module.BatchWriteError) as incomplete:
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(safe, "safe-new"),
                vault_module.PlannedWrite(guarded_target, "guarded-new"),
                vault_module.PlannedWrite(pending, "pending-new"),
            ],
            vault_root=tmp_path,
            required_guards=(census,),
        )

    assert incomplete.value.code == "BATCH_ROLLBACK_INCOMPLETE"
    assert incomplete.value.committed is False
    assert incomplete.value.as_public_dict()["outcome"] == {
        "kind": "rollback_incomplete",
        "committed": False,
        "incomplete": True,
        "affected_count": 3,
        "targets": ["safe.md", "guarded/guarded.md", "pending.md"],
        "omitted_target_count": 0,
    }
    assert isinstance(incomplete.value.__cause__, vault_module.PathGuardError)
    assert incomplete.value.__cause__.code == "PATH_GUARD_CHANGED"
    assert restore_targets == ["safe.md"]
    assert safe.read_bytes() == b"safe-old"
    assert guarded_target.read_bytes() == b"guarded-new"
    assert pending.read_bytes() == b"pending-old"
    assert concurrent.read_bytes() == b"concurrent-owned"
    assert _workspaces(tmp_path) == []
    retained = _workspaces(guarded)
    assert len(retained) == 1
    assert list(retained[0].iterdir()) == []


def test_directory_census_ignores_exact_valid_residue_without_touching_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    real_open = os.open
    noatime_opened = False

    def observe_noatime_open(path, flags, *args, **kwargs):
        nonlocal noatime_opened
        descriptor = real_open(path, flags, *args, **kwargs)
        if (
            os.fspath(path) == residue.name
            and flags & getattr(os, "O_NOATIME", 0)
        ):
            noatime_opened = True
        return descriptor

    monkeypatch.setattr(vault_module.os, "open", observe_noatime_open)
    _replace_capability_member(
        monkeypatch,
        "supports_dir_fd",
        real_open,
        observe_noatime_open,
    )

    census = vault_module.DirectoryCensusGuard.capture(
        tmp_path,
        "guarded",
        max_entries=0,
    )
    census.recheck(tmp_path)

    assert census.entries == ()
    residue_info = residue.stat()
    assert residue_info.st_mtime_ns == workspace_times[1]
    if noatime_opened:
        assert residue_info.st_atime_ns == workspace_times[0]
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


def test_directory_census_leaves_invalid_residue_structurally_intact(
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
    assert residue_info.st_mtime_ns == workspace_times[1]
    assert (residue / "unexpected.tmp").read_bytes() == b"residue:unexpected.tmp"


def test_directory_census_never_writes_residue_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    _make_residue(guarded, 1, children=("unexpected.tmp",))
    real_utime = os.utime

    def forbid_residue_timestamp_write(path, *args, **kwargs):
        raise AssertionError("residue classification must not call utime")

    monkeypatch.setattr(vault_module.os, "utime", forbid_residue_timestamp_write)
    _replace_capability_member(
        monkeypatch,
        "supports_fd",
        real_utime,
        forbid_residue_timestamp_write,
    )

    with pytest.raises(vault_module.PathGuardError) as unsafe:
        vault_module.DirectoryCensusGuard.capture(
            tmp_path,
            "guarded",
            max_entries=0,
        )

    assert unsafe.value.code == "BATCH_RESIDUE_UNSAFE"


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


def test_directory_census_ends_with_child_scan_after_coarse_metadata_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    residue = _make_residue(guarded, 1, children=("stage-0.tmp",))
    expected = vault_module._identity("guarded", guarded.lstat())
    state = _inject_residue_mutation_at_metadata_check(
        monkeypatch,
        residue,
        lambda: (residue / "unexpected.tmp").write_bytes(b"late injection"),
    )

    with pytest.raises(vault_module.PathGuardError) as unsafe:
        vault_module._bounded_directory_entries(
            guarded,
            relative="guarded",
            expected=expected,
            max_entries=0,
        )

    assert state["injected"] == 1
    assert state["masked_metadata_checks"] == 2
    assert state["post_injection_scans"] == 1
    assert unsafe.value.code == "BATCH_RESIDUE_UNSAFE"
    assert (residue / "unexpected.tmp").read_bytes() == b"late injection"


def test_directory_census_final_child_scan_rejects_identity_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    residue = _make_residue(guarded, 1, children=("stage-0.tmp",))
    original = (residue / "stage-0.tmp").stat()
    replacement = tmp_path / "replacement-child"
    replacement.write_bytes(b"replacement identity")
    expected = vault_module._identity("guarded", guarded.lstat())
    state = _inject_residue_mutation_at_metadata_check(
        monkeypatch,
        residue,
        lambda: os.replace(replacement, residue / "stage-0.tmp"),
    )

    with pytest.raises(vault_module.PathGuardError) as unsafe:
        vault_module._bounded_directory_entries(
            guarded,
            relative="guarded",
            expected=expected,
            max_entries=0,
        )

    swapped = (residue / "stage-0.tmp").stat()
    assert state["injected"] == 1
    assert state["masked_metadata_checks"] == 2
    assert state["post_injection_scans"] == 1
    assert (swapped.st_dev, swapped.st_ino) != (original.st_dev, original.st_ino)
    assert unsafe.value.code == "BATCH_RESIDUE_UNSAFE"


def test_directory_census_does_not_overwrite_concurrent_workspace_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    residue = _make_residue(guarded, 1, children=("stage-0.tmp",))
    initial_times = (
        1_701_111_111_123_456_789,
        1_702_222_222_987_654_321,
    )
    concurrent_times = (
        1_721_111_111_123_456_789,
        1_722_222_222_987_654_321,
    )
    os.utime(residue, ns=initial_times)
    residue_info = residue.stat()
    expected = vault_module._identity("guarded", guarded.lstat())
    real_fstat = os.fstat
    workspace_descriptor: int | None = None
    workspace_checks = 0

    def update_before_final_fstat(descriptor: int):
        nonlocal workspace_checks, workspace_descriptor
        if descriptor == workspace_descriptor:
            workspace_checks += 1
            if workspace_checks == 2:
                os.utime(residue, ns=concurrent_times)
        info = real_fstat(descriptor)
        if (
            workspace_descriptor is None
            and info.st_dev == residue_info.st_dev
            and info.st_ino == residue_info.st_ino
        ):
            workspace_descriptor = descriptor
            workspace_checks = 1
        return info

    monkeypatch.setattr(vault_module.os, "fstat", update_before_final_fstat)

    with pytest.raises(vault_module.PathGuardError) as unsafe:
        vault_module._bounded_directory_entries(
            guarded,
            relative="guarded",
            expected=expected,
            max_entries=0,
        )

    final_info = residue.stat()
    assert workspace_checks >= 2
    assert unsafe.value.code == "BATCH_RESIDUE_UNSAFE"
    assert (final_info.st_atime_ns, final_info.st_mtime_ns) == concurrent_times


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
    monkeypatch: pytest.MonkeyPatch,
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

    overflow.unlink()
    (residue / "stage-١.tmp").rename(residue / "stage-0.tmp")
    state = _inject_residue_mutation_at_metadata_check(
        monkeypatch,
        residue,
        overflow.touch,
    )

    with pytest.raises(vault_module.PathGuardError) as final_census_limit:
        census.recheck(tmp_path)
    assert state["injected"] == 1
    assert state["masked_metadata_checks"] == 2
    assert state["post_injection_scans"] == 1
    assert final_census_limit.value.code == "BATCH_RESIDUE_LIMIT"


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
    real_utime = os.utime

    def forbid_residue_timestamp_write(path, *args, **kwargs):
        raise AssertionError("residue classification must not call utime")

    _replace_capability_member(monkeypatch, "supports_dir_fd", os.open, None)
    _replace_capability_member(monkeypatch, "supports_dir_fd", os.stat, None)
    _replace_capability_member(monkeypatch, "supports_fd", os.scandir, None)
    monkeypatch.setattr(vault_module.os, "utime", forbid_residue_timestamp_write)
    for capability in ("supports_fd", "supports_dir_fd", "supports_follow_symlinks"):
        _replace_capability_member(
            monkeypatch,
            capability,
            real_utime,
            forbid_residue_timestamp_write,
        )

    census = vault_module.DirectoryCensusGuard.capture(
        tmp_path,
        "guarded",
        max_entries=0,
    )
    census.recheck(tmp_path)

    residue_info = residue.stat()
    assert census.entries == ()
    assert residue_info.st_mtime_ns == workspace_times[1]
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
    created_workspaces: list[str] = []
    artifact_workspaces: list[tuple[str, str]] = []
    real_create = vault_module._BatchWorkspace.create.__func__
    real_create_artifact = vault_module._BatchWorkspace.create_artifact

    def fixed_token_hex(_size: int) -> str:
        return next(suffixes)

    def observe_workspace_create(cls, parent: Path):
        workspace = real_create(cls, parent)
        created_workspaces.append(workspace.name)
        return workspace

    def observe_artifact_create(self, name: str, content: bytes):
        artifact_workspaces.append((self.name, name))
        return real_create_artifact(self, name, content)

    monkeypatch.setattr(vault_module.secrets, "token_hex", fixed_token_hex)
    monkeypatch.setattr(
        vault_module._BatchWorkspace,
        "create",
        classmethod(observe_workspace_create),
    )
    monkeypatch.setattr(
        vault_module._BatchWorkspace,
        "create_artifact",
        observe_artifact_create,
    )

    replaced = vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(target, "new")],
        vault_root=tmp_path,
        required_guards=(census,),
    )

    assert replaced == [target]
    assert target.read_bytes() == b"new"
    assert created_workspaces == [f".exomem-batch-{fresh_suffix}"]
    assert artifact_workspaces
    assert {workspace for workspace, _name in artifact_workspaces} == {
        f".exomem-batch-{fresh_suffix}"
    }
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
    old = vault / "Knowledge Base" / "Notes" / "old.md"
    inbound = vault / "Knowledge Base" / "Notes" / "inbound.md"
    old.write_text("# Old\n", encoding="utf-8")
    inbound.write_text("See [[Knowledge Base/Notes/old]].\n", encoding="utf-8")

    def fail_batch(*args, **kwargs):
        raise OSError("injected link batch failure")

    monkeypatch.setattr(move_module, "batch_atomic_write", fail_batch)
    with pytest.raises(OSError, match="link batch failure"):
        move_module.move_file(
            vault,
            old_path="Knowledge Base/Notes/old.md",
            new_path="Knowledge Base/Notes/new.md",
        )

    assert old.exists()
    assert not (old.parent / "new.md").exists()
    assert "[[Knowledge Base/Notes/old]]" in inbound.read_text(encoding="utf-8")
