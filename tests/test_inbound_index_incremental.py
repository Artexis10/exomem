"""Inbound-link index per-file patch API (OpenSpec: event-maintained-indexes, P3).

`vault.on_inbound_files_changed` patches an already-built (live) inbound index
in place instead of re-reading the whole vault. The correctness bar: after ANY
sequence of patch calls, `find_inbound_wikilinks(target)` must return the same
SET of inbound links a fresh full rebuild would — content equality, not `seq`
order (design D3 documents the ordering caveat for patched entries)."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from exomem import freshness
from exomem import vault as vault_module
from exomem.vault import (
    InboundLink,
    clear_inbound_index,
    find_inbound_wikilinks,
    on_inbound_files_changed,
)


def _links_key(links: list[InboundLink]) -> list[tuple]:
    return sorted((ln.path, ln.line_number, ln.context, ln.raw_target) for ln in links)


def _assert_matches_full_rebuild(vault_root: Path, target_rel: str) -> None:
    """The live (possibly patched) result must SET-equal an independent fresh
    full rebuild, without disturbing the live cache the test is exercising."""
    incremental = find_inbound_wikilinks(vault_root, target_rel)
    root = str(vault_root.resolve())
    saved = vault_module._INBOUND_INDEX.get(root)
    vault_module._INBOUND_INDEX.pop(root, None)
    try:
        full = find_inbound_wikilinks(vault_root, target_rel)
    finally:
        vault_module._INBOUND_INDEX.pop(root, None)
        if saved is not None:
            vault_module._INBOUND_INDEX[root] = saved
    assert _links_key(incremental) == _links_key(full), (incremental, full)


def _seed_registry(vault_root: Path) -> None:
    """Make the freshness registry live, as it is in a server process."""
    freshness.seed(vault_root, "vault", [
        (str(path), freshness.stat_signature(path))
        for path in vault_module.walk_vault_md(vault_root)
    ])


def _watcher_batch(
    vault_root: Path, *, live: bool, changed: tuple[str, ...] = (), deleted: tuple[str, ...] = ()
) -> None:
    """One watcher batch. In a server the registry announces the paths first."""
    if live:
        freshness.on_files_changed(
            vault_root,
            changed=[vault_root / rel for rel in changed],
            deleted=[vault_root / rel for rel in deleted],
        )
    on_inbound_files_changed(vault_root, changed_rels=list(changed), deleted_rels=list(deleted))


def _content(data: vault_module._InboundIndexData) -> tuple:
    """Everything an index answers from, without the order of patched entries."""
    return (
        {
            key: sorted((e.path, e.line_number, e.context, e.raw_target) for e in entries)
            for key, entries in data.buckets.items()
        },
        dict(data.stem_counts),
        set(data.known_rels),
        {rel: set(keys) for rel, keys in data.file_targets.items()},
    )


def _assert_resident_index_equals_rebuild(vault_root: Path) -> None:
    resident = vault_module._INBOUND_INDEX[str(vault_root.resolve())][1]
    assert _content(resident) == _content(vault_module._build_inbound_index(vault_root))
    assert resident.next_seq > max(
        (e.seq for entries in resident.buckets.values() for e in entries), default=-1
    )


def test_patch_is_noop_when_index_not_yet_built(vault: Path) -> None:
    """The patch path only mutates an index that already exists (design D3:
    'only used when the index is live'). Nothing cached -> nothing to do; the
    next real `find_inbound_wikilinks` call does the full build instead."""
    clear_inbound_index()
    root = str(vault.resolve())
    assert root not in vault_module._INBOUND_INDEX
    on_inbound_files_changed(
        vault, changed_rels=["Knowledge Base/Notes/never-built.md"], deleted_rels=[]
    )
    assert root not in vault_module._INBOUND_INDEX


def test_patch_noop_under_kill_switch_then_self_heals(
    vault: Path, monkeypatch
) -> None:
    """EXOMEM_DISABLE_EVENT_INDEXES makes the patch call a pure no-op (the
    cached entry is untouched), but the existing digest-keyed fallback in
    `_inbound_index` still detects the on-disk change and rebuilds on the
    next call — `find_inbound_wikilinks` stays byte-identical either way."""
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    target = notes / "kill-target.md"
    target.write_text("# Target\n", encoding="utf-8")
    target_rel = "Knowledge Base/Notes/kill-target.md"

    # Seed live (kill switch not yet set).
    assert find_inbound_wikilinks(vault, target_rel) == []

    monkeypatch.setenv("EXOMEM_DISABLE_EVENT_INDEXES", "1")
    linker = notes / "kill-linker.md"
    linker.write_text("# L\n\n[[Notes/kill-target]]\n", encoding="utf-8")
    linker_rel = "Knowledge Base/Notes/kill-linker.md"

    root = str(vault.resolve())
    before = vault_module._INBOUND_INDEX[root]
    on_inbound_files_changed(vault, changed_rels=[linker_rel], deleted_rels=[])
    after = vault_module._INBOUND_INDEX[root]
    assert after is before  # identity unchanged -> patch never ran

    got = find_inbound_wikilinks(vault, target_rel)
    assert len(got) == 1
    assert got[0].path == linker_rel


@pytest.mark.parametrize("live", [False, True], ids=["registry-not-live", "registry-live"])
def test_incremental_patch_matches_full_rebuild_across_add_edit_delete_move(
    vault: Path, live: bool
) -> None:
    """With a live registry every patch must also keep the index published
    (no rebuild) and its content equal to a fresh build, entry for entry."""
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    target = notes / "inc-target.md"
    target.write_text("# Target\n", encoding="utf-8")
    target_rel = "Knowledge Base/Notes/inc-target.md"

    linker_a = notes / "inc-linker-a.md"
    linker_a.write_text("# A\n\n[[Notes/inc-target]]\n", encoding="utf-8")
    linker_a_rel = "Knowledge Base/Notes/inc-linker-a.md"

    if live:
        _seed_registry(vault)
    # Seed: force a full build so the index becomes "live".
    seeded = find_inbound_wikilinks(vault, target_rel)
    assert len(seeded) == 1
    _assert_matches_full_rebuild(vault, target_rel)

    def assert_still_published() -> None:
        if live:
            assert vault_module.cached_inbound_wikilinks(vault, target_rel) is not None
            _assert_resident_index_equals_rebuild(vault)

    # 1. Add a link: a brand-new file links to the target.
    linker_b = notes / "inc-linker-b.md"
    linker_b.write_text("# B\n\n[[Notes/inc-target]]\n", encoding="utf-8")
    linker_b_rel = "Knowledge Base/Notes/inc-linker-b.md"
    _watcher_batch(vault, live=live, changed=(linker_b_rel,))
    assert_still_published()
    _assert_matches_full_rebuild(vault, target_rel)
    assert len(find_inbound_wikilinks(vault, target_rel)) == 2

    # 2. Edit a link: linker_a gains a second link to the target.
    ns = linker_a.stat().st_mtime_ns
    linker_a.write_text(
        "# A\n\n[[Notes/inc-target]] twice: [[Notes/inc-target]]\n", encoding="utf-8"
    )
    os.utime(linker_a, ns=(ns + 2_000_000_000, ns + 2_000_000_000))
    _watcher_batch(vault, live=live, changed=(linker_a_rel,))
    assert_still_published()
    _assert_matches_full_rebuild(vault, target_rel)
    assert len(find_inbound_wikilinks(vault, target_rel)) == 3

    # 3. Delete a linking file: its edges disappear.
    linker_b.unlink()
    _watcher_batch(vault, live=live, deleted=(linker_b_rel,))
    assert_still_published()
    _assert_matches_full_rebuild(vault, target_rel)
    assert len(find_inbound_wikilinks(vault, target_rel)) == 2

    # 4. Move (rename) a linking file: reported as delete-old + change-new.
    moved = notes / "inc-linker-a-moved.md"
    os.replace(linker_a, moved)
    moved_rel = "Knowledge Base/Notes/inc-linker-a-moved.md"
    _watcher_batch(vault, live=live, changed=(moved_rel,), deleted=(linker_a_rel,))
    assert_still_published()
    _assert_matches_full_rebuild(vault, target_rel)
    links = find_inbound_wikilinks(vault, target_rel)
    assert len(links) == 2
    assert {ln.path for ln in links} == {moved_rel}


def test_on_files_changed_same_rel_in_both_lists_retains_live_entries(
    vault: Path,
) -> None:
    """Mirrors the WikilinkResolver defense (#126): a rel present in BOTH
    `changed_rels` and `deleted_rels` in the same batch — two path-string
    forms of one file (Windows 8.3 short name vs. the walk's long form)
    collapsing to the identical rel upstream — must not drop a file's
    inbound-link edges when it is still on disk. The old code (`changed =
    set(changed_rels) - deleted`) unconditionally subtracted the conflicting
    rel out of `changed`, so the linker's edges were dropped and never
    re-added even though the file was live. The filesystem breaks the tie:
    re-add wins."""
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    target = notes / "dual-form-target.md"
    target.write_text("# Target\n", encoding="utf-8")
    target_rel = "Knowledge Base/Notes/dual-form-target.md"

    linker = notes / "dual-form-linker.md"
    linker.write_text("# L\n\n[[Notes/dual-form-target]]\n", encoding="utf-8")
    linker_rel = "Knowledge Base/Notes/dual-form-linker.md"

    # Seed live.
    seeded = find_inbound_wikilinks(vault, target_rel)
    assert len(seeded) == 1
    _assert_matches_full_rebuild(vault, target_rel)

    # Same rel in both lists (the collapse scenario) — file still exists.
    on_inbound_files_changed(vault, changed_rels=[linker_rel], deleted_rels=[linker_rel])

    got = find_inbound_wikilinks(vault, target_rel)
    assert len(got) == 1, (
        "inbound index dropped a live linker's edges when its rel appeared "
        "in both changed_rels and deleted_rels in the same batch"
    )
    assert got[0].path == linker_rel
    _assert_matches_full_rebuild(vault, target_rel)


def test_incremental_patch_updates_stem_counts_for_basename_uniqueness(
    vault: Path,
) -> None:
    """stem_counts (basename uniqueness for the bare `[[foo]]` match form)
    must be patched too, not just bucket edges — a rename/duplicate changes
    whether the bare-basename match is even allowed to fire."""
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    target = notes / "stem-target.md"
    target.write_text("# Stem target\n", encoding="utf-8")
    target_rel = "Knowledge Base/Notes/stem-target.md"

    linker = notes / "stem-linker.md"
    linker.write_text("# L\n\nBare [[stem-target]].\n", encoding="utf-8")

    # Seed live: basename is unique -> bare match fires.
    got = find_inbound_wikilinks(vault, target_rel)
    assert len(got) == 1
    _assert_matches_full_rebuild(vault, target_rel)

    # Add a duplicate-basename file elsewhere via the patch API.
    dup = vault / "Knowledge Base" / "Sources" / "stem-target.md"
    dup.parent.mkdir(parents=True, exist_ok=True)
    dup.write_text("# Duplicate basename\n", encoding="utf-8")
    dup_rel = "Knowledge Base/Sources/stem-target.md"
    on_inbound_files_changed(vault, changed_rels=[dup_rel], deleted_rels=[])
    _assert_matches_full_rebuild(vault, target_rel)
    assert find_inbound_wikilinks(vault, target_rel) == []  # ambiguous now

    # Delete the duplicate again -> unique once more, bare match returns.
    dup.unlink()
    on_inbound_files_changed(vault, changed_rels=[], deleted_rels=[dup_rel])
    _assert_matches_full_rebuild(vault, target_rel)
    assert len(find_inbound_wikilinks(vault, target_rel)) == 1


def test_a_patch_costs_the_changed_file_not_the_index(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under constant writes every patch used to rewrite every bucket and rescan
    every entry for the next sequence number, on the writer's thread."""
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    for n in range(30):
        (notes / f"cost-linker-{n}.md").write_text(f"# L{n}\n\n[[Notes/cost-t{n}]]\n", encoding="utf-8")
    _seed_registry(vault)
    find_inbound_wikilinks(vault, "Knowledge Base/Notes/cost-t0.md")
    data = vault_module._INBOUND_INDEX[str(vault.resolve())][1]
    buckets_before = dict(data.buckets)  # bucket key -> the list object a reader may hold

    rel = "Knowledge Base/Notes/cost-linker-0.md"
    (vault / rel).write_text("# L0\n\nNow [[Notes/cost-t0-renamed]].\n", encoding="utf-8")
    reads: list[Path] = []
    real_read = vault_module.read_guarded_text

    def counting_read(root, path):
        reads.append(path)
        return real_read(root, path)

    monkeypatch.setattr(vault_module, "read_guarded_text", counting_read)
    _watcher_batch(vault, live=True, changed=(rel,))

    assert reads == [vault / rel]
    touched = {"Notes/cost-t0", "Notes/cost-t0-renamed"}
    for key, bucket in buckets_before.items():
        if key not in touched:
            assert data.buckets[key] is bucket, f"patch rewrote the untouched bucket {key!r}"
    assert "Notes/cost-t0" not in data.buckets
    assert [e.path for e in data.buckets["Notes/cost-t0-renamed"]] == [rel]


def test_a_reader_never_sees_half_a_patched_file(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader that is still choosing among candidates while a patch rewrites a
    file's links sees the old links or the new ones, never a few of them."""
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    target_rel = "Knowledge Base/Notes/half-target.md"
    (notes / "half-target.md").write_text("# Target\n", encoding="utf-8")
    other = vault / "Knowledge Base" / "Sources" / "half-target.md"  # a shared stem makes the reader ask `visible`
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("# Same stem\n", encoding="utf-8")
    linker = notes / "half-linker.md"
    linker_rel = "Knowledge Base/Notes/half-linker.md"

    def write_links(words: tuple[str, ...]) -> None:
        body = "".join(f"[[Notes/half-target]] {word}\n" for word in words)
        linker.write_text(f"# L\n\n{body}", encoding="utf-8")

    write_links(("one", "two", "three"))
    _seed_registry(vault)
    assert len(find_inbound_wikilinks(vault, target_rel)) == 3
    write_links(("four", "five", "six"))
    freshness.on_files_changed(vault, changed=[linker])

    reader_inside, reader_go = threading.Event(), threading.Event()
    patch_inside, patch_go = threading.Event(), threading.Event()
    built = 0
    real_entry = vault_module._InboundEntry

    class PausingEntry(real_entry):
        def __init__(self, *args, **kwargs):
            nonlocal built
            super().__init__(*args, **kwargs)
            built += 1
            if built == 2:  # the patch is between its first and second new link
                patch_inside.set()
                assert patch_go.wait(10)

    def visible(_rel: str) -> bool:
        reader_inside.set()
        assert reader_go.wait(10)
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(find_inbound_wikilinks, vault, target_rel, visible=visible)
        assert reader_inside.wait(10)
        monkeypatch.setattr(vault_module, "_InboundEntry", PausingEntry)
        writer = pool.submit(on_inbound_files_changed, vault, [linker_rel], [])
        try:
            assert patch_inside.wait(10), "the patch never reached its second new link"
            reader_go.set()
            seen = reader.result(timeout=10)
        finally:
            reader_go.set()
            patch_go.set()
        writer.result(timeout=10)

    words = sorted(link.context.rsplit(" ", 1)[-1] for link in seen)
    assert words in (sorted(["one", "two", "three"]), sorted(["four", "five", "six"]))


def test_patch_keeps_the_index_published_when_the_vault_is_reached_through_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry names paths as the configured root spells them, the index
    names its cache by the resolved root; a patch must still match the two."""
    real = tmp_path / "real"
    notes = real / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    root = tmp_path / "link"
    root.symlink_to(real)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    target_rel = "Knowledge Base/Notes/sym-target.md"
    source_rel = "Knowledge Base/Notes/sym-source.md"
    (root / target_rel).write_text("# Target\n", encoding="utf-8")
    (root / source_rel).write_text("# Source\n", encoding="utf-8")
    clear_inbound_index()
    _seed_registry(root)
    assert find_inbound_wikilinks(root, target_rel) == []

    (root / source_rel).write_text("# Source\n\n[[Notes/sym-target]]\n", encoding="utf-8")
    _watcher_batch(root, live=True, changed=(source_rel,))

    borrowed = vault_module.cached_inbound_wikilinks(root, target_rel)
    assert borrowed is not None and [link.path for link in borrowed] == [source_rel]
