"""get/edit round-trip tests — body field must not accumulate blank lines."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from kb_mcp import edit as edit_module
from kb_mcp import get_page as get_module


TODAY = dt.date(2026, 5, 18)


def test_get_body_has_no_leading_newline(vault: Path) -> None:
    """Regression: previously `get` returned body with a leading \\n, the
    frontmatter-separator artifact. Round-tripping through edit accumulated
    blank lines. After the fix, body starts at the first content character.
    """
    result = get_module.get_page(
        vault,
        path="Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md",
    )
    assert not result.body.startswith("\n"), repr(result.body[:20])
    assert result.body.startswith("# "), repr(result.body[:40])


def test_edit_roundtrip_does_not_accumulate_blanks(vault: Path) -> None:
    path = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    first = get_module.get_page(vault, path=path)
    edit_module.edit(
        vault, path=path, why="round-trip test", new_body=first.body, today=TODAY
    )
    second = get_module.get_page(vault, path=path)
    edit_module.edit(
        vault, path=path, why="round-trip test 2", new_body=second.body, today=TODAY
    )
    third = get_module.get_page(vault, path=path)
    # The body should be byte-identical across edits.
    assert first.body == second.body == third.body


# ---------------- type-allowlist removal ----------------


def _make_identity_page(vault: Path) -> str:
    """Helper: drop a fixture page with type: identity (a novel, non-allowlisted type)."""
    rel = "Knowledge Base/Identity/Career.md"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: identity\nscope: career\ncreated: 2026-05-24\nupdated: 2026-05-24\ntags: []\n---\n"
        "# Career\n\noriginal body.\n",
        encoding="utf-8",
    )
    return rel


def test_edit_accepts_novel_type(vault: Path) -> None:
    """Regression: edit used to refuse any type not in a hardcoded set.

    type: identity was the original failure case. Any frontmatter-bearing
    page outside Sources/Evidence should now be editable.
    """
    rel = _make_identity_page(vault)
    result = edit_module.edit(
        vault, path=rel, why="adding Q section",
        new_body="# Career\n\nrevised body.\n", today=TODAY,
    )
    assert result.path == rel
    text = (vault / rel).read_text(encoding="utf-8")
    assert "revised body" in text


def test_edit_accepts_page_without_type_field(vault: Path) -> None:
    """A page with frontmatter but no `type:` should still be editable."""
    rel = "Knowledge Base/Identity/index.md"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ncreated: 2026-05-24\nupdated: 2026-05-24\n---\n# Identity\n\nhub.\n",
        encoding="utf-8",
    )
    result = edit_module.edit(
        vault, path=rel, why="hub update",
        new_body="# Identity\n\nrevised hub.\n", today=TODAY,
    )
    assert "revised hub" in (vault / rel).read_text(encoding="utf-8")


def test_edit_still_refuses_sources(vault: Path) -> None:
    """Append-only guard is the real safety; it must remain."""
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault,
            path="Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md",
            why="should fail",
            new_body="x",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_EDIT"
    assert "append-only" in exc.value.reason.lower()


def test_edit_still_refuses_superseded(vault: Path) -> None:
    rel = "Knowledge Base/Identity/Career.md"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: identity\nstatus: superseded\ncreated: 2026-05-24\nupdated: 2026-05-24\ntags: []\nsuperseded_by: \"[[Notes/...]]\"\n---\n# Career\n\nx.\n",
        encoding="utf-8",
    )
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault, path=rel, why="x",
            new_body="y", today=TODAY,
        )
    assert exc.value.code == "ALREADY_SUPERSEDED"
