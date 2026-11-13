"""Corpus parsing, walking, freshness, and filters for find()."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from . import freshness, privacy_log, request_budget
from .find_types import ParsedPage
from .vault import _FM_PATTERN as FRONTMATTER_PATTERN
from .vault import relative_posix

if TYPE_CHECKING:
    from .vault_store.read import PageMeta, StoredPage

log = logging.getLogger(__name__)

# Operational state is not knowledge: `_Governance` (policy/receipts) and
# `_Adoption` (durable run objects + run manifests) both name items whose own
# disclosure decisions may be restrictive, so neither may enter the content
# corpus or be reachable by recall.
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".graph-coordination",
        ".trash",
        "_Adoption",
        "_Schema",
        "_Governance",
        "_attachments",
        "_archive",
        "_trash",
    }
)
EXCLUDED_DIR_PREFIXES = (".exomem-batch-",)
NAVIGATION_BASENAMES = frozenset({"index.md", "log.md"})
H1_PATTERN = re.compile(r"^# (.+)$", re.MULTILINE)
_DEFAULT_PAGE_CACHE_SIZE = 4096


def _page_cache_size() -> int:
    raw = os.environ.get("EXOMEM_PAGE_CACHE_SIZE")
    if raw is None or not raw.strip():
        return _DEFAULT_PAGE_CACHE_SIZE
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("EXOMEM_PAGE_CACHE_SIZE=%r is not an int; using default", raw)
        return _DEFAULT_PAGE_CACHE_SIZE


def _page_cache_bytes() -> int | None:
    from . import mode

    default = 32 * 1024 * 1024 if mode.service_profile_enabled() else None
    raw = os.environ.get("EXOMEM_PAGE_CACHE_BYTES")
    if raw is None or not raw.strip():
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("EXOMEM_PAGE_CACHE_BYTES is not an int; using default")
        return default


def _page_retained_bytes(page: ParsedPage) -> int:
    # Charge existing lazy text copies before admission. Alias-aware traversal
    # also terminates on recursive YAML and does not duplicate shared metadata.
    page.__dict__["_byte_bounded_cache"] = True
    page.__dict__.pop("stem_set", None)
    _ = page.body_stripped
    _ = page.body_norm
    _ = page.title_norm
    total = sys.getsizeof(page)
    pending = [vars(page)]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        total += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(value)
    return total


@dataclass
class FrontmatterCache:
    """Per-process cache invalidated by file identity or content changes."""

    entries: OrderedDict[Path, ParsedPage] = field(default_factory=OrderedDict)
    #: A file's stat-and-hash signature, or ``("store", page revision, sha256)`` for a page that the
    #: store serves.
    _signatures: dict[Path, tuple] = field(default_factory=dict, repr=False)
    #: The store revision at which each stored page's signature was last checked: while the store
    #: stays at that revision no page of it changed, so a hit needs no per-page query.
    _validated_at: dict[Path, int] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    _weights: dict[Path, int] = field(default_factory=dict, repr=False)
    _retained_bytes: int = field(default=0, repr=False)
    #: Served-from-cache count: the use signal the idle reaper watches.
    hits: int = 0

    def cache_status(self) -> dict[str, int]:
        """Read page counts and cache hits under the cache lock."""
        with self._lock:
            return {
                "entries": len(self.entries),
                "body_chars": sum(len(page.body) for page in self.entries.values()),
                "hits": int(self.hits),
            }

    def clear(self) -> None:
        """Discard every row, and record it as a whole-cache re-derivation.

        The counter is what "rebuild counters unchanged" is asserted against,
        so a discard that a receipt did not ask for has to move it.
        """
        freshness.note_custody_rebuild(_CUSTODY_SEAM)
        self.release()

    def release(self) -> None:
        """Discard every row on the caller's explicit request, uncounted.

        A memory release is a policy decision, not drift: the reaper asked for
        this cache back and the next reader pays a re-parse it can predict.
        Counting it as a custody rebuild would put a reaper's noise into the
        one number that says whether a governed write discarded work.
        """
        with self._lock:
            self.entries.clear()
            self._signatures.clear()
            self._validated_at.clear()
            self._weights.clear()
            self._retained_bytes = 0

    def _discard(self, path: Path) -> bool:
        with self._lock:
            removed = self.entries.pop(path, None) is not None
            self._signatures.pop(path, None)
            self._validated_at.pop(path, None)
            self._retained_bytes -= self._weights.pop(path, 0)
            return removed

    def _evict_to_limits(self, byte_limit: int | None) -> None:
        with self._lock:
            while len(self.entries) > _page_cache_size() or (
                byte_limit is not None and self._retained_bytes > byte_limit
            ):
                self._discard(next(iter(self.entries)))

    def invalidate_paths(self, vault_root: Path, rel_paths: Iterable[str]) -> int:
        """Drop the entries a receipt names, and only those. Returns the count.

        Exact custody rather than a stat comparison. The signature check in
        `get` is the safety net, and its own docstring names the case it cannot
        see -- a same-size rewrite that preserves the whole stat tuple. A
        governed write knows exactly which pages it changed, so the row goes on
        the receipt's word rather than on a filesystem inference.
        """
        root = Path(vault_root)
        evicted = 0
        for rel in rel_paths:
            for candidate in _cache_key_candidates(root, rel):
                if self._discard(candidate):
                    evicted += 1
        return evicted

    def invalidate_scope(self, vault_root: Path, scope: str) -> int:
        """Drop every entry under one scope, for a change no receipt covers."""
        root = _scope_root(Path(vault_root), scope)
        try:
            anchor = root.resolve()
        except OSError:
            anchor = root
        dropped = 0
        with self._lock:
            paths = tuple(self.entries)
        for path in paths:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved == anchor or anchor in resolved.parents:
                self._discard(path)
                dropped += 1
        return dropped

    def stale_paths(
        self,
        vault_root: Path,
        rel_paths: Iterable[str],
        digests: Mapping[str, str | None] | None = None,
    ) -> tuple[str, ...]:
        """Which of these paths this cache holds a row that no longer describes.

        A path this cache holds no row for is not a mismatch: an absent row is
        re-parsed on the next read, which is exact by construction. Only a
        RESIDENT row can be wrong.

        `digests` is the batch's one read of each page (see
        `freshness.custody_digests`); it is hashed here only when a caller does
        not supply it.
        """
        root = Path(vault_root)
        stale: list[str] = []
        for rel in rel_paths:
            for candidate in _cache_key_candidates(root, rel):
                cached = self.entries.get(candidate)
                if cached is None:
                    continue
                if digests is not None and rel in digests:
                    digest = digests[rel]
                else:
                    content = _read_page_bytes(candidate, root)
                    digest = None if content is None else hashlib.sha256(content).hexdigest()
                if digest is None or cached.snapshot_hash != digest:
                    stale.append(rel)
                    break
        return tuple(stale)

    def get(self, path: Path, vault_root: Path) -> ParsedPage | None:
        stored = stored_relative(path, vault_root)
        if stored is not None:
            return self._get_stored(path, vault_root, stored)
        snapshot = _read_page_snapshot(path, vault_root)
        if snapshot is None:
            self._discard(path)
            return None
        content = snapshot.data
        # Stat metadata is not content identity: on native Windows ctime is
        # creation time, so a same-size rewrite can preserve this whole tuple.
        # Hash the bytes already needed on a miss and reuse those exact bytes
        # for parsing, keeping the fingerprint and ParsedPage consistent.
        signature = (
            int(snapshot.mtime * 1_000_000_000),
            0,
            len(content),
            snapshot.identity.device,
            snapshot.identity.inode,
            hashlib.blake2b(content, digest_size=16).digest(),
        )
        cached = self._resident(path, signature)
        if cached is not None:
            return cached
        return self._admit(
            path, signature, parse_page(path, snapshot.mtime, vault_root, content=content)
        )

    def _get_stored(self, path: Path, vault_root: Path, rel: str) -> ParsedPage | None:
        """``get`` for a page that the store serves: the store's revision and hash decide, not a
        file's stat.

        One snapshot gives the store revision and the page. A page last checked at this revision is
        current, so it costs no query. Otherwise the page's revision and hash decide, and only a
        changed page reads its body. ``rel`` names the page, so a link at ``path`` never names its
        target.
        """
        from .vault_store import read

        with read.snapshot(vault_root) as view:
            with self._lock:
                held = self._signatures.get(path)
                current = held is not None and self._validated_at.get(path) == view.revision
            if current and held is not None:
                cached = self._resident(path, held)
                if cached is not None:
                    return cached
            meta = view.page_meta(rel)
            if meta is None:
                self._discard(path)
                return None
            signature = ("store", meta.revision, meta.sha256)
            cached = self._resident(path, signature)
            if cached is None:
                stored = view.page(rel)
                if stored is None:  # the page left between the two reads of one snapshot: it cannot
                    self._discard(path)
                    return None
                cached = self._admit(
                    path,
                    signature,
                    parse_page(
                        path,
                        meta.mtime,
                        vault_root,
                        content=stored.body,
                        resolved_relative=rel,
                    ),
                )
            if cached is not None:
                with self._lock:
                    if self.entries.get(path) is cached:
                        self._validated_at[path] = view.revision
            return cached

    def _resident(self, path: Path, signature: tuple) -> ParsedPage | None:
        """The cached page when its signature is ``signature``, counted as a hit and kept most
        recent.
        """
        with self._lock:
            if len(self._signatures) != len(self.entries):
                self._signatures = {
                    cached_path: self._signatures[cached_path]
                    for cached_path in self.entries
                    if cached_path in self._signatures
                }
            byte_limit = _page_cache_bytes()
            if byte_limit is not None and len(self._weights) != len(self.entries):
                self._weights = {key: _page_retained_bytes(page) for key, page in self.entries.items()}
                self._retained_bytes = sum(self._weights.values())
            cached = self.entries.get(path)
            if cached and self._signatures.get(path) == signature:
                if byte_limit is not None:
                    weight = _page_retained_bytes(cached)
                    self._retained_bytes += weight - self._weights.get(path, 0)
                    self._weights[path] = weight
                self.entries.move_to_end(path)
                self.hits += 1
                self._evict_to_limits(byte_limit)
                return cached
        return None

    def _admit(self, path: Path, signature: tuple, parsed: ParsedPage | None) -> ParsedPage | None:
        """Replace the row for ``path`` by ``parsed`` under ``signature``, within the cache's
        limits.
        """
        byte_limit = _page_cache_bytes()
        weight = _page_retained_bytes(parsed) if parsed is not None and byte_limit is not None else 0
        with self._lock:
            self._discard(path)
            if parsed is not None and (byte_limit is None or weight <= byte_limit):
                self.entries[path] = parsed
                self._signatures[path] = signature
                if byte_limit is not None:
                    self._weights[path] = weight
                    self._retained_bytes += weight
                self.entries.move_to_end(path)
                self._evict_to_limits(byte_limit)
        return parsed


def stored_relative(path: Path, vault_root: Path) -> str | None:
    """The vault-relative path of ``path`` when the store serves that page, else None: read the
    file.

    The mode comes first: a file-mode vault pays only that check, which costs 20 to 45
    microseconds a call (a stat of the manifest and the placement lookup), and the path rule then
    runs only in store mode.
    """
    from .vault_store import ownership

    if not store_serves(vault_root):
        return None
    try:
        rel = path.absolute().relative_to(vault_root.absolute()).as_posix()
    except ValueError:
        return None
    return rel if ownership.classify(rel) is ownership.Ownership.PAGE else None


CACHE = FrontmatterCache()

_CUSTODY_SEAM = "frontmatter_cache"


def _scope_root(vault_root: Path, scope: str) -> Path:
    from .kbdir import kb_dirname

    return vault_root / kb_dirname() if scope == "kb" else vault_root


def _cache_key_candidates(vault_root: Path, rel: str) -> tuple[Path, ...]:
    """Every spelling this cache could be keyed under for one relative path.

    Callers reach `get` with `vault_root / rel`, and a vault under a symlink
    resolves to a second absolute spelling for the same file. Both are dropped,
    because leaving either behind leaves a row a later read can still serve.
    """
    direct = vault_root.joinpath(*rel.split("/"))
    candidates = [direct]
    try:
        resolved = direct.resolve()
    except OSError:
        resolved = direct
    if resolved != direct:
        candidates.append(resolved)
    return tuple(candidates)


def _custody_apply(
    vault_root: Path,
    changed: tuple[str, ...],
    deleted: tuple[str, ...],
    _digests: Mapping[str, str | None],
) -> None:
    CACHE.invalidate_paths(vault_root, (*changed, *deleted))


def register_path_custody() -> None:
    """Register the frontmatter cache's per-path invalidation seam."""
    freshness.register_custody_seam(
        freshness.CustodySeam(
            name=_CUSTODY_SEAM,
            apply=_custody_apply,
            verify=CACHE.stale_paths,
            invalidate_scope=lambda root, scope: CACHE.invalidate_scope(root, scope),
        )
    )


