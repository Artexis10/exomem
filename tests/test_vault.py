from __future__ import annotations

import hashlib
import io
import os
import stat
import threading
import time
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


@pytest.mark.skipif(os.name == "nt", reason="setgid is a POSIX-only directory semantic")
def test_batch_atomic_write_succeeds_under_a_setgid_volume_root(tmp_path: Path) -> None:
    """A Kubernetes fsGroup volume leaves its root setgid (`2770`, D2). A
    freshly created batch workspace directory then inherits the setgid bit
    from its parent even though it was created with mode `0o700` -- the
    kernel ORs the parent's setgid bit into the child regardless of the
    requested mode. `_BatchWorkspace.create`/`_create_held` used to capture
    the workspace's identity (including the setgid-inflated mode) before
    clearing it with `fchmod`, so the later `refresh_identity()` recheck saw
    its own correction as drift and raised `PathGuardError("PATH_GUARD_CHANGED",
    "batch workspace changed")` on every single write under a setgid root.
    """
    root = tmp_path / "setgid-root"
    root.mkdir()
    os.chmod(root, 0o2770)  # force the setgid bit; mkdir's mode arg can be masked
    assert os.stat(root).st_mode & stat.S_ISGID

    target = root / "target.md"
    vault.batch_atomic_write(
        [vault.PlannedWrite(target, "content under a setgid root")],
        vault_root=root,
    )

    assert target.read_text(encoding="utf-8") == "content under a setgid root"
    # The workspace directory is gone once the write commits; the property
    # under test is that no PathGuardError interrupted it along the way.


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


def test_batch_write_streams_binary_larger_than_copy_chunk(tmp_path: Path) -> None:
    payload = b"a" * (1024 * 1024) + b"b" * 17
    binary = tmp_path / "Evidence" / "large.bin"

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
            )
        ],
        vault_root=tmp_path,
    )

    assert binary.read_bytes() == payload


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


def test_batch_write_refuses_binary_stage_rewritten_between_last_check_and_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"reviewed payload"
    rewritten = b"tampered payload"
    assert len(rewritten) == len(payload)
    binary = tmp_path / "Evidence" / "artifact.bin"
    companion = tmp_path / "Evidence" / "artifact.bin.md"
    real_hook = vault._before_batch_destination_published
    rewrites = 0

    def rewrite_stage_in_place(path: Path, *, restoring: bool) -> None:
        nonlocal rewrites
        real_hook(path, restoring=restoring)
        if path == binary and not restoring and rewrites == 0:
            rewrites += 1
            (stage,) = binary.parent.glob(".exomem-batch-*/stage-1.tmp")
            with stage.open("r+b") as handle:
                handle.write(rewritten)

    monkeypatch.setattr(vault, "_before_batch_destination_published", rewrite_stage_in_place)

    with pytest.raises(vault.BatchWriteError) as changed:
        vault.batch_atomic_write(
            [
                vault.PlannedWrite(
                    companion,
                    "companion",
                    create_only=True,
                    expected_hash=vault.MISSING_CONTENT_HASH,
                ),
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
            ],
            vault_root=tmp_path,
        )

    # The companion is rolled back and the rewritten bytes never reach the destination. Cleanup
    # keeps the changed stage in its workspace rather than delete bytes it cannot account for.
    assert rewrites == 1
    assert isinstance(changed.value.__cause__, vault.PathGuardError)
    assert changed.value.__cause__.code == "PATH_GUARD_CONTENT"
    assert changed.value.code == "BATCH_CLEANUP_INCOMPLETE"
    assert not binary.exists()
    assert not companion.exists()
    assert not list(tmp_path.rglob(f"{held_fs.PUBLISH_TEMP_PREFIX}*"))


