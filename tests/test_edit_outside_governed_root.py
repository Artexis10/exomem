"""`edit` refuses a page that exists outside Knowledge Base/ honestly.

Regression for the read/write path-resolution asymmetry (issue #599):
`read_memory` accepts a vault-relative path and reads a page that lives
outside the governed `Knowledge Base/` root, but `edit`/`edit_memory`
silently re-roots the same string under `Knowledge Base/` and then reports
the *re-rooted* path as missing. The caller is handed a `NOT_FOUND` naming a
path it never gave, asserting a file does not exist when the page it
addressed does exist — indistinguishable from a typo.

The governance model is intentional (governed writes only happen under
`Knowledge Base/`), so the fix is not to write outside the root; it is to
refuse *honestly* so a caller can tell "outside the governed root" from
"no such page".
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import edit as edit_module

TODAY = dt.date(2026, 6, 1)


def _make_page_outside_kb(vault: Path, rel: str, body: str) -> str:
    """Write a real frontmatter page at a vault-relative path OUTSIDE KB/."""
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: decision\nstatus: active\n"
        "created: 2026-05-29\nupdated: 2026-05-29\ntags: [x]\n---\n" + body,
        encoding="utf-8",
    )
    return rel


def test_edit_outside_kb_refuses_honestly_not_as_missing(vault: Path) -> None:
    # A page the read side can address by its vault-relative path, living
    # outside the governed Knowledge Base/ root.
    rel = _make_page_outside_kb(
        vault, "projects/side-by-side.md", "# Side by side\n\nbody line\n"
    )

    with pytest.raises(edit_module.EditError) as exc_info:
        edit_module.edit(
            vault,
            path=rel,
            why="preview",
            old_string="body line",
            new_string="changed",
            validate_only=True,
            today=TODAY,
        )

    err = exc_info.value
    # The refusal is distinct from a genuine miss, and it names the path the
    # caller actually gave rather than a silently re-rooted Knowledge Base/ one.
    assert err.code == "OUTSIDE_GOVERNED_ROOT"
    assert rel in err.reason
    assert "Knowledge Base/" in err.reason
    # It must NOT falsely claim the addressed page does not exist.
    assert "does not exist" not in err.reason


def test_edit_genuinely_missing_page_still_not_found(vault: Path) -> None:
    # Nothing at this path anywhere — the honest answer stays NOT_FOUND so the
    # new branch does not swallow real misses.
    with pytest.raises(edit_module.EditError) as exc_info:
        edit_module.edit(
            vault,
            path="Notes/does-not-exist-anywhere.md",
            why="preview",
            old_string="x",
            new_string="y",
            validate_only=True,
            today=TODAY,
        )
    assert exc_info.value.code == "NOT_FOUND"
