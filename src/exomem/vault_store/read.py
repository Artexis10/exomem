"""The read interface of a store-mode vault (OpenSpec make-vault-store-canonical, tasks 1.2, 1.6).

In store mode the collection store holds each page's exact bytes, so a reader asks the store where a
file-mode reader asks the disk. ``snapshot`` gives one consistent view at one store revision: every
page, binary record and derived row that a reader asks for through it belongs to the same committed
state. ``owned_page`` is the one-page form that the byte chokepoints in ``vault`` and
``reserved_paths`` call: it answers only for a path that the store serves, and returns None for
every other path.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from sqlite3 import Connection
from typing import TypeVar

from .. import find_corpus, reserved_paths, vault
from ..collection_store import connection, runtime
from . import mode, ownership

NOT_ACTIVE = "VAULT_STORE_NOT_ACTIVE"
#: A reader of derived rows that this build adds, on a store that an older build set up and that no
#: registry pass has given those rows yet (``rederive.OWNER_SCHEMA``).
DERIVED_BEHIND = "VAULT_STORE_DERIVED_BEHIND"
T = TypeVar("T")

# One covering index serves every metadata read, so a point read of a page's hash never walks the
# body's overflow pages (the body precedes the hash in the row). SQLite prefers the primary key for
# a point lookup, so the hint is what keeps the read off the body.
_META_COLUMNS = "path, revision, sha256, size, mtime_ns, frontmatter_json"
_META_FROM = "store_pages INDEXED BY store_pages_meta"
#: The last code point sorts after every character: ``path < prefix + AFTER`` ends a prefix range.
AFTER = "\U0010ffff"


@dataclass(frozen=True, slots=True)
class PageMeta:
    """A stored page without its body: what a stat-and-hash read of the file would give."""

    path: str
    revision: int
    sha256: str
    size: int
    mtime_ns: int
    #: The stored frontmatter as JSON text, or None when the page has none.
    frontmatter_json: str | None

    @property
    def mtime(self) -> float:
        """Modification seconds with the same rounding as the file stat's timestamp."""
        seconds, nanos = divmod(self.mtime_ns, 1_000_000_000)
        return seconds + nanos / 1e9


@dataclass(frozen=True, slots=True)
class StoredPage:
    """A stored page with its exact bytes, read in one snapshot."""

    body: bytes
    meta: PageMeta


@dataclass(frozen=True, slots=True)
class BinaryMeta:
    """A binary the store records but does not hold: its bytes stay on disk."""

    path: str
    sha256: str
    size: int
    revision: int


@dataclass(frozen=True, slots=True)
class Child:
    """One direct child of a folder: a page, or a folder that holds at least one page."""

    name: str
    is_dir: bool