def walk_freshness_key(paths, signature=None) -> tuple[int, int, str]:
    """(file count, max mtime_ns, digest of sorted path+metadata records).

    Digest-strength on purpose: count/max-mtime alone miss a delete paired
    with a create, a rename (mtime preserved), and a replacement carrying an
    older mtime. Every consumer that caches corpus-derived state (the hot
    find cache, BM25, the wikilink resolver, the inbound-link index) compares
    the whole triple, so those histories now invalidate correctly.

    ``signature`` reads one path's change signature; it defaults to the path's stat, or, for the
    corpus of a store-mode vault, to the store's row for a stored page (``signature_lookup``).
    """
    read_signature = signature or signature_lookup(paths)
    entries: list[tuple[str, freshness.FileSignature]] = []
    for p in paths:
        try:
            entries.append((str(p), read_signature(p)))
        except OSError:
            continue
    return freshness.triple_from_entries(entries)


@dataclass(frozen=True)
class StoreCorpus:
    """The pages of one scope in a store-mode vault, read from the store and not from a walk of the
    disk.

    It lists the pages that the store serves, then the Markdown files that the store does not own (a
    Records or Planning layer, a page outside the Knowledge Base folder), which a walk of the disk
    finds. A file that lies at a path that the store owns is the projection of a stored page, or a
    stray: the store decides, so the walk's copy of it is left out.
    """

    vault_root: Path
    #: The store revision that ``pages`` was listed at.
    revision: int
    #: The stored pages of the scope, in path order.
    pages: tuple[PageMeta, ...]
    #: The walk of the disk for this scope; it runs once for each pass over ``other_paths``, with
    #: the ``skip`` test that drops a stored page's projected file unstatted.
    disk: Callable[..., Iterable[Path]] = field(repr=False)

    def __iter__(self):
        for meta in self.pages:
            yield self.vault_root.joinpath(*meta.path.split("/"))
        yield from self.other_paths()

    def other_paths(self):
        """The walk's files that the store does not own.

        The walk never stats the projected file of a stored page: the store decides that page, and
        its file may lag, be damaged or be absent.
        """
        return self.disk(skip=self._owned)

    def _owned(self, path: Path) -> bool:
        return _store_owned_path(path, self.vault_root)

    def signature_lookup(self) -> Callable[[Path], freshness.FileSignature]:
        """The change signature of each page: the store's row for a stored page, the stat of any
        other file.

        A stored page's signature is its mtime, its page revision and its size, so a rewrite of the
        page changes it. The lookup raises ``OSError`` for a file that cannot be stat'ed, as a stat
        does.
        """
        stored = {
            self.vault_root.joinpath(*meta.path.split("/")): (
                meta.mtime_ns,
                meta.revision,
                meta.size,
            )
            for meta in self.pages
        }
        return lambda path: stored.get(path) or freshness.stat_signature(path)


