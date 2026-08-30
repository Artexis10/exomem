"""Public contract for the registry-independent held-filesystem primitive."""

from __future__ import annotations

import hashlib
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


def test_windows_extended_rename_payload_binds_posix_replacement_flags() -> None:
    backend = importlib.import_module("exomem._held_fs_windows")
    payload = backend._name_payload(
        True,
        1,
        "x",
        information_class=backend.FILE_RENAME_INFORMATION_EX,
    )
    information = backend.FILE_NAME_INFORMATION.from_buffer(payload)

    assert backend.FILE_RENAME_INFORMATION_EX == 65
    assert information.Options.Flags == (
        backend.FILE_RENAME_REPLACE_IF_EXISTS | backend.FILE_RENAME_POSIX_SEMANTICS
    )


def test_windows_root_elevated_parent_access_is_explicitly_refused() -> None:
    backend = importlib.import_module("exomem._held_fs_windows")
    filesystem = object.__new__(backend.WindowsHeldFilesystem)
    filesystem.descriptor = 7
    filesystem.closed = False

    for access in ("flush", "mutate"):
        result = filesystem.parent(".", access=access)
        assert result.ok is False
        assert result.error is not None
        assert result.error.code == "INVALID_INPUT"


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
def test_acquire_reuses_one_capability_probe_for_the_same_stable_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held_fs = _module()
    backend = held_fs._backend()
    original_probe = backend.probe
    calls = 0

    def counted_probe(root: Path):
        nonlocal calls
        calls += 1
        return original_probe(root)

    held_fs.reset_capability_cache_for_tests()
    monkeypatch.setattr(backend, "probe", counted_probe)
    try:
        first = held_fs.acquire(tmp_path)
        second = held_fs.acquire(tmp_path)
        assert first.ok and second.ok
        first.require().close()
        second.require().close()
        assert calls == 1
    finally:
        held_fs.reset_capability_cache_for_tests()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fd accounting")
def test_repeated_enumeration_releases_its_scan_descriptors(tmp_path: Path) -> None:
    held_fs = _module()
    (tmp_path / "directory").mkdir()
    (tmp_path / "directory" / "entry.txt").write_text("entry", encoding="utf-8")
    baseline = len(os.listdir("/proc/self/fd"))

    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent(".").require() as root:
            for _ in range(32):
                assert filesystem.children(root).ok
                assert filesystem.enumerate(root).ok

    assert len(os.listdir("/proc/self/fd")) == baseline


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
                assert filesystem.sha256(source_file).require() == hashlib.sha256(
                    b"source"
                ).hexdigest()

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
def test_directory_flush_requires_flush_capable_access(tmp_path: Path) -> None:
    held_fs = _module()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("flush-parent", create=True).require() as parent:
            refused = filesystem.flush_directory(parent)
            assert refused.error is not None
            assert refused.error.code == "INVALID_ACCESS"
        with filesystem.parent("flush-parent", access="flush").require() as parent:
            assert filesystem.flush_directory(parent).ok


@_requires_native_route
def test_relative_replace_and_directory_rename_consume_retained_handles(
    tmp_path: Path,
) -> None:
    held_fs = _module()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("source-tree", create=True).require() as source_tree:
            with filesystem.file(
                source_tree, "payload.txt", access="write", create=True, exclusive=True
            ).require() as payload:
                assert filesystem.write(payload, b"payload").ok
        with filesystem.parent("destination", create=True).require() as destination:
            with filesystem.file(
                destination, "current.txt", access="write", create=True, exclusive=True
            ).require() as current:
                assert filesystem.write(current, b"current").ok
            with filesystem.file(
                destination, "replacement.txt", access="write", create=True, exclusive=True
            ).require() as replacement:
                assert filesystem.write(replacement, b"replacement").ok
            with filesystem.file(
                destination, "replacement.txt", access="mutate"
            ).require() as replacement:
                assert filesystem.rename(
                    replacement, destination, "current.txt", replace=True
                ).ok
            with filesystem.file(destination, "current.txt").require() as current:
                assert filesystem.read(current).require() == b"replacement"

            with filesystem.parent("source-tree", access="mutate").require() as source_tree:
                assert filesystem.rename_directory(
                    source_tree, destination, "moved-tree"
                ).ok
            moved = filesystem.parent("destination/moved-tree")
            assert moved.ok
            with moved.require() as moved_tree:
                with filesystem.file(moved_tree, "payload.txt").require() as payload:
                    assert filesystem.read(payload).require() == b"payload"


@_requires_native_route
def test_empty_directory_unlink_uses_the_retained_directory_handle(tmp_path: Path) -> None:
    held_fs = _module()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("empty", create=True, access="mutate").require() as empty:
            assert filesystem.unlink_directory(empty).ok

    assert not (tmp_path / "empty").exists()


