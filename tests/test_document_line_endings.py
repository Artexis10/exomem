"""One page, one line ending: a rewrite must not mix LF into a CRLF document.

Governed pages and `log.md` are ordinary files in the user's vault, and any
Windows editor that saves one leaves CRLF behind. `read_guarded_text` reads raw
bytes, so every rewrite path saw exactly what was on disk while reconstructing
documents with hardcoded LF delimiters. That mixed both endings into one file
and made the rewrite fire even when the pass had nothing to change, which is how
`audit_fix` came to report wikilink fixes for pages it was contractually
leaving alone.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import audit_fix as audit_fix_module
from exomem import commands
from exomem import edit as edit_module
from exomem import find as find_module
from exomem import indexes as indexes_module
from exomem import replace as replace_module
from exomem.vault import (
    document_newline,
    parse_frontmatter,
    render_frontmatter_document,
)

NOTE_REL = "Knowledge Base/Notes/Insights/line-endings.md"

SOURCE_LINK = "[[Knowledge Base/Sources/Articles/2026-06-02-postgres-autovacuum-tuning]]"

# Shaped like the compliant fixture insights so `audit_fix` has nothing legitimate
# to change here and any rewrite it performs is the defect under test.
PAGE = f"""\
---
type: insight
status: active
created: 2026-01-01
updated: 2026-01-01
sources:
  - "{SOURCE_LINK}"
tags: [legacy]
---

# Line Endings

## Claim

A page keeps whatever line ending it arrived with.

## Overview

Intro line.

## Connections