def signature_lookup(paths) -> Callable[[Path], freshness.FileSignature]:
    """The change signature of one page of ``paths``: a file stat, or a stored page's row."""
    return paths.signature_lookup() if isinstance(paths, StoreCorpus) else freshness.stat_signature


def store_serves(vault_root: Path) -> bool:
    """Whether this vault is in store mode, so its pages come from the store and its files are a
    projection.
    """
    from .vault_store import mode

    return mode.store_mode_for_reads(vault_root)


def corpus_paths(vault_root: Path, scope: str, disk_walk: Callable[..., Iterable[Path]]):
    """The page paths of ``scope`` ("kb" or "vault"): ``disk_walk()`` in file mode, a
    ``StoreCorpus`` in store mode.

    A caller hands over its own walk of the disk, as a function, so that a file-mode vault pays only
    a read of the mode records and calls the walk it always called. In store mode the walk also
    takes ``skip=``, the test that drops a stored page's file unstatted (``walk_md``'s ``skip``), so
    a caller passes its keywords on: ``lambda **walk: walk_md(root, **walk)``.
    """
    from .vault_store import read

    if not store_serves(vault_root):
        return disk_walk()
    with read.snapshot(vault_root) as view:
        members = tuple(meta for meta in view.pages() if read.corpus_member(meta.path, scope))
    return StoreCorpus(
        vault_root=Path(vault_root), revision=view.revision, pages=members, disk=disk_walk
    )


