"""Native Windows held-handle fixtures; executed by the NTFS CI gate."""

from __future__ import annotations

import ctypes
import errno
import gc
import importlib
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from exomem import vault

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows NTFS")


def _held_fs():
    return importlib.import_module("exomem.held_fs")


def _short_path(path: Path) -> Path:
    get_short = ctypes.windll.kernel32.GetShortPathNameW
    result = ctypes.create_unicode_buffer(32768)
    assert get_short(str(path), result, len(result)) > 0
    assert result.value.casefold() != str(path).casefold()
    return Path(result.value)


def _alternate_volume_directory(tmp_path: Path) -> Path:
    candidates: list[Path] = []
    for variable in ("USERPROFILE", "RUNNER_TEMP", "TEMP"):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value))
    candidates.extend(Path(f"{letter}:\\") for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ")

    failures: list[str] = []
    for candidate in candidates:
        if candidate.drive.casefold() == tmp_path.drive.casefold():
            continue
        directory = candidate / f"exomem-held-fs-{uuid.uuid4().hex}"
        try:
            directory.mkdir(parents=False)
        except OSError as error:
            failures.append(f"{candidate.drive or candidate}: {error.winerror or error.errno}")
            continue
        return directory
    raise AssertionError(
        "native cross-volume coverage requires a second writable Windows volume; "
        + ", ".join(failures)
    )


def test_windows_probe_and_relative_leaf_round_trip(tmp_path: Path) -> None:
    held_fs = _held_fs()
    capability = held_fs.probe(tmp_path)
    assert capability.relative_operations is True
    assert capability.no_follow is True
    assert capability.stable_identity is True

    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("parent", create=True).require() as parent:
            with filesystem.file(
                parent, "one.txt", access="write", create=True, exclusive=True
            ).require() as one:
                assert filesystem.write(one, b"one").ok
            with filesystem.file(parent, "one.txt", access="mutate").require() as one:
                assert filesystem.read(one).require() == b"one"
                assert filesystem.rename(one, parent, "two.txt").ok
            with filesystem.file(parent, "two.txt", access="mutate").require() as two:
                assert filesystem.link(two, parent, "three.txt").ok
            with filesystem.file(parent, "three.txt", access="mutate").require() as three:
                assert filesystem.unlink(three).ok


def test_windows_backend_declares_native_relative_ffi_contract() -> None:
    backend = importlib.import_module("exomem._held_fs_windows")
    assert backend.NtCreateFile is not None
    assert backend.NtSetInformationFile is not None
    assert backend.FILE_OPEN_REPARSE_POINT
    assert backend.FILE_RENAME_INFORMATION == 10
    assert backend.FILE_RENAME_INFORMATION_EX == 65
    assert backend.FILE_RENAME_REPLACE_IF_EXISTS == 0x00000001
    assert backend.FILE_RENAME_POSIX_SEMANTICS == 0x00000002
    assert backend.FILE_DISPOSITION_INFORMATION == 4


def test_windows_native_open_uses_a_root_handle_one_component_and_reparse_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_fs = _held_fs()
    backend = importlib.import_module("exomem._held_fs_windows")
    original = backend.NtCreateFile
    calls: list[tuple[int, str, int]] = []

    def observed(*args):
        attributes = ctypes.cast(args[2], ctypes.POINTER(backend.OBJECT_ATTRIBUTES)).contents
        calls.append(
            (int(attributes.RootDirectory), attributes.ObjectName.contents.Buffer, int(args[8]))
        )
        return original(*args)

    monkeypatch.setattr(backend, "NtCreateFile", observed)
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("native", create=True).require() as parent:
            with filesystem.file(
                parent, "leaf.txt", access="write", create=True, exclusive=True
            ).require() as leaf:
                assert filesystem.write(leaf, b"native").ok

    leaf_calls = [call for call in calls if call[1] == "leaf.txt"]
    assert leaf_calls
    assert all(root and "/" not in name and "\\" not in name for root, name, _ in leaf_calls)
    assert all(options & backend.FILE_OPEN_REPARSE_POINT for _, _, options in leaf_calls)


def test_windows_rename_uses_native_relative_information_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_fs = _held_fs()
    backend = importlib.import_module("exomem._held_fs_windows")
    with held_fs.acquire(tmp_path).require() as filesystem:
        original = backend.NtSetInformationFile
        information_classes: list[int] = []

        def observed(*args):
            information_classes.append(int(args[4]))
            return original(*args)

        monkeypatch.setattr(backend, "NtSetInformationFile", observed)
        with filesystem.parent("native-rename", create=True).require() as parent:
            with filesystem.file(
                parent, "before.txt", access="write", create=True, exclusive=True
            ).require() as before:
                assert filesystem.write(before, b"rename").ok
            with filesystem.file(parent, "before.txt", access="mutate").require() as before:
                assert filesystem.rename(before, parent, "after.txt").ok

    assert backend.FILE_RENAME_INFORMATION in information_classes


def test_windows_replace_uses_posix_semantics_while_old_target_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_fs = _held_fs()
    backend = importlib.import_module("exomem._held_fs_windows")
    with held_fs.acquire(tmp_path).require() as filesystem:
        original = backend.NtSetInformationFile
        calls: list[tuple[int, int]] = []

        def observed(*args):
            information_class = int(args[4])
            information = ctypes.cast(
                args[2], ctypes.POINTER(backend.FILE_NAME_INFORMATION)
            ).contents
            calls.append((information_class, int(information.Options.Flags)))
            return original(*args)

        monkeypatch.setattr(backend, "NtSetInformationFile", observed)
        with filesystem.parent("native-replace", create=True).require() as parent:
            for name, data in (("current.txt", b"old"), ("staged.txt", b"new")):
                with filesystem.file(
                    parent, name, access="write", create=True, exclusive=True
                ).require() as file:
                    assert filesystem.write(file, data).ok

            with filesystem.file(parent, "current.txt").require() as old_target:
                with filesystem.file(parent, "staged.txt", access="mutate").require() as staged:
                    assert filesystem.rename(
                        staged,
                        parent,
                        "current.txt",
                        replace=True,
                    ).ok
                os.lseek(old_target.descriptor, 0, os.SEEK_SET)
                assert os.read(old_target.descriptor, 3) == b"old"

            with filesystem.file(parent, "current.txt").require() as current:
                assert filesystem.read(current).require() == b"new"

    assert (
        backend.FILE_RENAME_INFORMATION_EX,
        backend.FILE_RENAME_REPLACE_IF_EXISTS | backend.FILE_RENAME_POSIX_SEMANTICS,
    ) in calls


def test_windows_probe_exercises_posix_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = importlib.import_module("exomem._held_fs_windows")
    original = backend._set_name_information
    calls: list[tuple[int, bool]] = []

    def observed(
        descriptor,
        destination_handle,
        leaf,
        information_class,
        *,
        replace=False,
    ):
        calls.append((information_class, replace))
        return original(
            descriptor,
            destination_handle,
            leaf,
            information_class,
            replace=replace,
        )

    monkeypatch.setattr(backend, "_set_name_information", observed)

    capability = backend.probe(tmp_path)

    assert capability.relative_operations is True
    assert (backend.FILE_RENAME_INFORMATION_EX, True) in calls


def test_windows_reparse_points_and_short_aliases_refuse(tmp_path: Path) -> None:
    held_fs = _held_fs()
    root = tmp_path / "Held Filesystem Eight Dot Three"
    root.mkdir()
    target = root / "target"
    target.mkdir()
    junction = root / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], check=True)
    final = root / "final-link.txt"
    os.symlink(target / "outside.txt", final)
    short_root = _short_path(root)

    with held_fs.acquire(short_root).require() as filesystem:
        assert filesystem.parent("junction").error is not None
        with filesystem.parent(".").require() as parent:
            refused = filesystem.file(parent, "final-link.txt")
            assert refused.error is not None
            assert refused.error.code == "UNSAFE_PATH"


