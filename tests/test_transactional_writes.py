from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import move_file as move_module
from exomem import vault as vault_module


def test_batch_atomic_write_rolls_back_mid_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first-old", encoding="utf-8")
    second.write_text("second-old", encoding="utf-8")
    real_replace = os.replace
    replacements = 0

    def fail_second_commit(src, dst):
        nonlocal replacements
        if str(src).endswith(".tmp"):
            replacements += 1
            if replacements == 2:
                raise OSError("injected second replacement failure")
        return real_replace(src, dst)

    monkeypatch.setattr(vault_module.os, "replace", fail_second_commit)
    with pytest.raises(OSError, match="second replacement"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ]
        )

    assert first.read_text(encoding="utf-8") == "first-old"
    assert second.read_text(encoding="utf-8") == "second-old"


def test_batch_atomic_write_removes_new_file_on_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = tmp_path / "created.md"
    existing = tmp_path / "existing.md"
    existing.write_text("old", encoding="utf-8")
    real_replace = os.replace
    replacements = 0

    def fail_second_commit(src, dst):
        nonlocal replacements
        if str(src).endswith(".tmp"):
            replacements += 1
            if replacements == 2:
                raise OSError("commit failed")
        return real_replace(src, dst)

    monkeypatch.setattr(vault_module.os, "replace", fail_second_commit)
    with pytest.raises(OSError, match="commit failed"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(created, "new"),
                vault_module.PlannedWrite(existing, "changed"),
            ]
        )
    assert not created.exists()
    assert existing.read_text(encoding="utf-8") == "old"


def test_batch_atomic_write_reports_and_retains_failed_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first-old", encoding="utf-8")
    second.write_text("second-old", encoding="utf-8")
    real_replace = os.replace
    temp_commits = 0

    def fail_commit_and_restore(src, dst):
        nonlocal temp_commits
        if str(src).endswith(".tmp"):
            temp_commits += 1
            if temp_commits == 2:
                raise OSError("commit failed")
        if str(src).endswith(".bak"):
            raise OSError("restore failed")
        return real_replace(src, dst)

    monkeypatch.setattr(vault_module.os, "replace", fail_commit_and_restore)
    with pytest.raises(RuntimeError, match="rollback also failed.*retained backups"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ]
        )
    assert list(tmp_path.glob("*.bak")) or list(tmp_path.glob(".*.bak"))


@pytest.mark.parametrize("change_kind", ["content", "identity"])
def test_batch_atomic_write_rejects_changed_staged_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change_kind: str
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    first = guarded / "first.md"
    second = guarded / "second.md"
    first.write_text("first-old", encoding="utf-8")
    second.write_text("second-old", encoding="utf-8")
    census = vault_module.DirectoryCensusGuard.capture(
        tmp_path, "guarded", max_entries=8
    )
    real_bind_content = vault_module._OwnedBatchArtifact.bind_content
    staged: list[Path] = []

    def change_after_second_stage(owner, descriptor, *, expected_content_hash):
        guard = real_bind_content(
            owner,
            descriptor,
            expected_content_hash=expected_content_hash,
        )
        if owner.path.suffix != ".tmp":
            return guard
        staged.append(owner.path)
        if len(staged) == 2:
            if change_kind == "content":
                staged[0].write_bytes(b"changed-staged-bytes")
            else:
                replacement = staged[0].with_suffix(".replacement")
                replacement.write_bytes(b"changed-staged-bytes")
                os.replace(replacement, staged[0])
        return guard

    monkeypatch.setattr(
        vault_module._OwnedBatchArtifact,
        "bind_content",
        change_after_second_stage,
    )

    expected_failure = (
        pytest.raises(vault_module.PathGuardError)
        if change_kind == "content"
        else pytest.raises(
            RuntimeError, match="batch cleanup retained changed artifacts"
        )
    )
    with expected_failure as failure:
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ],
            vault_root=tmp_path,
            required_guards=(census,),
        )

    assert first.read_text(encoding="utf-8") == "first-old"
    assert second.read_text(encoding="utf-8") == "second-old"
    if change_kind == "content":
        assert not list(guarded.glob(".*.tmp"))
    else:
        assert staged[0].read_bytes() == b"changed-staged-bytes"
        assert str(tmp_path) not in str(failure.value)
    assert not list(guarded.glob(".*.bak"))


def test_batch_atomic_write_never_restores_a_changed_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first-old", encoding="utf-8")
    second.write_text("second-old", encoding="utf-8")
    real_bind_content = vault_module._OwnedBatchArtifact.bind_content
    backups: list[Path] = []

    def capture_backup(owner, descriptor, *, expected_content_hash):
        guard = real_bind_content(
            owner,
            descriptor,
            expected_content_hash=expected_content_hash,
        )
        if owner.path.suffix == ".bak":
            backups.append(owner.path)
        return guard

    real_replace = os.replace
    temp_commits = 0

    def change_before_rollback(src, dst):
        nonlocal temp_commits
        if str(src).endswith(".tmp"):
            temp_commits += 1
            if temp_commits == 2:
                backups[0].write_bytes(b"changed-backup-bytes")
                raise OSError("injected second replacement failure")
        return real_replace(src, dst)

    monkeypatch.setattr(
        vault_module._OwnedBatchArtifact,
        "bind_content",
        capture_backup,
    )
    monkeypatch.setattr(vault_module.os, "replace", change_before_rollback)

    with pytest.raises(RuntimeError, match="rollback also failed.*PATH_GUARD"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ],
            vault_root=tmp_path,
        )

    assert first.read_text(encoding="utf-8") == "first-new"
    assert second.read_text(encoding="utf-8") == "second-old"
    assert first.read_bytes() != b"changed-backup-bytes"
    assert not list(tmp_path.glob(".*.tmp"))
    assert list(tmp_path.glob(".*.bak"))