def _store_owned_path(path: Path, vault_root: Path) -> bool:
    from .vault_store import ownership

    try:
        rel = relative_posix(path, vault_root)
    except ValueError:
        return False
    return ownership.classify(rel) is ownership.Ownership.PAGE


@dataclass(frozen=True)
class CorpusSource:
    """One corpus page: stored bytes and metadata, or its original file reader."""

    path: Path
    vault_root: Path
    stored: StoredPage | None = None

    def read_text(self) -> str:
        if self.stored is not None:
            return self.stored.body.decode("utf-8")
        from . import vault

        return vault.read_guarded_text(self.vault_root, self.path)[0]

    def read_bytes(self) -> bytes:
        if self.stored is not None:
            return self.stored.body
        from . import vault

        return vault.read_bytes_without_pinning(self.path, vault_root=self.vault_root)

    def parse(self, file_page: Callable[[Path], ParsedPage | None]) -> ParsedPage | None:
        if self.stored is None:
            return file_page(self.path)
        meta = self.stored.meta
        return parse_page(
            self.path, meta.mtime, self.vault_root,
            content=self.stored.body, resolved_relative=meta.path,
        )


def corpus_sources(
    vault_root: Path, scope: str, disk_walk: Callable[..., Iterable[Path]],
) -> Iterable[CorpusSource]:
    """Read a corpus's stored bytes and metadata in one query, then its unowned files.

    File mode keeps the caller's walk. A source reads a file only when the consumer asks for it,
    so admission still precedes parsing and an unreadable file can still count as a link target.
    """
    from .vault_store import read

    root = Path(vault_root)
    if store_serves(root):
        with read.snapshot(root) as view:
            for source in view.page_sources():
                request_budget.checkpoint()
                if read.corpus_member(source.meta.path, scope):
                    yield CorpusSource(root / source.meta.path, root, source)
        paths = disk_walk(skip=lambda path: _store_owned_path(path, root))
    else:
        paths = disk_walk()
    for path in paths:
        request_budget.checkpoint()
        yield CorpusSource(path, root)


