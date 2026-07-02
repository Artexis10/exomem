"""log.md hygiene: free-text rationales must not leak live wikilinks.

A `[[target]]` in an edit/note/link rationale is interpolated verbatim into a
log.md entry. Left live, the broken_wikilink audit then re-flags it — a
self-inflicted drift class. The writer neutralizes wikilink syntax in
log-bound free text via vault.escape_wikilinks_for_log.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from exomem import edit as edit_module
from exomem import vault


def test_escapes_bare_wikilink() -> None:
    assert vault.escape_wikilinks_for_log("de-link [[Foo]] now") == "de-link `Foo` now"


def test_escapes_aliased_and_pathed_wikilink() -> None:
    out = vault.escape_wikilinks_for_log("see [[a/b/Bar|Bar alias]]")
    assert out == "see `a/b/Bar|Bar alias`"
    assert "[[" not in out


def test_escapes_embed_and_multiple_links() -> None:
    out = vault.escape_wikilinks_for_log("![[img.png]] and [[X]] and [[Y]]")
    assert "[[" not in out and "![[" not in out
    assert "`img.png`" in out and "`X`" in out and "`Y`" in out


def test_leaves_plain_text_untouched() -> None:
    text = "repoint secrets-architecture-hardening to its handoff note"
    assert vault.escape_wikilinks_for_log(text) == text


def test_edit_rationale_does_not_leak_wikilink_into_log(vault: Path) -> None:
    """End-to-end: an `edit` whose `why` carries a wikilink must not write a
    live [[...]] into log.md. This is the exact self-inflicted drift class hit
    during the 2026-05-30 broken-link cleanup."""
    edit_module.edit(
        vault,
        path="Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation",
        why="de-link [[Foo Bar]] and repoint [[a/b/Baz|alias]] to the hub",
        new_body="Body touched by the escaping integration test.",
        today=dt.date(2026, 5, 31),
    )

    log = (vault / "Knowledge Base" / "log.md").read_text(encoding="utf-8")
    assert "[[Foo Bar]]" not in log
    assert "[[a/b/Baz" not in log
    assert "`Foo Bar`" in log
