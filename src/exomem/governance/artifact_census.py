"""The census of a vault's files that the artifact reference gate resolves against.

`egress` replaces a reference to a withheld artifact in free text. To find
one, it needs every file path in the vault and the aliases that name each
file. This module keeps that census per vault and lists again only the
directories that changed, so its cost does not grow with the vault while
the vault's names stay the same. It also finds the literal aliases in a
text with set lookups, with the matches of the alternation pattern of every
alias that it replaces.
"""

from __future__ import annotations

import bisect
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

# The stdlib's own table of the extra IGNORECASE pairs. If a future Python drops
# it, the import fails loudly: matching without it would miss case variants.
from re._casefix import _EXTRA_CASES as _SRE_EXTRA_CASES
from types import MappingProxyType
from urllib.parse import unquote

from ..kbdir import kb_dirname

#: A literal alias starts and ends where the text does not continue a path.
_NOT_PATH_CHARACTER = re.compile(r"[^\w./-]")


class _CaseClasses(dict):
    """Each character's representative under `re.IGNORECASE`, built on demand.

    `re` compares two characters case-insensitively by their simple
    lowercase mapping (`str.lower()[:1]`), and also equates the lowercase
    pairs listed in `re._casefix._EXTRA_CASES`, such as `s` and long `s`.
    Mapping each character to the smallest member of that class makes two
    strings equal exactly when `re` would match one against the other.
    """

    def __missing__(self, code: int) -> str:
        lowered = chr(code).lower()[:1] or chr(code)
        group = _SRE_EXTRA_CASES.get(ord(lowered))
        representative = chr(min(ord(lowered), *group)) if group else lowered
        self[code] = representative
        return representative


_CASE_CLASSES = _CaseClasses()


def _case_key(text: str) -> str:
    # Every ASCII character's class representative is its lowercase.
    return text.lower() if text.isascii() else text.translate(_CASE_CLASSES)


def _scan_stops(alias: str) -> Iterator[int]:
    """Where a text that matches `alias` case-insensitively may hold a non-path character.

    That is wherever `alias` holds one, and wherever it holds a cased
    non-ASCII character: a few case classes mix both kinds, such as Greek iota
    with the combining ypogegrammeni. ASCII case classes hold letters only.
    """
    yield from (match.start() for match in _NOT_PATH_CHARACTER.finditer(alias))
    if not alias.isascii():
        for index, character in enumerate(alias):
            if not character.isascii() and (
                character.lower() != character
                or character.upper() != character
                or ord(_CASE_CLASSES[ord(character)]) in _SRE_EXTRA_CASES
            ):
                yield index


@dataclass(frozen=True, slots=True)
class LiteralAliases:
    """Literal artifact aliases, as case keys, for the literal pass of `gate_text`.

    `prefixes` holds each alias's case key cut at each of its `_scan_stops`,
    so a scan stops as soon as no alias can continue. Lookups replace the
    single alternation of every alias, whose pattern grew with the vault and
    cost seconds to build and scan.
    """

    keys: frozenset[str] = frozenset()
    prefixes: frozenset[str] = frozenset()
    longest: int = 0

    @classmethod
    def build(cls, aliases: Iterable[str]) -> LiteralAliases:
        keys: set[str] = set()
        prefixes: set[str] = set()
        longest = 0
        for alias in aliases:
            key = _case_key(alias)
            keys.add(key)
            longest = max(longest, len(key))
            prefixes.update([key[:stop] for stop in _scan_stops(alias) if stop])
        return cls(frozenset(keys), frozenset(prefixes), longest)


def literal_alias_spans(
    text: str, aliases: Sequence[LiteralAliases]
) -> list[tuple[int, int]]:
    r"""The spans of `text` that name a literal alias, leftmost and longest first.

    This is the match set of the former pattern
    `(?<![\w./-])(?:alias|...)(?![\w./-])` under `re.IGNORECASE`, with its
    alternatives ordered longest first: a span starts where no path character
    precedes it and ends where none follows, the longest alias wins at each
    start, and the scan resumes after a match.
    """
    longest = max((index.longest for index in aliases), default=0)
    if not longest:
        return []
    key = _case_key(text)
    breaks = [match.start() for match in _NOT_PATH_CHARACTER.finditer(text)]
    starts = [0, *(position + 1 for position in breaks)]
    ends = [*breaks, len(text)]
    spans: list[tuple[int, int]] = []
    resume = 0
    for start in starts:
        if start < resume or start >= len(text):
            continue
        best = None
        for end in ends[bisect.bisect_right(ends, start) :]:
            if end - start > longest:
                break
            candidate = key[start:end]
            if any(candidate in index.keys for index in aliases):
                best = end
            if not any(candidate in index.prefixes for index in aliases):
                break
        if best is not None:
            spans.append((start, best))
            resume = best
    return spans


#: A directory stamped this close to a walk may change again within the same
#: timestamp tick, unseen. Its listing is used once and read again on the next
#: walk. The margin covers coarse filesystem timestamps and the small backward
#: steps of a virtualized clock.
_CENSUS_RACY_NS = 2_000_000_000
#: Censuses kept per process, one per vault, least recently used first out.
#: A process serves one vault; tests and tools that open several stay bounded.
_CENSUS_LIMIT = 8

_Stamp = tuple[int, int, int, int]


def _directory_stamp(directory: str) -> _Stamp:
    """Change detection for one directory's entries.

    Creating, removing or renaming an entry changes the directory's
    modification time. The change time is in the stamp too, because a tool
    that restores an old modification time cannot restore it.
    """
    status = os.stat(directory)
    return (status.st_dev, status.st_ino, status.st_mtime_ns, status.st_ctime_ns)