@dataclass(frozen=True, slots=True)
class StoreSnapshot:
    """One read-only view of the store at ``revision``; it ends when ``snapshot`` returns.

    ``connection`` is the pinned read-only connection, for the derived tables (lexical, graph,
    references) that a reader joins against the pages. Every read through it and through the methods
    below sees the same commit.
    """

    revision: int
    connection: Connection
    #: What a reader works out once per snapshot (``readiness``), shared by every read that joins it.
    memo: dict = field(default_factory=dict, compare=False, repr=False)

    def page(self, rel: str) -> StoredPage | None:
        """One page's exact bytes with its metadata, or None when the store holds no such page."""
        sources = self.memo.get("corpus_sources")
        if sources is not None:
            return sources.get(rel)
        prefetched = self.memo.get("pages", {})
        if rel in prefetched:
            return prefetched[rel]
        row = self.connection.execute(
            f"SELECT {_META_COLUMNS}, body FROM store_pages WHERE path = ?", (rel,)
        ).fetchone()
        return None if row is None else StoredPage(row[-1], PageMeta(*row[:-1]))

    def page_meta(self, rel: str) -> PageMeta | None:
        """One page's path, revision, hash, size, mtime and frontmatter, without its body."""
        metadata = self.memo.setdefault("page_metadata", {})
        if rel in metadata:
            return metadata[rel]
        if "corpus_sources" in self.memo:
            return None
        prefetched = self.memo.get("pages", {})
        if rel in prefetched:
            page = prefetched[rel]
            return None if page is None else page.meta
        row = self.connection.execute(
            f"SELECT {_META_COLUMNS} FROM {_META_FROM} WHERE path = ?", (rel,)
        ).fetchone()
        metadata[rel] = None if row is None else PageMeta(*row)
        return metadata[rel]

    def prime_page_metadata(self, rels: Iterable[str]) -> None:
        """Read the supplied pages' metadata in batches before checking each page."""
        metadata = self.memo.setdefault("page_metadata", {})
        missing = set(rels) - metadata.keys()
        found = {row[0]: PageMeta(*row) for row in self._each(missing, _META_COLUMNS)}
        metadata.update((rel, found.get(rel)) for rel in missing)

    def page_sources(self) -> Iterator[StoredPage]:
        """Each page's bytes and metadata, loaded once when a request scans the whole corpus."""
        from .. import request_budget

        sources = self.memo.get("corpus_sources")
        if sources is None:
            sources = {}
            for row in self.connection.execute(
                f"SELECT {_META_COLUMNS}, body FROM store_pages ORDER BY path"
            ):
                request_budget.checkpoint()
                meta = PageMeta(*row[:-1])
                self.memo.setdefault("page_metadata", {})[meta.path] = meta
                sources[meta.path] = StoredPage(row[-1], meta)
            # Audit's parse, link and drift scans share bytes until this pinned snapshot ends.
            self.memo["corpus_sources"] = sources
        yield from sources.values()

    def pages(self, prefix: str = "") -> Iterator[PageMeta]:
        """The metadata of every page whose path starts with ``prefix``, in path order."""
        select = f"SELECT {_META_COLUMNS} FROM {_META_FROM}"
        if prefix:
            rows = self.connection.execute(
                f"{select} WHERE path >= ? AND path < ? ORDER BY path", (prefix, prefix + AFTER)
            )
        else:
            rows = self.connection.execute(f"{select} ORDER BY path")
        for row in rows:
            yield PageMeta(*row)

    def children(self, dir_rel: str) -> Iterator[Child]:
        """The direct children of a folder, in name order: the pages and the folders that hold pages.

        A page that a commit stored counts at once, whether or not the projector has written its
        file, so a collision check reads the store and not the projection. ``dir_rel`` is the
        folder's vault-relative path, or "" for the vault root. The scan runs on the primary key and
        jumps from one child to the next, so a folder's own pages are not visited.
        """
        prefix = f"{dir_rel}/" if dir_rel else ""
        lower = prefix
        while True:
            row = self.connection.execute(
                "SELECT path FROM store_pages WHERE path >= ? AND path < ? ORDER BY path LIMIT 1",
                (lower, prefix + AFTER),
            ).fetchone()
            if row is None:
                return
            name, slash, _ = row[0][len(prefix) :].partition("/")
            yield Child(name, is_dir=bool(slash))
            # "0" is the code point after "/", so every path below "name/" sorts before it.
            lower = prefix + name + ("0" if slash else "\x00")

    def held_pages(self, rels: Iterable[str]) -> frozenset[str]:
        """The paths in ``rels`` that the store holds a page at, read in a few queries."""
        return frozenset(path for path, in self._each(rels, "path"))

    def page_metas(self, rels: Iterable[str]) -> dict[str, PageMeta]:
        """Each requested page's metadata, in keyed batches without loading its body."""
        return {row[0]: PageMeta(*row) for row in self._each(rels, _META_COLUMNS, metadata_only=True)}

    def prefetch_pages(self, rels: Iterable[str]) -> None:
        """Hold a bounded caller-selected batch for repeated parent and target reads in this snapshot."""
        pages = self.memo.setdefault("pages", {})
        wanted = {rel for rel in rels if rel not in pages}
        loaded = {
            row[0]: StoredPage(row[-1], PageMeta(*row[:-1]))
            for row in self._each(wanted, f"{_META_COLUMNS}, body")
        }
        pages.update(dict.fromkeys(wanted))
        pages.update(loaded)

    def bodies(self, rels: Iterable[str]) -> dict[str, bytes]:
        """The exact bytes of each page in ``rels`` that the store holds, read in a few queries."""
        sources = self.memo.get("corpus_sources")
        if sources is not None:
            return {rel: sources[rel].body for rel in rels if rel in sources}
        return dict(self._each(rels, "path, body"))

    def _each(self, rels: Iterable[str], columns: str, *, metadata_only: bool = False) -> Iterator[tuple]:
        wanted = list(dict.fromkeys(rels))
        # Metadata batches use the same covering index as point metadata reads.
        source = _META_FROM if metadata_only or columns == _META_COLUMNS else "store_pages"
        for start in range(0, len(wanted), KEYS_PER_QUERY):
            chunk = wanted[start : start + KEYS_PER_QUERY]
            yield from self.connection.execute(
                f"SELECT {columns} FROM {source} WHERE path IN ({', '.join('?' * len(chunk))})",
                chunk,
            )

    def binary(self, rel: str) -> BinaryMeta | None:
        """The record of one binary, or None when the store records no such path."""
        row = self.connection.execute(
            "SELECT path, sha256, size, revision FROM store_binaries WHERE path = ?", (rel,)
        ).fetchone()
        return None if row is None else BinaryMeta(*row)

    def binaries(self, prefix: str) -> Iterator[BinaryMeta]:
        """The record of every binary whose path starts with ``prefix``, in path order."""
        yield from (
            BinaryMeta(*row)
            for row in self.connection.execute(
                "SELECT path, sha256, size, revision FROM store_binaries "
                "WHERE path >= ? AND path < ? ORDER BY path",
                (prefix, prefix + AFTER),
            )
        )

    def holds_below(self, folder: str) -> bool:
        """Whether the store holds a page or a binary record whose path is below ``folder``."""
        prefix = f"{folder}/"
        return (
            next(self.pages(prefix), None) is not None
            or next(self.binaries(prefix), None) is not None
        )

    def binary_records(self, rels: Iterable[str]) -> dict[str, BinaryMeta]:
        """The record of each binary in ``rels`` that the store records, read in a few queries."""
        wanted = list(dict.fromkeys(rels))
        found: dict[str, BinaryMeta] = {}
        for start in range(0, len(wanted), KEYS_PER_QUERY):
            chunk = wanted[start : start + KEYS_PER_QUERY]
            for row in self.connection.execute(
                "SELECT path, sha256, size, revision FROM store_binaries "
                f"WHERE path IN ({', '.join('?' * len(chunk))})",
                chunk,
            ):
                found[row[0]] = BinaryMeta(*row)
        return found


