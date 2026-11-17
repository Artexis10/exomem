"""Whether a new page's name is another live page's title, answered with no vault scan.

A stem match beats a title match when a bare wikilink resolves, so a page named by another page's
title would take over every bare link to it. `unique_path` numbers such a name when it is given
the lookup that this module builds.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import find
from .vault import derives_title_name, title_key

#: The warning a write carries when no resident title index could answer. Same shape as
#: `duplicates.unavailable_warning`: a code, then what could not run and what the write did instead.
UNAVAILABLE_WARNING = (
    "title_check_unavailable: the title index could not answer, so this file name came from its "
    "folder alone; if another page has this title, a bare link to it now reaches this page"
)


def occupancy(
    vault_root: Path, slug: str | None, *, frozen: bool = False
) -> tuple[Callable[[str], bool] | None, str | None]:
    """The title lookup for a name that is derived from a title, and the warning when it cannot run.

    Returns `(held, warning)`. `held(stem)` says whether the stem is a live page's title. Both are
    None when no title-derived name is being chosen: an explicit `slug`, a slug-style vault, or a
    `frozen` name (an import plan froze its target, so no live title may move it). When a title name
    is chosen and no index answers, `held` is None and `warning` is `UNAVAILABLE_WARNING`.

    The answer is the title index that the running server keeps resident. Borrowing never builds one,
    so a write scans no vault; a cold process (a one-shot command) or one behind its watcher names the
    page from its folder alone and says so.
    """
    if frozen or not derives_title_name(slug, vault_root):
        return None, None
    resolver = find.borrow_query_resolver(vault_root, schedule_rebuild=False)
    if resolver is None:
        return None, UNAVAILABLE_WARNING
    return (lambda stem: bool(resolver.titles.get(title_key(stem)))), None