def test_windows_descendant_short_alias_retains_file_identity(tmp_path: Path) -> None:
    held_fs = _held_fs()
    root = tmp_path / "Held Filesystem Descendant Alias"
    root.mkdir()
    long_leaf = root / "A Descendant With A Long Name.txt"
    long_leaf.write_bytes(b"alias")
    short_leaf = _short_path(long_leaf).name
    assert short_leaf.casefold() != long_leaf.name.casefold()

    with held_fs.acquire(root).require() as filesystem:
        with filesystem.parent(".").require() as parent:
            with filesystem.file(parent, long_leaf.name).require() as long_file:
                with filesystem.file(parent, short_leaf).require() as short_file:
                    assert short_file.identity == long_file.identity


def test_windows_parent_swap_race_cannot_redirect_a_retained_parent(tmp_path: Path) -> None:
    held_fs = _held_fs()
    outside = tmp_path / "outside"
    outside.mkdir()
    go = threading.Event()
    finished = threading.Event()
    outcome: list[OSError | None] = []

    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("retained", create=True).require() as parent:
            with filesystem.file(
                parent, "note.txt", access="write", create=True, exclusive=True
            ).require() as note:
                assert filesystem.write(note, b"before").ok

            def swap() -> None:
                go.wait()
                try:
                    (tmp_path / "retained").rename(tmp_path / "moved")
                    os.symlink(outside, tmp_path / "retained", target_is_directory=True)
                    outcome.append(None)
                except OSError as error:  # Windows sharing may stop the swap.
                    outcome.append(error)
                finally:
                    finished.set()

            worker = threading.Thread(target=swap)
            worker.start()
            go.set()
            assert finished.wait(5)
            with filesystem.file(parent, "note.txt", access="write").require() as note:
                result = filesystem.write(note, b"after")
            worker.join()

    assert result.ok or (
        result.error is not None and result.error.code in {"IO_REFUSED", "UNSAFE_PATH"}
    )
    assert not (outside / "note.txt").exists()
    if outcome == [None] and result.ok:
        assert (tmp_path / "moved" / "note.txt").read_bytes() == b"after"
    assert outcome