@pytest.mark.parametrize("generation_trusted", [True, False])
def test_batch_write_detects_installed_binary_rewritten_before_a_later_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generation_trusted: bool,
) -> None:
    monkeypatch.setattr(vault, "STAT_GENERATION_TRUSTED", generation_trusted)
    payload = b"reviewed payload"
    rewritten = b"tampered payload"
    assert len(rewritten) == len(payload)
    binary = tmp_path / "Evidence" / "artifact.bin"
    first_note = tmp_path / "Evidence" / "first.md"
    last_note = tmp_path / "Evidence" / "last.md"
    real_hook = vault._before_batch_destination_published

    def rewrite_installed_binary(path: Path, *, restoring: bool) -> None:
        real_hook(path, restoring=restoring)
        if path == first_note and not restoring:
            # Outside the filesystem's timestamp tick, which the requirement excludes.
            time.sleep(0.05)
            with binary.open("r+b") as handle:
                handle.write(rewritten)

    monkeypatch.setattr(vault, "_before_batch_destination_published", rewrite_installed_binary)

    with pytest.raises(vault.BatchWriteError) as failed:
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
                    first_note,
                    "first",
                    create_only=True,
                    expected_hash=vault.MISSING_CONTENT_HASH,
                ),
                vault.PlannedWrite(
                    last_note,
                    "last",
                    create_only=True,
                    expected_hash=vault.MISSING_CONTENT_HASH,
                ),
            ],
            vault_root=tmp_path,
        )

    # The rewrite is caught before the last note is published, and rollback removes the
    # notes but refuses to delete bytes that are no longer the ones the batch installed.
    assert isinstance(failed.value.__cause__, vault.PathGuardError)
    assert failed.value.__cause__.code == "PATH_GUARD_CONTENT"
    assert failed.value.code == "BATCH_ROLLBACK_INCOMPLETE"
    assert not first_note.exists()
    assert not last_note.exists()
    assert binary.read_bytes() == rewritten


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