@dataclass(frozen=True, slots=True)
class Listing:
    """The store's names below one folder, relative to it."""

    pages: tuple[str, ...]
    #: The folders that hold at least one listed page.
    folders: tuple[str, ...]


def listing(vault_root: Path, folder: str, recursive: bool) -> Listing:
    """The store's pages and the folders that hold them below ``folder`` ("" for the vault root).

    A page under a reserved path is left out, as the walk of the disk leaves out its file. A page
    that a commit stored counts at once, whether or not the projector has written its file. A
    vault that is not in store mode raises ``VAULT_STORE_NOT_ACTIVE``.
    """
    root = _as_path(vault_root)
    prefix = f"{folder.rstrip('/')}/" if folder else ""
    pages: list[str] = []
    folders: set[str] = set()
    with snapshot(root) as view:
        if recursive:
            for meta in view.pages(prefix):
                if reserved_paths.classify_logical(meta.path).blocked:
                    continue
                name = meta.path[len(prefix) :]
                pages.append(name)
                parts = name.split("/")
                folders.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
        else:
            for child in view.children(folder):
                if reserved_paths.classify_logical(prefix + child.name).blocked:
                    continue
                if child.is_dir:
                    folders.add(child.name)
                else:
                    pages.append(child.name)
    return Listing(tuple(pages), tuple(sorted(folders)))


def holds_folder(vault_root: Path, rel: str) -> bool:
    """Whether the vault is in store mode and its store holds a page below this folder."""
    root = _as_path(vault_root)
    if not mode.store_mode_for_reads(root):
        return False
    with snapshot(root) as view:
        return next(view.children(rel), None) is not None


def corpus_member(rel: str, scope: str) -> bool:
    """Whether the corpus of ``scope`` ("kb" or "vault") holds this stored page, as a walk of the
    disk would.

    The Knowledge Base corpus is ``find_corpus.walk_md`` over that folder; the vault corpus is
    ``vault.walk_vault_md``. The two differ in their anchor and in the folders they skip.
    """
    return find_corpus.kb_walk_member(rel) if scope == "kb" else walk_member(rel)


#: The most paths or page keys that one keyed store query binds. SQLite allows 32766 parameters;
#: a smaller chunk keeps each statement short to prepare and each result small.
KEYS_PER_QUERY = 500


def walk_member(rel: str) -> bool:
    """Whether ``vault.walk_vault_md`` would reach the file at this path, judged by path alone.

    The walk's own entry rule (``vault._walk_vault_md_entry``) also lstat's the file for a symlink,
    a hard link and a regular file, and a stored page may have no file yet, so it cannot judge a
    page. That is the gap: this keeps the rule's path tests (the reserved registry, the excluded
    scan folders, the Markdown suffix, the conflict copy). The importer asks it about files that
    the store does not own, so it cannot leave the conflict copy to ``ownership.classify``.
    """
    return (
        find_corpus.walk_name_admits(rel.rpartition("/")[2], directory=False)
        and not reserved_paths.classify_logical(rel).blocked
        and not vault.in_excluded_scan_dir(rel)
    )