def test_windows_probe_failure_disables_acquisition_without_a_path_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_fs = _held_fs()
    backend = importlib.import_module("exomem._held_fs_windows")
    monkeypatch.setattr(backend, "_probe", lambda _root: held_fs.Capabilities.disabled("fixture"))

    refused = held_fs.acquire(tmp_path)
    assert refused.error is not None
    assert refused.error.code == "CAPABILITY_UNAVAILABLE"


def test_windows_copy_failure_removes_verified_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_fs = _held_fs()
    backend = importlib.import_module("exomem._held_fs_windows")
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("copy", create=True).require() as parent:
            with filesystem.file(
                parent, "source.txt", access="write", create=True, exclusive=True
            ).require() as source:
                assert filesystem.write(source, b"source").ok

            original = backend._set_name_information

            def refuse_copy(descriptor, destination_handle, leaf, information_class):
                if leaf == "destination.txt":
                    raise OSError("forced rename refusal")
                return original(descriptor, destination_handle, leaf, information_class)

            monkeypatch.setattr(backend, "_set_name_information", refuse_copy)
            with filesystem.file(parent, "source.txt").require() as source:
                refused = filesystem.copy(source, parent, "destination.txt")
            assert refused.error is not None

    assert not (tmp_path / "copy" / "destination.txt").exists()
    assert list((tmp_path / "copy").glob(".exomem-held-*")) == []


def test_windows_copy_write_failure_removes_early_verified_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_fs = _held_fs()
    backend = importlib.import_module("exomem._held_fs_windows")
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("copy-write-failure", create=True).require() as parent:
            with filesystem.file(
                parent, "source.txt", access="write", create=True, exclusive=True
            ).require() as source:
                assert filesystem.write(source, b"source").ok

            def refuse_write(_descriptor, _data):
                raise OSError(errno.ENOSPC, "forced write refusal")

            monkeypatch.setattr(backend.os, "write", refuse_write)
            with filesystem.file(parent, "source.txt").require() as source:
                refused = filesystem.copy(source, parent, "destination.txt")
            assert refused.error is not None

    assert not (tmp_path / "copy-write-failure" / "destination.txt").exists()
    assert list((tmp_path / "copy-write-failure").glob(".exomem-held-*")) == []


