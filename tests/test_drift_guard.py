"""Optimistic-concurrency guard for the two-writer pattern.

`get` returns a `content_hash` (sha256 over the raw file) and `mtime`. A
writer can echo that hash back to `edit` via `expected_hash`; if the file
changed on disk since the read, the edit is refused (STALE_EDIT) rather than
silently clobbering the other writer's change. No new state — the hash is
derived from bytes already on disk.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import edit as edit_module
from exomem import get_page as get_page_module
from exomem import vault as vault_module


TODAY = dt.date(2026, 6, 1)


def _make_page(vault: Path, body: str) -> str:
    rel = "Knowledge Base/Notes/Research/Taste/scratch-taste.md"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: research-note\nproject: taste\nstatus: active\n"
        "created: 2026-05-29\nupdated: 2026-05-29\ntags: [taste]\n---\n" + body,
        encoding="utf-8",
    )
    return rel


def test_get_returns_content_hash_and_mtime(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    result = get_page_module.get_page(vault, path=rel)
    assert isinstance(result.content_hash, str)
    assert len(result.content_hash) == 64  # sha256 hex digest
    assert result.mtime > 0
    # The hash is exactly sha256 of the raw content the caller received.
    assert result.content_hash == vault_module.content_hash(result.content)


def test_edit_with_matching_hash_commits(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nold line\n")
    g = get_page_module.get_page(vault, path=rel)
    edit_module.edit(
        vault,
        path=rel,
        why="rename line",
        old_string="old line",
        new_string="new line",
        expected_hash=g.content_hash,
        today=TODAY,
    )
    text = (vault / rel).read_text(encoding="utf-8")
    assert "new line" in text
    assert "old line" not in text


def test_edit_with_stale_hash_refuses_and_leaves_file_untouched(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nold line\n")
    g = get_page_module.get_page(vault, path=rel)
    # Another writer changes the body out of band.
    p = vault / rel
    p.write_text(
        p.read_text(encoding="utf-8") + "\nappended out of band\n", encoding="utf-8"
    )
    before = p.read_text(encoding="utf-8")
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault,
            path=rel,
            why="rename line",
            old_string="old line",
            new_string="new line",
            expected_hash=g.content_hash,
            today=TODAY,
        )
    assert exc.value.code == "STALE_EDIT"
    assert p.read_text(encoding="utf-8") == before  # nothing written


def test_hash_covers_frontmatter_change(vault: Path) -> None:
    """A concurrent frontmatter-only edit must still trip the guard."""
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    g = get_page_module.get_page(vault, path=rel)
    p = vault / rel
    p.write_text(
        p.read_text(encoding="utf-8").replace("tags: [taste]", "tags: [taste, new]"),
        encoding="utf-8",
    )
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault,
            path=rel,
            why="change body",
            old_string="body.",
            new_string="changed.",
            expected_hash=g.content_hash,
            today=TODAY,
        )
    assert exc.value.code == "STALE_EDIT"


def test_no_expected_hash_skips_guard(vault: Path) -> None:
    """Omitting expected_hash preserves prior behavior exactly."""
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    edit_module.edit(
        vault, path=rel, why="x", old_string="body.", new_string="z.", today=TODAY
    )
    assert "z." in (vault / rel).read_text(encoding="utf-8")
