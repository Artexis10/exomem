"""Obsidian's wikilink resolution, and the shortest link text that it resolves to a single file.

Obsidian reads `[[Name]]` against the basenames of the Markdown files it sees, and
`[[group/Name]]` against the ends of their paths. A link text that two files satisfy
resolves to neither of them for a writer that needs the link to be stable, unless the text
is a file's full path, which names that file whatever else ends with it. These pure
functions encode that rule over an index, so a planner, a writer and a verifier share it:

- the index maps `basename_key` of a file to the paths of every file with that basename
  (`build_index` builds it for Markdown files, `build_attachment_index` for every other file,
  which a link names by its full file name, extension included);
- `resolve_link` applies the rule to a link text: the exact vault path first, then the
  strict union of basename and trailing-path matches, and `IndexResolver` does so for many
  link texts over a single index;
- `link_form` returns the shortest link text that resolves to a given file, or its full
  vault path when no shorter text is unique, and `link_forms` does so for every file of an
  index in a single pass;
- `page_text` is the text that a link uses for a file, and `link_text` puts a page text back
  into a link, keeping its anchor and alias.

A link text that ends in `.md` carries the Markdown extension, which Obsidian reads as implied:
`[[notes.md]]` and `[[notes]]` name the same file. A file whose own name is `notes.md.md` is
therefore named by `notes.md.md` and by no shorter text.

Nothing here reads a file or the vault. Paths are relative to Obsidian's vault root and
use `/`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .held_fs import vault_name_key

# Obsidian's documented behaviour fixes the Markdown extension that a link leaves implicit.
MARKDOWN_SUFFIX = ".md"

#: The basename index: `basename_key` of a file, mapped to every visible Markdown path with it.
Index = Mapping[str, Sequence[str]]


class Resolution(StrEnum):
    """What a link text does against an index. Obsidian's rule fixes these three outcomes."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class LinkMatch:
    """The files a link text names. A single path means the link resolves."""

    outcome: Resolution
    paths: tuple[str, ...] = ()

    @property
    def path(self) -> str | None:
        """The single file the link resolves to, or None."""
        return self.paths[0] if self.outcome is Resolution.RESOLVED else None


def _without_suffix(text: str) -> str:
    return text[: -len(MARKDOWN_SUFFIX)] if text.lower().endswith(MARKDOWN_SUFFIX) else text


def page_text(path: str) -> str:
    """The text that names the Markdown file at `path`: its path without `.md`.

    A file whose stem itself ends in `.md` keeps its extension in the text, because a link text
    loses a single trailing `.md` before it is read, and the text without it would name another file.
    A file that is not Markdown keeps its full name.
    """
    stem = _without_suffix(path)
    return path if stem.lower().endswith(MARKDOWN_SUFFIX) else stem


def basename_key(path: str) -> str:
    """The index key of a file path or a link text: its last segment without `.md`, under `vault_name_key`."""
    return vault_name_key(_without_suffix(path).rsplit("/", 1)[-1])


def visible(path: str) -> bool:
    """Whether Obsidian lists a file: a Markdown file with no dot-named folder or name above it.

    Machinery folders such as a trash folder still count, because Obsidian shows what they hold.
    """
    segments = path.split("/")
    # Obsidian's documented behaviour: it hides a file or folder whose name starts with a dot.
    hidden = any(segment.startswith(".") for segment in segments)
    return path.lower().endswith(MARKDOWN_SUFFIX) and not hidden


def visible_attachment(path: str) -> bool:
    """Whether Obsidian lists a file that is not Markdown: no dot-named folder or name above it."""
    return not path.lower().endswith(MARKDOWN_SUFFIX) and not any(
        segment.startswith(".") for segment in path.split("/")
    )


def names_attachment(page: str) -> bool:
    """Whether the page part of a link text ends in a file extension other than `.md`.

    Obsidian reads such a text as the full name of a file, and `.md` is only implied for a
    text without an extension. A text that this accepts may still name a Markdown file whose title holds
    a dot, so a caller tries the attachments first and falls back to the Markdown files.
    """
    name = page.strip().rsplit("/", 1)[-1]
    stem, dot, extension = name.rpartition(".")
    return bool(stem and dot and extension) and extension.lower() != MARKDOWN_SUFFIX[1:]


def build_attachment_index(paths: Iterable[str]) -> dict[str, list[str]]:
    """The basename index of the non-Markdown files Obsidian would list among `paths`; its keys keep the extension."""
    index: dict[str, list[str]] = {}
    seen: set[str] = set()
    for path in paths:
        if path in seen or not visible_attachment(path):
            continue
        seen.add(path)
        index.setdefault(basename_key(path), []).append(path)
    return index


def build_index(paths: Iterable[str]) -> dict[str, list[str]]:
    """The basename index of the files Obsidian would list among `paths`. A repeated path counts once."""
    index: dict[str, list[str]] = {}
    seen: set[str] = set()
    for path in paths:
        if path in seen or not visible(path):
            continue
        seen.add(path)
        index.setdefault(basename_key(path), []).append(path)
    return index


