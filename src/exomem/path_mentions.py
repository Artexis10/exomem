"""Pages that name a moved file by its vault path, and the rewrite that follows the move.

A move keeps every `[[wikilink]]` true, but a page that cites the path in prose (a code span,
a plain sentence, a markdown link target) keeps naming the old location. This module finds
those pages through the maintained lexical catalogue, so a move never walks the vault, and
rewrites the exact old path in the page body to the new one.

Only the body changes, and never text inside a recorded origin carrier. A page whose body is
immutable (a Source or Evidence page) is counted and left as captured. A frontmatter pointer is
not a body mention: the moved page's own pointers follow `move_file._repoint_artifact`, and the
other cards are found by the `source_pointer_drift` audit.

Known limits: a mention written with Windows separators or percent-encoded (`%20`) is not
recognised, and a mention inside a URL (a query string, say) is left as written.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from . import provenance
from .kbdir import managed_join, managed_relative
from .vault import _FM_PATTERN

#: The warning a move or a reclassification gives when the catalogue could not answer.
NOT_CHECKED = (
    "path mentions were not checked: the search index is not current, so pages that cite "
    "the old path in prose keep it"
)
# The characters that open a token in prose or markdown, where a URL before a mention starts.
_TOKEN_OPENERS = " \t\n(<[\"'"


@dataclass(frozen=True)
class Lookup:
    """The pages the catalogue says may name a path, and whether it could answer."""

    pages: tuple[str, ...]
    #: False when no maintained index could answer; `pages` then holds only what it did know.
    complete: bool
    unsupported: bool = False
    overflow: bool = False


@dataclass(frozen=True)
class Rename:
    """One moved path, as the mention rewrite reads it."""

    old: str
    new: str
    #: What follows the old name in each longer name beside it (`" copy.json"` for
    #: `tides.json copy.json`), so a mention of that other file is never split.
    longer: tuple[str, ...] = ()


def renames(vault_root: Path, moves: Sequence[tuple[str, str]]) -> tuple[Rename, ...]:
    """Each `(old, new)` move with the longer names its old folder holds, read once per move."""
    found: list[Rename] = []
    for old_rel, new_rel in moves:
        old = PurePosixPath(old_rel)
        try:
            with os.scandir(Path(vault_root) / old.parent) as entries:
                longer = tuple(
                    entry.name[len(old.name):]
                    for entry in entries
                    if entry.name != old.name and entry.name.startswith(old.name)
                )
        except OSError:
            longer = ()
        found.append(Rename(old_rel, new_rel, longer))
    return tuple(found)


def find_pages(
    vault_root: Path, paths: Sequence[str], *, current_published: bool = False,
    candidate_limit: int | None = None,
) -> Lookup:
    """Pages whose body may name any of `paths`; each is verified against its bytes at rewrite.

    The query is the path without the knowledge-base folder, a substring of both spellings a
    page may use. A managed preview uses a current publication on its request budget.
    A candidate limit also selects the published reader and can return overflow.
    """
    from . import lexstore

    queries = [managed_relative(rel).lower() for rel in paths]
    answer = (
        lexstore.search_current_published_substrings(vault_root, queries, candidate_limit=candidate_limit)
        if current_published or candidate_limit is not None
        else lexstore.search_substrings_result(vault_root, queries, scope="kb")
    )
    if not answer.readiness.complete or answer.value is None:
        return Lookup(
            (), False, unsupported=answer.readiness.status == "unsupported",
            overflow=answer.readiness.status == "overflow",
        )
    return Lookup(tuple(sorted({rel for found in answer.value.values() for rel in found})), True)


def _pattern(old_rel: str) -> re.Pattern[str]:
    spellings = {old_rel, managed_relative(old_rel)}
    alternatives = "|".join(re.escape(form) for form in sorted(spellings, key=len, reverse=True))
    # Boundary only: the old path must not sit inside a longer path or file name. Which text may
    # change is decided by the body split, the carrier spans and the checks below, not by this pattern.
    return re.compile(rf"(?<![\w/\\.-])({alternatives})(?![\w/\\-]|\.\w)")


def _in_url(text: str, start: int) -> bool:
    """Whether the token that holds `start` begins with a URL, so the path is part of that URL."""
    token_start = max(text.rfind(opener, 0, start) for opener in _TOKEN_OPENERS) + 1
    parts = urlsplit(text[token_start:start])
    return bool(parts.scheme and parts.netloc)


def rewrite(text: str, moves: Sequence[Rename], *, apply: bool = True) -> tuple[str, int]:
    """`text` with each moved path in its body renamed, and how many mentions that is.

    A mention keeps the spelling it used: the full path becomes the full new path, and a path
    without the knowledge-base folder becomes the new path without it. With `apply=False` the
    text is returned as it is and the count says how many mentions it holds.
    """
    total = 0
    for move in moves:
        if move.old == move.new:
            continue  # a mention of a path that keeps its name changes no bytes
        renamed, count = _rename(text, move)
        total += count
        if apply:
            text = renamed
    return text, total


def _rename(text: str, move: Rename) -> tuple[str, int]:
    prefix = managed_join("")
    frontmatter = _FM_PATTERN.match(text)
    body_start = frontmatter.start(2) if frontmatter else 0
    inside_carrier = provenance.in_carrier(text)
    count = 0

    def renamed(match: re.Match[str]) -> str:
        nonlocal count
        if (
            match.start() < body_start
            or inside_carrier(match.start(), match.end())
            or any(text.startswith(rest, match.end()) for rest in move.longer)
            or _in_url(text, match.start())
        ):
            return match.group(0)
        count += 1
        return move.new if match.group(1).startswith(prefix) else move.new.removeprefix(prefix)

    return _pattern(move.old).sub(renamed, text), count
