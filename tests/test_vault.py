from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import vault


def test_wikilink_resolver_from_entries_matches_disk_resolution(tmp_path: Path) -> None:
    entries = (
        ("Knowledge Base/Notes/one.md", "Display One"),
        ("Knowledge Base/Elsewhere/shared.md", "Shared A"),
        ("Knowledge Base/Notes/shared.md", "Shared B"),
    )
    for rel_path, title in entries:
        path = tmp_path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: {title}\n---\n", encoding="utf-8")

    disk = vault.WikilinkResolver(tmp_path)
    snapshot = vault.WikilinkResolver.from_entries(tmp_path, entries)
    targets = (
        "Knowledge Base/Notes/one",
        "Notes/one",
        "one",
        "display one",
        "shared",
        "missing",
    )

    assert [
        vault.normalize_wikilink(target, tmp_path, resolver=snapshot) for target in targets
    ] == [vault.normalize_wikilink(target, tmp_path, resolver=disk) for target in targets]

    snapshot.add_pending("Knowledge Base/Notes/pending", title="Pending title")
    assert vault.normalize_wikilink("pending", tmp_path, resolver=snapshot) == (
        "Knowledge Base/Notes/pending",
        None,
    )
    assert vault.normalize_wikilink("PENDING TITLE", tmp_path, resolver=snapshot) == (
        "Knowledge Base/Notes/pending",
        None,
    )


