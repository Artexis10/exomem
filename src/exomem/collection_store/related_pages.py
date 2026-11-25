"""The pages the knowledge base already holds about a new row's subject, returned with the write.

A row's subject is named by its natural-key text values and its presentation title field. One batched
lookup of those names against the activation index (every page's title, stem and aliases) finds the pages
that bear them, and the write reply lists a few of them so the agent reads them before it writes an
unknown or a placeholder. The server does not judge whether a written value means "unknown".

The writer's own visibility decides each page, so a page the writer may not read answers exactly as a
missing one. The index is read as stored: a lookup never refreshes it or walks the vault, and a page
captured moments ago may not be listed yet.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .. import find_corpus, memory_refs, working_set_index

#: Pages one new row lists and pages one reply lists. Bounds on a reply's size, not a vocabulary.
PER_ROW = 3
PER_REPLY = 30
UNAVAILABLE = "RELATED_PAGES_UNAVAILABLE"


def unavailable_warning(cause: str) -> str:
    """The warning that says no lookup ran, so that a reply with no `related_pages` never reads as "no pages"."""
    return f"{UNAVAILABLE}: {cause}, so no related pages were looked up."


def subject_fields(manifest) -> list[str]:
    """The fields whose text values name what a row is about: its natural-key fields, then its presentation title field.

    A link field is no name: it holds a page reference, or an anchored one (`LINK_ANCHORED`) that a write kept as written.
    """
    fields = list(manifest.schema.natural_key)
    presentation = manifest.item_presentation
    if presentation is not None and presentation.title.field not in fields:
        fields.append(presentation.title.field)
    return [name for name in fields if getattr(manifest.schema.fields.get(name), "type", None) != "link"]


def subject_names(manifest, values: Mapping[str, Any]) -> tuple[str, ...]:
    """The text values of `subject_fields`, trimmed and without repeats."""
    names = (values.get(name) for name in subject_fields(manifest))
    return tuple(dict.fromkeys(value.strip() for value in names if type(value) is str and value.strip()))


class RelatedPages:
    """One write request's related-page lookup, shared by every new row of a bulk request."""

    def __init__(self, writer, manifest):
        self._root = writer.root
        self._manifest = manifest
        self._allows = writer._operation.allows_file
        # item key -> the visible pages that row's subject names, at most PER_ROW
        self._found: dict[str, list[dict[str, str]]] = {}
        self._unavailable = False
        self._pages: dict[str, dict[str, str] | None] = {}

    def collect(self, rows: Iterable[tuple[str, Mapping[str, Any]]]) -> None:
        """Resolve every row's subject names in one lookup and keep the visible pages, in row order."""
        names = {key: subject_names(self._manifest, values) for key, values in rows}
        wanted = [name for spellings in names.values() for name in spellings]
        if not wanted:
            return
        index = working_set_index.WorkingSetIndex(self._root)
        try:
            # An index nothing has built is silent about every name: that is unknown, not "no pages".
            resolved = index.resolve_names(wanted) if index.names_built() else None
        except (working_set_index.WorkingSetIndexUnavailable, sqlite3.Error, OSError):
            resolved = None
        finally:
            index.close()
        if resolved is None:
            self._unavailable = True
            return
        shown = 0
        for key, spellings in names.items():
            if shown >= PER_REPLY:
                break
            pages: list[dict[str, str]] = []
            for name in spellings:
                for path in resolved.get(working_set_index.normalize(name), ()):
                    page = self._page(path)
                    if page is not None and page not in pages:
                        pages.append(page)
                    if len(pages) == PER_ROW:
                        break
                if len(pages) == PER_ROW:
                    break
            if pages:
                self._found[key] = pages
                shown += len(pages)

    def notes(self, created: Iterable[str] | None = None) -> dict[str, Any]:
        """What the write reply adds for the rows the write created: `related_pages` and a failed-lookup warning."""
        made = None if created is None else set(created)
        rows, count = [], 0
        for key, pages in self._found.items():
            room = PER_REPLY - count
            if room <= 0:
                break
            if made is None or key in made:
                rows.append({"item_key": key, "pages": pages[:room]})
                count += len(pages[:room])
        notes: dict[str, Any] = {}
        if rows:
            notes["related_pages"] = rows
        if self._unavailable and (made is None or made):
            notes["warnings"] = [unavailable_warning("the page index could not be read")]
        return notes

    def _page(self, path: str) -> dict[str, str] | None:
        """The title and stable ref of one page this writer may read, or None; a page without a stable ref is not listed."""
        if path not in self._pages:
            self._pages[path] = self._read(path) if self._allows(path) else None
        return self._pages[path]

    def _read(self, path: str) -> dict[str, str] | None:
        page = find_corpus.CACHE.get(self._root / path, self._root)
        identity = memory_refs.normalize_id(page.frontmatter.get(memory_refs.ID_FIELD)) if page is not None else None
        return None if identity is None else {"title": page.title, "ref": memory_refs.memory_ref(identity)}


def for_write(writer, manifest, rows: Sequence[tuple[str, Mapping[str, Any]]]) -> RelatedPages | None:
    """The related pages of the rows a Records write may create, or None; call it while the write's authorization is live.

    A Planning item names work, not a subject, so only the two profiles' closed protocol value `records` takes the lookup.
    """
    if not rows or manifest.semantic_profile != "records":
        return None
    related = RelatedPages(writer, manifest)
    related.collect(rows)
    return related
