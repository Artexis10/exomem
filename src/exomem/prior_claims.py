"""Prior-path claims for link resolution: the derived index, read once per index state.

The resolver's prior-path and prior-stem steps, and its guard against a file that sits at
a reserved path, read the derived `prior_paths` table (`memory_refs`). The table holds
only the paths that pages left, so it is small, and a vault without moves pays one empty
read. `current(vault_root)` reads it once and keeps the answer until the index file
changes, as `collection_store.aliases.LinkTargets` keeps a collection's rows for a query:
every link after the first is answered from memory.

The table is read from the store in store mode and from the reference sidecar otherwise.
An unavailable or older index claims nothing: no read scans the vault for lineage.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TypeVar

_T = TypeVar("_T")


def _index_files(vault_root: Path) -> tuple[bool, Path]:
    """Whether the store serves this vault, and the database file that holds its index."""
    from . import memory_refs
    from .collection_store import connection
    from .vault_store import mode

    if mode.store_mode_for_reads(vault_root):
        return True, connection.store_path(vault_root)
    return False, memory_refs.ReferenceIndex(vault_root).path


def _signature(database: Path) -> tuple[object, ...]:
    """A cheap token that changes with every commit to `database` (its file and its WAL)."""
    marks: list[object] = []
    for path in (database, database.with_name(database.name + "-wal")):
        try:
            info = path.stat()
        except OSError:
            marks.append(None)
            continue
        marks.append((info.st_ino, info.st_size, info.st_mtime_ns))
    return tuple(marks)


def _read(
    vault_root: Path,
    stored: bool,
    query: Callable[[sqlite3.Connection], _T],
    *,
    newest: bool = False,
) -> _T | None:
    """`query` on the index that serves this vault, or None when no current index exists.

    It reads at the revision of this thread's store request unless `newest` asks for the newest
    commit.
    """
    from . import memory_refs
    from .collection_store import connection
    from .vault_store import read

    if stored:
        try:
            with read.snapshot(vault_root, join_request=not newest) as view:
                return query(view.connection)
        except sqlite3.OperationalError:
            # A store an older build set up has no prior-path table until its registry pass runs.
            return None
        except connection.CollectionStoreError:
            return None
    conn = memory_refs.ReferenceIndex(vault_root)._current_readonly_connection()
    if conn is None:
        # A cold or older sidecar claims nothing now; a service builds it once in the background.
        if memory_refs._managed_runtime():
            memory_refs.request_rebuild(vault_root)
        return None
    try:
        return query(conn)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _name_key(path: str) -> str:
    from .held_fs import vault_name_key

    return vault_name_key(path.replace("\\", "/").strip("/"))


def reservation_key(rel: str) -> str:
    """The key under which a path is reserved: the vault's name identity, without `.md`.

    Two spellings that differ only in case or Unicode form name one file on Windows and macOS,
    so a reservation holds for every spelling (`held_fs.vault_name_key`).
    """
    return _name_key(rel).removesuffix(".md")


class PriorClaims:
    """Which pages claim each prior path and prior stem, at one state of the index."""

    def __init__(
        self,
        vault_root: Path,
        stored: bool,
        rows: list[tuple[str, str, str]],
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.root = Path(vault_root)
        self._stored = stored
        self._connection = connection
        self._rows = rows
        by_key: dict[str, list[str]] = {}
        by_stem: dict[str, list[str]] = {}
        for prior, path, stem in rows:
            by_key.setdefault(prior.removesuffix(".md"), []).append(path)
            by_stem.setdefault(stem, []).append(path)
        self._by_key = {key: tuple(dict.fromkeys(value)) for key, value in by_key.items()}
        self._by_stem = {key: tuple(dict.fromkeys(value)) for key, value in by_stem.items()}
        self._identified: dict[str, bool] = {}
        self._reserved: dict[str, tuple[str, ...]] | None = None
        self._names: dict[str, frozenset[str]] | None = None

    def with_pages(self, pages: Mapping[str, bytes | None]) -> PriorClaims:
        """These claims once each page in `pages` holds its bytes, or is gone where they are None.

        A derivation of the state a commit will leave binds them before the index has refreshed,
        as a store commit binds its own transaction's claims (`from_connection`).
        """
        from . import memory_refs, relayout_record

        relayout = relayout_record.load(self.root)
        rows = [row for row in self._rows if row[1] not in pages]
        for rel, raw in pages.items():
            if raw is not None:
                rows.extend(memory_refs.prior_path_rows(rel, raw, relayout))
        claims = PriorClaims(self.root, self._stored, rows, self._connection)
        claims._identified.update(self._identified)
        for rel, raw in pages.items():
            row = None if raw is None else memory_refs.identity_row(self.root, rel, raw)
            claims._identified[rel] = row is not None and row[4] == "valid"
        return claims

    def names_in(self, folder: str) -> frozenset[str]:
        """The name keys (`reservation_key`) of the prior paths claimed directly in `folder`."""
        if self._names is None:
            names: dict[str, set[str]] = {}
            for key in self._reservations():
                parent, _, leaf = key.rpartition("/")
                names.setdefault(parent, set()).add(leaf + ".md")
            self._names = {parent: frozenset(leaves) for parent, leaves in names.items()}
        return self._names.get(_name_key(folder), frozenset())

    def __bool__(self) -> bool:
        return bool(self._by_key)

    def claimants(self, key: str) -> tuple[str, ...]:
        """The pages, by current path, whose lineage or relayout rows claim the link key `key`.

        A link key is a vault-relative path without `.md`, as the resolver keys paths.
        """
        return self._by_key.get(key, ())

    def reserving(self, rel: str) -> tuple[str, ...]:
        """The pages that claim `rel` in any spelling of its name (`reservation_key`)."""
        return self._reservations().get(reservation_key(rel), ())

    def _reservations(self) -> dict[str, tuple[str, ...]]:
        if self._reserved is None:
            reserved: dict[str, list[str]] = {}
            for key, paths in self._by_key.items():
                reserved.setdefault(reservation_key(key), []).extend(paths)
            self._reserved = {key: tuple(dict.fromkeys(value)) for key, value in reserved.items()}
        return self._reserved

    def stem_claimants(self, stem: str) -> tuple[str, ...]:
        """The pages that claim a prior path whose file name is `stem`."""
        return self._by_stem.get(stem, ())

    def has_identity(self, path: str) -> bool:
        """Whether the page at `path` holds a well-formed `exomem_id`, from the identity index."""
        if path not in self._identified:

            def query(conn: sqlite3.Connection) -> object:
                return conn.execute(
                    "SELECT 1 FROM identities WHERE path = ? AND status = 'valid'", (path,)
                ).fetchone()

            row = (
                query(self._connection)
                if self._connection is not None
                else _read(self.root, self._stored, query)
            )
            self._identified[path] = row is not None
        return self._identified[path]


_ROWS_QUERY = "SELECT prior_path, path, prior_stem FROM prior_paths ORDER BY prior_path, path"


def changed_priors(before: PriorClaims, after: PriorClaims, paths: Iterable[str]) -> set[str]:
    """The prior paths claimed differently in `after` than in `before`, with each prior path a
    page at one of `paths` claims in either: a link that names one of them may resolve anew."""
    pages = set(paths)
    before_rows, after_rows = set(before._rows), set(after._rows)
    return {prior for prior, _path, _stem in before_rows ^ after_rows} | {
        prior for prior, path, _stem in before_rows | after_rows if path in pages
    }


def from_connection(vault_root: Path, conn: sqlite3.Connection) -> PriorClaims:
    """The claims that `conn`'s transaction holds, for a derivation inside that transaction.

    A store commit derives graph rows against the claims it is writing, not the last commit's.
    """
    rows = conn.execute(_ROWS_QUERY).fetchall()
    return PriorClaims(vault_root, True, [(str(a), str(b), str(c)) for a, b, c in rows], conn)


_CACHE: dict[str, tuple[tuple[object, ...], PriorClaims]] = {}
_CACHE_LOCK = threading.Lock()


_MEMO_KEY = "prior_claims"


def current(vault_root: Path, *, newest: bool = False) -> PriorClaims:
    """The claims at the index's current state, read once until the index changes.

    A store request reads them once at its own revision, which the file token of the process
    cache cannot name. `newest` reads the newest commit instead.
    """
    from .vault_store import read

    root = Path(vault_root)
    stored, database = _index_files(root)
    memo = read.request_memo(root) if stored and not newest else None
    if memo is not None:
        claims = memo.get(_MEMO_KEY)
        if claims is None:
            rows = _read(root, True, lambda conn: conn.execute(_ROWS_QUERY).fetchall())
            claims = memo[_MEMO_KEY] = PriorClaims(
                root, True, [(str(a), str(b), str(c)) for a, b, c in rows or ()]
            )
        return claims
    # Taken before the read, so a commit that lands during it changes the token and forces a reread.
    token = (stored, *_signature(database))
    key = str(root)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None and cached[0] == token:
        return cached[1]
    rows = _read(root, stored, lambda conn: conn.execute(_ROWS_QUERY).fetchall(), newest=True)
    claims = PriorClaims(root, stored, [(str(a), str(b), str(c)) for a, b, c in rows or ()])
    with _CACHE_LOCK:
        _CACHE[key] = (token, claims)
    return claims


def _live(vault_root: Path, rel: str) -> bool:
    from . import vault

    return vault.served_page_bytes(vault_root, rel) is not None


def reserved_claimants(
    vault_root: Path, rel: str, *, own: str | None = None, newest: bool = False
) -> tuple[str, ...]:
    """The live pages that claim `rel`, in any spelling of its name, as a prior path.

    None when `own` is one of them: `own` is the current path of a page that moves to `rel`, and
    a move back into the page's own lineage takes the path it left.
    """
    claimants = current(vault_root, newest=newest).reserving(rel)
    if not claimants or (
        own is not None and reservation_key(own) in {reservation_key(path) for path in claimants}
    ):
        return ()
    return tuple(path for path in claimants if _live(vault_root, path))


class ReservedPathError(ValueError):
    """A create or move names a path that a live page's lineage still claims."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def require_unreserved(
    vault_root: Path, rel: str, *, own: str | None = None, newest: bool = True
) -> None:
    """Refuse a write that would take `rel` while a live page claims it as a prior path.

    The reservation covers every spelling of `rel` that names the same file: case and Unicode
    form variants (`reservation_key`). A caller that may see a claimant hears which page holds
    the path. A caller that may see none gets the refusal that any unavailable target gets,
    which names no claimant, path or content.

    Under path-based scopes, admission refuses a create at a hidden page's current path before
    anything is disclosed. A create at its former path refuses here in one vault and succeeds in
    a twin vault without the claim. So the reservation does disclose something new: that a live page
    the writer cannot see once lived at that exact path, or a spelling variant of it, in a folder
    the writer can write to. It never says which page. The reservation stands for integrity:
    without it, the writer's page would win exact-path resolution and take the owner's old links.

    A commit reads the newest claims, because no commit guard rechecks a reservation made after
    its request began; a validation passes `newest=False` and reads its request's revision.
    """
    from .governance import egress

    claimants = reserved_claimants(vault_root, rel, own=own, newest=newest)
    if not claimants:
        return
    visible = egress.restricted_release_filter(vault_root)
    shown = [path for path in claimants if visible is None or visible(path)]
    if not shown:
        raise ReservedPathError("WRITE_REFUSED", "target is unavailable")
    raise ReservedPathError(
        "FILE_EXISTS",
        f"{rel} is reserved: {', '.join(shown)} moved from it and links to it still resolve there",
    )