def derived_behind():
    """The refusal of a reader whose derived rows the store does not hold yet: never an empty answer."""
    from ..cli_ops import OpError

    return OpError(
        DERIVED_BEHIND,
        "This vault's store was set up by an older Exomem build, and its link rows are not derived "
        "yet, so Exomem cannot say which pages link here.",
        "Start the Exomem service: it derives the rows in the background. `exomem store-mode "
        "status` shows `owner_schema` as current once they are done.",
    )


def require_link_rows(conn: Connection) -> None:
    """Refuse a read of the store's link rows while a store that an older build set up lacks some:
    between the batches of its pass the tables exist, but the pages after its cursor lack rows."""
    from . import rederive

    if rederive.owner_schema_behind(conn):
        raise derived_behind()


@dataclass(frozen=True, slots=True)
class StoreReadiness:
    """How current the derived rows of one snapshot are: what a store reader serves and shows.

    The snapshot's revision proves the rows current for the pages: every commit derives its pages'
    rows in its own transaction. The registries are the one input that can lag them (``rederive``):
    ``registry`` is that module's closed state, ``current``, ``rebuilding`` or ``unknown``.
    """

    revision: int
    registry: str
    registry_revision: int | None
    registry_target: int | None
    #: Overlay paths that still wait to reach the disk, which the registry pass waits for.
    waiting_on_paths: int
    #: When the importer last finished a sweep of the files that the store does not own, whose
    #: rows are as fresh as that sweep; None when no sweep is recorded.
    unowned_rows_as_of: str | None


def readiness(vault_root: Path) -> StoreReadiness:
    """The readiness of the snapshot that this request reads, worked out once per snapshot."""
    from . import rederive

    root = _as_path(vault_root)
    with snapshot(root) as view:
        found = view.memo.get("readiness")
        if found is None:
            registry = rederive.read(view.connection)
            waiting = 0 if registry.state == rederive.CURRENT else rederive.waiting_on(
                view.connection, root
            )
            found = view.memo["readiness"] = StoreReadiness(
                view.revision,
                registry.state,
                registry.revision,
                registry.target,
                waiting,
                unowned_sweep(view.connection).get("at"),
            )
        return found


#: The ``store_meta`` key of the importer's last full sweep of the files that the store does not
#: own: ``{"revision", "at", "corpus_files"}``. It is no derived row, so writing it moves no revision.
UNOWNED_SWEEP = "vault_store_unowned_sweep"


def unowned_sweep(conn: Connection) -> dict:
    """The importer's record of its last full sweep of the unowned files, or {} when none is kept."""
    import json

    row = conn.execute("SELECT value FROM store_meta WHERE key = ?", (UNOWNED_SWEEP,)).fetchone()
    try:
        found = json.loads(row[0]) if row is not None else {}
    except ValueError:
        return {}  # an unreadable record says no sweep is known, which status shows as such
    return found if isinstance(found, dict) else {}


#: The one sentence for each registry state that every store reader (search, graph or links)
#: attaches unchanged while the pass runs or its state is unknown: an agent can repeat it as is,
#: and the figures go in ``lag``. It names every derived row that the pass rewrites, so no reader
#: needs a sentence of its own. A pass follows a change to either overlay input, the schema or the
#: access policy (``rederive.is_overlay_source``), so the sentence names both.
_REBUILDING_NOTICE = (
    "A schema or access-policy change is still being applied to the stored pages, so some "
    "search, graph and link rows may follow the previous schema or access policy until it "
    "finishes."
)
_UNKNOWN_NOTICE = (
    "Exomem has not yet confirmed that the search, graph and link rows of every stored page "
    "follow the current schema and access policy; a background pass is checking them."
)


def registry_notice(state: StoreReadiness) -> dict | None:
    """The notice and lag figures for a reader to attach, or None when the registry is current.

    A reader serves the rows of its snapshot in every state: a pass re-derives pages in path order
    and never drops one, so only the labels of the pages after its cursor can lag, and the notice
    names them instead of refusing recall for the length of the pass.
    """
    from . import rederive

    if state.registry == rederive.CURRENT:
        return None
    return {
        "notice": _REBUILDING_NOTICE if state.registry == rederive.REBUILDING else _UNKNOWN_NOTICE,
        "lag": {
            "registry": state.registry,
            "revision": state.registry_revision,
            "target": state.registry_target,
            "waiting_on_paths": state.waiting_on_paths,
        },
    }


def request_registry_notice(vault_root: Path) -> dict | None:
    """``registry_notice`` at this request's snapshot, or None when the vault is not in store mode."""
    root = _as_path(vault_root)
    if not mode.store_mode_for_reads(root):
        return None
    try:
        return registry_notice(readiness(root))
    except connection.CollectionStoreError as error:
        if error.code != NOT_ACTIVE:
            raise
        return None  # the records stopped saying store mode: the request read files


