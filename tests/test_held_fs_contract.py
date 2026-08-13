"""Public contract for the registry-independent held-filesystem primitive."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path


def _module():
    assert importlib.util.find_spec("exomem.held_fs") is not None, (
        "the held filesystem primitive must be available"
    )
    return importlib.import_module("exomem.held_fs")


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


def test_relative_leaf_operations_use_held_parents_only(tmp_path: Path) -> None:
    held_fs = _module()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("one/two", create=True).require() as source:
            assert filesystem.write(source, "source.txt", b"source").ok
            assert filesystem.read(source, "source.txt").require() == b"source"
            source_identity = filesystem.identity(source, "source.txt").require()
            assert source_identity.kind == "file"
            assert source_identity.link_count == 1

            with filesystem.parent("destination", create=True).require() as destination:
                assert filesystem.rename(source, "source.txt", destination, "moved.txt").ok
                assert filesystem.read(destination, "moved.txt").require() == b"source"
                assert filesystem.link(destination, "moved.txt", destination, "linked.txt").ok
                assert filesystem.identity(destination, "linked.txt").require() == filesystem.identity(
                    destination, "moved.txt"
                ).require()
                assert filesystem.unlink(destination, "linked.txt").ok


def test_copy_is_destination_atomic_and_recursive_enumeration_is_ordered(tmp_path: Path) -> None:
    held_fs = _module()
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("source", create=True).require() as source:
            assert filesystem.write(source, "payload.bin", b"payload").ok
            with filesystem.parent("destination/tree", create=True).require() as destination:
                assert filesystem.write(destination, "z.txt", b"z").ok
                assert filesystem.write(destination, "a.txt", b"a").ok
            with filesystem.parent("destination", create=False).require() as destination:
                assert filesystem.copy(source, "payload.bin", destination, "copied.bin").ok
                assert filesystem.read(destination, "copied.bin").require() == b"payload"
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


def test_symlink_parent_and_final_leaf_refuse_without_destination_publication(tmp_path: Path) -> None:
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
            leaf = filesystem.read(root, "final-link")
            assert leaf.ok is False
            assert leaf.error is not None
            assert leaf.error.code == "UNSAFE_PATH"


def test_sqlite_identity_publication_waits_for_the_complete_reachable_family(tmp_path: Path) -> None:
    held_fs = _module()
    published: list[dict[str, object]] = []
    with held_fs.acquire(tmp_path).require() as filesystem:
        with filesystem.parent("sqlite", create=True).require() as parent:
            for name in ("state.sqlite", "state.sqlite-wal", "state.sqlite-shm"):
                assert filesystem.write(parent, name, b"sqlite").ok
            result = held_fs.publish_sqlite_identities(
                filesystem, parent, "state.sqlite", published.append
            )

    assert result.ok
    assert list(published[0]) == ["state.sqlite", "state.sqlite-wal", "state.sqlite-shm"]
