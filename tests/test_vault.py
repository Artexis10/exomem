from __future__ import annotations

import os
import threading
from pathlib import Path

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

    with pytest.raises(vault.PathGuardError):
        vault.batch_atomic_write(writes, vault_root=tmp_path)

    assert not first.exists()
    assert not pending.exists()


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