def scope_triple(vault_root: Path, scope: str) -> tuple[int, int, str]:
    """A freshness triple for ``scope`` ("kb" or "vault") at this request's snapshot.

    It stands where file mode's triple over the files stands, as a cache key, so the key never
    needs a walk. The revision moves with every commit. A registry batch derives rows without a
    commit, and the catalogue's own triggers rotate its ``frequency_revision`` on every row write,
    so the key names both. The count is the derived page rows in the scope, read through the
    scope's index.
    """
    statement = _VAULT_PAGES if scope == "vault" else _KB_PAGES
    with snapshot(_as_path(vault_root)) as view:
        found = view.memo.get(statement)
        if found is None:
            count, rows = view.connection.execute(statement).fetchone()
            found = view.memo[statement] = (count, view.revision, f"store:{view.revision}:{rows}")
        return found


#: The page rows of each scope, counted through the scope's own index (``pages_kb``,
#: ``pages_vault``), with the revision that the catalogue's triggers give its rows.
_KB_PAGES = (
    "SELECT COUNT(*), (SELECT value FROM meta WHERE key = 'frequency_revision') "
    "FROM pages WHERE in_kb = 1"
)
_VAULT_PAGES = (
    "SELECT COUNT(*), (SELECT value FROM meta WHERE key = 'frequency_revision') "
    "FROM pages WHERE in_vault = 1"
)


def recall_checkpoint(vault_root: Path, scope: str):
    """The recall checkpoint of ``scope`` at this request's snapshot.

    The store's derived page rows are the recall projection, so the checkpoint names the store and
    its revision where file mode names a freshness generation, with the live policy identity.
    """
    from .. import freshness, recall_policy

    root = _as_path(vault_root)
    triple = scope_triple(root, scope)
    policy_version, access_fingerprint = recall_policy.recall_policy_identity(root)
    return freshness.RecallFreshnessCheckpoint(
        STORE_CHECKPOINT, triple[1], triple, policy_version, access_fingerprint
    )


#: The ``instance_id`` of a store checkpoint: no freshness registry instance issued it.
STORE_CHECKPOINT = "store"


def checkpoint_is_current(vault_root: Path, scope: str, expected) -> bool:
    """Whether a store checkpoint still names the store's projection: same revision and policy.

    Inside a request it reads the request's own snapshot, so a proof that the request took stays
    current for the request, as one freshness generation does in file mode.
    """
    try:
        return recall_checkpoint(vault_root, scope) == expected
    except connection.CollectionStoreError as error:
        if error.code != NOT_ACTIVE:
            raise
        return False  # the vault left store mode, so no store checkpoint names it any more


@dataclass(frozen=True, slots=True)
class _RequestScope:
    """The snapshot that one request holds open, and the thread and vault it belongs to."""

    root: Path
    thread: int
    view: StoreSnapshot


#: The open request scope of this context. A connection cannot cross threads, so a worker thread
#: that inherits the context still opens its own snapshot (see ``snapshot``).
_REQUEST: ContextVar[_RequestScope | None] = ContextVar(
    "vault_store_request_snapshot", default=None
)


@contextmanager
def request_snapshot(vault_root: Path) -> Iterator[None]:
    """Hold one snapshot of the store for a whole request: every ``snapshot`` call that the request
    makes on this thread, for this vault, joins it.

    A reader that opens its own snapshot per page pays a connection, a transaction and a mode query
    for each page, and the pages of one request may then belong to different revisions. Here the
    request reads one revision. A vault that is not in store mode binds only its mode answer
    (``mode.read_mode_scope``), so a file-mode request pays one read of the mode records and no stat
    at each chokepoint; the scope nests, and an inner one joins the outer.
    """
    root = Path(vault_root)
    with mode.read_mode_scope(root) as stored:
        if _REQUEST.get() is None and stored:
            with ExitStack() as scope:
                try:
                    view = scope.enter_context(snapshot(root))
                except connection.CollectionStoreError as error:
                    if error.code != NOT_ACTIVE:
                        raise
                    # The records stopped saying store mode: the request reads files.
                else:
                    token = _REQUEST.set(_RequestScope(root, threading.get_ident(), view))
                    scope.callback(_REQUEST.reset, token)
                    yield
                    return
        yield


