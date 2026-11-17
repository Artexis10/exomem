"""Whether a new page's name is another live page's title, answered with no extra vault scan.

A stem match beats a title match when a bare wikilink resolves, so a page named by another page's
title would take over every bare link to it. `unique_path` numbers such a name when it is given
`TitleCheck.held`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import find, readiness
from .vault import WikilinkResolver, derives_title_name, title_key

#: The warning a write carries when no title index could answer. Same shape as
#: `duplicates.unavailable_warning`: a code, then what could not run and what the write did instead.
UNAVAILABLE_WARNING = (
    "title_check_unavailable: the title index could not answer, so this file name came from its "
    "folder alone; if another page has this title, a bare link to it now reaches this page"
)


class TitleCheck:
    """The title lookup for one write, and whether it could be answered.

    `held(stem)` says whether the stem is a live page's title. It is False, and asks nothing, when no
    title-derived name is being chosen: an explicit `slug`, a slug-style vault, or a `frozen` name (a
    draft token or an import plan fixed its target, so no live title may move it).

    The answer comes from the first source that has one, and never from a new vault scan:
    1. `resolver`, a callable for the writer snapshot that this write builds anyway for its own links.
       It is called on the first question only, so a refusal that comes first never pays for it.
    2. The title index the running process keeps resident, borrowed and never built. A managed
       service that finds none schedules the one background rebuild, so the next write is checked; a
       one-shot command never does, because its process ends before the build would.

    A write that neither source could answer names the page from its folder alone, and `warning` says so.
    """

    def __init__(
        self,
        vault_root: Path,
        slug: str | None,
        *,
        frozen: bool = False,
        resolver: Callable[[], WikilinkResolver] | None = None,
    ) -> None:
        self._root = Path(vault_root)
        self._applies = not frozen and derives_title_name(slug, vault_root)
        self._source = resolver
        self._asked = False
        self._answerer: WikilinkResolver | None = None

    def _resolver(self) -> WikilinkResolver | None:
        if not self._asked:
            self._asked = True
            if self._source is not None:
                self._answerer = self._source()
            else:
                self._answerer = find.borrow_query_resolver(
                    self._root, schedule_rebuild=readiness.runtime_managed()
                )
        return self._answerer

    def held(self, stem: str) -> bool:
        if not self._applies:
            return False
        resolver = self._resolver()
        return resolver is not None and bool(resolver.titles.get(title_key(stem)))

    @property
    def warning(self) -> str | None:
        """`UNAVAILABLE_WARNING` when a title name was chosen and no source answered, else None."""
        return UNAVAILABLE_WARNING if self._applies and self._asked and self._answerer is None else None
