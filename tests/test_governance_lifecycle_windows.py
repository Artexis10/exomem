"""Native Windows durability coverage for governed lifecycle barriers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from exomem import delete_file, mutation_lock, recover_from_trash
from exomem.governance import lifecycle

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows lifecycle contract")

_SCOPE = "00000000-0000-4000-8000-0000000000d1"
_RULE = "00000000-0000-4000-8000-0000000000d2"


def _synthetic_windows_path(*parts: str) -> str:
    return "\\".join(("C:", "example", *parts))


def _write_restricting_policy(vault: Path, pattern: str) -> None:
    governance = vault / "Knowledge Base" / "_Governance"
    scope = governance / "scopes" / "lifecycle.yaml"
    rule = governance / "rules" / "lifecycle.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    rule.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        f'governance_version: 1\nid: {_SCOPE}\npaths: ["{pattern}"]\n',
        encoding="utf-8",
    )
    rule.write_text(
        f"governance_version: 1\nid: {_RULE}\n"
        f'scope_ids: ["{_SCOPE}"]\naudience: external\nceiling: 4\n',
        encoding="utf-8",
    )
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()


def _reject_crt_directory_open(original_open):  # noqa: ANN001
    def reject(path, *args, **kwargs):  # noqa: ANN001
        if Path(path).is_dir():
            pytest.fail("native lifecycle durability attempted a CRT directory open")
        return original_open(path, *args, **kwargs)

    return reject


def _junction(path: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(path), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        pytest.skip(f"junction creation unavailable: {completed.stderr or completed.stdout}")


def test_windows_governed_delete_and_recovery_flush_tombstones_without_crt_directory_open(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tombstone write/unlink and both lifecycle moves use native directory handles."""
    rel = "Knowledge Base/Notes/Insights/windows-lifecycle.md"
    target = vault / rel
    target.write_text("---\ntype: insight\nstatus: draft\n---\n# Windows lifecycle\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/windows-lifecycle.md")
    monkeypatch.setattr(lifecycle.os, "open", _reject_crt_directory_open(lifecycle.os.open))

    deleted = delete_file.delete_file(vault, path=rel, confirm=True)
    recovered = recover_from_trash.recover_from_trash(vault, trash_path=deleted.trash_path)

    assert recovered.restored_path == rel
    assert target.exists()


def test_windows_ungoverned_atomic_rename_flushes_parents_without_crt_directory_open(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ungoverned move barrier cannot route a parent directory through the CRT."""
    source_rel = "Knowledge Base/Scratch/windows-rename.txt"
    trash_rel = "Knowledge Base/_trash/windows-rename.txt"
    source = vault / source_rel
    destination = vault / trash_rel
    source.parent.mkdir(parents=True)
    source.write_text("rename me\n", encoding="utf-8")
    operation = lifecycle.begin_deletion(vault, source_rel=source_rel, trash_rel=trash_rel)
    assert operation.governed is False
    monkeypatch.setattr(lifecycle.os, "open", _reject_crt_directory_open(lifecycle.os.open))

    lifecycle.atomic_rename(operation, source=source, destination=destination)

    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "rename me\n"


def test_windows_lifecycle_directory_flush_refusals_are_content_free(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsafe native path and raw flush errors preserve the lifecycle error boundary."""
    directory = vault / "Knowledge Base" / "_Governance" / "deletion-tombstones"
    directory.mkdir(parents=True)
    detail = f"FlushFileBuffers {_synthetic_windows_path('private', 'lifecycle')}"

    def fail_flush(_handle: int) -> None:
        raise OSError(detail)

    monkeypatch.setattr(mutation_lock, "_windows_flush_directory_handle", fail_flush, raising=False)
    with pytest.raises(lifecycle.LifecycleError) as exc_info:
        lifecycle._fsync_directory(directory)

    assert exc_info.value.code == "LIFECYCLE_PATH_UNSAFE"
    assert detail not in str(exc_info.value)


def test_windows_lifecycle_directory_refuses_direct_child_and_identity_changes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native leaf that escapes its retained parent or changes identity is refused."""
    directory = vault / "Knowledge Base" / "_Governance" / "deletion-tombstones"
    directory.mkdir(parents=True)
    detail = _synthetic_windows_path("private", "replacement")

    monkeypatch.setattr(mutation_lock, "_windows_child_is_in_directory", lambda *_args: False)
    with pytest.raises(lifecycle.LifecycleError) as direct_child_error:
        lifecycle._fsync_directory(directory)
    assert direct_child_error.value.code == "LIFECYCLE_PATH_UNSAFE"
    assert detail not in str(direct_child_error.value)

    monkeypatch.setattr(mutation_lock, "_windows_child_is_in_directory", lambda *_args: True)
    monkeypatch.setattr(
        mutation_lock,
        "_windows_handle_identity",
        lambda _handle: (_ for _ in ()).throw(OSError(detail)),
    )
    with pytest.raises(lifecycle.LifecycleError) as identity_error:
        lifecycle._fsync_directory(directory)
    assert identity_error.value.code == "LIFECYCLE_PATH_UNSAFE"
    assert detail not in str(identity_error.value)


def test_windows_lifecycle_directory_refuses_a_reparse_leaf_content_free(
    vault: Path, tmp_path: Path
) -> None:
    """A tombstone junction is refused instead of opening or flushing its target."""
    directory = vault / "Knowledge Base" / "_Governance" / "deletion-tombstones"
    directory.parent.mkdir(parents=True)
    outside = tmp_path / "outside-tombstones"
    outside.mkdir()
    _junction(directory, outside)

    with pytest.raises(lifecycle.LifecycleError) as exc_info:
        lifecycle._fsync_directory(directory)

    assert exc_info.value.code == "LIFECYCLE_PATH_UNSAFE"
    assert str(outside) not in str(exc_info.value)


def test_windows_shared_directory_flush_closes_the_raw_handle_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared native primitive owns one raw final-directory handle exactly once."""
    directory = vault / "Knowledge Base" / "_Governance" / "deletion-tombstones"
    directory.mkdir(parents=True)
    closed: list[int] = []
    flushed: list[int] = []

    class Retained:
        windows_handle = 401

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(mutation_lock, "_open_secure_directory", lambda *_args, **_kwargs: Retained())
    monkeypatch.setattr(mutation_lock, "_windows_open_path", lambda *_args, **_kwargs: 409)
    monkeypatch.setattr(mutation_lock, "_windows_child_is_in_directory", lambda *_args: True)
    monkeypatch.setattr(mutation_lock, "_windows_handle_identity", lambda _handle: (1, 2, 3))
    monkeypatch.setattr(mutation_lock, "_windows_close_handle", closed.append)
    monkeypatch.setattr(mutation_lock, "_windows_flush_directory_handle", flushed.append, raising=False)

    lifecycle._fsync_directory(directory)

    assert flushed == [409]
    assert closed == [409]