def reserved_names(vault_root: Path, folder_rel: str) -> frozenset[str]:
    """The name keys (`reservation_key` plus `.md`) of claimed prior paths in `folder_rel`.

    `unique_path` compares a candidate file name by the same key.
    """
    return current(vault_root).names_in(folder_rel)


def issues(vault_root: Path) -> list[dict[str, object]]:
    """The audit's prior-path findings, from the derived index in one read.

    `prior_path_conflict`: two live pages claim one prior path, so its links stay unresolved.
    `reserved_path_occupied`: a page without a valid `exomem_id` sits at a claimed prior path;
    links to that path keep resolving to the claimant.
    `reserved_path_identified`: a page with a valid `exomem_id` sits at a claimed prior path,
    for example after a create raced the move or a sync brought the page in. Exact-path
    resolution sends the old links to that page, not to the claimant, so nothing else shows it.
    """
    root = Path(vault_root)
    stored, _database = _index_files(root)
    rows = _read(
        root,
        stored,
        lambda conn: conn.execute(
            "SELECT p.prior_path, p.path, i.status FROM prior_paths p "
            "LEFT JOIN identities i ON i.path = p.prior_path ORDER BY p.prior_path, p.path"
        ).fetchall(),
    ) or []
    claimants: dict[str, list[str]] = {}
    occupied: dict[str, str] = {}
    for prior, path, status in rows:
        claimants.setdefault(str(prior), []).append(str(path))
        if status is not None:
            occupied[str(prior)] = str(status)
    found: list[dict[str, object]] = []
    for prior, paths in claimants.items():
        if len(paths) > 1:
            found.append({"kind": "prior_path_conflict", "path": prior, "paths": paths})
        if prior in occupied:
            kind = "reserved_path_identified" if occupied[prior] == "valid" else "reserved_path_occupied"
            found.append({"kind": kind, "path": prior, "paths": paths})
    return found