def test_wikilink_resolver_from_entries_performs_no_io(tmp_path: Path, monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("from_entries must not walk or read the vault")

    monkeypatch.setattr(vault, "walk_vault_md", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)

    resolver = vault.WikilinkResolver.from_entries(
        tmp_path, (("Knowledge Base/Notes/one.md", "One"),)
    )

    assert resolver.full_paths == {"Knowledge Base/Notes/one"}


def test_strict_frontmatter_rejects_duplicate_keys_without_weakening_legacy_parse() -> None:
    source = "---\nexomem_id: first\nexomem_id: second\n---\n\nBody.\n"

    assert vault.parse_frontmatter(source)[0]["exomem_id"] == "second"
    with pytest.raises(vault.FrontmatterError) as exc:
        vault.parse_frontmatter(source, strict=True)

    assert exc.value.code == "DUPLICATE_FRONTMATTER_KEY"
    assert "first" not in exc.value.reason
    assert "second" not in exc.value.reason


def test_create_only_write_refuses_existing_leaf_and_preserves_legacy_overwrite(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.md"
    target.write_text("old", encoding="utf-8")

    with pytest.raises(vault.CreateOnlyConflict) as exc:
        vault.batch_atomic_write(
            [vault.PlannedWrite(target, "new", create_only=True)],
            vault_root=tmp_path,
        )

    assert exc.value.code == "CREATE_ONLY_CONFLICT"
    assert exc.value.target == "target.md"
    assert target.read_text(encoding="utf-8") == "old"
    vault.batch_atomic_write([vault.PlannedWrite(target, "legacy")], vault_root=tmp_path)
    assert target.read_text(encoding="utf-8") == "legacy"


def test_path_guards_allow_fresh_multiwrite_without_self_invalidation(
    tmp_path: Path,
) -> None:
    paths = [tmp_path / "artifact.json", tmp_path / "aux.md", tmp_path / "page.md"]
    writes = [
        vault.PlannedWrite(
            path,
            f"content-{index}",
            create_only=True,
            guard=vault.PathGuard.capture(
                tmp_path, path.relative_to(tmp_path).as_posix(), leaf_policy="absent"
            ),
        )
        for index, path in enumerate(paths)
    ]

    replaced = vault.batch_atomic_write(writes, vault_root=tmp_path)

    assert replaced == paths
    assert [path.read_text(encoding="utf-8") for path in paths] == [
        "content-0",
        "content-1",
        "content-2",
    ]


def test_path_guards_share_new_parent_chain_safely(tmp_path: Path) -> None:
    paths = [tmp_path / "new/nested/one.md", tmp_path / "new/nested/two.md"]
    writes = [
        vault.PlannedWrite(
            path,
            path.stem,
            create_only=True,
            guard=vault.PathGuard.capture(
                tmp_path,
                path.relative_to(tmp_path).as_posix(),
                leaf_policy="absent",
            ),
        )
        for path in paths
    ]

    assert vault.batch_atomic_write(writes, vault_root=tmp_path) == paths
    assert [path.read_text(encoding="utf-8") for path in paths] == ["one", "two"]


@pytest.mark.skipif(os.name != "nt", reason="Windows binary-read regression")
def test_read_guarded_text_preserves_crlf_bytes_on_windows(tmp_path: Path) -> None:
    target = tmp_path / "guarded.md"
    target.write_bytes(b"first\r\nsecond\r\n")

    text, guard = vault.read_guarded_text(tmp_path, target)

    assert text == "first\r\nsecond\r\n"
    guard.recheck(tmp_path)


def test_missing_parent_swap_cannot_redirect_nested_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "safe/nested/page.md"
    outside = tmp_path / "outside"
    outside.mkdir()
    guard = vault.PathGuard.capture(tmp_path, "safe/nested/page.md", leaf_policy="absent")
    real_mkdir = os.mkdir
    swapped = False

    def swap_created_parent(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and Path(path).name == "nested":
            swapped = True
            (tmp_path / "safe").rename(tmp_path / "safe-displaced")
            (tmp_path / "safe").symlink_to(outside, target_is_directory=True)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(vault.os, "mkdir", swap_created_parent)

    with pytest.raises(vault.PathGuardError):
        vault.batch_atomic_write(
            [
                vault.PlannedWrite(
                    target,
                    "page",
                    create_only=True,
                    guard=guard,
                )
            ],
            vault_root=tmp_path,
        )

    assert not (outside / "nested").exists()


def test_path_guard_rejects_pending_parent_swap_after_prior_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_dir = tmp_path / "first"
    pending_dir = tmp_path / "pending"
    first_dir.mkdir()
    pending_dir.mkdir()
    first = first_dir / "one.md"
    pending = pending_dir / "two.md"
    writes = [
        vault.PlannedWrite(
            first,
            "one",
            create_only=True,
            guard=vault.PathGuard.capture(tmp_path, "first/one.md", leaf_policy="absent"),
        ),
        vault.PlannedWrite(
            pending,
            "two",
            create_only=True,
            guard=vault.PathGuard.capture(tmp_path, "pending/two.md", leaf_policy="absent"),
        ),
    ]
    real_replace = os.replace
    swapped = False

    def swap_after_first(src, dst):
        nonlocal swapped
        result = real_replace(src, dst)
        if not swapped and Path(dst) == first:
            swapped = True
            pending_dir.rename(tmp_path / "pending-old")
            pending_dir.mkdir()
        return result

    monkeypatch.setattr(vault.os, "replace", swap_after_first)

    with pytest.raises(vault.BatchWriteError) as incomplete:
        vault.batch_atomic_write(writes, vault_root=tmp_path)

    assert incomplete.value.code == "BATCH_CLEANUP_INCOMPLETE"
    assert incomplete.value.committed is False
    assert incomplete.value.as_public_dict()["outcome"] == {
        "kind": "cleanup_incomplete",
        "committed": False,
        "incomplete": True,
        "affected_count": 2,
        "targets": ["first/one.md", "pending/two.md"],
        "omitted_target_count": 0,
    }
    assert isinstance(incomplete.value.__cause__, vault.PathGuardError)
    assert incomplete.value.__cause__.code == "PATH_GUARD_CHANGED"
    assert not first.exists()
    assert not pending.exists()
    assert list(first_dir.glob(".exomem-batch-*")) == []
    retained = list((tmp_path / "pending-old").glob(".exomem-batch-*"))
    assert len(retained) == 1
    assert [path.name for path in retained[0].iterdir()] == ["stage-1.tmp"]
    assert (retained[0] / "stage-1.tmp").read_text(encoding="utf-8") == "two"


def test_vault_creation_lock_rejects_nested_namespaces_and_hashes_filename(
    tmp_path: Path,
) -> None:
    with vault.vault_creation_lock(tmp_path, "activation-manifest") as first_path:
        assert str(tmp_path) not in first_path.name
        assert first_path.name.endswith(".lock")
        with pytest.raises(vault.VaultLockError) as exc:
            with vault.vault_creation_lock(tmp_path, "semantic-creation"):
                pass

    assert exc.value.code == "VAULT_LOCK_NESTED"


def test_vault_creation_lock_timeout_covers_thread_wait(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with vault.vault_creation_lock(tmp_path, "semantic-creation"):
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(vault.VaultLockTimeout):
            with vault.vault_creation_lock(tmp_path, "semantic-creation", timeout=0.01):
                pass
    finally:
        release.set()
        thread.join(5)

    assert not thread.is_alive()


def test_guarded_reader_dispatches_to_the_windows_descriptor_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[Path, tuple[str, ...], str, int]] = []
    expected = vault.PathGuard("entry.md", (), (), None, "content", "a" * 64)

    def windows(root: Path, parts: tuple[str, ...], target: str, limit: int):
        captured.append((root, parts, target, limit))
        return b"", expected

    monkeypatch.setattr(vault.os, "name", "nt")
    monkeypatch.setattr(vault, "_read_bounded_windows_snapshot", windows)

    data, guard = vault._read_bounded_guarded_snapshot(tmp_path, "entry.md", 64)

    assert data == b""
    assert guard is expected
    assert len(captured) == 1
    assert captured[0][0].as_posix() == tmp_path.as_posix()
    assert captured[0][1:] == (("entry.md",), "entry.md", 64)


def test_windows_guarded_reader_bounds_reads_and_rechecks_ancestor_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    leaf_path = guarded / "entry.md"
    leaf_path.write_bytes(b"safe")
    real_read = os.read
    real_lstat = Path.lstat
    reads: list[int] = []
    swapped = False

    def open_leaf(path: Path, **_kwargs: object) -> int:
        return os.open(path, os.O_RDONLY)

    def read_and_swap(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        reads.append(size)
        data = real_read(descriptor, size)
        if not swapped:
            swapped = True
        return data

    def lstat_after_read(path: Path):
        info = real_lstat(path)
        if swapped and path == guarded:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_dev=info.st_dev,
                st_ino=info.st_ino + 1,
                st_file_attributes=getattr(info, "st_file_attributes", 0),
            )
        return info

    if os.name != "nt":
        monkeypatch.setattr(vault, "_open_windows_path_descriptor", open_leaf)
    monkeypatch.setattr(vault.os, "read", read_and_swap)
    monkeypatch.setattr(Path, "lstat", lstat_after_read)

    with pytest.raises(vault.PathGuardError) as excinfo:
        vault._read_bounded_windows_snapshot(
            tmp_path, ("guarded", "entry.md"), "guarded/entry.md", 4
        )

    assert excinfo.value.code == "PATH_GUARD_CHANGED"
    assert max(reads) == 5
    assert all(size <= 5 for size in reads)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows descriptors")
def test_guarded_reader_uses_windows_descriptor_branch_for_regular_files(tmp_path: Path) -> None:
    target = tmp_path / "entry.md"
    target.write_bytes(b"safe")

    data, guard = vault.read_bounded_guarded_bytes(tmp_path, "entry.md", limit=16)

    assert data == b"safe"
    guard.recheck(tmp_path)
