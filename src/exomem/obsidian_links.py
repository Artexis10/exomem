"""Obsidian's wikilink resolution, and the shortest link text that it resolves to a single file.

Obsidian reads `[[Name]]` against the basenames of the Markdown files it sees, and
`[[group/Name]]` against the ends of their paths. A link text that two files satisfy
resolves to neither of them for a writer that needs the link to be stable. These pure
functions encode that rule over an index, so a planner, a writer and a verifier share it:

- the index maps `basename_key` of a file to the paths of every file with that basename
  (`build_index` builds it);
- `resolve_link` applies the rule to a link text;
- `link_form` returns the shortest link text that resolves to a given file, and `link_forms`
  does so for every file of an index in a single pass;
- `link_text` puts that page text back into a link, keeping its anchor and alias.

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


def build_index(paths: Iterable[str]) -> dict[str, list[str]]:
    """The basename index of the files Obsidian would list among `paths`. A repeated path counts once."""
    index: dict[str, list[str]] = {}
    for path in paths:
        if not visible(path):
            continue
        group = index.setdefault(basename_key(path), [])
        if path not in group:
            group.append(path)
    return index


def resolve_link(page: str, index: Index) -> LinkMatch:
    """Apply Obsidian's rule to the page part of a link text (no `#anchor`, no `|alias`).

    A text with `/` names the files whose path equals it or ends with `/<text>`, with `.md`
    implied. A bare name names the files that have that basename. Both compare under
    `vault_name_key`. A single file resolves; none is unresolved; several are ambiguous.
    """
    text = _without_suffix(page.strip())
    group = index.get(basename_key(text), ()) if text else ()
    if "/" in text:
        key = vault_name_key(text)
        tail = "/" + key
        group = [
            path
            for path in group
            if (path_key := vault_name_key(_without_suffix(path))) == key or path_key.endswith(tail)
        ]
    found = tuple(sorted(set(group)))
    if not found:
        return LinkMatch(Resolution.UNRESOLVED)
    return LinkMatch(Resolution.RESOLVED if len(found) == 1 else Resolution.AMBIGUOUS, found)


def _group_forms(group: Iterable[str]) -> dict[str, str | None]:
    """The link form of every path in a basename group, from a single pass over the group."""
    paths = list(dict.fromkeys(group))
    keys = {path: vault_name_key(_without_suffix(path)).split("/") for path in paths}
    # A trailing path names every file whose path ends with it; a form is unique when it names a single file.
    holders = Counter(
        "/".join(parts[-depth:]) for parts in keys.values() for depth in range(1, len(parts) + 1)
    )
    forms: dict[str, str | None] = {}
    for path in paths:
        segments = _without_suffix(path).split("/")
        forms[path] = next(
            (
                "/".join(segments[-depth:])
                for depth in range(1, len(segments) + 1)
                if holders["/".join(keys[path][-depth:])] == 1
            ),
            None,
        )
    return forms


def link_forms(index: Index) -> dict[str, str | None]:
    """`link_form` of every file in the index, at the cost of a single pass over each basename group.

    A planner that needs the form of many files calls this once; `link_form` per file would
    read the whole group each time, which grows with the square of a large group.
    """
    forms: dict[str, str | None] = {}
    for group in index.values():
        forms.update(_group_forms(group))
    return forms


def link_form(target: str, index: Index) -> str | None:
    """The shortest trailing path of `target`, cut at a folder boundary, that resolves to exactly `target`.

    `Name` when the basename is unique, else `group/Name`, then longer. It reads only the
    target's own basename group. None when no trailing path is unique: another file's path
    ends with all of the target's. Raises `ValueError` when `target` is not in its group,
    which means the index was built without it. Each call reads the whole group, so use
    `link_forms` for many files.
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
