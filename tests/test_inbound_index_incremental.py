"""Inbound-link index per-file patch API (OpenSpec: event-maintained-indexes, P3).

`vault.on_inbound_files_changed` patches an already-built (live) inbound index
in place instead of re-reading the whole vault. The correctness bar: after ANY
sequence of patch calls, `find_inbound_wikilinks(target)` must return the same
SET of inbound links a fresh full rebuild would — content equality, not `seq`
order (design D3 documents the ordering caveat for patched entries)."""

from __future__ import annotations

import os
from pathlib import Path

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


def test_incremental_patch_matches_full_rebuild_across_add_edit_delete_move(
    vault: Path,
) -> None:
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    target = notes / "inc-target.md"
    target.write_text("# Target\n", encoding="utf-8")
    target_rel = "Knowledge Base/Notes/inc-target.md"

    linker_a = notes / "inc-linker-a.md"
    linker_a.write_text("# A\n\n[[Notes/inc-target]]\n", encoding="utf-8")
    linker_a_rel = "Knowledge Base/Notes/inc-linker-a.md"

    # Seed: force a full build so the index becomes "live".
    seeded = find_inbound_wikilinks(vault, target_rel)
    assert len(seeded) == 1
    _assert_matches_full_rebuild(vault, target_rel)

    # 1. Add a link: a brand-new file links to the target.
    linker_b = notes / "inc-linker-b.md"
    linker_b.write_text("# B\n\n[[Notes/inc-target]]\n", encoding="utf-8")
    linker_b_rel = "Knowledge Base/Notes/inc-linker-b.md"
    on_inbound_files_changed(vault, changed_rels=[linker_b_rel], deleted_rels=[])
    _assert_matches_full_rebuild(vault, target_rel)
    assert len(find_inbound_wikilinks(vault, target_rel)) == 2

    # 2. Edit a link: linker_a gains a second link to the target.
    ns = linker_a.stat().st_mtime_ns
    linker_a.write_text(
        "# A\n\n[[Notes/inc-target]] twice: [[Notes/inc-target]]\n", encoding="utf-8"
    )
    os.utime(linker_a, ns=(ns + 2_000_000_000, ns + 2_000_000_000))
    on_inbound_files_changed(vault, changed_rels=[linker_a_rel], deleted_rels=[])
    _assert_matches_full_rebuild(vault, target_rel)
    assert len(find_inbound_wikilinks(vault, target_rel)) == 3

    # 3. Delete a linking file: its edges disappear.
    linker_b.unlink()
    on_inbound_files_changed(vault, changed_rels=[], deleted_rels=[linker_b_rel])
    _assert_matches_full_rebuild(vault, target_rel)
    assert len(find_inbound_wikilinks(vault, target_rel)) == 2

    # 4. Move (rename) a linking file: reported as delete-old + change-new.
    moved = notes / "inc-linker-a-moved.md"
    os.replace(linker_a, moved)
    moved_rel = "Knowledge Base/Notes/inc-linker-a-moved.md"
    on_inbound_files_changed(
        vault, changed_rels=[moved_rel], deleted_rels=[linker_a_rel]
    )
    _assert_matches_full_rebuild(vault, target_rel)
    links = find_inbound_wikilinks(vault, target_rel)
    assert len(links) == 2
    assert {ln.path for ln in links} == {moved_rel}


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