def test_batch_atomic_write_records_flip_before_final_binding_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first-old", encoding="utf-8")
    second.write_text("second-old", encoding="utf-8")
    real_replace = os.replace

    def change_first_final_after_flip(src, dst):
        result = real_replace(src, dst)
        if str(src).endswith(".tmp") and Path(dst) == first:
            first.write_bytes(b"concurrent-final-bytes")
        return result

    monkeypatch.setattr(vault_module.os, "replace", change_first_final_after_flip)

    with pytest.raises(RuntimeError, match="rollback also failed.*unbound"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ],
            vault_root=tmp_path,
        )

    assert first.read_bytes() == b"concurrent-final-bytes"
    assert second.read_text(encoding="utf-8") == "second-old"
    assert not list(tmp_path.glob(".*.tmp"))
    assert list(tmp_path.glob(".*.bak"))


def test_batch_atomic_write_owns_staged_descriptor_during_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = tmp_path / "final.md"
    unrelated = tmp_path / "unrelated.md"
    unrelated.write_text("unrelated-original", encoding="utf-8")
    real_mkstemp = vault_module.tempfile.mkstemp

    def substitute_stage_name(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        artifact = Path(name)
        owned = artifact.with_name(f"{artifact.name}.owned")
        os.replace(artifact, owned)
        os.link(unrelated, artifact)
        return fd, name

    monkeypatch.setattr(vault_module.tempfile, "mkstemp", substitute_stage_name)

    with pytest.raises(
        RuntimeError, match="batch cleanup retained changed artifacts"
    ) as failure:
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(final, "planned")],
            vault_root=tmp_path,
        )

    assert unrelated.read_text(encoding="utf-8") == "unrelated-original"
    assert not final.exists()
    assert str(tmp_path) not in str(failure.value)


def test_batch_atomic_write_owns_backup_descriptor_during_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = tmp_path / "final.md"
    unrelated = tmp_path / "unrelated.md"
    final.write_text("final-original", encoding="utf-8")
    unrelated.write_text("unrelated-original", encoding="utf-8")
    real_mkstemp = vault_module.tempfile.mkstemp

    def substitute_backup_name(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        if kwargs.get("suffix") != ".bak":
            return fd, name
        artifact = Path(name)
        owned = artifact.with_name(f"{artifact.name}.owned")
        os.replace(artifact, owned)
        os.link(unrelated, artifact)
        return fd, name

    monkeypatch.setattr(vault_module.tempfile, "mkstemp", substitute_backup_name)

    with pytest.raises(
        RuntimeError, match="batch cleanup retained changed artifacts"
    ) as failure:
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(final, "planned")],
            vault_root=tmp_path,
        )

    assert unrelated.read_text(encoding="utf-8") == "unrelated-original"
    assert final.read_text(encoding="utf-8") == "final-original"
    assert str(tmp_path) not in str(failure.value)


def test_batch_atomic_write_retains_changed_name_during_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = tmp_path / "final.md"
    final.write_text("final-original", encoding="utf-8")
    real_mkstemp = vault_module.tempfile.mkstemp
    replacement: Path | None = None

    def substitute_before_cleanup(*args, **kwargs):
        nonlocal replacement
        if kwargs.get("suffix") == ".bak":
            replacement = next(tmp_path.glob(".*.tmp"))
            replacement.unlink()
            replacement.write_text("replacement", encoding="utf-8")
            raise OSError("injected backup creation failure")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(vault_module.tempfile, "mkstemp", substitute_before_cleanup)

    with pytest.raises(
        RuntimeError, match="batch cleanup retained changed artifacts"
    ) as failure:
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(final, "planned")],
            vault_root=tmp_path,
        )

    assert replacement is not None
    assert replacement.read_text(encoding="utf-8") == "replacement"
    assert str(tmp_path) not in str(failure.value)
    assert final.read_text(encoding="utf-8") == "final-original"


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
    first.write_text("first-old", encoding="utf-8")
    second.write_text("second-old", encoding="utf-8")
    real_replace = os.replace
    replacements = 0

    def inject_census_change(src, dst):
        nonlocal replacements
        result = real_replace(src, dst)
        if str(src).endswith(".tmp"):
            replacements += 1
            if replacements == 1:
                (guarded / "concurrent").write_text("new", encoding="utf-8")
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
    assert first.read_text(encoding="utf-8") == "first-old"
    assert second.read_text(encoding="utf-8") == "second-old"


def test_directory_census_guard_enforces_its_raw_entry_bound(tmp_path: Path) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    for index in range(5):
        (guarded / f"{index}.txt").write_text("entry", encoding="utf-8")

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
