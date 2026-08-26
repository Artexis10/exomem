from __future__ import annotations

import hashlib
import io
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import held_fs, vault


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


def test_batch_write_streams_binary_and_markdown_in_one_rollback_set(
    tmp_path: Path,
) -> None:
    payload = b"\x00binary payload\xff"
    binary = tmp_path / "Evidence" / "artifact.bin"
    companion = tmp_path / "Evidence" / "artifact.bin.md"

    written = vault.batch_atomic_write(
        [
            vault.PlannedWrite(
                binary,
                vault.PreparedBinaryContent(
                    io.BytesIO(payload),
                    len(payload),
                    hashlib.sha256(payload).hexdigest(),
                ),
                create_only=True,
                expected_hash=vault.MISSING_CONTENT_HASH,
            ),
            vault.PlannedWrite(
                companion,
                "---\ntitle: Artifact\n---\n\nBound companion.\n",
                create_only=True,
                expected_hash=vault.MISSING_CONTENT_HASH,
            ),
        ],
        vault_root=tmp_path,
    )

    assert written == [binary, companion]
    assert binary.read_bytes() == payload
    assert companion.read_text(encoding="utf-8").endswith("Bound companion.\n")


def test_batch_binary_create_only_refuses_existing_non_utf8_leaf(
    tmp_path: Path,
) -> None:
    target = tmp_path / "Evidence" / "artifact.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xffexisting")
    payload = b"replacement"

    with pytest.raises(vault.CreateOnlyConflict) as conflict:
        vault.batch_atomic_write(
            [
                vault.PlannedWrite(
                    target,
                    vault.PreparedBinaryContent(
                        io.BytesIO(payload),
                        len(payload),
                        hashlib.sha256(payload).hexdigest(),
                    ),
                    create_only=True,
                    expected_hash=vault.MISSING_CONTENT_HASH,
                )
            ],
            vault_root=tmp_path,
        )

    assert conflict.value.code == "CREATE_ONLY_CONFLICT"
    assert target.read_bytes() == b"\xffexisting"