def corpus_pages(
    vault_root: Path,
    scope: str,
    disk_walk: Callable[..., Iterable[Path]],
    file_page: Callable[[Path], ParsedPage | None],
    *,
    keep: Callable[[str, bytes | None], bool],
) -> Iterable[ParsedPage]:
    """Parse admitted corpus sources without a query or a file probe per stored page."""
    for source in corpus_sources(vault_root, scope, disk_walk):
        content = source.stored.body if source.stored is not None else None
        if not keep(source.path.relative_to(vault_root).as_posix(), content):
            continue
        try:
            page = source.parse(file_page)
        except OSError:
            continue
        if page is not None:
            yield page


def kb_walk_member(rel: str) -> bool:
    """Whether ``walk_md`` over the Knowledge Base folder reaches the file at this vault-relative
    path, by path alone.

    The walk's entry rule (`_classify_walk_entry`) also lstat's each entry, and a stored page may
    have no file yet, so it cannot judge a page. That is the gap: this keeps the rule's path tests
    (the reserved registry, the excluded folders, the Markdown suffix, the conflict copy).
    """
    from .kbdir import kb_dirname
    from .reserved_paths import classify_logical

    prefix = kb_dirname() + "/"
    if not rel.startswith(prefix):
        return False
    parts = rel[len(prefix) :].split("/")
    return (
        walk_name_admits(parts[-1], directory=False)
        and all(walk_name_admits(part, directory=True) for part in parts[:-1])
        and not classify_logical(rel).blocked
    )


class EntryKind(StrEnum):
    """What a walk makes of one directory entry, judged from its own ``lstat``."""

    SYMLINK = "symlink"
    JUNCTION = "junction"
    DIRECTORY = "directory"
    #: A regular file with no other hard link: the only kind that a walk reads as a file.
    REGULAR = "regular"
    HARD_LINKED = "hard-linked"
    #: A named pipe, a socket or a device.
    OTHER = "other"


def entry_kind(path: Path | str, info: os.stat_result) -> EntryKind:
    """The entry rule that every Knowledge Base walk shares; ``info`` is ``lstat(path)``.

    ``os.path.isjunction`` exists from Python 3.12, so older interpreters see no junctions.
    """
    if stat.S_ISLNK(info.st_mode):
        return EntryKind.SYMLINK
    if getattr(os.path, "isjunction", lambda _path: False)(path):
        return EntryKind.JUNCTION
    if stat.S_ISDIR(info.st_mode):
        return EntryKind.DIRECTORY
    if not stat.S_ISREG(info.st_mode):
        return EntryKind.OTHER
    return EntryKind.REGULAR if info.st_nlink == 1 else EntryKind.HARD_LINKED