@contextmanager
def snapshot(vault_root: Path) -> Iterator[StoreSnapshot]:
    """Open one consistent view of a store-mode vault's store.

    The first read pins the WAL snapshot, so ``revision`` is the revision that every later read
    sees. A vault whose store row does not say store mode raises ``VAULT_STORE_NOT_ACTIVE``: an
    unknown state never reads as an empty store. That row is also the check on a reader's kept
    mode answer, at no cost beyond this query: when the row disagrees, the kept answer is dropped,
    so a read that runs again meets the mode the records now show.

    Inside ``request_snapshot`` on the same thread and vault, the request's own view is returned
    and nothing is opened.
    """
    root = Path(vault_root)
    scope = _REQUEST.get()
    if scope is not None and scope.thread == threading.get_ident() and scope.root == root:
        yield scope.view
        return
    path = connection.store_path(root)
    with connection.store_reader(path, runtime.reader_pool(root, path)) as reader:
        reader.execute("BEGIN")
        try:
            row = reader.execute("SELECT mode, revision FROM store_mode WHERE id = 1").fetchone()
            if row is None or row[0] != mode.STORE_MODE:
                mode.forget_read_mode(vault_root)
                raise connection.CollectionStoreError(NOT_ACTIVE, "this vault is not in store mode")
            yield StoreSnapshot(revision=row[1], connection=reader)
        finally:
            if reader.in_transaction:
                reader.execute("ROLLBACK")


class _Lend:
    """The connection of a store snapshot, lent to a reader that treats it as its own and closes it.

    The graph readers open a private connection, install a deadline handler or SQL functions on it,
    and close it when they are done. Inside a request the lend is the request's own connection, so
    ``close`` ends only what lends opened: when the last open lend of the snapshot closes, it clears
    the progress handler and releases each SQL function registered through any of them (SQLite
    keeps the name, and a later call fails), and outside a request it ends the pooled snapshot. A lend that a reader opens while another is open (a nested
    read) leaves the outer reader's handler and functions in place. ``commit`` and ``rollback``
    raise, because either would end the snapshot that the rest of the request reads. Everything
    else delegates.
    """

    __slots__ = ("_view", "_scope")

    def __init__(self, view: StoreSnapshot, scope: ExitStack) -> None:
        self._view = view
        self._scope: ExitStack | None = scope
        view.memo["lends"] = view.memo.get("lends", 0) + 1

    def __getattr__(self, name: str):
        return getattr(self._view.connection, name)

    def __enter__(self):
        raise TypeError("a lent store snapshot is not a context manager; call close()")

    def __exit__(self, *exc):
        raise TypeError("a lent store snapshot is not a context manager; call close()")

    def create_function(self, name: str, narg: int, func, **options) -> None:
        self._view.connection.create_function(name, narg, func, **options)
        self._view.memo.setdefault("lent_functions", set()).add((name, narg))

    def commit(self) -> None:
        raise sqlite3.ProgrammingError("a lent store snapshot cannot commit; it is read-only")

    def rollback(self) -> None:
        raise sqlite3.ProgrammingError("a lent store snapshot cannot roll back the request's view")

    def close(self) -> None:
        scope, self._scope = self._scope, None
        if scope is None:
            return
        with scope:
            memo = self._view.memo
            memo["lends"] -= 1
            if memo["lends"]:
                return
            # A deadline handler or a function left on a shared or pooled connection would outlive
            # the read: it aborts the request's later reads and holds the reader's objects.
            connection = self._view.connection
            connection.set_progress_handler(None, 0)
            for name, narg in memo.pop("lent_functions", ()):
                connection.create_function(name, narg, None)


def lend(vault_root: Path) -> _Lend | None:
    """The connection of this request's snapshot (or of a pooled one), for a reader to close.

    None when the records no longer say store mode: the caller reads as file mode does.
    """
    scope = ExitStack()
    try:
        view = scope.enter_context(snapshot(_as_path(vault_root)))
    except connection.CollectionStoreError as error:
        scope.close()
        if error.code != NOT_ACTIVE:
            raise
        return None
    return _Lend(view, scope)


def current_revision(vault_root: Path) -> int:
    """The store revision now: it moves by one for each committed change and for each batch of the
    registry pass (``rederive.settle``), which changes derived rows without a page commit."""
    with snapshot(vault_root) as view:
        return view.revision


def snapshot_identity(reader, keys: tuple[str, ...]) -> dict[str, str]:
    """The open graph reader's identity: its store revision, or file mode's graph metadata.

    Graph generations advance on graph writes, but cannot identify every store commit.
    A lend already holds its revision. Static graph metadata retains the projection's identity;
    the copied recall checkpoint never describes a store snapshot.
    """
    values = {}
    if isinstance(reader, _Lend):
        values["generation"] = str(reader._view.revision)
        # These are graph metadata fields: the store revision replaces both volatile witnesses.
        keys = tuple(key for key in keys if key not in ("generation", "recall_projection_identity"))
    if keys:
        values.update(reader.execute(
            f"SELECT key, value FROM graph_meta WHERE key IN ({', '.join('?' for _ in keys)})",
            keys,
        ).fetchall())
    return values