@dataclass(frozen=True, slots=True)
class _Listing:
    """One directory's file and subdirectory names, as of `stamp`.

    `stamp` is `None` when the listing must be read again on the next walk:
    the directory changed too recently to trust its stamp, or it was
    unreadable.
    """

    stamp: _Stamp | None
    files: tuple[str, ...]
    directories: tuple[str, ...]


def _list_directory(path: str, racy_after: int) -> _Listing:
    # The stamp comes first: a change made while the entries are read moves it.
    try:
        stamp: _Stamp | None = _directory_stamp(path)
        files: list[str] = []
        directories: list[str] = []
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    # Like the former `rglob("*")`: a symlinked directory is not walked.
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(entry.name)
                    elif entry.is_file():
                        files.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return _Listing(None, (), ())
    if stamp is not None and max(stamp[2:]) >= racy_after:
        stamp = None
    return _Listing(stamp, tuple(sorted(files)), tuple(sorted(directories)))


def _walk_listings(
    root: str, previous: Mapping[str, _Listing], racy_after: int
) -> dict[str, _Listing]:
    """List every directory under `root`, reading again only those that changed."""
    listings: dict[str, _Listing] = {}
    pending = [""]
    while pending:
        relative = pending.pop()
        path = os.path.join(root, relative) if relative else root
        listing = previous.get(relative)
        if listing is not None and listing.stamp is not None:
            try:
                if _directory_stamp(path) != listing.stamp:
                    listing = None
            except OSError:
                listing = None
        else:
            listing = None
        if listing is None:
            listing = _list_directory(path, racy_after)
        listings[relative] = listing
        pending.extend(f"{relative}/{name}" if relative else name for name in listing.directories)
    return listings


@dataclass(frozen=True, slots=True)
class ArtifactCensus:
    """Every artifact path in a vault, with the aliases the gate resolves.

    The aliases depend on the file names only, so a walk that finds the same
    names keeps them, as when a write replaces a file in place.
    """

    by_path: Mapping[str, tuple[str, ...]]
    by_name: Mapping[str, tuple[str, ...]]
    by_stem: Mapping[str, tuple[str, ...]]
    literals: LiteralAliases
    listings: Mapping[str, _Listing]

    @classmethod
    def empty(cls) -> ArtifactCensus:
        return cls(
            MappingProxyType({}), MappingProxyType({}), MappingProxyType({}), LiteralAliases(), {}
        )

    def same_names(self, listings: Mapping[str, _Listing]) -> bool:
        return listings.keys() == self.listings.keys() and all(
            listing.files == self.listings[relative].files for relative, listing in listings.items()
        )


_CENSUSES: OrderedDict[tuple[str, str], ArtifactCensus] = OrderedDict()
_CENSUS_LOCK = threading.Lock()


def _alias_table(rows: Mapping[str, list[str]]) -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType({key: tuple(values) for key, values in rows.items()})


def add_alias(table: dict[str, list[str]], alias: str, rel: str) -> None:
    rows = table.setdefault(alias.casefold(), [])
    if rel not in rows:
        rows.append(rel)


def _census_from(listings: Mapping[str, _Listing]) -> ArtifactCensus:
    kb_prefix = f"{kb_dirname()}/"
    by_path: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    by_stem: dict[str, list[str]] = {}
    literal_aliases: set[str] = set()
    for directory, listing in listings.items():
        for name in listing.files:
            rel = f"{directory}/{name}" if directory else name
            aliases = {rel, unquote(rel)}
            if rel.startswith(kb_prefix):
                aliases.add(rel[len(kb_prefix) :])
            # The posix spelling throughout: an alias with backslash separators
            # never matches the forward-slash token `resolve` normalises to, so a
            # reference to a withheld page would survive redaction on Windows.
            suffix = PurePosixPath(name).suffix
            stem = name[: -len(suffix)] if suffix else name
            aliases.add(rel[: -len(suffix)] if suffix else rel)
            for alias in aliases:
                add_alias(by_path, alias, rel)
                if len(alias) >= 3:
                    literal_aliases.add(alias)
            add_alias(by_name, name, rel)
            add_alias(by_stem, stem, rel)
            if len(name) >= 3:
                literal_aliases.add(name)
    return ArtifactCensus(
        by_path=_alias_table(by_path),
        by_name=_alias_table(by_name),
        by_stem=_alias_table(by_stem),
        literals=LiteralAliases.build(literal_aliases),
        listings=MappingProxyType(listings),
    )


def vault_census(vault_root: Path) -> ArtifactCensus:
    """The vault's census, walked again only where a directory changed."""
    key = (str(vault_root), kb_dirname())
    with _CENSUS_LOCK:
        previous = _CENSUSES.get(key)
    racy_after = time.time_ns() - _CENSUS_RACY_NS
    if not os.path.isdir(vault_root):
        return ArtifactCensus.empty()
    listings = _walk_listings(
        str(vault_root), previous.listings if previous is not None else {}, racy_after
    )
    if previous is not None and previous.same_names(listings):
        census = replace(previous, listings=MappingProxyType(listings))
    else:
        census = _census_from(listings)
    with _CENSUS_LOCK:
        _CENSUSES.pop(key, None)
        _CENSUSES[key] = census
        while len(_CENSUSES) > _CENSUS_LIMIT:
            _CENSUSES.popitem(last=False)
    return census


def clear() -> None:
    """Forget every census, for a test that rebuilds a vault in place."""
    with _CENSUS_LOCK:
        _CENSUSES.clear()
