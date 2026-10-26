"""Which files a folder import takes, and the reason it leaves each of the others."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem.import_selection import Reason, select

not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file, so none is unreadable")


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_a_deeper_gitignore_overrides_a_shallower_one_the_way_git_does(tmp_path):
    _write(tmp_path, {
        ".gitignore": "*.log\nbuild/\n/top-only.txt\n",
        "top-only.txt": "ignored at the root",
        "sub/top-only.txt": "an anchored pattern does not reach a subfolder",
        "app.log": "ignored",
        "build/out.bin": "ignored folder",
        "sub/.gitignore": "!keep.log\n*.tmp\n",
        "sub/keep.log": "un-ignored by the deeper file",
        "sub/other.log": "still ignored",
        "sub/scratch.tmp": "ignored by the deeper file",
        "sub/real.txt": "kept",
    })

    selection = select(tmp_path)

    assert [item.rel for item in selection.kept] == ["sub/keep.log", "sub/real.txt", "sub/top-only.txt"]
    ignored = {item.rel for item in selection.skipped if item.reason is Reason.GITIGNORE}
    assert ignored == {"top-only.txt", "app.log", "build", "sub/other.log", "sub/scratch.tmp"}


def test_a_catch_all_with_folder_and_markdown_re_includes_keeps_markdown_in_subfolders(tmp_path):
    # Git descends into `sub` here because `!*/` re-includes folders, then keeps `sub/b.md` through `!*.md`.
    _write(tmp_path, {
        ".gitignore": "*\n!*/\n!*.md\n!.gitignore\n",
        "a.md": "kept", "x.bin": "ignored", "sub/b.md": "kept", "sub/c.txt": "ignored", "sub/deep/d.md": "kept",
    })

    selection = select(tmp_path, include_hidden=True)

    assert [item.rel for item in selection.kept] == [".gitignore", "a.md", "sub/b.md", "sub/deep/d.md"]
    assert {item.rel for item in selection.skipped} == {"x.bin", "sub/c.txt"}


def test_hidden_entries_at_any_depth_stay_behind_unless_asked_for_and_are_counted(tmp_path):
    _write(tmp_path, {"a.txt": "kept", ".env": "12345", ".cache/one": "123", ".cache/deep/two": "1234", "d/.note": "12"})

    default = select(tmp_path)
    opted_in = select(tmp_path, include_hidden=True)

    assert [item.rel for item in default.kept] == ["a.txt"]
    assert default.tally()[Reason.HIDDEN] == (4, 5 + 3 + 4 + 2)
    assert [item.rel for item in opted_in.kept] == [".cache/deep/two", ".cache/one", ".env", "a.txt", "d/.note"]


def test_gitignore_rules_are_honoured_by_default_and_can_be_switched_off(tmp_path):
    _write(tmp_path, {".gitignore": "*.log\n", "a.log": "ignored", "b.txt": "kept"})

    honoured = select(tmp_path)
    ignored_rules = select(tmp_path, use_gitignore=False)

    assert [item.rel for item in honoured.kept] == ["b.txt"]
    assert [item.rel for item in ignored_rules.kept] == ["a.log", "b.txt"]


def test_links_are_skipped_and_never_followed_and_special_files_are_named(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not part of the folder")
    folder = tmp_path / "folder"
    _write(folder, {"a.txt": "kept"})
    try:
        (folder / "file-link").symlink_to(outside / "secret.txt")
        (folder / "folder-link").symlink_to(outside, target_is_directory=True)
        os.mkfifo(folder / "pipe")
    except (OSError, AttributeError):
        pytest.skip("links or pipes are not available here")

    selection = select(folder)

    assert [item.rel for item in selection.kept] == ["a.txt"]
    reasons = {item.rel: item.reason for item in selection.skipped}
    assert reasons == {"file-link": Reason.SYMLINK, "folder-link": Reason.SYMLINK, "pipe": Reason.SPECIAL}


@not_root
def test_an_unreadable_file_is_named_before_any_copy_is_tried(tmp_path):
    _write(tmp_path, {"a.txt": "kept", "locked.txt": "12345678"})
    (tmp_path / "locked.txt").chmod(0)
    try:
        selection = select(tmp_path)
    finally:
        (tmp_path / "locked.txt").chmod(0o600)

    assert [item.rel for item in selection.kept] == ["a.txt"]
    assert selection.tally()[Reason.UNREADABLE] == (1, 8)


def test_a_folder_junction_is_skipped_and_never_entered(tmp_path, monkeypatch):
    # A junction is a folder link that `is_symlink` does not report, and Linux cannot make one;
    # the platform's own predicate is replaced so the walk sees the folder "linked" as a junction.
    outside = tmp_path / "outside"
    _write(outside, {"secret.txt": "not part of the folder"})
    folder = tmp_path / "folder"
    _write(folder, {"a.txt": "kept", "linked/secret.txt": "stands in for the junction target"})
    monkeypatch.setattr(os.path, "isjunction", lambda path: os.path.basename(path) == "linked", raising=False)

    selection = select(folder)

    assert [item.rel for item in selection.kept] == ["a.txt"]
    assert [(item.rel, item.reason, item.files) for item in selection.skipped] == [("linked", Reason.SYMLINK, 1)]
