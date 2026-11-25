"""Exact mentions of known Entity names in page prose, and the edits that link them.

The vault defines the names: an index is built from the titles and aliases of the Entities the
reader may see, so no word list in code decides what a person meant. A name matches after the
registry's own normalisation (`entity_recurrence.identity_key`), on word boundaries, as the
longest run of words that is a name. Text that is not prose (code, links, images, HTML,
headings, wikilinks) is never read, and a name that two Entities answer to matches nothing.

This module reads text only. Which Entities a page already links, and what a writer may see,
belong to the caller (`capture_sweep.unlinked_mentions`).
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from . import entity_recurrence, markdown_regions

Span = tuple[int, int]

#: How many punctuation characters a word may carry on each side of a name: `(`, `"`, `**`, `,`, `.`.
#: Inside a name the punctuation must match the name exactly.
_MAX_EDGE = 4

#: A word is a run of non-whitespace characters. The class is fixed by Unicode, not a vocabulary.
_WORD = re.compile(r"\S+")


@dataclass(frozen=True, slots=True)
class NameIndex:
    """What the writer's Entities answer to, as normalised word runs."""

    #: A normalised name, its words joined by one space, to the paths of the Entities that answer to it.
    owners: Mapping[str, Sequence[str]]
    #: The first word of a name to the most words in any name that starts with it.
    runs: Mapping[str, int]
    #: A one-word name to its written forms (NFC) on the Entities that answer to it.
    forms: Mapping[str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class Mention:
    """One exact, unambiguous mention of an Entity's name."""

    path: str
    start: int
    end: int
    written: str
    words: int


@dataclass(frozen=True, slots=True)
class Edit:
    """One `replace_text` item: `old` occurs once and `new` links the mention inside it."""

    old: str
    new: str


def build_index(entries: Iterable[entity_recurrence.RegistryEntry], *, max_chars: int) -> NameIndex:
    """The name index of these registry entries: every title and alias, once per Entity.

    A name longer than `max_chars` is left out: it can never be offered, and it would cost scan time.
    One pass over the entries, with no per-name tokenising beyond the first word: it runs once per
    durable page write, so its cost is linear in the Entities the writer can see.
    """
    owners: dict[str, list[str]] = {}
    runs: dict[str, int] = {}
    forms: dict[str, set[str]] = {}
    for entry in entries:
        one_word = False
        for identity in entry.identities:
            if len(identity) > max_chars:
                continue
            # An Entity lists each of its names once, so a second path under a name is a second Entity.
            if (held := owners.get(identity)) is None:
                owners[identity] = [entry.path]
            else:
                held.append(entry.path)
            first, _, rest = identity.partition(" ")
            words = rest.count(" ") + 2 if rest else 1
            one_word = one_word or not rest
            if runs.get(first, 0) < words:
                runs[first] = words
        if one_word:
            for name in entry.names:
                written = name.strip()
                key = entity_recurrence.identity_key(written)
                if key in owners and " " not in key:
                    forms.setdefault(key, set()).add(unicodedata.normalize("NFC", written))
    return NameIndex(owners, runs, {key: frozenset(held) for key, held in forms.items()})


def prose_mask(text: str) -> tuple[Span, ...]:
    """Where `text` is not prose to read: markdown-it's code, links, HTML and headings, and wikilinks.

    Raises `markdown_regions.MarkdownPositionError` when a position cannot be bound; the
    caller offers nothing for that page.
    """
    from . import vault

    spans = [*markdown_regions.inert_spans(text), *(m.span() for m in vault.find_body_wikilinks(text))]
    merged: list[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def find_mentions(text: str, index: NameIndex, mask: Sequence[Span]) -> list[Mention]:
    """The unambiguous name mentions in `text`, in page order, longest match first and never overlapping.

    A name that two Entities answer to takes its words (a shorter name inside it is not read) and
    yields no mention. A name never spans a line break.
    """
    if not index.owners:
        return []
    words = [(found.start(), found.end()) for found in _WORD.finditer(text)]
    starts = [start for start, _end in mask]
    normal: dict[str, str] = {}

    def norm(piece: str) -> str:
        if piece not in normal:
            normal[piece] = entity_recurrence.identity_key(piece)
        return normal[piece]

    def blocked(position: int) -> bool:
        start, end = words[position]
        owner = bisect_right(starts, end - 1) - 1
        # `://` is the shape of a URL, so a bare URL is read as one word that is not prose.
        return (owner >= 0 and mask[owner][1] > start) or "://" in text[start:end]

    mentions: list[Mention] = []
    position = 0
    while position < len(words):
        if blocked(position):
            position += 1
            continue
        first = text[slice(*words[position])]
        leads, trails = _trims(first), _trims(first, trailing=True)
        longest = max(
            (index.runs.get(norm(first[lead : len(first) - trail]), 0)
             for lead in leads for trail in trails if lead + trail < len(first)),
            default=0,
        )
        match = None
        for length in range(min(longest, len(words) - position), 0, -1):
            match = _run_match(text, words, position, length, index, norm, blocked)
            if match is not None:
                break
        if match is None:
            position += 1
            continue
        key, start, end, length = match
        position += length
        paths = index.owners[key]
        if len(paths) == 1:
            (path,) = paths
            mentions.append(Mention(path, start, end, text[start:end], len(key.split())))
    return mentions


def _trims(word: str, *, trailing: bool = False) -> list[int]:
    """How many characters may be cut from one edge of `word`: only punctuation, up to `_MAX_EDGE`.

    A letter, a digit or a combining mark stops the cut: a mark belongs to the letter before it.
    """
    edge = word[::-1] if trailing else word
    cuts = [0]
    for char in edge[:_MAX_EDGE]:
        if char.isalnum() or unicodedata.category(char).startswith("M"):
            break
        cuts.append(cuts[-1] + 1)
    return cuts


def _run_match(text, words, position, length, index, norm, blocked):  # noqa: ANN001 -- one helper of `find_mentions`
    """`(name key, start, end, words used)` of the name that these `length` words spell, or None."""
    last = position + length - 1
    first = text[slice(*words[position])]
    final = text[slice(*words[last])]
    if length == 1:
        for lead in _trims(first):
            for trail in _trims(first, trailing=True):
                if lead + trail >= len(first):
                    continue
                shown = first[lead : len(first) - trail]
                key = norm(shown)
                # A one-word name matches only in its written form: a vault that titles an Entity with a
                # common word (`Signal`) must not link every sentence that uses it. A shape rule over the
                # vault's own names, not a list of words; names of two or more words ignore case.
                if key in index.owners and unicodedata.normalize("NFC", shown) in index.forms.get(key, ()):
                    start, end = words[position]
                    return key, start + lead, end - trail, 1
        return None
    if any(blocked(at) for at in range(position + 1, last + 1)):
        return None
    for at in range(position, last):
        # One name is one line: a break would put the link across two lines.
        if "\n" in text[words[at][1] : words[at + 1][0]]:
            return None
    middle = [norm(text[slice(*words[at])]) for at in range(position + 1, last)]
    if not all(middle):
        return None
    for lead in _trims(first):
        head = norm(first[lead:])
        for trail in _trims(final, trailing=True):
            tail = norm(final[: len(final) - trail])
            key = " ".join((head, *middle, tail))
            if key in index.owners:
                return key, words[position][0] + lead, words[last][1] - trail, length
    return None


def edits_for(
    text: str,
    raw: str,
    mentions: Sequence[Mention],
    replacements: Sequence[str],
    mask: Sequence[Span],
    *,
    max_chars: int,
    shown: Callable[[str], str] | None = None,
) -> list[tuple[int, Edit]]:
    """`(index into mentions, edit)` in the order a `replace_text` call applies them; a mention with no unique anchor is left out.

    Each edit replaces the smallest span around its mention that occurs once in `raw` (the written
    page, frontmatter included, so the count is never lower than the body's own) by that span with the
    mention swapped for its replacement. The span grows one word at a time, within the mention's line
    and between the masked regions around it. Edits run from the last mention to the first, and each
    is counted against the page as the edits before it left it, so no edit can find its text moved
    or doubled by another. A span longer than `max_chars` is never used.

    A surface that shows links by title counts the span there, not in the stored bytes. `shown` maps a
    page to the form that surface reads, and the span must occur once in both.
    """
    current = raw.replace("\r\n", "\n")
    view = shown(current) if shown else None
    limit = len(text)
    edits: list[tuple[int, Edit]] = []
    for at in sorted(range(len(mentions)), key=lambda i: mentions[i].start, reverse=True):
        found = _anchor(text, mentions[at], replacements[at], mask, current, view, limit, max_chars)
        if found is not None:
            edit, limit = found
            current = current.replace(edit.old, edit.new, 1)
            view = shown(current) if shown else None
            edits.append((at, edit))
    return edits


def _anchor(
    text: str, mention: Mention, replacement: str, mask: Sequence[Span], current: str, view: str | None,
    limit: int, max_chars: int,
) -> tuple[Edit, int] | None:
    """The edit for one mention and where its span starts, or None when no span within bounds occurs once.

    `current` is the page in stored bytes and `view` the same page as a title-showing surface reads it.
    """
    line_end = text.find("\n", mention.end)
    low, high = text.rfind("\n", 0, mention.start) + 1, min(len(text) if line_end < 0 else line_end, limit)
    owner = bisect_right([end for _start, end in mask], mention.start) - 1
    if owner >= 0:
        low = max(low, mask[owner][1])
    owner = bisect_left([start for start, _end in mask], mention.end)
    if owner < len(mask):
        high = min(high, mask[owner][0])
    words = [found.span() for found in _WORD.finditer(text, low, high)]
    lefts = [mention.start, *sorted({a for a, _b in words if a < mention.start}, reverse=True)]
    rights = [mention.end, *sorted({b for _a, b in words if b > mention.end})]
    for left, right in _growth(len(lefts), len(rights)):
        old = text[lefts[left] : rights[right]]
        if len(old) > max_chars:
            return None
        if current.count(old) == 1 and (view is None or view.count(old) == 1):
            new = text[lefts[left] : mention.start] + replacement + text[mention.end : rights[right]]
            return Edit(old, new), lefts[left]
    return None


def _growth(lefts: int, rights: int) -> Iterable[tuple[int, int]]:
    """Indexes into the left and right edges, from the mention alone, one more word on each side in turn.

    The right edge grows first: edits run from the last mention, and a later edit that stays to its
    own right leaves the earlier mentions their room.
    """
    left = right = 0
    yield left, right
    while left + 1 < lefts or right + 1 < rights:
        if right + 1 < rights:
            right += 1
            yield left, right
        if left + 1 < lefts:
            left += 1
            yield left, right
