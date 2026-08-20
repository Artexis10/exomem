"""POSIX no-follow descriptor-relative held-filesystem coverage."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux openat2 mount confinement",
)


def _held_fs():
    assert importlib.util.find_spec("exomem.held_fs") is not None, (
        "the held filesystem primitive must be available"
    )
    return importlib.import_module("exomem.held_fs")


def test_posix_backend_never_authorizes_descendants_by_pathname_reopen(tmp_path: Path) -> None:
    held_fs = _held_fs()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("retained", create=True).require() as parent:
            with filesystem.file(
                parent, "note.txt", access="write", create=True, exclusive=True
            ).require() as note:
                assert filesystem.write(note, b"before").ok
            moved = tmp_path / "moved"
            (tmp_path / "retained").rename(moved)
            (tmp_path / "retained").symlink_to(tmp_path / "outside", target_is_directory=True)

            with filesystem.file(parent, "note.txt", access="write").require() as note:
                assert filesystem.write(note, b"after").ok
            assert (moved / "note.txt").read_bytes() == b"after"
            assert not (tmp_path / "outside" / "note.txt").exists()


def test_posix_capability_failure_disables_operations_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_fs = _held_fs()
    posix = importlib.import_module("exomem._held_fs_posix")
    monkeypatch.setattr(
        posix, "_probe", lambda _root: held_fs.Capabilities.disabled("probe failed")
    )

    capability = held_fs.probe(tmp_path)
    assert capability.relative_operations is False
    refused = held_fs.acquire(tmp_path)
    assert refused.ok is False
    assert refused.error is not None
    assert refused.error.code == "CAPABILITY_UNAVAILABLE"


def test_posix_source_and_destination_must_be_on_the_same_filesystem(tmp_path: Path) -> None:
    held_fs = _held_fs()
    alternate = Path("/dev/shm") / f"exomem-held-fs-{os.getpid()}"
    try:
        alternate.mkdir()
    except OSError:
        pytest.skip("an alternate writable filesystem is unavailable")
    try:
        if tmp_path.stat().st_dev == alternate.stat().st_dev:
            pytest.skip("the available temporary roots share one filesystem")
        with held_fs.acquire(tmp_path).require() as source_filesystem:
            with source_filesystem.parent("source", create=True).require() as source:
                with source_filesystem.file(
                    source, "source.txt", access="write", create=True, exclusive=True
                ).require() as created:
                    assert source_filesystem.write(created, b"source").ok
                with held_fs.acquire(alternate).require() as destination_filesystem:
                    with destination_filesystem.parent(
                        "destination", create=True
                    ).require() as destination:
                        with source_filesystem.file(
                            source, "source.txt", access="mutate"
                        ).require() as source_file:
                            copied = source_filesystem.copy(source_file, destination, "copied.txt")
                            assert copied.ok
                            moved = source_filesystem.rename(source_file, destination, "moved.txt")
                            assert moved.ok is False
                            assert moved.error is not None
                            assert moved.error.code == "CROSS_DEVICE"
                            assert source_filesystem.read(source_file).require() == b"source"
                        with destination_filesystem.file(
                            destination, "copied.txt"
                        ).require() as copied_file:
                            assert destination_filesystem.read(copied_file).require() == b"source"
    finally:
        try:
            alternate.rmdir()
        except OSError:
            pass


def test_linux_parent_opens_require_no_cross_mount_resolution() -> None:
    posix = importlib.import_module("exomem._held_fs_posix")
    if not sys.platform.startswith("linux"):
        pytest.skip("openat2 mount confinement is Linux-specific")

    assert posix._OPENAT2_RESOLVE & posix.RESOLVE_BENEATH
    assert posix._OPENAT2_RESOLVE & posix.RESOLVE_NO_SYMLINKS
    assert posix._OPENAT2_RESOLVE & posix.RESOLVE_NO_MAGICLINKS
    assert posix._OPENAT2_RESOLVE & posix.RESOLVE_NO_XDEV