def _as_path(value: Path | str) -> Path:
    """``Path(value)`` without copying a path that already is one: a file-mode read pays this per call."""
    return value if isinstance(value, Path) else Path(value)


def serves(vault_root: Path, rel: str) -> bool:
    """Whether the store, not the disk, answers for ``rel``: store mode is on and it owns the page.

    The one place that decides it; each byte chokepoint asks here. The path rule is pure and runs
    first, so a path that the store never owns costs no read of the mode records. The mode answer
    is the reader's: it holds until the manifest changes.
    """
    return (
        ownership.classify(rel) is ownership.Ownership.PAGE and mode.store_mode_for_reads(vault_root)
    )


def from_store(vault_root: Path, rel: str, store_read: Callable[[], T]) -> T | None:
    """``store_read()`` when the store serves ``rel``, else None: the disk decides.

    A reader whose kept mode answer is stale meets ``VAULT_STORE_NOT_ACTIVE`` from ``snapshot``,
    which has dropped that answer. The read then runs once more, through the mode that the records
    now show, and a second refusal reaches the caller.
    """
    root = _as_path(vault_root)
    if not serves(root, rel):
        return None
    try:
        return store_read()
    except connection.CollectionStoreError as error:
        if error.code != NOT_ACTIVE:
            raise
    return store_read() if serves(root, rel) else None


def prefetch_pages(vault_root: Path, rels: Iterable[str]) -> None:
    """Batch a request's upcoming page reads; unowned paths keep their file-mode readers."""
    root = _as_path(vault_root)
    if mode.store_mode_for_reads(root):
        with snapshot(root) as view:
            view.prefetch_pages(rel for rel in rels if ownership.classify(rel) is ownership.Ownership.PAGE)


def owned_page(vault_root: Path, rel: str) -> StoredPage | None:
    """The stored page at ``rel`` when the store serves that path, else None: read the file.

    A path that the store serves but holds no page for raises ``FileNotFoundError``, as a missing
    file does.
    """
    root = _as_path(vault_root)
    if not serves(root, rel):
        return None
    with snapshot(root) as view:
        page = view.page(rel)
    if page is None:
        raise FileNotFoundError(rel)
    return page


def owned_meta(vault_root: Path, rel: str) -> PageMeta | None:
    """``owned_page`` without the body: the stored page's size, hash and revision, else None."""
    root = _as_path(vault_root)
    if not serves(root, rel):
        return None
    with snapshot(root) as view:
        meta = view.page_meta(rel)
    if meta is None:
        raise FileNotFoundError(rel)
    return meta


def page_exists(vault_root: Path, rel: str) -> bool:
    """Whether a page is at ``rel``: the store's page when the store serves that path, else the
    file.
    """
    root = _as_path(vault_root)
    try:
        if owned_meta(root, rel) is not None:
            return True
    except FileNotFoundError:
        return False
    return (root / rel).exists()


def prime_page_metadata(vault_root: Path, rels: Iterable[str]) -> None:
    """Prime only the supplied store-owned pages in the current request snapshot."""
    if mode.store_mode_for_reads(vault_root):
        with snapshot(vault_root) as view:
            view.prime_page_metadata(
                rel for rel in rels if ownership.classify(rel) is ownership.Ownership.PAGE
            )


def holds_file(vault_root: Path, rel: str) -> bool:
    """``(root / rel).is_file()`` as the store sees it: for a path that the store serves, whether it
    holds the page, with no look at the projection; for any other path, the disk's answer."""
    root = _as_path(vault_root)
    try:
        return owned_meta(root, rel) is not None or (root / rel).is_file()
    except FileNotFoundError:
        return False


def page_source(reader: Connection, vault_root: Path, rel: str) -> tuple[bytes, int] | None:
    """The exact bytes and modification time that a commit derives the page at ``rel`` from, or
    None when it has none: the store's page when ``reader``'s snapshot holds one, else the file.

    A page path the store holds no row for is a file like any unowned one: the disk is its source
    until a governed write holds it. A graph reader that re-derives a page's links reads here, so
    it derives them from the bytes that its rows came from.
    """
    if ownership.classify(rel) is ownership.Ownership.PAGE:
        row = reader.execute(
            "SELECT body, mtime_ns FROM store_pages WHERE path = ?", (rel,)
        ).fetchone()
        if row is not None:
            return bytes(row[0]), row[1]
    path = _as_path(vault_root) / rel
    try:
        raw = vault.read_disk_bytes_without_pinning(path)
        return raw, os.stat(path).st_mtime_ns
    except OSError:
        return None


