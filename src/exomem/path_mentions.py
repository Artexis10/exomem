"""Pages that name a moved file by its vault path, and the rewrite that follows the move.

A move keeps every `[[wikilink]]` true, but a page that cites the path in prose (a code span,
a plain sentence, a markdown link target) keeps naming the old location. This module finds
those pages through the maintained lexical catalogue, so a move never walks the vault, and
rewrites the exact old path in the page body to the new one.

Only the body changes, and never text inside a recorded origin carrier. A page whose body is
immutable (a Source or Evidence page) is counted and left as captured. A frontmatter pointer is
not a body mention: the moved page's own pointers follow `move_file._repoint_artifact`, and the
other cards are found by the `source_pointer_drift` audit.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import provenance
from .kbdir import kb_prefix
from .vault import _FM_PATTERN


@dataclass(frozen=True)
class Lookup:
    """The pages the catalogue says may name a path, and whether it could answer."""

    pages: tuple[str, ...]
    #: False when no maintained index could answer; `pages` then holds only what it did know.
    complete: bool


def find_pages(vault_root: Path, paths: Sequence[str]) -> Lookup:
    """Pages whose body may name any of `paths`; each is verified against its bytes at rewrite.

    The query is the path without the knowledge-base folder, a substring of both spellings a
    page may use. The catalogue answers from its rows; when it is not current, one repairing
    query runs, the same one a search makes. A page it cannot answer for is never guessed.
    """
    from . import lexstore

    found: set[str] = set()
    complete = True
    for rel in paths:
        query = rel.removeprefix(kb_prefix()).lower()
        answer = lexstore.search_substring_result(vault_root, query, scope="kb")
        pages = answer.value if answer.readiness.complete else None
        if pages is None:
            pages = lexstore.search_substring(vault_root, query, scope="kb", repair=True)
        if pages is None:
            complete = False
            continue
        found.update(pages)
    return Lookup(tuple(sorted(found)), complete)


def _pattern(old_rel: str) -> re.Pattern[str]:
    prefix = kb_prefix()
    spellings = {old_rel, old_rel.removeprefix(prefix)}
    alternatives = "|".join(re.escape(form) for form in sorted(spellings, key=len, reverse=True))
    # Boundary only: the old path must not sit inside a longer path or file name. Which text may
    # change is decided by the body split and the carrier spans below, not by this pattern.
    return re.compile(rf"(?<![\w/\\.-])({alternatives})(?![\w/\\-]|\.\w)")


def rewrite(
    text: str, moves: Sequence[tuple[str, str]], *, apply: bool = True
) -> tuple[str, int]:
    """`text` with each moved path in its body renamed, and how many mentions that is.

    A mention keeps the spelling it used: the full path becomes the full new path, and a path
    without the knowledge-base folder becomes the new path without it. With `apply=False` the
    text is returned as it is and the count says how many mentions it holds.
    """
    total = 0
    for old_rel, new_rel in moves:
        if old_rel == new_rel:
            continue  # a mention of a path that keeps its name changes no bytes
        renamed, count = _rename(text, old_rel, new_rel)
        total += count
        if apply:
            text = renamed
    return text, total


def _rename(text: str, old_rel: str, new_rel: str) -> tuple[str, int]:
    prefix = kb_prefix()
    frontmatter = _FM_PATTERN.match(text)
    body_start = frontmatter.start(2) if frontmatter else 0
    inside_carrier = provenance.in_carrier(text)
    count = 0

    def renamed(match: re.Match[str]) -> str:
        nonlocal count
        if match.start() < body_start or inside_carrier(match.start(), match.end()):
            return match.group(0)
        count += 1
        return new_rel if match.group(1).startswith(prefix) else new_rel.removeprefix(prefix)

    return _pattern(old_rel).sub(renamed, text), count
