"""`exomem import` stages a folder from outside the vault so adoption can scan it."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import pytest

from exomem import __main__ as cli
from exomem import adoption_run, commands, local_import
from exomem.cli_progress import Progress
from exomem.local_import import LocalImportError, stage

TODAY = dt.date(2026, 10, 26)
not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file, so none is unreadable")


@pytest.fixture(autouse=True)
def _private_manifest_folder(tmp_path, monkeypatch):
    """The manifests go to the user's state folder; a test must never write the real one."""
    monkeypatch.setattr(local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def _folder(tmp_path: Path, files: dict[str, bytes], name: str = "Field Notes") -> Path:
    folder = tmp_path / "elsewhere" / name
    for rel, data in files.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return folder


def _manifest(result: dict) -> list[dict]:
    return [json.loads(line) for line in Path(result["manifest"]).read_text(encoding="utf-8").splitlines()]


def _tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _stage_until_interrupted(folder: Path, vault: Path, copies: int, **options) -> Path:
    """Run the command and stop it after `copies` files; return the import folder it leaves in the vault."""
    seen = 0

    def stop_after_the_last_copy(line: str) -> None:
        nonlocal seen
        if line.startswith("Copying:"):
            seen += 1
            if seen == copies:
                raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        stage(folder, vault, progress=Progress(stop_after_the_last_copy, interval=0), **options)
    (import_dir,) = (vault / "_Imports").iterdir()
    return import_dir


@not_root
def test_folder_is_staged_verified_and_adoption_scans_it_under_the_folder_name(tmp_path):
    vault = _vault(tmp_path)
    files = {"notes.md": b"# Notes\n\nlibration\n", "sub/plan.txt": b"plan\n", "image.png": b"\x89PNG\r\n\x1a\n-binary",
             ".hidden/x.txt": b"hidden", ".gitignore": b"*.log\n", "debug.log": b"ignored", "locked.txt": b"unreadable"}
    folder = _folder(tmp_path, files)
    (folder / "link.md").symlink_to(folder / "notes.md")
    (folder / "locked.txt").chmod(0)
    originals = {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}
    before = sorted(path.name for path in folder.rglob("*"))
    try:
        result = stage(folder, vault)
    finally:
        (folder / "locked.txt").chmod(0o600)

    staged = vault / result["start_path"] / "Field Notes"
    assert _tree(staged) == {rel: files[rel] for rel in ("notes.md", "sub/plan.txt", "image.png")}
    assert sorted(path.name for path in folder.rglob("*")) == before
    records = _manifest(result)
    copied = {record["path"]: record["sha256"] for record in records if record["kind"] == "file"}
    assert copied == {rel: originals[rel] for rel in ("notes.md", "sub/plan.txt", "image.png")}
    codes = {record["path"]: record["code"] for record in records if record["kind"] == "left_out"}
    assert codes == {"link.md": "SYMLINK", "locked.txt": "UNREADABLE", ".hidden": "HIDDEN", ".gitignore": "HIDDEN",
                     "debug.log": "GITIGNORE"}
    assert records[-1]["kind"] == "done"

    run = adoption_run.start(vault, path=result["start_path"], initialize_kb=True, today=TODAY)
    prefix = result["start_path"] + "/Field Notes/"
    assert run["collection"]["label"] == "Field Notes"
    assert {row["path"] for row in run["inventory"]} == {prefix + rel for rel in copied}


def test_folder_larger_than_the_free_space_is_refused_before_anything_is_copied(tmp_path):
    vault = _vault(tmp_path)
    folder = _folder(tmp_path, {"big.bin": b"x" * 4096})

    with pytest.raises(LocalImportError) as refused:
        stage(folder, vault, free_bytes=lambda path: 1000)

    assert refused.value.code == "INSUFFICIENT_DISK"
    assert "4096 bytes" in refused.value.message and "1000 bytes free" in refused.value.message
    assert not (vault / "_Imports").exists()


def test_interrupted_copy_resumes_under_the_same_import_without_copying_verified_files_again(tmp_path):
    vault = _vault(tmp_path)
    files = {f"{name}.txt": name.encode() * 100 for name in ("a", "b", "c", "d")}
    folder = _folder(tmp_path, files)
    copied = 0

    def stop_after_two(line: str) -> None:
        nonlocal copied
        if line.startswith("Copying:"):
            copied += 1
            if copied == 2:
                raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        stage(folder, vault, progress=Progress(stop_after_two, interval=0))
    (import_dir,) = (vault / "_Imports").iterdir()
    first_two = {path.name: path.stat().st_ino for path in (import_dir / "Field Notes").iterdir()}
    assert sorted(first_two) == ["a.txt", "b.txt"]

    result = stage(folder, vault)

    assert result["import_id"] == import_dir.name
    assert result["staged"] == {"files": 4, "bytes": sum(len(data) for data in files.values())}
    staged = import_dir / "Field Notes"
    assert {name: (staged / name).stat().st_ino for name in first_two} == first_two
    assert _tree(staged) == files
    assert [path.name for path in (vault / "_Imports").iterdir()] == [import_dir.name]


def test_a_file_that_changes_while_staging_keeps_the_import_open_and_a_rerun_copies_only_that_file(tmp_path):
    vault = _vault(tmp_path)
    folder = _folder(tmp_path, {"a.txt": b"first", "z.txt": b"last"})
    announced = False

    def edit_last_file_after_the_first_copy(line: str) -> None:
        nonlocal announced
        if line.startswith("Copying:") and not announced:
            announced = True
            (folder / "z.txt").write_bytes(b"last, then edited while staging")

    first = stage(folder, vault, progress=Progress(edit_last_file_after_the_first_copy, interval=0))

    # The file is left out with a code, not staged torn, and the import stays open for a rerun.
    assert (first["ok"], first["code"], first["failed_while_copying"]) == (False, "COPY_INCOMPLETE", 1)
    assert first["manifest"].endswith(".partial.jsonl")
    records = _manifest(first)
    assert {record["path"]: record["code"] for record in records if record["kind"] == "left_out"} == {"z.txt": "SOURCE_CHANGED"}
    assert all(record["kind"] != "done" for record in records)
    assert _tree(vault / first["start_path"] / "Field Notes") == {"a.txt": b"first"}
    first_inode = (vault / first["start_path"] / "Field Notes" / "a.txt").stat().st_ino
    lines: list[str] = []

    second = stage(folder, vault, progress=Progress(lines.append, interval=0))

    assert second["ok"] and second["import_id"] == first["import_id"]
    assert not second["manifest"].endswith(".partial.jsonl") and _manifest(second)[-1]["kind"] == "done"
    assert _tree(vault / second["start_path"] / "Field Notes") == {"a.txt": b"first", "z.txt": b"last, then edited while staging"}
    assert (vault / second["start_path"] / "Field Notes" / "a.txt").stat().st_ino == first_inode
    assert any("1 file is already staged" in line for line in lines)


def test_the_command_exits_with_an_error_and_names_the_rerun_when_a_file_could_not_be_copied(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    folder = _folder(tmp_path, {"a.txt": b"first", "z.txt": b"last"})
    monkeypatch.delenv("EXOMEM_VAULT_PATH", raising=False)
    scan = local_import.import_selection.select

    def scan_then_edit(*arguments, **options):
        selection = scan(*arguments, **options)
        (folder / "z.txt").write_bytes(b"edited after the scan")
        return selection

    monkeypatch.setattr(local_import.import_selection, "select", scan_then_edit)
    status = cli._dispatch_main(["import", str(folder), "--vault", str(vault)])

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert (status, result["ok"], result["code"]) == (1, False, "COPY_INCOMPLETE")
    assert "Run the same command again" in captured.err and result["import_id"] in captured.err
    assert "Next: run adoption_studio start" not in captured.err

    monkeypatch.setattr(local_import.import_selection, "select", scan)
    assert cli._dispatch_main(["import", str(folder), "--vault", str(vault)]) == 0
    assert json.loads(capsys.readouterr().out)["import_id"] == result["import_id"]


@pytest.mark.skipif(os.name == "nt", reason="a backslash separates folders on Windows, so no such file name exists")
def test_a_file_the_vault_refuses_is_left_out_with_its_code_and_the_import_still_finishes(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    folder = _folder(tmp_path, {"good.md": b"kept", "a\\b.md": b"a name the vault never accepts"})
    monkeypatch.delenv("EXOMEM_VAULT_PATH", raising=False)

    status = cli._dispatch_main(["import", str(folder), "--vault", str(vault)])

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    # A rerun would be refused the same way, so asking for one would never end.
    assert (status, result["ok"], result["failed_while_copying"], result["refused_by_vault"]) == (0, True, 0, 1)
    assert result["manifest"].endswith(".jsonl") and not result["manifest"].endswith(".partial.jsonl")
    records = [json.loads(line) for line in Path(result["manifest"]).read_text(encoding="utf-8").splitlines()]
    refused = [record for record in records if record["kind"] == "left_out" and record["code"] == "STAGING_REFUSED"]
    assert [(record["path"], record["final"]) for record in refused] == [("a\\b.md", True)]
    assert records[-1]["kind"] == "done"
    assert _tree(vault / result["start_path"] / "Field Notes") == {"good.md": b"kept"}
    assert "1 file left out because the vault refused it" in captured.err
    assert captured.err.strip().splitlines()[-1].startswith("Next: run adoption_studio start")


def test_resume_removes_staged_files_the_folder_no_longer_selects_and_records_each_removal(tmp_path):
    vault = _vault(tmp_path)
    files = {f"{name}.txt": name.encode() * 100 for name in ("a", "b", "c", "d")}
    folder = _folder(tmp_path, files)
    import_dir = _stage_until_interrupted(folder, vault, copies=2)
    assert sorted(path.name for path in (import_dir / "Field Notes").iterdir()) == ["a.txt", "b.txt"]
    (folder / "a.txt").unlink()

    result = stage(folder, vault)

    # Adoption would scan a.txt otherwise, though it is no longer in the folder.
    assert sorted(_tree(import_dir / "Field Notes")) == ["b.txt", "c.txt", "d.txt"]
    removed = [record for record in _manifest(result) if record["kind"] == "removed"]
    assert [(record["path"], record["code"]) for record in removed] == [("a.txt", "NOT_SELECTED")]
    assert result["staged"]["files"] == 3


def test_resume_copies_a_staged_file_again_when_it_changed_after_it_was_verified(tmp_path):
    vault = _vault(tmp_path)
    files = {f"{name}.txt": name.encode() * 100 for name in ("a", "b", "c", "d")}
    folder = _folder(tmp_path, files)
    import_dir = _stage_until_interrupted(folder, vault, copies=2)
    # Same size and same content length, so only the staged file's own change time tells it apart.
    (import_dir / "Field Notes" / "a.txt").write_bytes(b"z" * 100)

    result = stage(folder, vault)

    assert result["ok"]
    assert _tree(import_dir / "Field Notes") == files


def test_an_unfinished_import_made_with_other_options_is_named_and_left_alone(tmp_path):
    vault = _vault(tmp_path)
    folder = _folder(tmp_path, {f"{name}.txt": name.encode() * 100 for name in ("a", "b", "c")} | {".hidden.txt": b"h"})
    unfinished = _stage_until_interrupted(folder, vault, copies=1)
    lines: list[str] = []

    result = stage(folder, vault, include_hidden=True, progress=Progress(lines.append, interval=0))

    assert result["ok"] and result["import_id"] != unfinished.name
    note = next(line for line in lines if unfinished.name in line)
    assert str(unfinished / "Field Notes") in note and "unfinished" in note
    assert (unfinished / "Field Notes").is_dir()


def test_a_torn_last_journal_line_is_cut_off_so_the_resumed_manifest_reads_whole(tmp_path):
    vault = _vault(tmp_path)
    files = {f"{name}.txt": name.encode() * 100 for name in ("a", "b", "c")}
    folder = _folder(tmp_path, files)
    import_dir = _stage_until_interrupted(folder, vault, copies=2)
    (partial,) = (tmp_path / "user-state" / "local-imports").glob("*.partial.jsonl")
    with partial.open("ab") as journal:
        journal.write(b'{"kind": "file", "path": "c.t')

    result = stage(folder, vault)

    assert result["import_id"] == import_dir.name
    lines = Path(result["manifest"]).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in lines][-1] == "done"
    assert _tree(import_dir / "Field Notes") == files


def test_two_runs_cannot_write_one_import_at_the_same_time(tmp_path):
    from filelock import FileLock

    vault = _vault(tmp_path)
    folder = _folder(tmp_path, {f"{name}.txt": name.encode() * 100 for name in ("a", "b", "c")})
    import_dir = _stage_until_interrupted(folder, vault, copies=1)
    before = sorted(path.name for path in (import_dir / "Field Notes").iterdir())

    with FileLock(tmp_path / "user-state" / "local-imports" / f"{import_dir.name}.lock"):
        with pytest.raises(LocalImportError) as refused:
            stage(folder, vault)

    assert (refused.value.code, refused.value.import_id) == ("IMPORT_IN_PROGRESS", import_dir.name)
    assert sorted(path.name for path in (import_dir / "Field Notes").iterdir()) == before
    assert stage(folder, vault)["ok"]


@pytest.mark.parametrize("case", ["inside the vault", "contains the vault", "hidden name", "only hidden files"])
def test_folders_that_cannot_be_staged_are_refused_with_a_code_and_copy_nothing(tmp_path, case):
    vault = _vault(tmp_path)
    folder = _folder(tmp_path, {"a.txt": b"x"})
    if case == "inside the vault":
        folder = vault / "Old Notes"
        folder.mkdir()
        (folder / "a.txt").write_text("x")
        code = "FOLDER_INSIDE_VAULT"
    elif case == "contains the vault":
        folder = tmp_path
        code = "FOLDER_CONTAINS_VAULT"
    elif case == "hidden name":
        folder = folder.rename(folder.with_name(".Field Notes"))
        code = "FOLDER_NAME_UNUSABLE"
    else:
        (folder / "a.txt").rename(folder / ".a.txt")
        code = "NOTHING_TO_IMPORT"

    with pytest.raises(LocalImportError) as refused:
        stage(folder, vault)

    assert refused.value.code == code
    assert not (vault / "_Imports").exists()


def test_exomem_import_command_prints_the_result_and_the_start_path(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    folder = _folder(tmp_path, {"a.txt": b"kept"})
    monkeypatch.delenv("EXOMEM_VAULT_PATH", raising=False)

    status = cli._dispatch_main(["import", str(folder), "--vault", str(vault)])

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert status == 0
    assert result["start_path"] == f"_Imports/{result['import_id']}"
    assert result["staged"] == {"files": 1, "bytes": 4}
    # The last thing a person reads is the route, and the second copy is named with the way to discard it.
    assert captured.err.strip().splitlines()[-1].startswith("Next: run adoption_studio start")
    assert "No Exomem command discards it" in captured.err and str(vault / "_Imports" / result["import_id"]) in captured.err


@pytest.mark.parametrize("spelling", ["absolute", "windows drive", "windows share", "parent traversal"])
def test_start_refuses_a_path_outside_the_vault_and_names_the_route_to_import(tmp_path, spelling):
    vault = _vault(tmp_path)
    folder = _folder(tmp_path, {"a.txt": b"x"})
    outside = {
        "absolute": str(folder),
        # Joined, so no drive-absolute literal sits in the file (the privacy gate refuses one).
        "windows drive": "C:" + "\\Notes",
        "windows share": "\\\\server\\share\\Notes",
        "parent traversal": "../elsewhere/Field Notes",
    }[spelling]

    with pytest.raises(ValueError, match="IMPORT_PATH_OUTSIDE_VAULT") as refused:
        commands.op_adoption_studio(vault, action="start", path=outside, initialize_kb=True)

    assert "exomem import" in str(refused.value)


@pytest.mark.parametrize("spelling", ["server share", "absolute"])
def test_start_refuses_an_absolute_path_without_resolving_it_on_the_filesystem(tmp_path, monkeypatch, spelling):
    # Resolving a network-share spelling on a Windows host connects to the host the caller named.
    vault = _vault(tmp_path)
    outside = {"server share": "\\\\server\\share\\Notes", "absolute": str(tmp_path / "elsewhere")}[spelling]

    def forbidden(*args, **kwargs):
        raise AssertionError("the path was resolved on the filesystem")

    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(os.path, "realpath", forbidden)

    with pytest.raises(adoption_run.AdoptionRunError, match="IMPORT_PATH_OUTSIDE_VAULT"):
        adoption_run.start(vault, path=outside, initialize_kb=True, today=TODAY)


def test_start_gives_the_route_not_a_vault_path_for_a_windows_spelling_when_the_directory_is_the_vault(
        tmp_path, monkeypatch):
    # On a POSIX host a drive spelling is a file name, so a path resolved from the working directory
    # would land inside the vault and advise a name that is no folder there.
    vault = _vault(tmp_path)
    monkeypatch.chdir(vault)

    with pytest.raises(ValueError, match="IMPORT_PATH_OUTSIDE_VAULT") as refused:
        commands.op_adoption_studio(vault, action="start", path="C:" + "\\Notes", initialize_kb=True)

    assert "exomem import" in str(refused.value) and "use '" not in str(refused.value)


def test_start_given_the_absolute_path_of_a_vault_folder_names_the_vault_relative_path(tmp_path):
    vault = _vault(tmp_path)
    (vault / "Old Notes").mkdir()

    with pytest.raises(ValueError, match="IMPORT_PATH_OUTSIDE_VAULT") as refused:
        commands.op_adoption_studio(vault, action="start", path=str(vault / "Old Notes"), initialize_kb=True)

    assert "'Old Notes'" in str(refused.value)