def test_batch_write_rejects_changed_binary_stream_before_publication(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "Evidence" / "artifact.bin"
    companion = tmp_path / "Evidence" / "artifact.bin.md"
    reviewed = b"reviewed"

    with pytest.raises(vault.PathGuardError) as changed:
        vault.batch_atomic_write(
            [
                vault.PlannedWrite(
                    binary,
                    vault.PreparedBinaryContent(
                        io.BytesIO(b"changed!"),
                        len(reviewed),
                        hashlib.sha256(reviewed).hexdigest(),
                    ),
                    create_only=True,
                    expected_hash=vault.MISSING_CONTENT_HASH,
                ),
                vault.PlannedWrite(
                    companion,
                    "companion",
                    create_only=True,
                    expected_hash=vault.MISSING_CONTENT_HASH,
                ),
            ],
            vault_root=tmp_path,
        )

    assert changed.value.code == "PATH_GUARD_CONTENT"
    assert not binary.exists()
    assert not companion.exists()


def test_batch_write_rolls_back_binary_when_later_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"rollback payload"
    binary = tmp_path / "Evidence" / "artifact.bin"
    companion = tmp_path / "Evidence" / "artifact.bin.md"
    real_hook = vault._after_batch_destination_published

    def fail_after_companion(path: Path) -> None:
        real_hook(path)
        if path == companion:
            raise RuntimeError("injected publication failure")

    monkeypatch.setattr(vault, "_after_batch_destination_published", fail_after_companion)

    with pytest.raises(RuntimeError, match="injected publication failure"):
        vault.batch_atomic_write(
            [
                vault.PlannedWrite(
                    binary,
                    vault.PreparedBinaryContent(
                        io.BytesIO(payload),
                        len(payload),
                        hashlib.sha256(payload).hexdigest(),
                    ),
                    create_only=True,
                    expected_hash=vault.MISSING_CONTENT_HASH,
                ),
                vault.PlannedWrite(
                    companion,
                    "companion",
                    create_only=True,
                    expected_hash=vault.MISSING_CONTENT_HASH,
                ),
            ],
            vault_root=tmp_path,
        )

    assert not binary.exists()
    assert not companion.exists()


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


def test_preparing_a_bounded_content_guard_preserves_its_read_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "guarded.md"
    target.write_bytes(b"bounded bytes")
    _data, guard = vault.read_bounded_guarded_bytes(tmp_path, "guarded.md", limit=64)

    def unbounded_hash(*_args, **_kwargs):
        raise AssertionError("bounded guard must not use _leaf_hash")

    monkeypatch.setattr(vault, "_leaf_hash", unbounded_hash)
    prepared = vault._prepare_path_guards(tmp_path, (guard,))

    assert prepared[0].expected_content_size == len(b"bounded bytes")
    target.write_bytes(b"bounded bytes grew")
    with pytest.raises(vault.PathGuardError, match="PATH_GUARD"):
        prepared[0].recheck(tmp_path)


def test_guarded_reader_retries_a_transient_windows_sharing_refusal_from_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "guarded.md"
    target.write_bytes(b"bounded bytes")
    expected = vault._read_bounded_guarded_snapshot(tmp_path, "guarded.md", 64)
    attempts = 0

    def transient_snapshot(
        vault_root: Path, relative: str, limit: int
    ) -> tuple[bytes, vault.PathGuard]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            sharing = PermissionError("transient Windows sharing refusal")
            sharing.winerror = 32
            raise vault.PathGuardError(
                "PATH_GUARD_IO", "guarded content could not be opened"
            ) from sharing
        assert vault_root == tmp_path
        assert relative == "guarded.md"
        assert limit == 64
        return expected

    monkeypatch.setattr(vault, "_uses_windows_guarded_reader", lambda: True)
    monkeypatch.setattr(vault, "_read_bounded_guarded_snapshot", transient_snapshot)
    monkeypatch.setattr(vault.time, "sleep", lambda _seconds: None)

    data, guard = vault._read_bounded_guarded_snapshot_tolerating_transient_sharing(
        tmp_path, "guarded.md", 64
    )

    assert data == b"bounded bytes"
    assert guard is expected[1]
    assert attempts == 2


def test_batch_write_refuses_portably_colliding_destinations(tmp_path: Path) -> None:
    with pytest.raises(vault.PathGuardError) as raised:
        vault.batch_atomic_write(
            [
                vault.PlannedWrite(tmp_path / "Record.md", "first"),
                vault.PlannedWrite(tmp_path / "record.md", "second"),
            ],
            vault_root=tmp_path,
        )

    assert raised.value.code == "PATH_GUARD_TARGET"
    assert not (tmp_path / "Record.md").exists()
    assert not (tmp_path / "record.md").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows binary-read regression")
def test_read_guarded_text_preserves_crlf_bytes_on_windows(tmp_path: Path) -> None:
    target = tmp_path / "guarded.md"
    target.write_bytes(b"first\r\nsecond\r\n")

    text, guard = vault.read_guarded_text(tmp_path, target)

    assert text == "first\r\nsecond\r\n"
    guard.recheck(tmp_path)


def test_read_guarded_text_refuses_multiply_linked_private_alias(tmp_path: Path) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    private = governance / "policy.md"
    private.write_text("private policy", encoding="utf-8")
    alias = notes / "ordinary.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(vault.PathGuardError) as error:
        vault.read_guarded_text(tmp_path, alias)

    assert error.value.code == "PATH_GUARD_UNSAFE"
    assert "private policy" not in error.value.reason


def test_read_guarded_text_preserves_missing_file_signal(tmp_path: Path) -> None:
    missing = tmp_path / "Knowledge Base" / "_Schema" / "project-keys.yaml"

    with pytest.raises(FileNotFoundError):
        vault.read_guarded_text(tmp_path, missing)


def test_missing_parent_swap_cannot_redirect_nested_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "safe/nested/page.md"
    outside = tmp_path / "outside"
    outside.mkdir()
    guard = vault.PathGuard.capture(tmp_path, "safe/nested/page.md", leaf_policy="absent")
    real_parent_hook = vault._after_batch_parent_created
    swapped = False

    def swap_created_parent(root: Path, relative: str) -> None:
        nonlocal swapped
        real_parent_hook(root, relative)
        if not swapped and relative == "safe":
            swapped = True
            (tmp_path / "safe").rename(tmp_path / "safe-displaced")
            (tmp_path / "safe").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(vault, "_after_batch_parent_created", swap_created_parent)

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


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows refuses to rename a directory holding an open stage file",
)
def test_path_guard_rejects_pending_parent_swap_after_prior_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent of a pending write is swapped between two flips.

    Not stageable on Windows, and not because of anything this test
    does wrong: the batch still holds `stage-1.tmp` open inside
    `pending/` while the first artifact flips, and Windows refuses to
    rename a directory that contains an open file. The setup's own
    `pending_dir.rename(...)` raises `PermissionError [WinError 5]`
    from inside the patched held publication, where the writer correctly
    classifies it as a transient sharing refusal and retries -- so the
    test failed on its own injection rather than on the guard.

    The swap this defends against therefore cannot occur there while a
    batch is in flight: the platform refuses it before the guard is
    reached. POSIX permits the rename, which is why the guard has to
    exist, and why this stays asserted there.
    """
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
    real_publish = held_fs.publish_bytes
    swapped = False

    def swap_after_first(filesystem, parent, leaf, data, **kwargs):  # noqa: ANN001
        nonlocal swapped
        result = real_publish(filesystem, parent, leaf, data, **kwargs)
        if not swapped and leaf == first.name:
            swapped = True
            pending_dir.rename(tmp_path / "pending-old")
            pending_dir.mkdir()
        return result

    monkeypatch.setattr(held_fs, "publish_bytes", swap_after_first)

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

    monkeypatch.setattr(vault, "_uses_windows_guarded_reader", lambda: True)
    monkeypatch.setattr(vault, "_read_bounded_windows_snapshot", windows)

    data, guard = vault._read_bounded_guarded_snapshot(tmp_path, "entry.md", 64)

    assert data == b""
    assert guard is expected
    assert len(captured) == 1
    assert captured[0][0].as_posix() == tmp_path.as_posix()
    assert captured[0][1:] == (("entry.md",), "entry.md", 64)


def test_windows_guarded_directory_share_allows_cooperating_child_writes() -> None:
    share = vault._WINDOWS_GUARDED_DIRECTORY_SHARE

    assert share & 0x1  # FILE_SHARE_READ
    assert share & 0x2  # FILE_SHARE_WRITE
    assert not share & 0x4  # FILE_SHARE_DELETE keeps the directory name pinned


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
    directory_open_modes: list[tuple[int, int | None]] = []
    swapped = False

    def open_leaf(path: Path, **_kwargs: object) -> int:
        return os.open(path, os.O_RDONLY)

    def open_pinned_directory(
        path: Path,
        *,
        desired_access: int = 0,
        share_mode: int | None = None,
    ) -> int:
        directory_open_modes.append((desired_access, share_mode))
        return os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))

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
        monkeypatch.setattr(vault, "_open_directory_path", open_pinned_directory)
    monkeypatch.setattr(vault.os, "read", read_and_swap)
    monkeypatch.setattr(Path, "lstat", lstat_after_read)

    with pytest.raises(vault.PathGuardError) as excinfo:
        vault._read_bounded_windows_snapshot(
            tmp_path, ("guarded", "entry.md"), "guarded/entry.md", 4
        )

    assert excinfo.value.code == "PATH_GUARD_CHANGED"
    assert max(reads) == 5
    assert all(size <= 5 for size in reads)
    if os.name != "nt":
        assert directory_open_modes == [
            (
                vault._WINDOWS_FILE_LIST_DIRECTORY,
                vault._WINDOWS_GUARDED_DIRECTORY_SHARE,
            )
        ] * 2


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows descriptors")
def test_guarded_reader_uses_windows_descriptor_branch_for_regular_files(tmp_path: Path) -> None:
    target = tmp_path / "entry.md"
    target.write_bytes(b"safe")

    data, guard = vault.read_bounded_guarded_bytes(tmp_path, "entry.md", limit=16)

    assert data == b"safe"
    guard.recheck(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows share semantics")
def test_windows_guarded_reader_pins_parent_through_leaf_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    leaf = guarded / "entry.md"
    leaf.write_bytes(b"safe")
    retired = tmp_path / "guarded-retired"
    real_open = vault._open_windows_path_descriptor
    real_read = os.read
    swap_attempted = False
    swap_blocked = False
    forbidden_bytes_read = False

    def try_parent_swap(path: Path, **kwargs: object) -> int:
        nonlocal swap_attempted, swap_blocked
        if Path(path) == leaf and kwargs.get("desired_access") == 0x80000000:
            swap_attempted = True
            try:
                guarded.rename(retired)
            except OSError:
                swap_blocked = True
            else:
                guarded.mkdir()
                (guarded / "entry.md").write_bytes(b"SECRET")
        return real_open(path, **kwargs)

    def observe_read(descriptor: int, size: int) -> bytes:
        nonlocal forbidden_bytes_read
        data = real_read(descriptor, size)
        forbidden_bytes_read |= b"SECRET" in data
        return data

    monkeypatch.setattr(vault, "_open_windows_path_descriptor", try_parent_swap)
    monkeypatch.setattr(vault.os, "read", observe_read)

    data, guard = vault.read_bounded_guarded_bytes(
        tmp_path,
        "guarded/entry.md",
        limit=16,
    )

    assert data == b"safe"
    assert swap_attempted is True
    assert swap_blocked is True
    assert forbidden_bytes_read is False
    guard.recheck(tmp_path)
