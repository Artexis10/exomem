"""The census of a vault's files that the artifact reference gate resolves against.

`egress` replaces a reference to a withheld artifact in free text. To find
one, it needs every file path in the vault and the aliases that name each
file. This module keeps that census per vault and lists again only the
directories that changed, so its cost does not grow with the vault while
the vault's names stay the same. It also finds the literal aliases in a
text with set lookups, with the matches of the alternation pattern of every
alias that it replaces.

A background owner can publish the census as a base. Writers and file
events then add the aliases of each new file to an overlay, so a request
reads the base and the overlay and lists no directory. The overlay only
adds names: a removed name stays until a rebuilt base proves the removal,
because a removed longer alias could expose a shorter withheld name inside
it. Without a published base a request walks the vault, as it always did.
"""

from __future__ import annotations

import bisect
import functools
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

# The stdlib's own table of the extra IGNORECASE pairs. If a future Python drops
# it, the import fails loudly: matching without it would miss case variants.
from re._casefix import _EXTRA_CASES as _SRE_EXTRA_CASES
from types import MappingProxyType
from urllib.parse import unquote

from .. import held_fs
from ..kbdir import kb_dirname

#: A literal alias starts and ends where the text does not continue a path.
_NOT_PATH_CHARACTER = re.compile(r"[^\w./-]")
#: A run of path characters. The same closed set as `_NOT_PATH_CHARACTER`, so a
#: run ends exactly where a literal alias match ends.
_PATH_RUN = re.compile(r"[\w./-]+")


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
    root: str,
    previous: Mapping[str, _Listing],
    racy_after: int,
    *,
    relist: Collection[str] = (),
    between: Callable[[], object] | None = None,
) -> dict[str, _Listing]:
    """List every directory under `root`, reading again only those that changed.

    A directory in `relist` is read again whatever its stamp says. `between`
    runs before each directory, so a background owner can yield to requests.
    """
    listings: dict[str, _Listing] = {}
    pending = [""]
    while pending:
        relative = pending.pop()
        if between is not None:
            between()
        path = os.path.join(root, relative) if relative else root
        listing = None if relative in relist else previous.get(relative)
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

    def files_named(self, name: str) -> tuple[str, ...]:
        """The vault-relative files called exactly `name`; the name index folds case, so filter it."""
        return tuple(rel for rel in self.by_name.get(name.casefold(), ()) if rel.rpartition("/")[2] == name)

    def files_under(self, directory: str) -> Iterator[str]:
        """The vault-relative files in `directory` and in the directories below it, as of the walk.

        A walk lists a symlinked directory as neither file nor directory, so none is entered.
        """
        pending = [directory]
        while pending:
            current = pending.pop()
            listing = self.listings.get(current)
            if listing is not None:
                yield from (f"{current}/{name}" for name in listing.files)
                pending.extend(f"{current}/{name}" for name in listing.directories)


_CENSUSES: OrderedDict[tuple[str, str], ArtifactCensus] = OrderedDict()
_CENSUS_LOCK = threading.Lock()


def _alias_table(rows: Mapping[str, list[str]]) -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType({key: tuple(values) for key, values in rows.items()})


def add_alias(table: dict[str, list[str]], alias: str, rel: str) -> None:
    rows = table.setdefault(alias.casefold(), [])
    if rel not in rows:
        rows.append(rel)


