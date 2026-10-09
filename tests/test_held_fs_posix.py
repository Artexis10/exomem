"""POSIX no-follow descriptor-relative held-filesystem coverage."""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
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
        shutil.rmtree(alternate, ignore_errors=True)


def test_linux_parent_opens_require_no_cross_mount_resolution() -> None:
    posix = importlib.import_module("exomem._held_fs_posix")
    if not sys.platform.startswith("linux"):
        pytest.skip("openat2 mount confinement is Linux-specific")

    assert posix._OPENAT2_RESOLVE & posix.RESOLVE_BENEATH
    assert posix._OPENAT2_RESOLVE & posix.RESOLVE_NO_SYMLINKS
    assert posix._OPENAT2_RESOLVE & posix.RESOLVE_NO_MAGICLINKS
    assert posix._OPENAT2_RESOLVE & posix.RESOLVE_NO_XDEV


def test_streaming_names_yield_before_reading_the_remaining_census(tmp_path, monkeypatch):
    # A name iterator implemented by materializing children defeats disk spooling.
    held_fs = _held_fs()
    posix = importlib.import_module("exomem._held_fs_posix")
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text(name)
    visited = []
    original = posix._scandir

    class Scan:
        def __init__(self, descriptor):
            self.scan = original(descriptor)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.scan.close()

        def __iter__(self):
            for entry in self.scan:
                visited.append(entry.name)
                yield entry

    with held_fs.acquire(tmp_path).require() as filesystem:
        monkeypatch.setattr(posix, "_scandir", Scan)
        with filesystem.parent(".").require() as parent:
            names = filesystem.iter_names(parent)
            try:
                assert next(names) in {"a", "b", "c"}
                assert len(visited) == 1
            finally:
                names.close()


@pytest.mark.parametrize("kind", ["file", "directory", "copy"])
def test_no_replace_publication_preserves_a_destination_despite_stale_absence(
    tmp_path, monkeypatch, kind
):
    # An absence check can be stale by the time rename reaches the kernel.
    # Existing no-clobber tests do not exercise that competing-file window.
    held_fs = _held_fs()
    posix = importlib.import_module("exomem._held_fs_posix")
    source_path = tmp_path / "source"
    destination_path = tmp_path / "destination"
    if kind != "directory":
        source_path.write_bytes(b"source")
        destination_path.write_bytes(b"foreign")
    else:
        source_path.mkdir()
        (source_path / "payload").write_bytes(b"source")
        destination_path.mkdir()
    destination_identity = destination_path.stat().st_ino
    original_stat = posix._stat

    def stale_absence(name, *args, **kwargs):
        if name == "destination":
            raise FileNotFoundError(name)
        return original_stat(name, *args, **kwargs)

    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent(".").require() as parent:
            monkeypatch.setattr(posix, "_stat", stale_absence)
            if kind != "directory":
                with filesystem.file(parent, "source", access="mutate").require() as source:
                    if kind == "copy":
                        result = filesystem.copy(source, parent, "destination")
                    else:
                        result = filesystem.rename(source, parent, "destination", replace=False)
            else:
                with filesystem.parent("source", access="mutate").require() as source:
                    result = filesystem.rename_directory(source, parent, "destination")

    assert result.error is not None
    assert result.error.code == "DESTINATION_EXISTS"
    assert destination_path.stat().st_ino == destination_identity
    assert (source_path / "payload" if kind == "directory" else source_path).read_bytes() == b"source"
    if kind != "directory":
        assert destination_path.read_bytes() == b"foreign"