class IndexResolver:
    """`resolve_link` over a single index, for a caller that resolves many link texts.

    The first query into a basename group keys that group once, so each later query is a
    lookup. A group of n files that is queried n times costs n steps, where `resolve_link`
    alone costs n squared.
    """

    def __init__(self, index: Index) -> None:
        self._index = index
        self._keyed: dict[str, tuple[dict[str, set[str]], dict[str, set[str]]]] = {}

    def _group(self, key: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """The group's files by full path key, and by every shorter trailing path key at a folder boundary."""
        found = self._keyed.get(key)
        if found is None:
            exact: dict[str, set[str]] = {}
            tails: dict[str, set[str]] = {}
            for path in self._index.get(key, ()):
                parts = vault_name_key(_without_suffix(path)).split("/")
                exact.setdefault("/".join(parts), set()).add(path)
                for depth in range(1, len(parts)):
                    tails.setdefault("/".join(parts[-depth:]), set()).add(path)
            found = self._keyed[key] = (exact, tails)
        return found

    def resolve(self, page: str) -> LinkMatch:
        """Apply Obsidian's rule to the page part of a link text (no `#anchor`, no `|alias`).

        The exact vault path comes first: a text that, with `.md` implied, names a file's full
        path resolves to that file. Without an exact match, the text names every file whose
        path ends with it at a folder boundary; a bare name names every file with that
        basename. Both compare under `vault_name_key`. A single file resolves; none is
        unresolved; several are ambiguous, whichever file Obsidian itself would prefer, so a
        writer never relies on that.
        """
        text = _without_suffix(page.strip())
        if not text:
            return LinkMatch(Resolution.UNRESOLVED)
        # `text` has already lost its single `.md`; a stem that still ends in `.md` is part of the file name.
        exact, tails = self._group(vault_name_key(text.rsplit("/", 1)[-1]))
        key = vault_name_key(text)
        found = tuple(sorted(exact.get(key) or tails.get(key) or ()))
        if not found:
            return LinkMatch(Resolution.UNRESOLVED)
        return LinkMatch(Resolution.RESOLVED if len(found) == 1 else Resolution.AMBIGUOUS, found)


def resolve_link(page: str, index: Index) -> LinkMatch:
    """`IndexResolver.resolve` for a single link text; use `IndexResolver` to resolve many."""
    return IndexResolver(index).resolve(page)


@dataclass(frozen=True, slots=True)
class LinkForm:
    """The link text of a file. `full_path` says that no shorter text is unique, so `text` is the full vault path."""

    text: str
    full_path: bool = False


def _group_forms(group: Iterable[str]) -> dict[str, LinkForm]:
    """The link form of every path in a basename group, from a single pass over the group."""
    paths = list(dict.fromkeys(group))
    keys = {path: vault_name_key(_without_suffix(path)).split("/") for path in paths}
    # A trailing path names every file whose path ends with it; a form is unique when it names a single file.
    holders = Counter(
        "/".join(parts[-depth:]) for parts in keys.values() for depth in range(1, len(parts) + 1)
    )
    forms: dict[str, LinkForm] = {}
    for path in paths:
        segments = page_text(path).split("/")
        unique = next(
            (
                "/".join(segments[-depth:])
                for depth in range(1, len(segments) + 1)
                if holders["/".join(keys[path][-depth:])] == 1
            ),
            None,
        )
        # The full vault path always names its own file first, so it is the form that never fails.
        forms[path] = (
            LinkForm(unique) if unique is not None else LinkForm("/".join(segments), full_path=True)
        )
    return forms


def link_forms(index: Index) -> dict[str, LinkForm]:
    """`link_form` of every file in the index, at the cost of a single pass over each basename group.

    A planner that needs the form of many files calls this once; `link_form` per file would
    read the whole group each time, which grows with the square of a large group.
    """
    forms: dict[str, LinkForm] = {}
    for group in index.values():
        forms.update(_group_forms(group))
    return forms


def link_form(target: str, index: Index) -> LinkForm:
    """The shortest trailing path of `target`, cut at a folder boundary, that names only `target`.

    `Name` when the basename is unique, else `group/Name`, then longer. When another path ends
    with all of the target's own, no trailing path is unique, and the form is the target's full
    vault path, which resolves by exact match (`LinkForm.full_path`). It reads only the target's
    own basename group. Raises `ValueError` when `target` is not in its group, which means the
    index was built without it. Each call reads the whole group, so use `link_forms` for many files.
    """
    group = index.get(basename_key(target), ())
    if target not in group:
        raise ValueError(f"{target!r} is not in the index group of its basename")
    return _group_forms(group)[target]


def link_text(inner: str, page: str) -> str:
    """The text between `[[` and `]]` with its page part replaced by `page`.

    The `#anchor` and the `|alias` keep their bytes. Whitespace around the old page part goes.
    """
    # The wikilink grammar fixes these two characters as the end of the page part.
    cut = next(
        (position for position, character in enumerate(inner) if character in "#|"), len(inner)
    )
    return page + inner[cut:]