class _Tables:
    """The alias tables of a census as they are built, file by file."""

    def __init__(self) -> None:
        self.kb_prefix = f"{kb_dirname()}/"
        self.by_path: dict[str, list[str]] = {}
        self.by_name: dict[str, list[str]] = {}
        self.by_stem: dict[str, list[str]] = {}
        self.literal_aliases: set[str] = set()

    def add_file(self, rel: str) -> None:
        name = rel.rpartition("/")[2]
        aliases = {rel, unquote(rel)}
        if rel.startswith(self.kb_prefix):
            aliases.add(rel[len(self.kb_prefix) :])
        # The posix spelling throughout: an alias with backslash separators
        # never matches the forward-slash token `resolve` normalises to, so a
        # reference to a withheld page would survive redaction on Windows.
        suffix = PurePosixPath(name).suffix
        stem = name[: -len(suffix)] if suffix else name
        aliases.add(rel[: -len(suffix)] if suffix else rel)
        for alias in aliases:
            add_alias(self.by_path, alias, rel)
            if len(alias) >= 3:
                self.literal_aliases.add(alias)
        add_alias(self.by_name, name, rel)
        add_alias(self.by_stem, stem, rel)
        if len(name) >= 3:
            self.literal_aliases.add(name)

    def census(self, listings: Mapping[str, _Listing]) -> ArtifactCensus:
        return ArtifactCensus(
            by_path=_alias_table(self.by_path),
            by_name=_alias_table(self.by_name),
            by_stem=_alias_table(self.by_stem),
            literals=LiteralAliases.build(self.literal_aliases),
            listings=MappingProxyType(listings),
        )


def _census_from(listings: Mapping[str, _Listing]) -> ArtifactCensus:
    tables = _Tables()
    for directory, listing in listings.items():
        for name in listing.files:
            tables.add_file(f"{directory}/{name}" if directory else name)
    return tables.census(listings)


def _census_from_rels(rels: Iterable[str]) -> ArtifactCensus:
    tables = _Tables()
    for rel in rels:
        tables.add_file(rel)
    return tables.census({})


@functools.lru_cache(maxsize=32)
def _resolved_root(root: str) -> str:
    try:
        return str(Path(root).resolve())
    except OSError:
        return root


def _vault_key(vault_root: Path | str) -> tuple[str, str]:
    # One spelling per vault: the writers, the file watcher and the gate each
    # hold the root as their caller spelled it.
    return (_resolved_root(os.fspath(vault_root)), kb_dirname())