- {SOURCE_LINK}
"""

newlines = pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])


def _seed(vault: Path, newline: str) -> Path:
    path = vault / NOTE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PAGE.replace("\n", newline).encode("utf-8"))
    find_module.clear_cache()
    return path


def _endings(text: str) -> set[str]:
    """Report which line endings a document actually carries."""
    found = set()
    if "\r\n" in text:
        found.add("\r\n")
    if "\n" in text.replace("\r\n", ""):
        found.add("\n")
    return found


@newlines
def test_a_document_round_trips_byte_exact_through_the_renderer(newline: str) -> None:
    """The parse/render pair is the seam every rewrite path composes through."""
    text = PAGE.replace("\n", newline)
    _fm, body, fm_text = parse_frontmatter(text)
    assert fm_text is not None

    rebuilt = render_frontmatter_document(
        fm_text, body, newline=document_newline(text), blank_line=True
    )

    assert rebuilt == text


@newlines
def test_edit_leaves_one_consistent_line_ending(vault: Path, newline: str) -> None:
    """Edit normalizes to LF deliberately; what it must never do is mix.

    `edit` folds CRLF to LF on read (`edit.py`) so the optimistic-concurrency
    hash matches the newline-normalized text `get` returned. Rewriting a CRLF
    page as LF is therefore the contract, not the defect. The defect was
    reconstructing the document with LF delimiters around a body that had kept
    its CRLF, leaving both endings inside one file.
    """
    path = _seed(vault, newline)

    edit_module.edit(
        vault,
        path=NOTE_REL,
        why="line ending regression probe",
        heading="Overview",
        section_position="append",
        new_string="Added line.",
    )

    text = path.read_bytes().decode("utf-8")
    assert "Added line." in text
    assert _endings(text) == {"\n"}


@newlines
def test_audit_fix_leaves_a_compliant_page_byte_identical(
    vault: Path, newline: str
) -> None:
    """`audit_fix` promises not to touch a compliant page. Bytes are the proof."""
    path = _seed(vault, newline)
    before = path.read_bytes()

    report = audit_fix_module.audit_fix(vault, today=dt.date(2026, 8, 5))

    assert path.read_bytes() == before
    assert not [
        finding for finding in report.fixed if finding.path.endswith("line-endings.md")
    ]


@newlines
def test_supersede_marks_the_old_page_whatever_its_line_ending(newline: str) -> None:
    """A private LF-only frontmatter pattern made supersession a silent no-op.

    `replace` never folds CRLF to LF on read, so a page a Windows editor had
    saved refused to match, `_mark_superseded` handed it straight back, and the
    call reported success with the old page still `active` and no
    `superseded_by` link written. Nothing raised.
    """
    page = PAGE.replace("\n", newline)

    marked = replace_module._mark_superseded(
        page, "Knowledge Base/Notes/Insights/successor", "2026-08-19"
    )

    fm, _body, _fm_text = parse_frontmatter(marked)
    assert fm["status"] == "superseded"
    assert any("successor" in str(link) for link in fm["superseded_by"])
    assert _endings(marked) == {newline}


@newlines
def test_the_drift_guard_accepts_the_hash_get_just_handed_out(
    vault: Path, newline: str
) -> None:
    """`get` -> `edit(expected_hash=...)` must round-trip on any page.

    `content_hash` is defined as the sha256 of a file's full raw text, and
    `get_page` hands out exactly that, but `edit` hashed the newline-normalized
    form before comparing. On a CRLF page the two never agreed, so the guard
    reported STALE_EDIT when nothing had changed -- and re-reading returned the
    same raw hash, leaving the page permanently un-editable through the guarded
    path with no way for a caller to recover.
    """
    _seed(vault, newline)

    got = commands.op_get(vault, path=NOTE_REL)
    commands.op_edit(
        vault,
        path=NOTE_REL,
        new_body=got["body"] + "\nappended line\n",
        expected_hash=got["content_hash"],
        why="drift guard round-trip probe",
    )

    assert "appended line" in (vault / NOTE_REL).read_bytes().decode("utf-8")


def _settle_indexes(vault: Path) -> list[Path]:
    """Bring the vault's indexes to a fixed point and return the ones present."""
    kb = vault / "Knowledge Base"
    top = kb / "index.md"
    for _ in range(4):
        current_top = top.read_bytes().decode("utf-8")
        writes, new_top = indexes_module.compute_subindex_writes(
            vault, top_index_text=current_top
        )
        for write in writes:
            write.path.write_bytes(write.content.encode("utf-8"))
        if new_top is not None:
            top.write_bytes(new_top.encode("utf-8"))
        if not writes and new_top == current_top:
            break
    else:  # pragma: no cover - a non-converging refresh is itself the bug
        raise AssertionError("index refresh never reached a fixed point")
    candidates = [
        top,
        kb / "Sources" / "index.md",
        kb / "Notes" / "index.md",
        kb / "Entities" / "index.md",
    ]
    return [path for path in candidates if path.exists()]


@newlines
def test_a_settled_index_is_not_rewritten_just_for_its_line_ending(
    vault: Path, newline: str
) -> None:
    """Index refresh is also the change detector; it must be ending-agnostic.

    Every splice in `indexes` is written against LF, and CRLF text does not
    make one fail -- `find("\\n## ")` matches the LF half of the pair and the
    surrounding `rstrip()` eats the `\\r`. The regenerated rows then come back
    LF-only, mixing both endings into one index. Because the caller only writes
    `if new != base`, that also made an index whose counts were already correct
    differ from what was read, so every maintenance pass rewrote every index on
    a CRLF vault while reporting `files_rewritten: 0`.
    """
    targets = _settle_indexes(vault)
    assert targets, "fixture vault has no indexes to exercise"
    for path in targets:
        logical = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
        path.write_bytes(logical.replace("\n", newline).encode("utf-8"))

    before = {path: path.read_bytes() for path in targets}
    top = vault / "Knowledge Base" / "index.md"

    writes, new_top = indexes_module.compute_subindex_writes(
        vault, top_index_text=top.read_bytes().decode("utf-8")
    )

    assert writes == []
    assert new_top == top.read_bytes().decode("utf-8")
    assert {path: path.read_bytes() for path in targets} == before
    for path in targets:
        assert _endings(path.read_bytes().decode("utf-8")) == {newline}
