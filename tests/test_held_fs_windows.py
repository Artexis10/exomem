"""Native Windows held-handle fixtures; executed by the NTFS CI gate."""

from __future__ import annotations

import ctypes
import importlib
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows NTFS")


def _held_fs():
    return importlib.import_module("exomem.held_fs")


def _short_path(path: Path) -> Path:
    get_short = ctypes.windll.kernel32.GetShortPathNameW
    result = ctypes.create_unicode_buffer(32768)
    assert get_short(str(path), result, len(result)) > 0
    assert result.value.casefold() != str(path).casefold()
    return Path(result.value)


def test_windows_probe_and_relative_leaf_round_trip(tmp_path: Path) -> None:
    held_fs = _held_fs()
    capability = held_fs.probe(tmp_path)
    assert capability.relative_operations is True
    assert capability.no_follow is True
    assert capability.stable_identity is True

    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("parent", create=True).require() as parent:
            assert filesystem.write(parent, "one.txt", b"one").ok
            assert filesystem.read(parent, "one.txt").require() == b"one"
            assert filesystem.rename(parent, "one.txt", parent, "two.txt").ok
            assert filesystem.link(parent, "two.txt", parent, "three.txt").ok
            assert filesystem.unlink(parent, "three.txt").ok


def test_windows_backend_declares_native_relative_ffi_contract() -> None:
    backend = importlib.import_module("exomem._held_fs_windows")
    assert backend.NtCreateFile is not None
    assert backend.NtSetInformationFile is not None
    assert backend.SetFileInformationByHandle is not None
    assert backend.FILE_OPEN_REPARSE_POINT


def test_windows_native_open_uses_a_root_handle_one_component_and_reparse_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_fs = _held_fs()
    backend = importlib.import_module("exomem._held_fs_windows")
    original = backend.NtCreateFile
    calls: list[tuple[int, str, int]] = []

    def observed(*args):
        attributes = ctypes.cast(
            args[2], ctypes.POINTER(backend.OBJECT_ATTRIBUTES)
        ).contents
        calls.append(
            (int(attributes.RootDirectory), attributes.ObjectName.contents.Buffer, int(args[8]))
        )
        return original(*args)

    monkeypatch.setattr(backend, "NtCreateFile", observed)
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("native", create=True).require() as parent:
            assert filesystem.write(parent, "leaf.txt", b"native").ok

    leaf_calls = [call for call in calls if call[1] == "leaf.txt"]
    assert leaf_calls
    assert all(root and "/" not in name and "\\" not in name for root, name, _ in leaf_calls)
    assert all(options & backend.FILE_OPEN_REPARSE_POINT for _, _, options in leaf_calls)


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
            refused = filesystem.read(parent, "final-link.txt")
            assert refused.error is not None
            assert refused.error.code == "UNSAFE_PATH"


def test_windows_parent_swap_race_cannot_redirect_a_retained_parent(tmp_path: Path) -> None:
    held_fs = _held_fs()
    outside = tmp_path / "outside"
    outside.mkdir()
    go = threading.Event()
    finished = threading.Event()
    outcome: list[OSError | None] = []

    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("retained", create=True).require() as parent:
            assert filesystem.write(parent, "note.txt", b"before").ok

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
            result = filesystem.write(parent, "note.txt", b"after")
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