def _walk_census(
    vault_root: Path,
    *,
    relist: Collection[str] = (),
    forget: bool = False,
    between: Callable[[], object] | None = None,
) -> ArtifactCensus:
    key = _vault_key(vault_root)
    with _CENSUS_LOCK:
        previous = _CENSUSES.get(key)
    racy_after = time.time_ns() - _CENSUS_RACY_NS
    if not os.path.isdir(vault_root):
        return ArtifactCensus.empty()
    listings = _walk_listings(
        str(vault_root),
        {} if forget or previous is None else previous.listings,
        racy_after,
        relist=relist,
        between=between,
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


def vault_census(vault_root: Path) -> ArtifactCensus:
    """The vault's census, walked again only where a directory changed."""
    return _walk_census(vault_root)


def _joined(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    if not second:
        return first
    if not first:
        return second
    return tuple(dict.fromkeys((*first, *second)))


@dataclass(frozen=True, slots=True)
class CensusView:
    """What one request resolves references against: a base and the names added since."""

    base: ArtifactCensus
    overlay: ArtifactCensus | None = None
    #: A published view lists no directory, so a file it lacks may still exist.
    #: A view from a walk holds every file that the walk found.
    published: bool = False

    @classmethod
    def empty(cls) -> CensusView:
        return cls(ArtifactCensus.empty())

    def paths(self, key: str) -> tuple[str, ...]:
        rows = self.base.by_path.get(key, ())
        return rows if self.overlay is None else _joined(rows, self.overlay.by_path.get(key, ()))

    def names(self, key: str) -> tuple[str, ...]:
        rows = self.base.by_name.get(key, ())
        return rows if self.overlay is None else _joined(rows, self.overlay.by_name.get(key, ()))

    def stems(self, key: str) -> tuple[str, ...]:
        rows = self.base.by_stem.get(key, ())
        return rows if self.overlay is None else _joined(rows, self.overlay.by_stem.get(key, ()))

    @property
    def literals(self) -> tuple[LiteralAliases, ...]:
        if self.overlay is None:
            return (self.base.literals,)
        return (self.base.literals, self.overlay.literals)


#: Files the overlay may hold before the published census is withdrawn. A
#: larger burst, such as a repository pull, is cheaper to walk than to track.
_OVERLAY_LIMIT = 4096
#: Directories named by events before the next rebuild lists every directory again.
_DIRTY_LIMIT = 4096


@dataclass(slots=True)
class _Feed:
    """One vault's published census and the changes noted since its walk began.

    `sequence` orders the notes. A walk that began at sequence N holds every
    file noted at or before N, so publishing it drops those notes.
    """

    base: ArtifactCensus | None = None
    overlay: ArtifactCensus | None = None
    added: dict[str, int] = field(default_factory=dict)
    dirty: dict[str, int] = field(default_factory=dict)
    relist_all_after: int = 0
    sequence: int = 0
    #: Changes when the published census is withdrawn, so a walk begun earlier
    #: cannot publish a census that misses what the withdrawal dropped.
    epoch: int = 0
    #: Changes with `added` and `base`, so a stale overlay snapshot is never stored.
    version: int = 0
    collecting: bool = False
    demanded: bool = False


_FEEDS: OrderedDict[tuple[str, str], _Feed] = OrderedDict()


def _feed_locked(key: tuple[str, str]) -> _Feed:
    feed = _FEEDS.get(key)
    if feed is None:
        feed = _FEEDS[key] = _Feed()
        while len(_FEEDS) > _CENSUS_LIMIT:
            _FEEDS.popitem(last=False)
    return feed


def _withdraw_locked(feed: _Feed) -> None:
    feed.base = None
    feed.overlay = None
    feed.added.clear()
    feed.dirty.clear()
    feed.relist_all_after = 0
    feed.collecting = False
    feed.epoch += 1
    feed.version += 1


def withdraw(vault_root: Path) -> None:
    """Stop serving the published census, so requests walk the vault again."""
    with _CENSUS_LOCK:
        feed = _FEEDS.get(_vault_key(vault_root))
        if feed is not None:
            _withdraw_locked(feed)


def published_view(vault_root: Path) -> CensusView | None:
    """The published base and overlay, or `None` while the vault has none.

    Asking marks the vault as wanted: the background owner publishes only for
    a vault that a request needed, so an idle vault costs nothing.
    """
    key = _vault_key(vault_root)
    with _CENSUS_LOCK:
        feed = _feed_locked(key)
        feed.demanded = True
        base = feed.base
        if base is None:
            return None
        overlay = feed.overlay
        rels = tuple(feed.added) if overlay is None else ()
        version = feed.version
    if rels:
        overlay = _census_from_rels(rels)
        with _CENSUS_LOCK:
            if feed.version == version:
                feed.overlay = overlay
    return CensusView(base, overlay, published=True)


def request_census(vault_root: Path) -> CensusView:
    """What a request resolves against: the published census, else a walk of the vault."""
    return published_view(vault_root) or CensusView(vault_census(vault_root))


def rebuild_published(
    vault_root: Path,
    *,
    eager: bool = False,
    between: Callable[[], object] | None = None,
) -> bool:
    """Walk the vault and publish the census as the base; the background owner's one call.

    It publishes for a vault that a request needed, or when `eager`, and keeps
    a published base current. A walk that a withdrawal overtook publishes
    nothing. `between` runs before each directory so the owner can yield.
    Returns whether a base is published.
    """
    if not os.path.isdir(vault_root):
        return False
    key = _vault_key(vault_root)
    with _CENSUS_LOCK:
        feed = _feed_locked(key)
        if not (eager or feed.demanded):
            return feed.base is not None
        started, epoch = feed.sequence, feed.epoch
        feed.collecting = True
        relist = frozenset(feed.dirty)
        forget = feed.relist_all_after > 0
    try:
        census = _walk_census(vault_root, relist=relist, forget=forget, between=between)
    except BaseException:
        with _CENSUS_LOCK:
            if feed.base is None:
                feed.collecting = False
        raise
    with _CENSUS_LOCK:
        if feed.epoch != epoch:
            return False
        feed.base = census
        feed.added = {rel: seq for rel, seq in feed.added.items() if seq > started}
        feed.dirty = {rel: seq for rel, seq in feed.dirty.items() if seq > started}
        if feed.relist_all_after <= started:
            feed.relist_all_after = 0
        feed.overlay = None
        feed.version += 1
    return True


def _relatives(vault_root: Path, paths: Iterable[os.PathLike[str] | str]) -> list[str]:
    root = Path(vault_root)
    relatives: list[str] = []
    for value in paths:
        spelling = os.fspath(value)
        if os.path.isabs(spelling):
            path = Path(spelling)
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                try:
                    relative = path.resolve().relative_to(root.resolve()).as_posix()
                except (ValueError, OSError):
                    continue
        else:
            relative = spelling.replace("\\", "/").strip("/")
        if relative and relative != "." and not relative.startswith("../"):
            relatives.append(relative)
    return relatives


def _note(
    vault_root: Path, paths: Iterable[os.PathLike[str] | str], *, created: bool
) -> None:
    key = _vault_key(vault_root)
    try:
        with _CENSUS_LOCK:
            feed = _FEEDS.get(key)
            if feed is None or not feed.collecting:
                return
        relatives = _relatives(vault_root, paths)
        with _CENSUS_LOCK:
            if not feed.collecting:
                return
            feed.sequence += 1
            sequence = feed.sequence
            known = feed.base.by_path if feed.base is not None else {}
            for relative in relatives:
                if created:
                    if relative in known.get(relative.casefold(), ()):
                        continue
                    if relative not in feed.added:
                        feed.version += 1
                        feed.overlay = None
                    feed.added[relative] = sequence
                else:
                    feed.dirty[relative] = sequence
                feed.dirty[relative.rpartition("/")[0]] = sequence
            if len(feed.added) > _OVERLAY_LIMIT:
                _withdraw_locked(feed)
            elif len(feed.dirty) > _DIRTY_LIMIT:
                feed.dirty.clear()
                feed.relist_all_after = sequence
    except Exception:  # noqa: BLE001 - a note that fails must not leave the census short
        withdraw(vault_root)


def note_created(vault_root: Path, paths: Iterable[os.PathLike[str] | str]) -> None:
    """Add the aliases of files that now exist, before the caller acknowledges.

    Paths are absolute or vault-relative. Never raises: a note that fails
    withdraws the published census, so requests walk the vault again.
    """
    _note(vault_root, paths, created=True)


def note_touched(vault_root: Path, paths: Iterable[os.PathLike[str] | str]) -> None:
    """Name paths that were removed or are directories, so the next rebuild lists them again.

    This adds no alias. A removal waits for a rebuilt base to prove it.
    """
    _note(vault_root, paths, created=False)


def _probe_failed(error: held_fs.HeldFsError | None) -> str:
    # A missing leaf, a non-file leaf (a link or a directory) and a malformed
    # path are not a file. Any other refusal leaves the question open.
    return "absent" if error is not None and error.code in {"MISSING", "UNSAFE_PATH"} else "unknown"


def _probe(vault_root: Path, relative: str) -> str:
    """`file`, `absent` or `unknown` for one vault-relative path, through retained handles."""
    parent_path, _, leaf = relative.rpartition("/")
    try:
        acquired = held_fs.acquire(Path(vault_root))
        if not acquired.ok:
            return _probe_failed(acquired.error)
        with acquired.require() as filesystem:
            parent = filesystem.parent(parent_path or ".")
            if not parent.ok:
                return _probe_failed(parent.error)
            with parent.require() as directory:
                found = filesystem.file(directory, leaf)
                if not found.ok:
                    return _probe_failed(found.error)
                with found.require():
                    return "file"
    except (OSError, held_fs.HeldFsError):
        return "unknown"


def direct_paths(vault_root: Path, token: str) -> tuple[str, ...]:
    """The vault paths that a path-shaped token names on disk, by one no-follow check each.

    The token is vault-relative, or relative to the knowledge base folder. A
    path that cannot be ruled out, such as an unreadable one, counts as
    present, because a gate that misses a withheld name leaks it. A file that
    the check finds joins the overlay, so the next request resolves it.
    """
    candidates = tuple(dict.fromkeys((token, f"{kb_dirname()}/{token}")))
    outcomes = {relative: _probe(vault_root, relative) for relative in candidates}
    present = tuple(relative for relative, outcome in outcomes.items() if outcome != "absent")
    note_created(
        vault_root, [relative for relative, outcome in outcomes.items() if outcome == "file"]
    )
    return present


def with_path_spans(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """`spans` plus the runs of `text` that have the shape of a vault path.

    A published census lacks a file that appeared a moment ago, so its name
    is not a literal alias yet. A run of path characters that holds a slash
    is a candidate, and so is a knowledge base folder name with the run that
    follows it, because that name holds a space. A candidate that overlaps a
    literal span is dropped, and the earlier of two candidates wins.
    """
    candidates: list[tuple[int, int]] = []
    key = _case_key(text)
    prefix = _case_key(f"{kb_dirname()}/")
    at = key.find(prefix)
    while at != -1:
        if at == 0 or _NOT_PATH_CHARACTER.match(text, at - 1):
            run = _PATH_RUN.match(text, at + len(prefix))
            if run is not None and run.group().rstrip("."):
                candidates.append((at, run.start() + len(run.group().rstrip("."))))
        at = key.find(prefix, at + 1)
    for run in _PATH_RUN.finditer(text):
        value = run.group().rstrip(".")
        if "/" in value:
            candidates.append((run.start(), run.start() + len(value)))
    starts = [start for start, _end in spans]
    accepted: list[tuple[int, int]] = []
    resume = 0
    for start, end in sorted(candidates):
        if start < resume:
            continue
        neighbour = bisect.bisect_right(starts, end - 1) - 1
        if neighbour >= 0 and spans[neighbour][1] > start:
            continue
        accepted.append((start, end))
        resume = end
    return sorted([*spans, *accepted])


@functools.lru_cache(maxsize=4)
def tombstone_aliases(
    tombstones: frozenset[str], kb_name: str
) -> tuple[Mapping[str, tuple[str, ...]], LiteralAliases]:
    """The aliases of the paths a lifecycle operation tombstoned, cached by that set.

    The lifecycle returns the same set until its state changes, so a request
    builds these once per generation. `kb_name` is part of the cache key
    because the knowledge base folder name changes the aliases.
    """
    kb_prefix = f"{kb_name}/"
    table: dict[str, list[str]] = {}
    literal_aliases: set[str] = set()
    for rel in tombstones:
        if rel.startswith(("exomem://", "sha256:")):
            continue
        path = PurePosixPath(rel)
        aliases = {rel, unquote(rel), path.name, path.stem}
        if rel.startswith(kb_prefix):
            aliases.add(rel[len(kb_prefix) :])
        for alias in aliases:
            add_alias(table, alias, rel)
            if len(alias) >= 3:
                literal_aliases.add(alias)
    return _alias_table(table), LiteralAliases.build(literal_aliases)


def clear() -> None:
    """Forget every census, for a test that rebuilds a vault in place."""
    with _CENSUS_LOCK:
        _CENSUSES.clear()
        _FEEDS.clear()
    _resolved_root.cache_clear()
    tombstone_aliases.cache_clear()
