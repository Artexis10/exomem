"""Writers must REUSE the process-shared WikilinkResolver, not rebuild it.

The resolver build reads + YAML-parses every vault .md file. find()'s graph
lane learned this lesson long ago (`_get_query_resolver` + freshness-checked
`_RESOLVER_CACHE`, watcher-patched) — but every WRITE op still constructed a
fresh `WikilinkResolver(vault_root)` per call, which on a real ~1,900-file
vault measured ~2.1s of a 4.6s note() (cProfile, 2026-07-04) and is the
dominant reason the whole write-tool family (note/edit/link/…) ran multi-
second medians in production. These tests pin the fix: with a warm shared
resolver, a write triggers ZERO additional full builds.

Also pinned: a FAILED batch write must purge the `add_pending` registration
from the shared resolver — otherwise a phantom entry (a path that never
landed on disk) lingers in the cache and later writes resolve wikilinks to a
nonexistent page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import edit as edit_module
from exomem import find as find_module
from exomem import link as link_module
from exomem import note as note_module
from exomem.vault import WikilinkResolver


@pytest.fixture
def build_counter(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count full WikilinkResolver builds (each one is an O(vault) read+parse)."""
    builds: list[int] = []
    orig = WikilinkResolver._build

    def counting(self) -> None:
        builds.append(1)
        orig(self)

    monkeypatch.setattr(WikilinkResolver, "_build", counting)
    return builds


def _warm_resolver(vault: Path) -> None:
    """Prime the shared cache the way a server request / warm-up does."""
    find_module._get_query_resolver(vault)


def test_note_reuses_cached_resolver(vault: Path, build_counter: list[int]) -> None:
    _warm_resolver(vault)
    warm_builds = len(build_counter)
    note_module.note(
        vault,
        content="# Resolver reuse probe\n\nBody referencing [[Profile]].",
        note_type="insight",
        title="Resolver reuse probe",
        status="draft",
    )
    assert len(build_counter) == warm_builds, (
        "note() rebuilt the WikilinkResolver instead of reusing the shared "
        "freshness-checked cache"
    )


def test_edit_reuses_cached_resolver(vault: Path, build_counter: list[int]) -> None:
    r = note_module.note(
        vault,
        content="# Edit resolver probe\n\nOriginal body.",
        note_type="insight",
        title="Edit resolver probe",
        status="draft",
    )
    _warm_resolver(vault)
    warm_builds = len(build_counter)
    edit_module.edit(
        vault,
        path=r.path,
        new_body="# Edit resolver probe\n\nRewritten body.",
        why="test: resolver reuse on new_body edit",
    )
    edit_module.edit(
        vault,
        path=r.path,
        old_string="Rewritten body.",
        new_string="Rewritten body twice.",
        why="test: resolver reuse on surgical edit",
    )
    assert len(build_counter) == warm_builds, (
        "edit() rebuilt the WikilinkResolver instead of reusing the shared cache"
    )


def test_link_reuses_cached_resolver(vault: Path, build_counter: list[int]) -> None:
    _warm_resolver(vault)
    warm_builds = len(build_counter)
    link_module.link(
        vault,
        entity_type="concept",
        name="Resolver Reuse Concept",
        summary="Probe entity for the shared-resolver test.",
    )
    assert len(build_counter) == warm_builds, (
        "link() rebuilt the WikilinkResolver instead of reusing the shared cache"
    )


def test_failed_note_write_purges_pending_resolver_entry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _warm_resolver(vault)

    def boom(writes, *, vault_root):  # signature-compatible with batch_atomic_write
        raise OSError("simulated disk failure")

    monkeypatch.setattr(note_module.semantic_writes.vault, "batch_atomic_write", boom)
    with pytest.raises(OSError):
        note_module.note(
            vault,
            content="# Phantom entry probe\n\nBody.",
            note_type="insight",
            title="Phantom entry probe",
            status="draft",
        )
    resolver = find_module._get_query_resolver(vault)
    phantom = "Knowledge Base/Notes/Insights/phantom-entry-probe"
    assert phantom not in resolver.full_paths, (
        "failed note() left a phantom add_pending entry in the shared resolver"
    )