def is_sync_conflict_copy(name: str) -> bool:
    """Whether a file name is a Syncthing conflict copy, a transient duplicate that no walk keeps.

    The token is the marker that Syncthing documents for its conflict copies: a protocol token, not a vocabulary.
    """
    return ".sync-conflict-" in name


def _walk_md_entry(
    anchor: Path,
    child: Path,
    reserved_paths,
    on_error: Callable[[OSError], None] | None = None,
) -> str | None:
    """How `walk_md` treats one directory entry: "dir" to descend, "md" to yield.

    None skips it. The walk's whole per-entry rule lives here so `walk_md_admits`
    answers for one path by the same rule, not by a copy that can drift. Callers
    import `reserved_paths` once per walk and pass it in, as the walk always did.
    """
    return _classify_walk_entry(anchor, child, reserved_paths, on_error)[0]


def _classify_walk_entry(
    anchor: Path,
    child: Path,
    reserved_paths,
    on_error: Callable[[OSError], None] | None = None,
) -> tuple[str | None, os.stat_result | None]:
    """`_walk_md_entry`'s answer, with the `lstat` that decided it when there was one."""
    try:
        relative = relative_posix(child.absolute(), anchor.absolute())
    except ValueError:
        return None, None
    if reserved_paths.classify_logical(relative).blocked:
        return None, None
    try:
        info = child.lstat()
    except OSError as error:
        if on_error is not None:
            on_error(error)
        return None, None
    kind = entry_kind(child, info)
    if kind is EntryKind.DIRECTORY:
        return ("dir" if walk_name_admits(child.name, directory=True) else None), info
    if kind is EntryKind.REGULAR and walk_name_admits(child.name, directory=False):
        return "md", info
    return None, info


def walk_name_admits(name: str, *, directory: bool) -> bool:
    """The walk's rule that reads an entry's name alone, for a folder to enter or a file to yield.

    A folder is entered unless it is excluded by name; a file is yielded when it is Markdown and
    not a sync conflict copy. The entry's kind and the reserved registry are the caller's to add.
    """
    if directory:
        return name not in EXCLUDED_DIR_NAMES and not name.startswith(EXCLUDED_DIR_PREFIXES)
    return Path(name).suffix.lower() == ".md" and not is_sync_conflict_copy(name)


def walk_md(
    root: Path,
    *,
    on_error: Callable[[OSError], None] | None = None,
    skip: Callable[[Path], bool] | None = None,
):
    """Yield every .md path under root, skipping excluded subtrees.

    Skips Obsidian `*.sync-conflict-*.md` files — transient conflict
    duplicates that would otherwise pollute the index and search results.
    `on_error` receives every directory or entry the walk could not read, so a
    caller can tell an incomplete walk from a whole one; without it the walk
    stays best-effort. `skip` is asked about each Markdown-named entry before
    the walk stats it, and an entry that it takes is dropped unstatted.
    """
    for path, _info in walk_md_with_stat(root, on_error=on_error, skip=skip):
        yield path


def walk_md_with_stat(
    root: Path,
    *,
    on_error: Callable[[OSError], None] | None = None,
    skip: Callable[[Path], bool] | None = None,
):
    """`walk_md`, yielding `(path, lstat)`: the stat that admitted each page.

    A caller that keys on file identity takes it from here instead of statting
    every page a second time.
    """
    from . import foreground_priority, reserved_paths

    anchor = Path(root)

    def walk(current: Path):
        foreground_priority.check_cancelled()
        try:
            children = tuple(current.iterdir())
        except OSError as error:
            if on_error is not None:
                on_error(error)
            return
        for child in children:
            foreground_priority.check_cancelled()
            if skip is not None and walk_name_admits(child.name, directory=False) and skip(child):
                continue
            kind, info = _classify_walk_entry(anchor, child, reserved_paths, on_error)
            if kind == "dir":
                yield from walk(child)
            elif kind == "md":
                yield child, info

    yield from walk(anchor)


def walk_md_admits(
    root: Path, path: Path, listings: dict[Path, frozenset[str] | None]
) -> bool:
    """Whether `walk_md(root)` yields exactly `path`, by visiting only its ancestry."""
    from . import reserved_paths

    return walk_entry_admits(
        root, path, listings,
        lambda anchor, child: _walk_md_entry(anchor, child, reserved_paths),
    )


