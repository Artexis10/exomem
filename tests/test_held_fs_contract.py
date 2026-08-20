"""Public contract for the registry-independent held-filesystem primitive."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_requires_native_route = pytest.mark.skipif(
    os.name != "nt" and not sys.platform.startswith("linux"),
    reason="mount-aware held operations require Linux openat2 or native Windows handles",
)


def _module():
    assert importlib.util.find_spec("exomem.held_fs") is not None, (
        "the held filesystem primitive must be available"
    )
    return importlib.import_module("exomem.held_fs")


def test_windows_ffi_structures_construct_on_every_supported_interpreter() -> None:
    backend = importlib.import_module("exomem._held_fs_windows")
    string, buffer = backend._unicode("leaf.txt")
    attributes = backend.OBJECT_ATTRIBUTES(
        backend.ctypes.sizeof(backend.OBJECT_ATTRIBUTES),
        backend.ctypes.c_void_p(1),
        backend.ctypes.pointer(string),
        backend.OBJ_CASE_INSENSITIVE,
        None,
        None,
    )

    assert buffer.value == "leaf.txt"
    assert attributes.ObjectName.contents.Buffer == "leaf.txt"
    assert len(backend._name_payload(False, 1, "x")) >= backend.ctypes.sizeof(
        backend.FILE_NAME_INFORMATION
    )


@_requires_native_route
def test_public_contract_uses_closed_results_and_content_free_refusals(tmp_path: Path) -> None:
    held_fs = _module()
    capability = held_fs.probe(tmp_path)

    assert isinstance(capability, held_fs.Capabilities)
    assert capability.relative_operations is True
    assert capability.no_follow is True
    assert capability.stable_identity is True

    acquired = held_fs.acquire(tmp_path)
    assert isinstance(acquired, held_fs.HeldResult)
    assert acquired.ok is True
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with acquired.require() as filesystem:
        unsafe = filesystem.parent("link/out")
        assert unsafe.ok is False
        assert unsafe.error is not None
        assert unsafe.error.code == "UNSAFE_PATH"
        assert "link/out" not in unsafe.error.detail


@_requires_native_route
def test_relative_leaf_operations_use_held_parents_only(tmp_path: Path) -> None:
    held_fs = _module()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("one/two", create=True).require() as source:
            with filesystem.file(
                source, "source.txt", access="write", create=True, exclusive=True
            ).require() as source_file:
                assert filesystem.write(source_file, b"source").ok
                assert source_file.identity.kind == "file"
                assert source_file.identity.link_count == 1

            with filesystem.file(source, "source.txt", access="mutate").require() as source_file:
                assert filesystem.read(source_file).require() == b"source"

                with filesystem.parent("destination", create=True).require() as destination:
                    assert filesystem.rename(source_file, destination, "moved.txt").ok
                    with filesystem.file(
                        destination, "moved.txt", access="mutate"
                    ).require() as moved:
                        assert filesystem.read(moved).require() == b"source"
                        assert filesystem.link(moved, destination, "linked.txt").ok
                        with filesystem.file(destination, "linked.txt").require() as linked:
                            assert linked.identity == moved.identity
                        with filesystem.file(
                            destination, "linked.txt", access="mutate"
                        ).require() as linked:
                            assert filesystem.unlink(linked).ok


@_requires_native_route
def test_copy_is_destination_atomic_and_recursive_enumeration_is_ordered(tmp_path: Path) -> None:
    held_fs = _module()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("source", create=True).require() as source:
            with filesystem.file(
                source, "payload.bin", access="write", create=True, exclusive=True
            ).require() as payload:
                assert filesystem.write(payload, b"payload").ok
            with filesystem.parent("destination/tree", create=True).require() as destination:
                for name, data in (("z.txt", b"z"), ("a.txt", b"a")):
                    with filesystem.file(
                        destination, name, access="write", create=True, exclusive=True
                    ).require() as leaf:
                        assert filesystem.write(leaf, data).ok
            with filesystem.parent("destination", create=False).require() as destination:
                with filesystem.file(source, "payload.bin").require() as payload:
                    assert filesystem.copy(payload, destination, "copied.bin").ok
                with filesystem.file(destination, "copied.bin").require() as copied:
                    assert filesystem.read(copied).require() == b"payload"
                records = filesystem.enumerate(destination).require()

    assert [record.relative_path for record in records] == sorted(
        record.relative_path for record in records
    )
    assert [record.relative_path for record in records] == [
        "copied.bin",
        "tree",
        "tree/a.txt",
        "tree/z.txt",
    ]


@_requires_native_route
def test_symlink_parent_and_final_leaf_refuse_without_destination_publication(
    tmp_path: Path,
) -> None:
    held_fs = _module()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    (tmp_path / "final-link").symlink_to(outside / "secret.txt")

    with held_fs.acquire(tmp_path).require() as filesystem:
        parent = filesystem.parent("link")
        assert parent.ok is False
        assert parent.error is not None
        assert parent.error.code == "UNSAFE_PATH"
        with filesystem.parent(".").require() as root:
            leaf = filesystem.file(root, "final-link")
            assert leaf.ok is False
            assert leaf.error is not None
            assert leaf.error.code == "UNSAFE_PATH"


@_requires_native_route
def test_held_leaf_survives_name_exchange_and_name_mutation_refuses(tmp_path: Path) -> None:
    held_fs = _module()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("retained", create=True).require() as parent:
            for name, data in (("reviewed.txt", b"reviewed"), ("reserved.txt", b"reserved")):
                with filesystem.file(
                    parent, name, access="write", create=True, exclusive=True
                ).require() as leaf:
                    assert filesystem.write(leaf, data).ok

            with filesystem.file(parent, "reviewed.txt", access="mutate").require() as reviewed:
                (tmp_path / "retained" / "reviewed.txt").rename(
                    tmp_path / "retained" / "moved-reviewed.txt"
                )
                (tmp_path / "retained" / "reserved.txt").rename(
                    tmp_path / "retained" / "reviewed.txt"
                )

                assert filesystem.read(reviewed).require() == b"reviewed"
                refused = filesystem.unlink(reviewed)
                if refused.ok:
                    assert not (tmp_path / "retained" / "moved-reviewed.txt").exists()
                else:
                    assert refused.error is not None
                    assert refused.error.code == "IDENTITY_CHANGED"

            assert (tmp_path / "retained" / "reviewed.txt").read_bytes() == b"reserved"


@_requires_native_route
def test_sqlite_identity_publication_waits_for_the_complete_reachable_family(
    tmp_path: Path,
) -> None:
    held_fs = _module()
    published: list[dict[str, object]] = []
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("sqlite", create=True).require() as parent:
            for name in ("state.sqlite", "state.sqlite-wal", "state.sqlite-shm"):
                with filesystem.file(
                    parent, name, access="write", create=True, exclusive=True
                ).require() as leaf:
                    assert filesystem.write(leaf, b"sqlite").ok
            result = held_fs.publish_sqlite_identities(
                filesystem, parent, "state.sqlite", published.append
            )

    assert result.ok
    assert list(published[0]) == ["state.sqlite", "state.sqlite-wal", "state.sqlite-shm"]


@pytest.mark.skipif(
    os.name == "nt" or sys.platform.startswith("linux"),
    reason="unsupported POSIX route only",
)
def test_posix_without_mount_aware_resolution_fails_closed(tmp_path: Path) -> None:
    held_fs = _module()

    capability = held_fs.probe(tmp_path)
    assert capability.relative_operations is False
    assert capability.no_follow is False
    refused = held_fs.acquire(tmp_path)
    assert refused.ok is False
    assert refused.error is not None
    assert refused.error.code == "CAPABILITY_UNAVAILABLE"