@_requires_native_route
def test_exclusive_directory_creation_refuses_an_existing_final_name(
    tmp_path: Path,
) -> None:
    held_fs = _module()
    (tmp_path / "existing").mkdir()

    with held_fs.acquire(tmp_path).require() as filesystem:
        created = filesystem.parent("fresh", create=True, exclusive=True)
        assert created.ok
        with created.require() as fresh:
            assert fresh.identity.kind == "directory"

        duplicate = filesystem.parent("existing", create=True, exclusive=True)
        assert not duplicate.ok
        assert duplicate.error is not None
        assert duplicate.error.code == "DESTINATION_EXISTS"

        invalid = filesystem.parent("missing/child", create=True, exclusive=True)
        assert not invalid.ok
        assert invalid.error is not None
        assert invalid.error.code == "MISSING"

    assert not (tmp_path / "missing").exists()


@_requires_native_route
def test_retained_directory_name_survives_its_own_child_creation(
    tmp_path: Path,
) -> None:
    held_fs = _module()

    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("container", create=True).require() as container:
            with filesystem.parent(
                "container/child",
                create=True,
                exclusive=True,
            ).require():
                pass

            assert filesystem.validate_directory(container).ok


@_requires_native_route
def test_publish_bytes_is_no_replace_or_expected_identity_replace(tmp_path: Path) -> None:
    held_fs = _module()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("publish", create=True).require() as parent:
            created = held_fs.publish_bytes(filesystem, parent, "item.txt", b"one")
            assert created.ok
            duplicate = held_fs.publish_bytes(filesystem, parent, "item.txt", b"two")
            assert not duplicate.ok
            assert duplicate.error is not None
            assert duplicate.error.code == "DESTINATION_EXISTS"

            with filesystem.file(parent, "item.txt").require() as current:
                expected = current.identity
            stale = held_fs.publish_bytes(
                filesystem,
                parent,
                "item.txt",
                b"two",
                expected_identity=expected,
                expected_sha256="0" * 64,
            )
            assert not stale.ok
            assert stale.error is not None
            assert stale.error.code == "IDENTITY_CHANGED"
            replaced = held_fs.publish_bytes(
                filesystem,
                parent,
                "item.txt",
                b"two",
                expected_identity=expected,
                expected_sha256=hashlib.sha256(b"one").hexdigest(),
            )
            assert replaced.ok
            with filesystem.file(parent, "item.txt").require() as current:
                assert filesystem.read(current).require() == b"two"


@_requires_native_route
def test_create_publish_recovers_when_temporary_unlink_refuses_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held_fs = _module()
    acquired = held_fs.acquire(tmp_path)
    assert acquired.ok
    filesystem = acquired.require()
    filesystem_type = type(filesystem)
    real_unlink = filesystem_type.unlink
    refused = False

    def refuse_first_temporary_unlink(self, file):  # noqa: ANN001
        nonlocal refused
        if not refused and file.name.startswith(held_fs.PUBLISH_TEMP_PREFIX):
            refused = True
            return held_fs.HeldResult(
                error=held_fs.HeldFsError("IO_REFUSED", "injected refusal")
            )
        return real_unlink(self, file)

    monkeypatch.setattr(
        filesystem_type,
        "unlink",
        refuse_first_temporary_unlink,
    )
    with filesystem:
        with filesystem.parent("publish", create=True).require() as parent:
            created = held_fs.publish_bytes(filesystem, parent, "item.txt", b"one")
            assert created.ok
            with filesystem.file(parent, "item.txt").require() as current:
                assert current.identity.link_count == 1
                assert filesystem.read(current).require() == b"one"

    assert refused
    assert not list((tmp_path / "publish").glob(f"{held_fs.PUBLISH_TEMP_PREFIX}*"))


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
def test_immediate_children_are_shallow_stable_and_omit_aliases(tmp_path: Path) -> None:
    held_fs = _module()
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "nested.txt").write_text("nested", encoding="utf-8")
    (tmp_path / "top.txt").write_text("top", encoding="utf-8")
    alias = tmp_path / "directory-alias"
    try:
        alias.symlink_to(directory, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")

    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent(".").require() as root:
            records = filesystem.children(root).require()

    assert [record.relative_path for record in records] == ["directory", "top.txt"]
    assert [record.identity.kind for record in records] == ["directory", "file"]


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO contract")
def test_held_file_refuses_fifo_without_waiting_for_a_peer(tmp_path: Path) -> None:
    held_fs = _module()
    private = tmp_path / "private"
    private.mkdir()
    os.mkfifo(private / "control.json")

    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("private").require() as directory:
            result = filesystem.file(directory, "control.json")

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "UNSAFE_PATH"


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

            removed_by_handle = False
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
                    removed_by_handle = True
                else:
                    assert refused.error is not None
                    assert refused.error.code == "IDENTITY_CHANGED"

            if removed_by_handle:
                assert not (tmp_path / "retained" / "moved-reviewed.txt").exists()
            else:
                assert (tmp_path / "retained" / "moved-reviewed.txt").read_bytes() == b"reviewed"
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
