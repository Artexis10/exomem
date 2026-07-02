"""`validate_only` on edit — preview a surgical match without committing.

Lets a caller confirm `old_string` matches uniquely (and see the surrounding
context) before mutating — the main `replace_all` footgun is an ambiguous
match silently hitting more rows than intended. Mirrors the `dry_run`
precedent in audit_fix/reconcile.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import edit as edit_module


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


def test_validate_only_unique_match_previews_without_writing(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nunique line here\n")
    result = edit_module.edit(
        vault,
        path=rel,
        why="preview",
        old_string="unique line here",
        new_string="changed",
        validate_only=True,
        today=TODAY,
    )
    assert result.validate_only is True
    assert result.mode == "surgical"
    assert result.match_count == 1
    assert any("unique line here" in m for m in result.matches)
    # Nothing written: body unchanged, updated: NOT bumped.
    text = (vault / rel).read_text(encoding="utf-8")
    assert "unique line here" in text
    assert "changed" not in text
    assert "updated: 2026-05-29" in text


def test_validate_only_ambiguous_reports_count_without_raising(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nrepeat\nrepeat\nrepeat\n")
    result = edit_module.edit(
        vault,
        path=rel,
        why="preview",
        old_string="repeat",
        new_string="z",
        validate_only=True,
        today=TODAY,
    )
    assert result.match_count == 3


def test_validate_only_zero_match_reports_count_without_raising(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    result = edit_module.edit(
        vault,
        path=rel,
        why="preview",
        old_string="absent",
        new_string="z",
        validate_only=True,
        today=TODAY,
    )
    assert result.match_count == 0
    assert result.matches == []


def test_validate_only_replace_all_lists_each_match(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nrepeat\nrepeat\n")
    result = edit_module.edit(
        vault,
        path=rel,
        why="preview",
        old_string="repeat",
        new_string="z",
        replace_all=True,
        validate_only=True,
        today=TODAY,
    )
    assert result.match_count == 2
    assert len(result.matches) == 2


def test_validate_only_requires_surgical_mode(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault,
            path=rel,
            why="preview",
            new_body="# Scratch\n\nwhole.\n",
            validate_only=True,
            today=TODAY,
        )
    assert exc.value.code == "INVALID_EDIT"


def test_validate_only_arg_guards_still_fire(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault,
            path=rel,
            why="preview",
            old_string="body",  # new_string missing
            validate_only=True,
            today=TODAY,
        )
    assert exc.value.code == "INVALID_EDIT"


def test_validate_only_honors_expected_hash(vault: Path) -> None:
    """The drift guard runs before the preview — you validate current bytes."""
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault,
            path=rel,
            why="preview",
            old_string="body.",
            new_string="z.",
            expected_hash="not-the-real-hash",
            validate_only=True,
            today=TODAY,
        )
    assert exc.value.code == "STALE_EDIT"