def walk_entry_admits(
    root: Path,
    path: Path,
    listings: dict[Path, frozenset[str] | None],
    entry_kind: Callable[[Path, Path], str | None],
) -> bool:
    """Apply a scanner's entry rule to exactly one listed path's ancestry.

    The walk yields a page when every directory above it lists (a directory it
    cannot list contributes nothing), every step is an entry its parent lists
    under exactly that name (the walk yields listed names, so on a
    case-insensitive filesystem a differently-cased spelling that would still
    stat is not one of them), each ancestor passes `entry_kind` as a
    directory and the page itself as Markdown. Admission is decided before a
    directory is listed, so a reserved tree is never enumerated -- the same
    order the walk keeps. `listings` memoizes directory listings (None for a
    directory the walk would not enter) across calls on one pass.
    """
    anchor = Path(root)
    try:
        parts = Path(path).relative_to(anchor).parts
    except ValueError:
        return False
    if not parts:
        return False
    if anchor not in listings:
        listings[anchor] = _listed_names(anchor)
    current = anchor
    names = listings[anchor]
    for depth, part in enumerate(parts):
        if names is None or part not in names:
            return False
        current = current / part
        if depth == len(parts) - 1:
            return entry_kind(anchor, current) == "md"
        if current not in listings:
            listings[current] = (
                _listed_names(current)
                if entry_kind(anchor, current) == "dir"
                else None
            )
        names = listings[current]
    return False


def _listed_names(directory: Path) -> frozenset[str] | None:
    try:
        return frozenset(os.listdir(directory))
    except OSError:
        return None


def _read_page_snapshot(path: Path, vault_root: Path):
    from . import reserved_paths

    try:
        relative = path.absolute().relative_to(vault_root.absolute()).as_posix()
        # The corpus reads pages a walk of the disk found, so it opens the
        # spelling the walk found: the freshness identity and the graph name a
        # macOS-origin NFD page that way, and its NFKC form names no file on a
        # byte-exact file system.
        return reserved_paths.read_generic_bytes(vault_root, relative, physical=True)
    except (OSError, ValueError, reserved_paths.ReservedPathLeafError) as error:
        if privacy_log.content_private_logging_enabled():
            log.warning("hosted content parse failed code=HOSTED_CONTENT_READ_FAILED")
        else:
            log.warning("could not read %s: %s", path, error)
        return None


def _read_page_bytes(path: Path, vault_root: Path) -> bytes | None:
    snapshot = _read_page_snapshot(path, vault_root)
    return snapshot.data if snapshot is not None else None


def read_page_bytes(path: Path, vault_root: Path) -> bytes | None:
    """The bytes the parser reads for one page, or None when it cannot be read."""
    return _read_page_bytes(path, vault_root)


@dataclass(frozen=True, slots=True)
class PageStat:
    """The fields of a page's stat that a cache key reads."""

    st_mtime: float
    st_mtime_ns: int
    st_size: int


def page_stat(path: Path, vault_root: Path) -> PageStat:
    """What a stat of the page at ``path`` gives: the store's row when the store serves that page,
    else the file's own stat.

    A stored page's file may lag a commit, be damaged or be absent, so a cache key built on its stat
    would key on the wrong bytes. A page that the store serves but lacks raises
    ``FileNotFoundError``, as a stat of a missing file does.
    """
    rel = stored_relative(path, vault_root)
    if rel is not None:
        from .vault_store import read

        meta = read.owned_meta(vault_root, rel)
        if meta is not None:
            return PageStat(meta.mtime, meta.mtime_ns, meta.size)
    found = path.stat()
    return PageStat(found.st_mtime, found.st_mtime_ns, found.st_size)


