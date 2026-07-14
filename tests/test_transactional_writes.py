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