def link_target_exists(reader: Connection, vault_root: Path, rel: str) -> bool:
    """Whether a typed link to ``rel`` lands on a page, as a commit derives the graph rows: for a
    page path, whether ``reader``'s snapshot holds the page; for any other path, the file.

    A page path that the store holds no row for is no target, as every reader treats it: its file
    may be one that a commit just removed and the projector has not yet deleted, and nothing
    re-derives the linking page when the file goes. A graph reader that re-derives a page's links
    asks here, so they land as its rows did.
    """
    if ownership.classify(rel) is ownership.Ownership.PAGE:
        return (
            reader.execute("SELECT 1 FROM store_pages WHERE path = ?", (rel,)).fetchone()
            is not None
        )
    return (_as_path(vault_root) / rel).exists()


def served_lookup(vault_root: Path) -> Callable[[str], bool | None]:
    """``owned_meta``'s question for one pass over many paths: one query, not one per path.

    The lookup answers None for a path that the store does not serve (the disk decides it), and
    otherwise whether the store holds the page. It is as of the moment it was made.
    """
    root = _as_path(vault_root)
    if not mode.store_mode_for_reads(root):
        return _not_served
    with snapshot(root) as view:
        held = frozenset(row[0] for row in view.connection.execute("SELECT path FROM store_pages"))
    page = ownership.Ownership.PAGE
    return lambda rel: rel in held if ownership.classify(rel) is page else None


def held_lookup(vault_root: Path, rels: Iterable[str]) -> Callable[[str], bool | None]:
    """``served_lookup`` for the paths ``rels`` alone: a few keyed reads, not a read of every page.

    The lookup answers None for a path that the store does not serve, and whether the store holds
    each served path in ``rels``.
    """
    root = _as_path(vault_root)
    if not mode.store_mode_for_reads(root):
        return _not_served
    page = ownership.Ownership.PAGE
    with snapshot(root) as view:
        held = view.held_pages(rel for rel in rels if ownership.classify(rel) is page)
    return lambda rel: rel in held if ownership.classify(rel) is page else None


def _not_served(_rel: str) -> None:
    return None


def owned_bytes(vault_root: Path, rel: str) -> bytes | None:
    """``owned_page``'s bytes: the stored page when the store serves ``rel``, else None."""
    page = owned_page(vault_root, rel)
    return None if page is None else page.body


def owned_page_at(vault_root: Path, path: Path) -> StoredPage | None:
    """``owned_page`` for the file at ``path``, which a caller names by its location, not its vault path.

    A path that does not lie below the root is no page of it, and that lexical test runs first and
    costs no read of the mode records. In store mode only, the parent folder is resolved: a
    symlinked folder or a ``..`` spelling that leads into the vault names the page it reaches, and
    one that leads out names none. The file's own name is never resolved, so a symlink at a stored
    page's path does not name its target: the store key decides at the leaf. A file-mode read never
    pays that resolution.
    """
    root = _as_path(vault_root)
    try:
        rel = vault.relative_posix(path, root)  # a string slice for a path built under the root
    except ValueError:
        # A spelling of the vault that is not lexically below this root is a gap: it reads the disk.
        return None
    # A ``..`` can lead from a folder that is no page's into one that is, so it is not judged lexically.
    if ".." not in rel.split("/") and ownership.classify(rel) is not ownership.Ownership.PAGE:
        return None
    if not mode.store_mode_for_reads(root):
        return None
    key = _key_below(root, path)
    return None if key is None else owned_page(root, key)


def _key_below(root: Path, path: Path) -> str | None:
    """The vault path that the file at ``path`` names: its folders resolved, its own name never.

    None for a folder spelling and for a path whose folders lead out of the vault.
    """
    if path.name in ("", ".", ".."):
        return None  # a folder spelling, not a file's name: the store holds no page there
    try:
        return (path.parent.resolve() / path.name).relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def served_key(vault_root: Path, rel: str) -> str | None:
    """The store key of the page that vault path ``rel`` names: None when the disk decides.

    ``owned_page_at``'s rule for a caller that holds a vault path. The folders of ``rel`` resolve:
    a folder link inside the vault names the page it reaches and one that leads out names none. The
    page's own name never resolves: a link at a stored page's path is a stray projection, not the
    page, so it names neither its target nor an escape. A path that the store serves but holds no
    page for raises ``FileNotFoundError``, as ``owned_page`` does: the page is gone, and a file left
    at its name is not it.
    """
    root = _as_path(vault_root)
    if not serves(root, rel):
        return None
    key = _key_below(root, root / rel)
    if key is None:
        return None
    return key if owned_meta(root, key) is not None else None