def test_windows_copy_crosses_volumes_but_rename_refuses(tmp_path: Path) -> None:
    held_fs = _held_fs()
    alternate = _alternate_volume_directory(tmp_path)
    try:
        with held_fs.acquire(tmp_path).require() as source_filesystem:
            with source_filesystem.parent("cross-volume", create=True).require() as source_parent:
                with source_filesystem.file(
                    source_parent,
                    "source.txt",
                    access="write",
                    create=True,
                    exclusive=True,
                ).require() as source:
                    assert source_filesystem.write(source, b"source").ok

                with held_fs.acquire(alternate).require() as destination_filesystem:
                    with destination_filesystem.parent(
                        "destination", create=True
                    ).require() as destination_parent:
                        with source_filesystem.file(
                            source_parent, "source.txt", access="mutate"
                        ).require() as source:
                            copied = source_filesystem.copy(
                                source, destination_parent, "copied.txt"
                            )
                            assert copied.ok
                            moved = source_filesystem.rename(
                                source, destination_parent, "moved.txt"
                            )
                            assert moved.ok is False
                            assert moved.error is not None
                            assert moved.error.code == "CROSS_DEVICE"
                            assert source_filesystem.read(source).require() == b"source"
                        with destination_filesystem.file(
                            destination_parent, "copied.txt"
                        ).require() as copied_file:
                            assert destination_filesystem.read(copied_file).require() == b"source"
    finally:
        shutil.rmtree(alternate, ignore_errors=True)


def test_windows_recursive_enumeration_closes_every_child_handle(tmp_path: Path) -> None:
    get_handle_count = ctypes.windll.kernel32.GetProcessHandleCount
    get_handle_count.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    get_handle_count.restype = ctypes.c_int
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = ctypes.c_void_p

    tree = tmp_path / "tree"
    for index in range(12):
        child = tree / f"level-{index}" / "nested"
        child.mkdir(parents=True)
        (child / "leaf.txt").write_text("held", encoding="utf-8")

    def handle_count() -> int:
        value = ctypes.c_ulong()
        assert get_handle_count(get_current_process(), ctypes.byref(value))
        return int(value.value)

    held_fs = _held_fs()
    gc.collect()
    before = handle_count()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("tree").require() as parent:
            for _ in range(10):
                assert len(filesystem.enumerate(parent).require()) == 36
    gc.collect()
    assert handle_count() <= before + 2


def test_windows_live_wal_identity_publication_uses_compatible_share_modes(
    tmp_path: Path,
) -> None:
    held_fs = _held_fs()
    database = tmp_path / "state.sqlite"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('held')")
        connection.commit()
        connection.execute("BEGIN")
        connection.execute("SELECT * FROM records").fetchall()

        published: list[dict[str, object]] = []
        with held_fs.acquire(tmp_path).require() as filesystem:
            with filesystem.parent(".").require() as parent:
                result = held_fs.publish_sqlite_identities(
                    filesystem, parent, "state.sqlite", published.append
                )
        assert result.ok
        assert set(published[0]) == {"state.sqlite", "state.sqlite-wal", "state.sqlite-shm"}
    finally:
        connection.close()


def test_windows_guarded_reader_waits_for_transient_parent_mutation_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    knowledge_base = tmp_path / "Knowledge Base"
    knowledge_base.mkdir()
    record = knowledge_base / "record.md"
    record.write_bytes(b"record")
    opening_knowledge_base = threading.Event()
    real_open_directory = vault._open_directory_path

    def observe_directory_open(path: Path, **kwargs: object) -> int:
        if Path(path) == knowledge_base:
            opening_knowledge_base.set()
        return real_open_directory(path, **kwargs)

    monkeypatch.setattr(vault, "_open_directory_path", observe_directory_open)
    held_fs = _held_fs()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with filesystem.parent("Knowledge Base", access="mutate").require():
                future = executor.submit(
                    vault.read_bounded_guarded_bytes,
                    tmp_path,
                    "Knowledge Base/record.md",
                    limit=16,
                )
                assert opening_knowledge_base.wait(5)
                time.sleep(0.03)
            data, guard = future.result(timeout=5)

    assert data == b"record"
    guard.recheck(tmp_path)


def test_windows_native_information_buffers_use_abi_offsets() -> None:
    backend = importlib.import_module("exomem._held_fs_windows")
    assert backend.FILE_NAME_INFORMATION.FileName.offset > backend.ctypes.sizeof(
        backend.ctypes.c_void_p
    )
    assert backend.FILE_NAME_INFORMATION.FileNameLength.offset < (
        backend.FILE_NAME_INFORMATION.FileName.offset
    )
    assert backend.FILE_DISPOSITION_INFO.DeleteFile.size == 1
    assert len(backend._name_payload(False, 1, "x")) >= backend.ctypes.sizeof(
        backend.FILE_NAME_INFORMATION
    )