def page_relative_path(path: Path, vault_root: Path) -> str:
    """The canonical path identity used by the page parser.

    A page that the store serves is known by its store key at the leaf: its projected file may be
    a link, and the target that a link resolves to is neither the page's path nor a path that scope
    membership or a governance decision may use for it.
    """
    stored = stored_relative(path, vault_root)
    if stored is not None:
        return stored
    try:
        return path.resolve().relative_to(vault_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def parse_page(
    path: Path,
    mtime: float,
    vault_root: Path,
    *,
    content: bytes | None = None,
    resolved_relative: str | None = None,
) -> ParsedPage | None:
    from .foreground_activity import checkpoint

    request_budget.checkpoint()
    checkpoint(vault_root)
    return _parse_page_after_checkpoint(
        path, mtime, vault_root, content=content, resolved_relative=resolved_relative
    )


def _parse_page_after_checkpoint(
    path: Path,
    mtime: float,
    vault_root: Path,
    *,
    content: bytes | None = None,
    resolved_relative: str | None = None,
) -> ParsedPage | None:
    """Parse one representation after the caller's foreground checkpoint."""
    if content is None:
        content = _read_page_bytes(path, vault_root)
        if content is None:
            return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as e:
        if privacy_log.content_private_logging_enabled():
            log.warning("hosted content parse failed code=HOSTED_CONTENT_READ_FAILED")
        else:
            log.warning("could not read %s: %s", path, e)
        return None

    # Markdown has a logical LF grammar even when editors persist CRLF on
    # Windows. Normalize for parsing while callers that need exact bytes keep
    # using the original `content`/PathGuard.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    fm_match = FRONTMATTER_PATTERN.match(text)
    frontmatter_valid = True
    if fm_match:
        try:
            # Hot path: every page-cache miss parses here (warm-up walks the
            # whole vault through it). libyaml loader via the vault seam.
            from .vault import yaml_safe_load

            frontmatter = yaml_safe_load(fm_match.group(1)) or {}
            if not isinstance(frontmatter, dict):
                frontmatter = {}
                frontmatter_valid = False
        except yaml.YAMLError as e:
            if privacy_log.content_private_logging_enabled():
                log.warning("hosted content parse failed code=HOSTED_CONTENT_PARSE_FAILED")
            else:
                log.warning("YAML parse error in %s: %s", path, e)
            frontmatter = {}
            frontmatter_valid = False
        body = fm_match.group(2)
        # The FRONTMATTER_PATTERN consumes the closing `\n---\n` but not the
        # blank line that conventionally follows. Strip a single leading `\n`
        # so callers (notably `get`) can feed `body` back into `edit` without
        # accumulating blanks across round-trips.
        if body.startswith("\n"):
            body = body[1:]
    else:
        frontmatter = {}
        body = text
        if text.removeprefix("\ufeff").startswith("---\n"):
            # An opening fence without the complete frontmatter grammar is
            # ambiguous metadata, not proof that the page has no metadata.
            frontmatter_valid = False

    if resolved_relative is not None:
        rel_path = resolved_relative
    else:
        rel_path = page_relative_path(path, vault_root)

    from . import provenance
    from .vault import resolve_display_title

    display_frontmatter = frontmatter
    authored_title = frontmatter.get("title")
    if authored_title is not None:
        # Titles are search fields every audience reads, like `search_body`.
        display_title = provenance.withheld_prose(str(authored_title), owner_path=rel_path)
        if display_title != str(authored_title):
            display_frontmatter = {**frontmatter, "title": display_title}
    title = resolve_display_title(
        display_frontmatter, provenance.withheld_prose(body, owner_path=rel_path), path
    )

    return ParsedPage(
        path=path,
        rel_path=rel_path,
        frontmatter=frontmatter,
        body=body,
        title=title,
        mtime=mtime,
        snapshot_hash=hashlib.sha256(content).hexdigest(),
        frontmatter_valid=frontmatter_valid,
        vault_root=vault_root,
    )


def passes_filters(
    page: ParsedPage,
    *,
    vault_root: Path | None = None,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
) -> bool:
    # `excluded` tier (_access.yaml): never surfaced. Checked first — an excluded
    # page is invisible regardless of how well it matches. (vault_root omitted in
    # unit tests -> skip; real find paths always pass it.)
    if vault_root is not None:
        from . import access

        if not access.is_indexable(vault_root, page.rel_path):
            return False
    if types and page.page_type not in types:
        return False
    if projects:
        page_projects = all_projects(page.frontmatter)
        if not any(p in page_projects for p in projects):
            return False
    if tags:
        page_tags = set(page.tags)
        if not any(t.lower() in page_tags for t in tags):
            return False
    if speakers:
        page_speakers = {s.lower() for s in page.speakers}
        if not any(s.lower() in page_speakers for s in speakers):
            return False
    # File-type scoping (opt-in; default None/None lets every kind through — a
    # search must never hide an artifact type by default).
    if file_types or exclude_file_types:
        kind = page.file_kind
        if file_types and kind not in {ft.lower() for ft in file_types}:
            return False
        if exclude_file_types and kind in {ft.lower() for ft in exclude_file_types}:
            return False
    return True


def all_projects(fm: dict) -> set[str]:
    out: set[str] = set()
    if p := fm.get("project"):
        out.add(str(p))
    if ps := fm.get("projects"):
        if isinstance(ps, list):
            out.update(str(x) for x in ps)
        else:
            out.add(str(ps))
    return out