def test_batch_completion_guard_rolls_back_when_source_changes_after_last_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Evidence" / "parent.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"reviewed parent")
    _data, completion_guard = vault.read_bounded_guarded_bytes(
        tmp_path,
        "Evidence/parent.bin",
        limit=len(b"reviewed parent"),
    )
    stable_guard = vault.PathGuard.capture(
        tmp_path,
        "Evidence/parent.bin",
        leaf_policy="stable",
    )
    binary = tmp_path / "Evidence" / "frame.jpg"
    companion = tmp_path / "Evidence" / "frame.jpg.md"
    real_hook = vault._after_batch_destination_published

    def change_source_after_companion(path: Path) -> None:
        real_hook(path)
        if path == companion:
            source.write_bytes(b"changed parent!")

    monkeypatch.setattr(
        vault,
        "_after_batch_destination_published",
        change_source_after_companion,
    )

    with pytest.raises(vault.PathGuardError) as changed:
        vault.batch_atomic_write(
            [
                vault.PlannedWrite(
                    binary,
                    vault.PreparedBinaryContent(
                        io.BytesIO(b"frame"),
                        len(b"frame"),
                        hashlib.sha256(b"frame").hexdigest(),
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
            required_guards=(stable_guard,),
            completion_guards=(completion_guard,),
        )

    assert changed.value.code == "PATH_GUARD_CONTENT"
    assert not binary.exists()
    assert not companion.exists()


def test_batch_completion_guard_hash_work_is_independent_of_write_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Evidence" / "parent.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"reviewed parent")
    _data, completion_guard = vault.read_bounded_guarded_bytes(
        tmp_path,
        "Evidence/parent.bin",
        limit=len(b"reviewed parent"),
    )
    real_snapshot = vault._read_bounded_guarded_snapshot
    reads = 0

    def count_snapshot(*args, **kwargs):
        nonlocal reads
        target = args[1] if len(args) > 1 else kwargs.get("target")
        if target == "Evidence/parent.bin":
            reads += 1
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(vault, "_read_bounded_guarded_snapshot", count_snapshot)

    vault.batch_atomic_write(
        [
            vault.PlannedWrite(
                tmp_path / "Evidence" / f"frame-{index}.md",
                f"frame {index}",
                create_only=True,
                expected_hash=vault.MISSING_CONTENT_HASH,
            )
            for index in range(12)
        ],
        vault_root=tmp_path,
        completion_guards=(completion_guard,),
    )

    assert reads == 2


@pytest.mark.parametrize("revoke_during_publication", [False, True])
def test_batch_binding_refusal_preserves_existing_bytes_and_removes_new_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revoke_during_publication: bool,
) -> None:
    """Input permission can change without changing any guarded source bytes."""
    existing = tmp_path / "existing.md"
    created = tmp_path / "new.md"
    existing.write_text("before", encoding="utf-8")
    allowed = revoke_during_publication
    original_hook = vault._after_batch_destination_published

    def revoke_after_first_publication(path: Path) -> None:
        nonlocal allowed
        original_hook(path)
        if not revoke_during_publication:
            raise AssertionError("an unavailable binding reached publication")
        if path == existing:
            allowed = False

    def validate_bindings() -> None:
        if not allowed:
            raise PermissionError("retained input unavailable")

    monkeypatch.setattr(
        vault, "_after_batch_destination_published", revoke_after_first_publication
    )
    with pytest.raises(PermissionError, match="retained input unavailable"):
        vault.batch_atomic_write(
            [
                vault.PlannedWrite(existing, "after"),
                vault.PlannedWrite(created, "new effect", create_only=True),
            ],
            vault_root=tmp_path,
            post_commit_fanout=False,
            _validate_prepared_bindings=validate_bindings,
        )

    assert existing.read_text(encoding="utf-8") == "before"
    assert not created.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["existing.md"]


def test_batch_binding_validation_accepts_only_complete_pre_and_post_images(tmp_path: Path) -> None:
    """A Source backref must not be judged against a destination still in flight."""
    source = tmp_path / "source.md"
    output = tmp_path / "output.md"
    source.write_text("original", encoding="utf-8")
    validated_images: set[str] = set()

    def validate_bindings() -> None:
        if source.read_text(encoding="utf-8") == "original":
            assert not output.exists()
            validated_images.add("before")
        else:
            assert source.read_text(encoding="utf-8") == "original with output backref"
            assert output.read_text(encoding="utf-8") == "compiled output"
            validated_images.add("after")

    result = vault.batch_atomic_write(
        [
            vault.PlannedWrite(source, "original with output backref"),
            vault.PlannedWrite(output, "compiled output", create_only=True),
        ],
        vault_root=tmp_path,
        post_commit_fanout=False,
        _validate_prepared_bindings=validate_bindings,
    )

    assert result == [source, output]
    assert source.read_text(encoding="utf-8") == "original with output backref"
    assert output.read_text(encoding="utf-8") == "compiled output"
    assert validated_images == {"before", "after"}


@pytest.mark.parametrize("edit_at_completion", [False, True])
def test_batch_rechecks_destination_bytes_after_a_binding_validation_wait(
    tmp_path: Path, edit_at_completion: bool
) -> None:
    """An editor's change during a slow input check must not be overwritten."""
    existing = tmp_path / "existing.md"
    existing.write_text("before", encoding="utf-8")
    guard = vault.PathGuard.capture(
        tmp_path,
        "existing.md",
        leaf_policy="content",
        expected_content_hash=hashlib.sha256(b"before").hexdigest(),
        expected_content_size=len(b"before"),
    )

    def validate_bindings() -> None:
        # Model the external edit that completes while the trusted reader waits
        # for current input authority. It does not share the batch's mutex.
        at_completion = existing.read_text(encoding="utf-8") == "planned replacement"
        if at_completion == edit_at_completion:
            existing.write_text("external edit", encoding="utf-8")

    expected_error = vault.BatchWriteError if edit_at_completion else vault.PathGuardError
    with pytest.raises(expected_error):
        vault.batch_atomic_write(
            [vault.PlannedWrite(existing, "planned replacement", guard=guard)],
            vault_root=tmp_path,
            post_commit_fanout=False,
            _validate_prepared_bindings=validate_bindings,
        )

    assert existing.read_text(encoding="utf-8") == "external edit"


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


@pytest.mark.parametrize("resolved_target", [False, True], ids=["lexical", "resolved"])
def test_guarded_reader_accepts_a_configured_root_alias(
    tmp_path: Path, resolved_target: bool
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    target = root / "guarded.md"
    target.write_bytes(b"first\r\nsecond\r\n")
    alias = tmp_path / "vault-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    text, guard = vault.read_guarded_text(alias, target if resolved_target else alias / target.name)

    assert text.encode("utf-8") == target.read_bytes()
    guard.recheck(root)


@pytest.mark.parametrize("alias_kind", ["directory", "file", "hardlink"])
def test_configured_root_alias_does_not_authorize_an_inner_alias(
    tmp_path: Path, alias_kind: str
) -> None:
    root = tmp_path / "vault"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "private.md").write_text("private material", encoding="utf-8")
    alias = tmp_path / "vault-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
        if alias_kind == "directory":
            (root / "inner-alias").symlink_to(outside, target_is_directory=True)
            target = alias / "inner-alias" / "private.md"
        elif alias_kind == "file":
            (root / "private.md").symlink_to(outside / "private.md")
            target = alias / "private.md"
        else:
            os.link(outside / "private.md", root / "private.md")
            target = alias / "private.md"
    except OSError:
        pytest.skip("required filesystem links are unavailable")

    with pytest.raises(vault.PathGuardError):
        vault.read_guarded_text(alias, target)


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

    thread = threading.Thread(target=hold_lock, name="lock-holder")
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(vault.VaultLockTimeout) as lost:
            with vault.vault_creation_lock(tmp_path, "semantic-creation", timeout=0.01):
                pass
    finally:
        release.set()
        thread.join(5)

    assert not thread.is_alive()
    # The timeout names the thread it lost to and where each side called from, without a path.
    details = lost.value.details
    assert (details["lock"], details["namespace"]) == ("mutex", "semantic-creation")
    assert details["wait_site"].split(":")[1] == "test_vault_creation_lock_timeout_covers_thread_wait"
    holder = details["holder"]
    assert (holder["scope"], holder["thread"], holder["phase"], holder["verified"]) == (
        "in_process", "lock-holder", "held", True)
    assert holder["site"].split(":")[1] == "hold_lock" and holder["held_seconds"] > 0
    assert str(tmp_path) not in str(details) and str(lost.value) == "VAULT_LOCK_TIMEOUT: timed out acquiring vault lock"


def test_vault_creation_lock_timeout_names_the_process_holding_the_file_lock(tmp_path: Path) -> None:
    import subprocess
    import sys

    holder_script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from exomem import vault\n"
        "with vault.vault_creation_lock(Path(sys.argv[1]), 'semantic-creation'):\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", holder_script, str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(vault.VaultLockTimeout) as lost:
            with vault.vault_creation_lock(tmp_path, "semantic-creation", timeout=0.2):
                pass
    finally:
        child.communicate("\n", timeout=30)

    details = lost.value.details
    assert details["lock"] == "flock" and details["waited_ms"] >= 150
    # Another process can only publish who it is; the waiter cannot verify it.
    assert details["holder"]["scope"] == "other_process" and details["holder"]["pid"] == child.pid
    assert details["holder"]["verified"] is False and details["holder"]["phase"] == "held"
    assert str(tmp_path) not in str(details)
    # A released lock leaves no record behind to blame.
    _, digest = vault._lock_key(tmp_path, "semantic-creation")
    assert vault._read_flock_holder(vault._private_lock_directory() / f"{digest}.lock") is None


posix_only = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership and modes")


def _setgid_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point tempfile at a setgid parent, like a cell's fsGroup `/tmp` emptyDir."""
    import tempfile

    parent = tmp_path / "tmp"
    parent.mkdir()
    parent.chmod(0o2777)
    if not parent.stat().st_mode & stat.S_ISGID:
        pytest.skip("filesystem does not keep the setgid bit on directories")
    monkeypatch.setenv("TMPDIR", str(parent))
    monkeypatch.setattr(tempfile, "tempdir", None)
    return parent


@posix_only
def test_private_lock_directory_clears_setgid_inherited_on_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import init as init_module

    parent = _setgid_tempdir(tmp_path, monkeypatch)
    directory = parent.resolve() / f"exomem-locks-{os.getuid()}"

    init_module.init_vault(tmp_path / "vault")

    assert stat.S_IMODE(directory.lstat().st_mode) == 0o700


@posix_only
def test_private_lock_directory_clears_setgid_on_an_existing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _setgid_tempdir(tmp_path, monkeypatch)
    directory = parent.resolve() / f"exomem-locks-{os.getuid()}"
    directory.mkdir(mode=0o700)
    directory.chmod(0o2700)
    assert stat.S_IMODE(directory.lstat().st_mode) == 0o2700

    assert vault._private_lock_directory() == directory
    assert stat.S_IMODE(directory.lstat().st_mode) == 0o700


@posix_only
def test_private_lock_directory_refuses_a_directory_swapped_before_the_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _setgid_tempdir(tmp_path, monkeypatch)
    directory = parent.resolve() / f"exomem-locks-{os.getuid()}"
    directory.mkdir(mode=0o700)
    directory.chmod(0o2700)
    impostor = tmp_path / "impostor"
    impostor.mkdir(mode=0o700)
    impostor.chmod(0o2700)
    real_open = os.open

    def swapped_open(path: object, flags: int, *args: object) -> int:
        if Path(str(path)) == directory:
            return real_open(impostor, flags, *args)
        return real_open(path, flags, *args)

    monkeypatch.setattr(vault.os, "open", swapped_open)

    with pytest.raises(vault.VaultLockError) as exc:
        vault._private_lock_directory()

    assert exc.value.code == "VAULT_LOCK_DIRECTORY"
    assert stat.S_IMODE(impostor.lstat().st_mode) == 0o2700
    assert stat.S_IMODE(directory.lstat().st_mode) == 0o2700


@posix_only
@pytest.mark.parametrize("mode", [0o750, 0o2750, 0o4700, 0o1700, 0o6700, 0o701])
def test_private_lock_directory_still_refuses_any_other_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    parent = _setgid_tempdir(tmp_path, monkeypatch)
    directory = parent.resolve() / f"exomem-locks-{os.getuid()}"
    directory.mkdir(mode=0o700)
    directory.chmod(mode)

    with pytest.raises(vault.VaultLockError) as exc:
        vault._private_lock_directory()

    assert exc.value.code == "VAULT_LOCK_DIRECTORY"
    assert stat.S_IMODE(directory.lstat().st_mode) == mode


@posix_only
def test_private_lock_directory_still_refuses_a_foreign_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _setgid_tempdir(tmp_path, monkeypatch)
    foreign = os.getuid() + 1
    directory = parent.resolve() / f"exomem-locks-{foreign}"
    directory.mkdir(mode=0o700)
    directory.chmod(0o2700)
    monkeypatch.setattr(vault.os, "getuid", lambda: foreign)

    with pytest.raises(vault.VaultLockError) as exc:
        vault._private_lock_directory()

    assert exc.value.code == "VAULT_LOCK_DIRECTORY"
    assert stat.S_IMODE(directory.lstat().st_mode) == 0o2700


@posix_only
def test_private_lock_directory_still_refuses_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _setgid_tempdir(tmp_path, monkeypatch)
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    target.chmod(0o2700)
    (parent.resolve() / f"exomem-locks-{os.getuid()}").symlink_to(target)

    with pytest.raises(vault.VaultLockError) as exc:
        vault._private_lock_directory()

    assert exc.value.code == "VAULT_LOCK_DIRECTORY"
    assert stat.S_IMODE(target.lstat().st_mode) == 0o2700


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
