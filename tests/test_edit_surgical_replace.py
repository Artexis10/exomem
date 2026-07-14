"""Surgical string-replace mode for `edit` — token-cheap in-place edits.

Motivation: filling a blank `[take: ]` row or appending one opinion to a
section used to require re-sending the WHOLE body via `new_body`. For an
ever-living note (e.g. the taste cluster) that's thousands of tokens per
one-line change. The surgical mode lets the caller send only old/new strings
while still going through the same writer (log + index + re-embed + updated:
bump) — no out-of-band drift.
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


def test_surgical_replace_fills_a_take(vault: Path) -> None:
    rel = _make_page(
        vault,
        "# Scratch\n\n## Opinions\n\n"
        "- Whiplash (2014) — 10/10 — [take: ]  <!-- platform:imdb -->\n"
        "- Seven (1995) — 10/10 — [take: ]  <!-- platform:imdb -->\n",
    )
    result = edit_module.edit(
        vault,
        path=rel,
        why="fill Whiplash take",
        old_string="- Whiplash (2014) — 10/10 — [take: ]  <!-- platform:imdb -->",
        new_string="- Whiplash (2014) — 10/10 — [take: relentless]  <!-- platform:imdb -->",
        today=TODAY,
    )
    assert result.path == rel
    text = (vault / rel).read_text(encoding="utf-8")
    assert "[take: relentless]" in text
    # The other row is untouched.
    assert "- Seven (1995) — 10/10 — [take: ]  <!-- platform:imdb -->" in text
    # updated: bumped.
    assert "updated: 2026-06-01" in text


def test_surgical_append_to_section(vault: Path) -> None:
    """Append a row by replacing a section anchor with itself + the new line."""
    rel = _make_page(
        vault,
        "# Scratch\n\n## Opinions\n\n### Conversation-derived\n\n"
        "## Connections\n\n- x\n",
    )
    anchor = "### Conversation-derived\n"
    edit_module.edit(
        vault,
        path=rel,
        why="append conv opinion",
        old_string=anchor,
        new_string=anchor + "\n- New take. <!-- conv:2026-06-01 -->\n",
        today=TODAY,
    )
    text = (vault / rel).read_text(encoding="utf-8")
    assert "- New take. <!-- conv:2026-06-01 -->" in text
    # Landed before Connections, not at EOF.
    assert text.index("New take") < text.index("## Connections")


def test_surgical_absent_string_errors(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault, path=rel, why="x",
            old_string="does-not-exist", new_string="y", today=TODAY,
        )
    assert exc.value.code == "STRING_NOT_FOUND"


def test_surgical_ambiguous_match_errors(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nrepeat\nrepeat\n")
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault, path=rel, why="x",
            old_string="repeat", new_string="z", today=TODAY,
        )
    assert exc.value.code == "AMBIGUOUS_MATCH"


def test_surgical_replace_all(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nrepeat\nrepeat\n")
    edit_module.edit(
        vault, path=rel, why="x",
        old_string="repeat", new_string="z", replace_all=True, today=TODAY,
    )
    text = (vault / rel).read_text(encoding="utf-8")
    assert text.count("repeat") == 0
    assert text.count("z") == 2


def test_surgical_requires_new_string(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(vault, path=rel, why="x", old_string="body", today=TODAY)
    assert exc.value.code == "INVALID_EDIT"


def test_surgical_mutually_exclusive_with_new_body(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault, path=rel, why="x",
            old_string="body", new_string="z", new_body="# Scratch\n\nwhole.\n",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_EDIT"


def test_surgical_noop_when_identical_errors(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault, path=rel, why="x",
            old_string="body", new_string="body", today=TODAY,
        )
    assert exc.value.code == "INVALID_EDIT"


def test_surgical_still_refuses_sources(vault: Path) -> None:
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            vault,
            path="Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md",
            why="should fail",
            old_string="x", new_string="y", today=TODAY,
        )
    assert exc.value.code == "INVALID_EDIT"
    assert "append-only" in exc.value.reason.lower()
