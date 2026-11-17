"""Whether a new page's name is taken, answered with no extra vault scan.

A stem match beats a title match when a bare wikilink resolves, so a page named by another page's
title would take over every bare link to it. `unique_path` numbers such a name when it is given
`TitleCheck.held`. In a `root` vault a derived name is also unique across the vault, so
`TitleCheck.unique_page_path` asks `name_held` for the file names of every owned page.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path

from . import find, layout, readiness
from .held_fs import vault_name_key
from .vault import (
    WikilinkResolver,
    derives_title_name,
    in_excluded_scan_dir,
    title_filename,
    title_key,
    unique_path,
    unique_vault_path,
    wikilink_candidates,
    writer_link_visibility,
)
from .vault_store.mode import store_mode_for_reads
from .vault_store.read import snapshot

#: The warning a write carries when no title index could answer. Same shape as
#: `duplicates.unavailable_warning`: a code, then what could not run and what the write did instead.
UNAVAILABLE_WARNING = (
    "title_check_unavailable: the title index could not answer, so this file name came from its "
    "folder alone; if another page has this title, a bare link to it now reaches this page"
)


def name_holder(
    vault_root: Path, resolver: Callable[[], WikilinkResolver | None]
) -> Callable[[str], bool]:
    """For one write, the question whether an owned page anywhere in the vault has a file name, under `vault_name_key`.

    A file-mode vault answers from the link index of the resolver that `resolver` returns. A store-mode
    vault reads the store's page paths once, at the first question, and answers every later candidate
    name from that single read: the store has no name index, so a question per candidate would be a pass
    per candidate. Neither reads the vault's folders. Every page counts, whatever the writer may see: a
    name that a withheld page holds would make a bare link ambiguous for its owner, and the number tells
    the writer only that the name is taken.
    """
    taken: frozenset[str] | None = None

    def held(file_name: str) -> bool:
        nonlocal taken
        key = vault_name_key(file_name.removesuffix(".md"))
        if store_mode_for_reads(vault_root):
            if taken is None:
                with snapshot(vault_root) as view:
                    taken = frozenset(
                        name
                        for name, paths in view.name_buckets().items()
                        if any(not in_excluded_scan_dir(path) for path in paths)
                    )
            return key in taken
        answerer = resolver()
        return answerer is not None and bool(answerer.link_names().index.get(key))

    return held


def page_index_source(vault_root: Path) -> Callable[[], WikilinkResolver] | None:
    """The page index that a file-mode `root` vault checks names against, built once per source, else None.

    A store-mode vault answers a name from the store, and a `nested` vault checks no name across folders.
    A file-mode `root` vault has no stored name index, so it reads the writer snapshot: the same snapshot that a
    link-bearing write takes, forked from the resident index when it is current and built from disk when
    it is not.
    """
    root = Path(vault_root)
    if layout.of(root) is not layout.Layout.ROOT or store_mode_for_reads(root):
        return None
    return functools.cache(lambda: find.writer_resolver_snapshot(root))


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

    A title counts only when its page is visible to the writer, by the filter that the writer's own
    links resolve over (`writer_link_visibility`), so a restricted writer names its page as if the
    withheld pages were absent. A title anywhere in the vault is a wider answer than a same-folder
    file name, and a numbered name must not tell a restricted writer that a withheld title exists.
    """

    def __init__(
        self,
        vault_root: Path,
        slug: str | None,
        *,
        frozen: bool = False,
        resolver: Callable[[], WikilinkResolver] | None = None,
        dated: bool = False,
        title: str | None = None,
    ) -> None:
        self._root = Path(vault_root)
        # A name derived under `title` style. `title` says whether it gave a file name: a title with none
        # takes a slug name. A `frozen` target skips the title rule but still needs a unique name.
        derived = derives_title_name(slug, vault_root, dated=dated)
        self._applies = not frozen and derived
        self._derived = derived and (title is None or title_filename(title) is not None)
        self._source = resolver or (page_index_source(self._root) if self.vault_wide else None)
        self._asked = False
        self._answerer: WikilinkResolver | None = None
        self._visible: Callable[[str], bool] | None = None
        self._name_holder: Callable[[str], bool] | None = None

    def _resolver(self) -> WikilinkResolver | None:
        if not self._asked:
            self._asked = True
            self._visible = writer_link_visibility(self._root)
            if self._source is not None:
                self._answerer = self._source()
            else:
                # Given the writer's view, a resident index that lags only by withheld pages still answers,
                # so a withheld write never decides whether this write carries the warning.
                self._answerer = find.borrow_query_resolver(
                    self._root, self._visible, schedule_rebuild=readiness.runtime_managed()
                )
        return self._answerer

    def held(self, stem: str) -> bool:
        if not self._applies:
            return False
        resolver = self._resolver()
        if resolver is None:
            return False
        key = title_key(stem)
        return bool(
            wikilink_candidates(
                resolver.candidates_by_key, resolver.titles.get(key, ()), title_key=key, visible=self._visible
            )
        )

    @property
    def vault_wide(self) -> bool:
        """Whether this page's derived name is unique across the vault: a title-derived name in a `root` vault."""
        return self._derived and layout.of(self._root) is layout.Layout.ROOT

    def name_held(self, file_name: str) -> bool:
        """Whether an owned page anywhere in the vault has this file name; see `name_holder`."""
        if self._name_holder is None:
            self._name_holder = name_holder(self._root, self._resolver)
        return self._name_holder(file_name)

    def unique_page_path(
        self, directory: Path, stem: str, suffix: str = ".md", *, date_prefix: str = ""
    ) -> Path:
        """The path for a new page's name: `unique_path`'s, or in a `root` vault the vault-wide rule's.

        `date_prefix` is the part of `stem` that comes before the name, which the byte cap counts.
        """
        if self.vault_wide:
            return unique_vault_path(
                directory,
                stem,
                suffix,
                vault_root=self._root,
                date_prefix=date_prefix,
                name_held=self.name_held,
                title_held=self.held,
            )
        return unique_path(directory, stem, suffix, vault_root=self._root, title_held=self.held)

    @property
    def warning(self) -> str | None:
        """`UNAVAILABLE_WARNING` when a title name was chosen and no source answered, else None."""
        return UNAVAILABLE_WARNING if self._applies and self._asked and self._answerer is None else None
