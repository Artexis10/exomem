"""The read interface of a store-mode vault (OpenSpec make-vault-store-canonical, tasks 1.2, 1.6).

In store mode the collection store holds each page's exact bytes, so a reader asks the store where a
file-mode reader asks the disk. ``snapshot`` gives one consistent view at one store revision: every
page, binary record and derived row that a reader asks for through it belongs to the same committed
state. ``owned_page`` is the one-page form that the byte chokepoints in ``vault`` and
``reserved_paths`` call: it answers only for a path that the store serves, and returns None for
every other path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import TypeVar

from .. import find_corpus, reserved_paths, vault
from ..collection_store import connection, runtime
from . import mode, ownership

NOT_ACTIVE = "VAULT_STORE_NOT_ACTIVE"
T = TypeVar("T")

# One covering index serves every metadata read, so a point read of a page's hash never walks the
# body's overflow pages (the body precedes the hash in the row). SQLite prefers the primary key for
# a point lookup, so the hint is what keeps the read off the body.
_META_COLUMNS = "path, revision, sha256, size, mtime_ns, frontmatter_json"
_META_FROM = "store_pages INDEXED BY store_pages_meta"
#: The last code point sorts after every character: ``path < prefix + _AFTER`` ends a prefix range.
_AFTER = "\U0010ffff"


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

    def page(self, rel: str) -> StoredPage | None:
        """One page's exact bytes with its metadata, or None when the store holds no such page."""
        row = self.connection.execute(
            f"SELECT {_META_COLUMNS}, body FROM store_pages WHERE path = ?", (rel,)
        ).fetchone()
        return None if row is None else StoredPage(row[-1], PageMeta(*row[:-1]))

    def page_meta(self, rel: str) -> PageMeta | None:
        """One page's path, revision, hash, size, mtime and frontmatter, without its body."""
        row = self.connection.execute(
            f"SELECT {_META_COLUMNS} FROM {_META_FROM} WHERE path = ?", (rel,)
        ).fetchone()
        return None if row is None else PageMeta(*row)

    def pages(self, prefix: str = "") -> Iterator[PageMeta]:
        """The metadata of every page whose path starts with ``prefix``, in path order."""
        select = f"SELECT {_META_COLUMNS} FROM {_META_FROM}"
        if prefix:
            rows = self.connection.execute(
                f"{select} WHERE path >= ? AND path < ? ORDER BY path", (prefix, prefix + _AFTER)
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
                (lower, prefix + _AFTER),
            ).fetchone()
            if row is None:
                return
            name, slash, _ = row[0][len(prefix) :].partition("/")
            yield Child(name, is_dir=bool(slash))
            # "0" is the code point after "/", so every path below "name/" sorts before it.
            lower = prefix + name + ("0" if slash else "\x00")

    def binary(self, rel: str) -> BinaryMeta | None:
        """The record of one binary, or None when the store records no such path."""
        row = self.connection.execute(
            "SELECT path, sha256, size, revision FROM store_binaries WHERE path = ?", (rel,)
        ).fetchone()
        return None if row is None else BinaryMeta(*row)


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


def walk_member(rel: str) -> bool:
    """Whether ``vault.walk_vault_md`` would reach this stored page, judged by path alone.

    The walk's own entry rule (``vault._walk_vault_md_entry``) also lstat's the file for a symlink,
    a hard link and a regular file, and a stored page may have no file yet, so it cannot judge a
    page. That is the gap: this keeps the rule's path tests (the reserved registry, the excluded
    scan folders, the Markdown suffix), and ``ownership.classify`` already keeps out a conflict copy.
    """
    return (
        ownership.is_markdown(rel)
        and not reserved_paths.classify_logical(rel).blocked
        and not vault.in_excluded_scan_dir(rel)
    )


@contextmanager
def snapshot(vault_root: Path) -> Iterator[StoreSnapshot]:
    """Open one consistent view of a store-mode vault's store.

    The first read pins the WAL snapshot, so ``revision`` is the revision that every later read
    sees. A vault whose store row does not say store mode raises ``VAULT_STORE_NOT_ACTIVE``: an
    unknown state never reads as an empty store. That row is also the check on a reader's kept
    mode answer, at no cost beyond this query: when the row disagrees, the kept answer is dropped,
    so a read that runs again meets the mode the records now show.
    """
    root = Path(vault_root)
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


def current_revision(vault_root: Path) -> int:
    """The store revision now: it moves by one for each committed change."""
    with snapshot(vault_root) as view:
        return view.revision


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
