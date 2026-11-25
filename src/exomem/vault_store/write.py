"""The write entry point of a store-mode vault (OpenSpec make-vault-store-canonical, 1.6, 2.2, 2.3).

``commit`` changes the store's pages, binary records, projection manifest, audit trail and
projection queue, and the derived rows of those pages (lexical, graph, identity; see ``derive``), in
one ``BEGIN IMMEDIATE`` transaction and advances ``store_mode.revision`` by exactly one. A reader
sees all of a commit or none of it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from sqlite3 import Connection
from typing import TYPE_CHECKING, Any

from .. import epistemic_graph, lexstore, vault
from ..cli_ops import OpError
from ..collection_store import connection
from . import derivation, derive, mode, ownership, read, rederive
from .read import AFTER, NOT_ACTIVE

if TYPE_CHECKING:
    from pathlib import Path

    from ..collection_store.writer import CollectionWriter

CONFLICT = "VAULT_STORE_CONFLICT"
NOT_OWNED = "VAULT_STORE_NOT_OWNED"
# Exact per-page audit reason for a preserve write whose projection allocates original bytes.
MEDIA_PAYLOAD_WRITE_REASON = "preserve original Markdown"
log = logging.getLogger(__name__)


class Origin(StrEnum):
    """Who changed the store, as the audit row names it.

    A closed set that the store contract fixes: a governed write, a person's edit that the importer
    adopted, the migration, and the rollback. The last two spell what those commands record today.
    """

    GOVERNED = "governed write"
    OUTSIDE_EDITOR = "outside editor"
    MIGRATION = "store-mode migrate"
    ROLLBACK = "store-mode rollback"


@dataclass(frozen=True, slots=True)
class Expect:
    """What a path must hold in the store when the commit runs, or the whole commit is refused.

    ``absent`` says the store holds nothing at the path. Otherwise each field that is set must
    match the stored page or binary record. ``projected`` says that the file on disk is the stored
    page: the projection manifest records the page's own revision, no projection intent is queued
    for the path and no outside change is held for it. ``linked_from`` names every page that may
    link to the path: a link from a page outside it, in the inbound-link rows at commit time (the
    rule that ``list_inbound_links`` and a move's rewrite use), refuses the commit. A delete reads
    its inbound links and commits on that reading through it.

    ``pages_below`` and ``binaries_below`` bind a folder: the number of stored pages, and of binary
    records, whose path starts with ``rel + "/"``. A folder write binds the counts it read, so a page
    that a commit adds below the folder after the read refuses it. An expectation that sets only
    these says nothing of ``rel`` itself, which as a folder holds no page.
    """

    revision: int | None = None
    sha256: str | None = None
    absent: bool = False
    projected: bool = False
    linked_from: frozenset[str] | None = None
    pages_below: int | None = None
    binaries_below: int | None = None

    def __post_init__(self) -> None:
        if self.absent and self.names_path_state:
            raise ValueError("an expectation of absence names nothing else")

    @property
    def names_path_state(self) -> bool:
        """Whether the expectation binds what ``rel`` itself holds, beyond its absence."""
        return (self.revision, self.sha256, self.projected, self.linked_from) != (
            None,
            None,
            False,
            None,
        )

    @property
    def binds_path(self) -> bool:
        """Whether ``rel`` itself must hold something, or nothing: a bare folder count binds neither."""
        return (
            self.absent
            or self.names_path_state
            or (self.pages_below, self.binaries_below) == (None, None)
        )


@dataclass(frozen=True, slots=True)
class PagePut:
    """A page's new exact bytes.

    ``on_disk_mtime_ns`` says that the file already holds these bytes, with that modification
    time: the commit records the file in the projection manifest and queues nothing. Without it,
    the commit queues the page for projection and stamps the page with the wall clock.
    """

    rel: str
    body: bytes
    on_disk_mtime_ns: int | None = None
    media_allocation: bool = False


@dataclass(frozen=True, slots=True)
class PageDelete:
    """A page removed from the store.

    ``on_disk`` says that its file is already gone: the commit drops the file's manifest entry and
    queues nothing. ``absent_ok`` says that the store may already hold no page there, so that the
    commit then only settles the path's file: it queues the file's removal, or nothing with
    ``on_disk``.
    """

    rel: str
    on_disk: bool = False
    absent_ok: bool = False


@dataclass(frozen=True, slots=True)
class PageMove:
    """A page's bytes moved to a path the store does not hold.

    ``on_disk`` says that the file already moved there. ``body`` gives the page new bytes at its new
    path, as a move that rewrites the page's own links does; the move is still one life that the
    trail follows from the old path, and the projector writes the new bytes there.
    """

    src: str
    dst: str
    on_disk: bool = False
    body: bytes | None = None

    def __post_init__(self) -> None:
        if self.on_disk and self.body is not None:
            raise ValueError("a file that already moved holds the stored bytes, not new ones")


@dataclass(frozen=True, slots=True)
class BinaryRecord:
    """A binary that is already durable on disk: the store records its path, hash and size."""

    rel: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class BinaryDelete:
    """Drop a binary's record and queue the removal of its file.

    ``on_disk`` says that the file is already gone, so the commit queues no removal: a file that a
    person restores before the projector runs is then never taken away.
    """

    rel: str
    on_disk: bool = False


@dataclass(frozen=True, slots=True)
class BinaryMove:
    """A binary's record moved to a path that the store does not hold, before its file moves.

    ``sha256`` and ``size`` are the bytes that the file at ``src`` holds, read before the commit:
    the record at ``dst`` names them, so a source that the store records nothing for (bytes that
    nothing imported, or a temporary that holds a new file's bytes) moves the same way. The commit
    queues one ``move`` intent at ``dst`` with ``src`` in ``from_path``. The writer renames the
    file right after the commit, and the projector finishes a move that a kill left undone.
    """

    src: str
    dst: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class HeldRow:
    """A held outside change that the commit resolves, named as the caller read its row.

    The commit drops the row, and refuses when no row matches all four fields.
    """

    rel: str
    kind: str
    detected_at: str
    sha256: str | None


@dataclass(frozen=True, slots=True)
class FileSeen:
    """The person's file at a path as the caller saw it, which the commit records as what the
    projection manifest saw there: the projector then replaces that file with the stored page.

    ``sha256`` is None when the file was gone, and the commit then drops the manifest entry; the
    size and modification time then stay None too.
    """

    rel: str
    sha256: str | None
    size: int | None = None
    mtime_ns: int | None = None

    def __post_init__(self) -> None:
        if (self.sha256 is None) != (self.size is None) or (self.size is None) != (
            self.mtime_ns is None
        ):
            raise ValueError("a seen file has a hash, a size and a time, or none of them")


@dataclass(frozen=True, slots=True)
class StoreChange:
    """Everything one commit changes. A path appears in at most one of the operations.

    ``refresh`` names files that the store does not own (a Markdown file outside the Knowledge Base
    folder, or one at a page path the store holds no row for, say) and that a caller made durable
    on disk before the commit: the commit derives their rows from the file's bytes, so a search
    sees them at the same revision as the pages. The store keeps no page, record or audit row for
    them.

    ``resolves_held`` and ``files_seen`` are the bookkeeping of resolving a held item, and
    ``commit`` applies them in the same transaction as the page changes.
    """

    origin: Origin
    reason: str
    puts: tuple[PagePut, ...] = ()
    deletes: tuple[PageDelete, ...] = ()
    moves: tuple[PageMove, ...] = ()
    binaries: tuple[BinaryRecord, ...] = ()
    binary_deletes: tuple[BinaryDelete, ...] = ()
    binary_moves: tuple[BinaryMove, ...] = ()
    expect: Mapping[str, Expect] = field(default_factory=dict)
    resolves_held: tuple[HeldRow, ...] = ()
    files_seen: tuple[FileSeen, ...] = ()
    refresh: tuple[str, ...] = ()
    #: Folders that this change moves away as a whole, as file mode's rename of a folder does: a
    #: folder trash's source tree, or a recovery's trash slot. Each is queued as a folder intent
    #: (``folder_intent``), and the projector removes the folder once nothing queued below it is
    #: left to move and it is empty.
    vacated_folders: tuple[str, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        """Every path the change names, a move naming both of its paths."""
        return (
            *(put.rel for put in self.puts),
            *(delete.rel for delete in self.deletes),
            *(path for move in self.moves for path in (move.src, move.dst)),
            *(record.rel for record in self.binaries),
            *(delete.rel for delete in self.binary_deletes),
            *(path for move in self.binary_moves for path in (move.src, move.dst)),
        )

    @property
    def page_paths(self) -> tuple[str, ...]:
        """The page paths whose derived rows the change moves: each put, delete and move side."""
        return (
            *(put.rel for put in self.puts),
            *(delete.rel for delete in self.deletes),
            *(path for move in self.moves for path in (move.src, move.dst)),
        )

    def __post_init__(self) -> None:
        if not self.paths and not self.refresh:
            raise ValueError("a store change names no file")
        named = (*self.paths, *self.refresh)
        if len(set(named)) != len(named):
            raise ValueError("a store change names a path more than once")
        if not {seen.rel for seen in self.files_seen} <= set(self.paths):
            raise ValueError("a store change records a file that it does not change")


@dataclass(frozen=True, slots=True)
class CommitResult:
    """The revision that the commit made, and the paths it gave bytes or records to and removed."""

    revision: int
    written: tuple[str, ...]
    deleted: tuple[str, ...]


class StoreConflict(OpError):
    """A path did not hold what the change expected; nothing was committed."""

    def __init__(self, path: str, reason: str, remediation: str | None = None) -> None:
        super().__init__(CONFLICT, f"{path}: {reason}", remediation)
        self.path = path
        self.reason = reason


def frontmatter_json(body: bytes) -> str | None:
    """The page's frontmatter as JSON, or None when it has none or the bytes are not text."""
    try:
        frontmatter, _, _ = vault.parse_frontmatter(body.decode("utf-8"))
    except (UnicodeDecodeError, vault.FrontmatterError):
        return None
    # YAML dates have no JSON form; their text is the form a reader of the page sees.
    return (
        json.dumps(_text_keyed(frontmatter), sort_keys=True, ensure_ascii=False, default=str)
        if frontmatter
        else None
    )


def _text_keyed(value):
    """Give every mapping a text key: YAML allows any scalar there, and JSON allows only text."""
    if isinstance(value, dict):
        return {str(key): _text_keyed(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_text_keyed(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class _PreparedOriginal:
    fields: str | None
    mtime_ns: int


def _fts_posting_bytes(text: str, *, trigram: bool) -> int:
    """Bound this FTS5 column's uncompressed terms, doclists and positions."""
    chars = len(text)
    ascii_text = text.isascii()
    # Position deltas are FTS5 varints, capped by the largest position in this column.
    position = min(9, max(1, ((chars + 2).bit_length() + 6) // 7))
    if trigram:
        occurrences = max(0, chars - 2)
        terms = min(occurrences, len(set(text)) ** 3)
        encoded = chars if ascii_text else len(text.encode())
        term_bytes = min(3 * encoded, terms * (3 if ascii_text else 12))
    elif ascii_text:
        # The BM25 producer joins ASCII letter/number stems with one space.
        stems = text.split()
        occurrences = len(stems)
        distinct = set(stems)
        terms = len(distinct)
        term_bytes = sum(map(len, distinct))
    else:
        # A newer Unicode category can differ from SQLite's. Charge up to one
        # token per character, with at most four UTF-8 bytes per folded character.
        terms = occurrences = chars
        term_bytes = 4 * chars
    # FTS5 doclists store a rowid and poslist length (nine bytes each). Terms add an
    # index byte, two key lengths, a footer offset and up to one column marker.
    return terms * (1 + 2 * 9 + 3 + 2 + 2 * 9) + term_bytes + occurrences * position


def _lexical_allocation_bytes(rows: lexstore.PageRows | None) -> int:
    if rows is None:
        return 0
    documents = (rows.stemmed, *(unit.stemmed for unit in rows.units))
    columns = (rows.title_lower, rows.body_lower)
    units = (rows.columns, *(unit.columns for unit in rows.units))
    structure_size = len((rows.structure_json or "").encode())
    records = sum(
        sum(9 + (len(value.encode()) if isinstance(value, str) else 8)
            for value in unit.values())
        for unit in units
    ) + structure_size
    if not lexstore.fts5_available():
        return records
    content = sum(len(text.encode()) for text in (*documents, *columns))
    postings = sum(_fts_posting_bytes(text, trigram=False) for text in documents)
    postings += sum(_fts_posting_bytes(text, trigram=True) for text in columns)
    # FTS5 stores the original index input in its content table. Budget posting
    # blocks separately for leaf slack, the segment index and the doclist index.
    return records + content + 4 * postings


def _marked_moves(tx: Connection, change: StoreChange) -> dict[str, PageMove]:
    """Find moves that carry a stored original's custody forward."""
    by_source = {move.src: move for move in change.moves}
    sources = tuple(by_source)
    retained = {}
    for start in range(0, len(sources), read.KEYS_PER_QUERY):
        batch = sources[start : start + read.KEYS_PER_QUERY]
        within = ", ".join("?" * len(batch))
        for path, sha256 in tx.execute(
            "SELECT p.path, p.sha256 FROM store_pages p "
            f"WHERE p.path IN ({within}) AND EXISTS ("
            "SELECT 1 FROM store_audit a WHERE a.path = p.path AND a.revision = p.revision "
            "AND a.sha256 = p.sha256 AND a.reason = ?)",
            (*batch, MEDIA_PAYLOAD_WRITE_REASON),
        ):
            move = by_source[path]
            # A guarded public move can add lineage bytes. Its source expectation
            # still binds the exact stored original that the audit trail marks.
            expectation = change.expect.get(path)
            if (move.body is None or (expectation is not None and expectation.sha256 == sha256)
                    or hashlib.sha256(move.body).hexdigest() == sha256):
                retained[path] = move
    return retained


def _original_inputs(
    tx: Connection, puts: tuple[PagePut, ...], moves: dict[str, PageMove], *, scrub: bool,
) -> Iterator[tuple[str, bytes, str | None, int, int]]:
    """Stream the exact inputs to the destination page and its lexical rows."""
    for put in puts:
        mtime_ns = time.time_ns() if put.on_disk_mtime_ns is None else put.on_disk_mtime_ns
        yield put.rel, put.body, frontmatter_json(put.body), mtime_ns, 0
    sources = tuple(moves)
    for start in range(0, len(sources), read.KEYS_PER_QUERY):
        batch = sources[start : start + read.KEYS_PER_QUERY]
        within = ", ".join("?" * len(batch))
        for path, body, fields, mtime_ns in tx.execute(
            "SELECT path, body, frontmatter_json, mtime_ns FROM store_pages "
            f"WHERE path IN ({within})", batch,
        ):
            move = moves[path]
            scrub_bytes = len(body) + 2 * len((fields or "").encode()) if scrub else 0
            if move.body is not None:
                body = move.body
                fields, mtime_ns = frontmatter_json(body), time.time_ns()
            yield move.dst, body, fields, mtime_ns, scrub_bytes


def _source_fts_delete_bytes(
    tx: Connection, sources: tuple[str, ...], *, scrub: bool,
) -> tuple[int, int]:
    """Bound new delete entries from the source's actual stored index inputs."""
    if not sources or not lexstore.fts5_available():
        return 0, 0
    postings = 0
    scrub_bytes = 0
    for start in range(0, len(sources), read.KEYS_PER_QUERY):
        batch = sources[start : start + read.KEYS_PER_QUERY]
        within = ", ".join("?" * len(batch))
        columns = tx.execute(
            "SELECT f.c0, t.c0, t.c1 FROM pages p "
            "LEFT JOIN fts_content f ON f.id = p.rowid "
            "LEFT JOIN tri_content t ON t.id = p.rowid "
            f"WHERE p.path IN ({within})", batch,
        )
        for words, title, body in columns:
            postings += _fts_posting_bytes(words or "", trigram=False)
            postings += _fts_posting_bytes(title or "", trigram=True)
            postings += _fts_posting_bytes(body or "", trigram=True)
            if scrub:
                scrub_bytes += sum(len(text.encode()) for text in (words, title, body) if text)
        for (words,) in tx.execute(
            "SELECT f.c0 FROM semantic_units u JOIN unit_fts_content f ON f.id = u.rowid "
            f"WHERE u.parent_path IN ({within})", batch,
        ):
            postings += _fts_posting_bytes(words, trigram=False)
            if scrub:
                scrub_bytes += len(words.encode())
    # Delete keys omit positions. Use the same posting bound as inserts, with overhead
    # for blocks and indexes, even when the destination is outside the search corpus.
    return 4 * postings, scrub_bytes


def _catalogue_record_size(columns: tuple[str, ...]) -> str:
    """SQL for an upper bound on a persisted table or index record's byte length."""
    # The record header uses one size varint and one serial-type varint per column.
    sizes = [str(9 * (len(columns) + 1))]
    for column in columns:
        field = '"' + column.replace('"', '""') + '"'
        sizes.append(
            f"CASE typeof({field}) WHEN 'null' THEN 0 "
            f"WHEN 'text' THEN length(CAST({field} AS BLOB)) "
            f"WHEN 'blob' THEN length({field}) ELSE 8 END"
        )
    return " + ".join(sizes)


def _tree_limits(usable: int) -> tuple[int, int]:
    """The largest local payload of a table cell and of an index cell (SQLite file format)."""
    # `usable` already excludes the four-byte overflow pointer. Subtract the maximum
    # reserved bytes for both limits.
    return usable + 4 - 35, (usable + 4 - 12) * 64 // 255 - 23


@dataclass(slots=True)
class _CellPages:
    """What writing or deleting a set of B-tree cells can allocate and rewrite."""

    overflow: int = 0  # overflow pages of the cells' records
    local_bytes: int = 0  # inline bytes, with cell pointers, varints and child pointers
    # Per tree, the runs of adjacent cells: each lands in or leaves one existing leaf.
    runs: dict[tuple[str, int], int] = field(default_factory=dict)

    @property
    def trees(self) -> int:
        return len(self.runs)

    def merge(self, other: _CellPages) -> None:
        self.overflow += other.overflow
        self.local_bytes += other.local_bytes
        for tree, runs in other.runs.items():
            self.runs[tree] = self.runs.get(tree, 0) + runs

    def add(self, size: int, limit: int, usable: int) -> None:
        self.local_bytes += min(size, limit) + 32
        if size > limit:
            self.overflow += (size + usable - 1) // usable

    def leaves(self, usable: int) -> int:
        """Leaf pages that hold the inline bytes when balancing leaves them half full."""
        return (2 * self.local_bytes + usable - 1) // usable

    def added(self, usable: int, depth: int) -> int:
        """New pages that inserting these cells allocates: overflow, leaves and their parents.

        An interior page holds at least four cells, so each level above the leaves needs at most
        a quarter of the level below, plus one partial page per level of each tree.
        """
        leaves = self.leaves(usable)
        return self.overflow + leaves + (leaves + 2) // 3 + self.trees * depth

    def run_rewrites(self, depth: int, pages: int) -> int:
        """Existing pages that the runs of these cells and their balances can dirty.

        Each run of adjacent cells dirties the leaf it lands in or leaves, and a balance there or at
        any ancestor rewrites that page and at most two existing siblings (SQLite's balance_nonroot).
        The runs of one tree share their ancestors: since an interior page holds at least four
        cells, the level `j` steps above the leaves holds at most `pages / 4**j` pages. The bound
        follows the runs, never the size of the database.
        """
        return sum(3 * (runs + sum(min(runs, pages // 4**level + 1) for level in range(1, depth)))
                   for runs in self.runs.values())

    def deleted_rewrites(self, usable: int, depth: int, pages: int) -> int:
        """Existing pages that deleting these cells dirties: the leaves that hold them, plus the
        balances of each run as `run_rewrites` bounds them."""
        return self.leaves(usable) + self.run_rewrites(depth, pages)


def _rowid_runs(rowids: list[int]) -> int:
    """Count the runs of consecutive rowids, which sit together in a rowid table."""
    rowids.sort()
    return sum(1 for index, rowid in enumerate(rowids) if index == 0 or rowid != rowids[index - 1] + 1)


# The schema tables that the lexical and graph writers delete by source path, with that column.
# The file's graph node stores its own copy of the structural summary.
_LEXICAL_SOURCE_TABLES = (("pages", "path"), ("semantic_units", "parent_path"), ("unit_revisions", "parent_path"))
_GRAPH_SOURCE_TABLES = (
    ("graph_nodes", "path"), ("graph_parent_refs", "path"), ("graph_edges", "source_path"),
    ("graph_dependencies", "source_path"), ("graph_dependency_coverage", "source_path"),
)


def _key_runs(
    tx: Connection, table: str, names: tuple[str, ...], keys: set[tuple[Any, ...]],
    *, others: tuple[str, tuple[str, ...]] | None = None, replacing: tuple[str, str] | None = None,
) -> int:
    """Count the runs that cells with these index keys form among the other rows of the index.

    Sorted, two neighbouring keys share a run unless another stored row sorts between them. For
    inserted cells that is a row after the first key and at or before the second: stored rows with
    an equal key keep their smaller rowids ahead of new cells. For deleted cells, `others` names
    the source column and sources whose rows are going, and any row of another source counts.
    `replacing` names the source column and the page whose own rows the write deletes first,
    so they never separate its new cells. A key that holds NULL or mixes value types forms a run
    of its own.
    """
    ordered = [key for key in keys if all(isinstance(value, (str, int)) and not isinstance(value, bool)
                                          for value in key)]
    runs = len(keys) - len(ordered)
    kinds = {tuple(type(value) for value in key) for key in ordered}
    if len(kinds) > 1:
        return len(keys)
    ordered.sort()
    columns = ", ".join('"' + name.replace('"', '""') + '"' for name in names)
    width = len(names)
    lows = ", ".join(f"p.l{at}" for at in range(width))
    highs = ", ".join(f"p.h{at}" for at in range(width))
    if others is None:
        between = f"({columns}) > ({lows}) AND ({columns}) <= ({highs})"
        extra: tuple[str, ...] = ()
        if replacing is not None:
            between += f" AND {replacing[0]} != ?"
            extra = (replacing[1],)
    else:
        source, going = others
        between = (f"({columns}) >= ({lows}) AND ({columns}) <= ({highs}) "
                   f"AND {source} NOT IN ({', '.join('?' * len(going))})")
        extra = going
    runs += 1 if ordered else 0
    pairs = list(zip(ordered, ordered[1:], strict=False))
    # One statement per batch of neighbouring pairs; each pair is one indexed range probe.
    batch = max(1, (900 - len(extra)) // (2 * width))
    labels = ", ".join([*(f"l{at}" for at in range(width)), *(f"h{at}" for at in range(width))])
    for start in range(0, len(pairs), batch):
        chunk = pairs[start : start + batch]
        rows = ", ".join("(" + ", ".join("?" * (2 * width)) + ")" for _ in chunk)
        (found,) = tx.execute(
            f"WITH p({labels}) AS (VALUES {rows}) "
            f"SELECT count(*) FROM p WHERE EXISTS (SELECT 1 FROM {table} WHERE {between})",
            (*(value for low, high in chunk for value in (*low, *high)), *extra),
        ).fetchone()
        runs += found
    return runs


def _source_layout(tx: Connection, table: str, key: str, usable: int) -> tuple[list[str], list[int], list[tuple[str, ...]]]:
    """The record size expressions, payload limits and placing keys of a table and its indexes."""
    table_limit, index_limit = _tree_limits(usable)
    columns = tuple(name for (name,) in tx.execute(
        "SELECT name FROM pragma_table_xinfo(?) WHERE hidden IN (0, 3)", (table,),
    ))
    sizes = [_catalogue_record_size(columns)]
    limits = [table_limit]
    keys: list[tuple[str, ...]] = []
    for (index,) in tx.execute("SELECT name FROM pragma_index_list(?)", (table,)):
        indexed = []
        for cid, name in tx.execute("SELECT cid, name FROM pragma_index_xinfo(?)", (index,)):
            if cid == -2:
                raise ValueError("cannot size an expression index in the source catalogue")
            indexed.append("rowid" if cid == -1 else name)
        sizes.append(_catalogue_record_size(tuple(indexed)))
        limits.append(index_limit)
        # A key without its trailing rowid places the cell among other rows. Rows of one source
        # sit together once the key reaches the source column, so the key stops there.
        placed = [name for name in indexed if name != "rowid"]
        if key in placed:
            placed = placed[: placed.index(key) + 1]
        keys.append(tuple(placed))
    return sizes, limits, keys


def _source_derived_pages(
    tx: Connection, sources: tuple[str, ...], usable: int,
    tables: tuple[tuple[str, str], ...] = _LEXICAL_SOURCE_TABLES + _GRAPH_SOURCE_TABLES,
    layouts: dict[str, tuple[list[str], list[int], list[tuple[str, ...]]]] | None = None,
) -> _CellPages:
    """Size the stored records that the writers delete for these source paths."""
    cells = _CellPages()
    layouts = {} if layouts is None else layouts
    for table, key in tables:
        if table not in layouts:
            layouts[table] = _source_layout(tx, table, key, usable)
        sizes, limits, keys = layouts[table]
        # Count each existing row's record separately. A partial index can only omit
        # some of these rows, so including its key gives a safe upper bound.
        quoted = ['"' + name.replace('"', '""') + '"' for placed in keys for name in placed]
        fields = ", ".join(["rowid", *sizes, *quoted])
        rowids: list[int] = []
        prefixes: list[set[tuple[Any, ...]]] = [set() for _ in keys]
        for start in range(0, len(sources), read.KEYS_PER_QUERY):
            batch = sources[start : start + read.KEYS_PER_QUERY]
            within = ", ".join("?" * len(batch))
            for row in tx.execute(f"SELECT {fields} FROM {table} WHERE {key} IN ({within})", batch):
                rowids.append(row[0])
                for size, limit in zip(row[1 : 1 + len(sizes)], limits, strict=True):
                    cells.add(size, limit, usable)
                at = 1 + len(sizes)
                for seen, placed in zip(prefixes, keys, strict=True):
                    seen.add(tuple(row[at : at + len(placed)]))
                    at += len(placed)
        if not rowids:
            continue
        runs = _rowid_runs(rowids)
        cells.runs[(table, 0)] = runs
        # Rows of one rowid run and one run of keys are adjacent in an index.
        for tree, (placed, seen) in enumerate(zip(keys, prefixes, strict=True), start=1):
            gaps = _key_runs(tx, table, placed, seen, others=(key, sources))
            cells.runs[(table, tree)] = min(len(rowids), gaps * runs)
    return cells


def _incoming_graph_pages(
    tx: Connection, rel: str, records: tuple[epistemic_graph.PageGraphRecords, ...], usable: int,
    *, encoding: str, layouts: dict[str, list[tuple[tuple[int, ...], int]]],
) -> _CellPages:
    """Size the graph rows that a page's derivation inserts."""
    cells = _CellPages()
    table_limit, index_limit = _tree_limits(usable)
    source_columns = dict(_GRAPH_SOURCE_TABLES)
    for table in records:
        if not table.values:
            continue
        source_column = source_columns[table.table]
        shapes = layouts.get(table.table)
        if shapes is None:
            positions = {column: ordinal for ordinal, column in enumerate(table.columns)}
            shapes = [(tuple(positions.values()), table_limit)]
            for (index,) in tx.execute("SELECT name FROM pragma_index_list(?)", (table.table,)):
                indexed = []
                for cid, name in tx.execute("SELECT cid, name FROM pragma_index_xinfo(?)", (index,)):
                    if cid != -1 and name not in positions:
                        raise ValueError("cannot size an unbound graph index column")
                    indexed.append(len(positions) if cid == -1 else positions[name])
                shapes.append((tuple(indexed), index_limit))
            layouts[table.table] = shapes
        # New rows take the largest rowids: they append to the table tree, and within an
        # index they gather at one position per distinct key ahead of the rowid.
        cells.runs[(table.table, 0)] = 1
        # Within an index, the page's cells gather at one position per distinct key ahead of the
        # page's own source column, which no other page shares.
        source_at = table.columns.index(source_column)
        placed = []
        for columns, _limit in shapes[1:]:
            keyed = [column for column in columns if column < len(table.columns)]
            placed.append(tuple(keyed[: keyed.index(source_at) + 1] if source_at in keyed else keyed))
        prefixes: list[set[tuple[Any, ...]]] = [set() for _ in placed]
        for record in table.values:
            sizes = tuple(
                len(value) if isinstance(value, str) and encoding == "UTF-8" and value.isascii()
                else len(value.encode(encoding)) if isinstance(value, str)
                else len(value) if isinstance(value, bytes)
                else 0 if value is None else 8
                for value in record
            ) + (8,)  # SQLite appends the integer rowid to ordinary secondary indexes.
            for columns, limit in shapes:
                cells.add(9 * (len(columns) + 1) + sum(sizes[column] for column in columns), limit, usable)
            for seen, columns in zip(prefixes, placed, strict=True):
                seen.add(tuple(record[column] for column in columns))
        for tree, (columns, seen) in enumerate(zip(placed, prefixes, strict=True), start=1):
            names = tuple(table.columns[column] for column in columns)
            cells.runs[(table.table, tree)] = _key_runs(tx, table.table, names, seen,
                                                        replacing=(source_column, rel))
    return cells


@contextmanager
def _reserve_payload_pages(
    writer: CollectionWriter, puts: tuple[PagePut, ...], moves: dict[str, PageMove],
) -> Iterator[dict[str, _PreparedOriginal]]:
    """Protect original payload allocation and its direct write metadata on the vault device.

    The commit writes the page, its audit row and its two queue rows. The search, identity and graph
    rows that the original causes follow in the drain, which reserves their room (``derived_room``).
    """
    from .. import media_capacity

    originals = tuple(put for put in puts if put.media_allocation)
    if not originals and not moves:
        yield {}
        return
    database = writer.handle.path
    with media_capacity.upload_room(writer.root, device=database.stat().st_dev, reserve=True) as room:
        if room is None:
            yield {}
            return
        conn = writer.connection
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        before = conn.execute("PRAGMA page_count").fetchone()[0]
        # SQLite file format: an overflow page reserves four bytes for the next page and up
        # to 255 reserved bytes. WAL stores a 32-byte header and 24 bytes per frame.
        usable = page_size - 4 - 255
        # The tables that write a direct record per path. Their indexes carry the path
        # too. SQLite's balance_nonroot uses one parent and up to five result pages.
        trees = _trees(conn, ("store_pages", "store_audit", "store_projection_queue", "store_derivation_queue"))
        touched = len(originals) + 2 * len(moves)
        # An interior page holds at least four cells, so a tree of these pages has at most this depth.
        depth = 1 + max(1, math.ceil(math.log(before + touched + 1, 4)))
        prepared = {}
        overflow_pages = 0
        upload_size = 0
        scrub = bool(moves) and conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        scrub_bytes = 0
        for rel, body, fields, mtime_ns, old_bytes in _original_inputs(conn, originals, moves, scrub=scrub):
            prepared[rel] = _PreparedOriginal(fields, mtime_ns)
            upload_size += len(body)
            scrub_bytes += old_bytes
            # Both the page table and the store_pages_meta covering index store frontmatter.
            record_size = len(body) + 2 * len((fields or "").encode()) + trees * len(rel.encode())
            overflow_pages += (record_size + usable - 1) // usable
        btree_paths = touched * depth * trees
        # A split can add two siblings. The WAL also rewrites the existing siblings and parent.
        database_pages = overflow_pages + 2 * btree_paths
        # Secure deletion can write the old overflow pages to WAL while freeing them in the DB.
        scrub_pages = (scrub_bytes + usable - 1) // usable
        wal_pages = overflow_pages + scrub_pages + 6 * btree_paths + 1  # Includes the revision row.
        room.reserve_store_pages(
            database,
            target_bytes=(before + database_pages) * page_size,
            wal_bytes=32 + wal_pages * (page_size + 24),
            upload_size=upload_size,
        )
        try:
            yield prepared
        finally:
            _record_page_count(room, database, conn, page_size)


def _trees(conn: Connection, tables: tuple[str, ...]) -> int:
    """How many b-trees hold ``tables``: each table and each of its indexes."""
    return len(tables) + sum(
        conn.execute("SELECT count(*) FROM pragma_index_list(?)", (table,)).fetchone()[0] for table in tables
    )


def _record_page_count(room: Any, database: Path, conn: Connection, page_size: int) -> None:
    try:
        room.set_store_pages(database, conn.execute("PRAGMA page_count").fetchone()[0] * page_size)
    except (sqlite3.Error, OSError) as error:
        # Keep the previous reservation through a failed refresh or process cut.
        log.warning("could not refresh the store checkpoint allocation (%s)", type(error).__name__)


@contextmanager
def derived_room(
    writer: CollectionWriter, tx: Connection, metered: list[str], sources: tuple[str, ...],
) -> Iterator[tuple[dict[str, derive._LexicalPage], derive.ReserveGraph | None, Callable[[], None]]]:
    """Protect the derived rows that an original causes on the vault device, for one drain batch.

    The rows of an original's page, of the path that a move of it vacated, and of the pages that link
    to it draw on the room that this reserves; a batch with no original reserves nothing. It yields
    the original's prepared lexical rows, the graph reservation callback and the settle step.
    """
    from .. import media_capacity

    if not metered and not sources:
        yield {}, None, lambda: None
        return
    database = writer.handle.path
    with media_capacity.upload_room(writer.root, device=database.stat().st_dev, reserve=True) as room:
        if room is None:
            yield {}, None, lambda: None
            return
        conn = writer.connection
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        before = conn.execute("PRAGMA page_count").fetchone()[0]
        usable = page_size - 4 - 255
        # The drain deletes one queue row per path and moves the revision row.
        trees = _trees(conn, ("store_derivation_queue",))
        touched = len(metered) + len(sources)
        depth = 1 + max(1, math.ceil(math.log(before + touched + 1, 4)))
        basis = epistemic_graph.producer_type_basis(writer.root)
        prepared: dict[str, derive._LexicalPage] = {}
        overflow_pages = 0
        upload_size = 0
        scrub = bool(sources) and conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        for rel in metered:
            found = read.page_source(tx, writer.root, rel)
            if found is None:
                continue
            body, mtime_ns = found
            lexical = derive.prepare_lexical_page(writer.root, rel, body, mtime_ns, basis)
            prepared[rel] = lexical
            upload_size += len(body)
            overflow_pages += (_lexical_allocation_bytes(lexical.rows) + usable - 1) // usable
        removed_postings, removed_content = _source_fts_delete_bytes(conn, sources, scrub=scrub)
        overflow_pages += (removed_postings + usable - 1) // usable
        btree_paths = touched * depth * trees
        database_pages = overflow_pages + 2 * btree_paths
        scrub_pages = (removed_content + usable - 1) // usable
        # Deleting the source's derived records rewrites the leaves that hold them, with or
        # without secure delete, and can rebalance their trees.
        source = _source_derived_pages(conn, sources, usable) if sources else _CellPages()
        if scrub:
            scrub_pages += source.overflow
        wal_pages = (overflow_pages + scrub_pages + min(before, source.deleted_rewrites(usable, depth, before))
                     + 6 * btree_paths + 1)  # Includes the revision row.
        graph = _CellPages()
        replaced = _CellPages()  # the graph rows of re-derived pages, deleted before their records land
        replaced_layouts: dict[str, tuple[list[str], list[int], list[tuple[str, ...]]]] = {}
        recorded = 0  # the checkpoint target this batch last recorded
        estimated = 0  # the largest checkpoint target that its estimates reached
        graph_layouts: dict[str, list[tuple[tuple[int, ...], int]]] = {}
        encoding, = conn.execute("PRAGMA encoding").fetchone()

        def reserve() -> None:
            nonlocal recorded, estimated
            # WAL writes each dirty page once per transaction, so the starting page count also
            # bounds the existing pages it rewrites. New graph pages also grow the database.
            graph_pages = graph.added(usable, depth)
            redistributed = min(before, graph.run_rewrites(depth, before)
                                + replaced.deleted_rewrites(usable, depth, before))
            estimate = {
                "target_bytes": (before + database_pages + graph_pages) * page_size,
                "wal_bytes": 32 + (wal_pages + graph_pages + redistributed) * (page_size + 24),
                "upload_size": upload_size,
            }
            # Record the target once. Each later page only checks the room in memory, and `settle`
            # records the final estimate before COMMIT; the real page count replaces it after.
            if recorded:
                estimated = max(estimated, room.require_store_pages(database, **estimate))
            else:
                room.reserve_store_pages(database, **estimate)
                recorded = estimated = estimate["target_bytes"]

        def settle() -> None:
            nonlocal recorded
            if estimated > recorded:
                room.set_store_pages(database, estimated)
                recorded = estimated

        def reserve_graph(rel: str, records: tuple[epistemic_graph.PageGraphRecords, ...]) -> None:
            cells = _incoming_graph_pages(conn, rel, records, usable, encoding=encoding, layouts=graph_layouts)
            graph.merge(cells)
            # A page that links to an original replaces its existing graph rows; a new page has none.
            old = _source_derived_pages(conn, (rel,), usable, _GRAPH_SOURCE_TABLES, replaced_layouts)
            replaced.merge(old)
            reserve()

        reserve()
        try:
            yield prepared, reserve_graph, settle
        finally:
            _record_page_count(room, database, conn, page_size)


def commit(
    writer: CollectionWriter, change: StoreChange, *, before_move_commit: Callable[[], None] | None = None,
) -> CommitResult:
    """Apply ``change`` in one transaction on the store thread's writer, and return its revision.

    The statements run in this order, so that a kill at any point leaves the old state whole: the
    expectations, the pages, the binary records and the manifest, one audit row per file, the
    projection intents, the derivation intents, the held rows that the change resolves, the files that
    it saw and last the revision. One intent stands for each path, and the oldest queue time stays. A
    move queues one ``move`` intent under its destination path, with the old path in ``from_path``. A
    held row that changed or went meanwhile rolls the whole commit back.

    The commit derives no row: each page path and each refreshed file gets a derivation intent, and
    the store thread derives the rows from it after this transaction (``derivation``). The writing
    request derives its own intents once it has released the vault mutation boundary.
    """
    _require_owned(change)
    if derivation.in_scope():
        raise connection.CollectionStoreError(
            "COLLECTION_STORE_BOUNDARY_REQUIRED",
            "a store commit needs the vault mutation boundary, which derived-row work does not hold",
        )
    # Close the transaction before releasing capacity, including on rollback. The
    # source lookup and admission run before the first canonical mutation. A commit writes only the
    # vault store's tables (`_require_owned`), so the collections' release cache holds across it,
    # unless it changes a registry or the access policy, which a collection's release also reads.
    overlay = rederive.changes_overlay(writer.root, change.paths)
    transaction = writer.handle.transaction(before_commit=before_move_commit, collection_free=not overlay)
    with ExitStack() as allocations, transaction as tx:
        row = tx.execute("SELECT mode, revision FROM store_mode WHERE id = 1").fetchone()
        if row is None or row[0] != mode.STORE_MODE:
            raise connection.CollectionStoreError(NOT_ACTIVE, "this vault is not in store mode")
        revision = row[1] + 1
        before = read.vault_triple(tx, row[1])
        check(tx, change.expect)
        _refuse_owned_refresh(tx, change.refresh)
        retained_moves = _marked_moves(tx, change)
        prepared = allocations.enter_context(
            _reserve_payload_pages(writer, change.puts, retained_moves),
        )
        _degrade_moves(tx, change.paths)
        now = timestamp()
        audit = _Trail(tx, revision, change, now)
        for put in change.puts:
            _put_page(tx, put, revision, audit, prepared=prepared.get(put.rel))
        for page in change.deletes:
            _delete_page(tx, page, audit)
        for move in change.moves:
            _move_page(tx, move, revision, audit, media_allocation=move.src in retained_moves,
                       prepared=prepared.get(move.dst))
        for record in change.binaries:
            _record_binary(tx, record, revision, audit)
        for binary in change.binary_deletes:
            _delete_binary(tx, binary, audit)
        for move in change.binary_moves:
            _move_binary(tx, move, revision, audit)
        if overlay:
            # The overlay job settles the rows that the registries under ``_Schema`` derive.
            rederive.record_overlay_change(tx, revision)
        _enqueue(tx, audit.intents, revision, now)
        _enqueue_folders(tx, change.vacated_folders, revision, now)
        # An original's rows, and those of the path that a move of one vacates, draw on the floor.
        metered = frozenset((
            *(put.rel for put in change.puts if put.media_allocation),
            *(path for move in change.moves if move.src in retained_moves for path in (move.src, move.dst)),
        ))
        derivation.enqueue(
            tx, list(dict.fromkeys(change.page_paths)), list(change.refresh), revision, now, metered
        )
        for held in change.resolves_held:
            _drop_held(tx, held)
        for seen in change.files_seen:
            _record_seen(tx, seen, revision)
        tx.execute("UPDATE store_mode SET revision = ? WHERE id = 1", (revision,))
        after = read.vault_triple(tx, revision)
    # A refreshed file is a page of the corpus too (the importer's non-owned Markdown).
    derivation.committed(writer, before, after, (*change.paths, *change.refresh))
    return CommitResult(revision, tuple(audit.written), tuple(audit.deleted))


def _refuse_owned_refresh(tx: Connection, refresh: tuple[str, ...]) -> None:
    for start in range(0, len(refresh), read.KEYS_PER_QUERY):
        batch = refresh[start : start + read.KEYS_PER_QUERY]
        owned = tx.execute(
            f"SELECT path FROM store_pages WHERE path IN ({', '.join('?' * len(batch))}) LIMIT 1", batch,
        ).fetchone()
        if owned is not None:
            raise OpError(NOT_OWNED, f"the store holds {owned[0]}: write it, do not refresh it")


def _require_owned(change: StoreChange) -> None:
    """Refuse, before any write, a path that the store cannot hold as its operation needs."""
    for rel in change.refresh:
        _require_safe(rel)
        # ``PAGE`` is refused inside ``commit``'s transaction, where the store_pages probe and the
        # derived rows it guards are one atomic step: a file at a page path that the store holds no
        # row for is disk-owned and refreshes like an unowned file.
        if ownership.classify(rel) is ownership.Ownership.BINARY:
            raise OpError(NOT_OWNED, f"the store holds {rel}: write it, do not refresh it")
    pages = (
        *(put.rel for put in change.puts),
        *(delete.rel for delete in change.deletes),
        *(path for move in change.moves for path in (move.src, move.dst)),
    )
    binaries = (
        *(record.rel for record in change.binaries),
        *(delete.rel for delete in change.binary_deletes),
        *(move.dst for move in change.binary_moves),
    )
    for rels, kind in ((pages, ownership.Ownership.PAGE), (binaries, ownership.Ownership.BINARY)):
        for rel in rels:
            _require_safe(rel)
            if ownership.classify(rel) is not kind:
                raise OpError(NOT_OWNED, f"the store holds no {kind.value} at {rel}")
    for move in change.binary_moves:
        # A move's source may be a temporary that the store does not own, never a page.
        _require_safe(move.src)
        if ownership.classify(move.src) is ownership.Ownership.PAGE:
            raise OpError(NOT_OWNED, f"the store holds a page at {move.src}, not a binary")
    for folder in change.vacated_folders:
        _require_safe(folder)


def folder_intent(folder: str) -> str:
    """The queue path of a folder that a change vacated: the folder's path and a closing slash.

    A queue row names a page or a binary by its path, which never ends in a slash, so the slash
    tells a folder intent apart in the existing columns, and its path sorts before every path
    below the folder.
    """
    return f"{folder}/"


def is_folder_intent(path: str) -> bool:
    return path.endswith("/")


def _enqueue_folders(
    tx: Connection, folders: tuple[str, ...], revision: int, now: str
) -> None:
    tx.executemany(
        "INSERT INTO store_projection_queue(path, revision, operation, from_path, queued_at) "
        "VALUES (?, ?, 'delete', NULL, ?) ON CONFLICT(path) DO UPDATE SET "
        "revision = excluded.revision, operation = 'delete', from_path = NULL",
        [(folder_intent(folder), revision, now) for folder in dict.fromkeys(folders)],
    )


def _require_safe(rel: str) -> None:
    try:
        vault._safe_guard_target(rel)  # the rule that every guarded vault read applies
    except vault.PathGuardError as error:
        raise OpError(NOT_OWNED, f"{rel!r} is not a safe vault-relative path") from error


def timestamp() -> str:
    """The store's one time format, to the second in UTC: queue, audit and held rows all use it."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_manifest(
    tx: Connection, rel: str, sha256: str, size: int, mtime_ns: int, revision: int
) -> None:
    """Record that the file at ``rel`` holds these bytes: the one write of a manifest row."""
    tx.execute(
        "INSERT INTO store_manifest(path, sha256, size, mtime_ns, revision) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET sha256 = excluded.sha256, size = excluded.size, "
        "mtime_ns = excluded.mtime_ns, revision = excluded.revision",
        (rel, sha256, size, mtime_ns, revision),
    )


def _drop_held(tx: Connection, held: HeldRow) -> None:
    dropped = tx.execute(
        "DELETE FROM store_held WHERE path = ? AND kind = ? AND detected_at = ? AND sha256 IS ?",
        (held.rel, held.kind, held.detected_at, held.sha256),
    ).rowcount
    if dropped != 1:
        raise StoreConflict(held.rel, "this held item changed while it was resolved")


def _record_seen(tx: Connection, seen: FileSeen, revision: int) -> None:
    if seen.sha256 is None or seen.size is None or seen.mtime_ns is None:
        tx.execute("DELETE FROM store_manifest WHERE path = ?", (seen.rel,))
    else:
        record_manifest(tx, seen.rel, seen.sha256, seen.size, seen.mtime_ns, revision)


def _stored(tx: Connection, rel: str) -> tuple[int, str] | None:
    """The revision and hash of the page or binary record at ``rel``, or None.

    A path is a page or a binary, never both.
    """
    return tx.execute(
        "SELECT revision, sha256 FROM store_pages WHERE path = ? "
        "UNION ALL SELECT revision, sha256 FROM store_binaries WHERE path = ?",
        (rel, rel),
    ).fetchone()


def _stored_many(tx: Connection, rels: list[str]) -> dict[str, tuple[int, str]]:
    """``_stored`` for each of ``rels`` that the store holds, a keyed read per chunk."""
    found: dict[str, tuple[int, str]] = {}
    for start in range(0, len(rels), read.KEYS_PER_QUERY):
        batch = rels[start : start + read.KEYS_PER_QUERY]
        marks = ", ".join("?" * len(batch))
        for path, revision, sha256 in tx.execute(
            f"SELECT path, revision, sha256 FROM store_pages WHERE path IN ({marks}) "
            f"UNION ALL SELECT path, revision, sha256 FROM store_binaries WHERE path IN ({marks})",
            (*batch, *batch),
        ):
            found[path] = (revision, sha256)
    return found


def check(tx: Connection, expect: Mapping[str, Expect]) -> None:
    """Raise ``StoreConflict`` for the first path that does not hold what ``expect`` says."""
    # One read of the inbound rows binds the linking pages of every path: a folder's delete binds
    # hundreds.
    linked = vault.store_inbound_wikilinks(
        tx, [rel for rel, want in expect.items() if want.linked_from is not None]
    )
    # One keyed read per chunk of paths binds what each holds, instead of a read per path.
    held = _stored_many(tx, [rel for rel, want in expect.items() if want.binds_path])
    for rel, want in expect.items():
        _check_below(tx, rel, want)
        if not want.binds_path:
            continue
        have = held.get(rel)
        if want.absent:
            if have is not None:
                raise StoreConflict(rel, "the store already holds it")
            continue
        if have is None:
            raise StoreConflict(rel, "the store holds nothing there")
        if want.revision is not None and have[0] != want.revision:
            raise StoreConflict(rel, f"its revision is {have[0]}, not {want.revision}")
        if want.sha256 is not None and have[1] != want.sha256:
            raise StoreConflict(rel, "its bytes are not the expected bytes")
        if want.projected and not _projected(tx, rel, *have):
            raise StoreConflict(rel, "its file on disk is not the stored page")
        if want.linked_from is not None:
            gained = {link.path for link in linked[rel]} - want.linked_from
            if gained:
                # No name: the page that linked may be one the caller is not allowed to see.
                raise StoreConflict(rel, "a page now links to it")


def _check_below(tx: Connection, rel: str, want: Expect) -> None:
    """Raise ``StoreConflict`` when a folder's count of pages or binary records is not the bound one.

    Each count is one range read of the table's path key, so a folder of thousands costs two reads.
    """
    prefix = f"{rel}/"
    # The two tables are the store schema's own, a closed pair: a page or a binary record.
    for table, bound, kind in (
        ("store_pages", want.pages_below, "pages"),
        ("store_binaries", want.binaries_below, "binary records"),
    ):
        if bound is None:
            continue
        (count,) = tx.execute(
            f"SELECT COUNT(*) FROM {table} WHERE path >= ? AND path < ?",
            (prefix, prefix + AFTER),
        ).fetchone()
        if count != bound:
            raise StoreConflict(rel, f"the folder holds {count} {kind}, not {bound}")


def _projected(tx: Connection, rel: str, revision: int, sha256: str) -> bool:
    """Whether the file on disk is the stored page: the manifest names the page's revision and bytes,
    and no projection is queued and no outside change is held for the path."""
    manifest = tx.execute(
        "SELECT revision, sha256 FROM store_manifest WHERE path = ?", (rel,)
    ).fetchone()
    queued = tx.execute("SELECT 1 FROM store_projection_queue WHERE path = ?", (rel,)).fetchone()
    held = tx.execute("SELECT 1 FROM store_held WHERE path = ?", (rel,)).fetchone()
    return manifest == (revision, sha256) and queued is None and held is None


#: Which audit operations open and end a path's life in the store, as ``_Trail`` records them. A
#: write, an import, or the new side of a move (the row that names the old path) opens a life; a
#: delete, or the old side of a move, ends one. The projector reads them to tell the bytes that
#: Evermatter put at a path in its current life from those of an earlier life, which a person may have
#: restored.
BEGINS_A_LIFE = (
    "(operation IN ('write', 'import') OR (operation = 'move' AND from_path IS NOT NULL))"
)
ENDS_A_LIFE = "(operation = 'delete' OR (operation = 'move' AND from_path IS NULL))"
#: A migration or a rollback writes one audit row for the whole vault, with no path. Every path's
#: life starts after the latest one: the vault ran in file mode in between, so no byte that the
#: audit trail names from before it is known to be Evermatter's.
RESETS_EVERY_LIFE = "(path IS NULL AND operation IN ('migrate', 'rollback'))"


class _Trail:
    """Writes a commit's audit rows and collects the intents to queue and the paths it touched."""

    def __init__(self, tx: Connection, revision: int, change: StoreChange, now: str) -> None:
        self._tx, self._revision, self._origin, self._reason, self._now = (
            tx, revision, change.origin, change.reason, now,
        )
        # A person's edit that the importer adopted is an import; any other new content is a write.
        self._entry = "import" if change.origin is Origin.OUTSIDE_EDITOR else "write"
        #: Each path's queued operation, and the old path of a move.
        self.intents: dict[str, tuple[str, str | None]] = {}
        self.written: list[str] = []
        self.deleted: list[str] = []

    def record(
        self, rel: str, operation: str, sha256: str | None, from_path: str | None = None,
        *, reason: str | None = None,
    ) -> None:
        self._tx.execute(
            "INSERT INTO store_audit("
            "revision, path, operation, origin, sha256, reason, at, from_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self._revision, rel, operation, self._origin.value, sha256,
             self._reason if reason is None else reason, self._now,
             from_path),
        )

    def wrote(self, rel: str, sha256: str, *, queue: bool = True, reason: str | None = None) -> None:
        self.record(rel, self._entry, sha256, reason=reason)
        self.written.append(rel)
        if queue:
            self.intents[rel] = ("write", None)

    def removed(self, rel: str, sha256: str | None, *, queue: bool = True) -> None:
        self.record(rel, "delete", sha256)
        self.deleted.append(rel)
        if queue:
            self.intents[rel] = ("delete", None)

    def moved_binary(self, move: BinaryMove, old_sha256: str | None) -> None:
        # A source that the store recorded ends its life here; one it never recorded had none.
        if old_sha256 is not None:
            self.record(move.src, "move", old_sha256)
            self.deleted.append(move.src)
        self.record(move.dst, "move", move.sha256, from_path=move.src)
        self.written.append(move.dst)
        self.intents[move.dst] = ("move", move.src)

    def moved(
        self, move: PageMove, old_sha256: str, new_sha256: str, *, media_allocation: bool,
    ) -> None:
        # Each side has its own row, so a read of either path's trail meets the move; the new path's
        # row names the old path. Each row holds the bytes that its path held or now holds.
        self.record(move.src, "move", old_sha256)
        self.record(move.dst, "move", new_sha256, from_path=move.src,
                    reason=MEDIA_PAYLOAD_WRITE_REASON if media_allocation else None)
        self.deleted.append(move.src)
        self.written.append(move.dst)
        if not move.on_disk:
            self.intents[move.dst] = ("move", move.src)


def _put_page(
    tx: Connection, put: PagePut, revision: int, audit: _Trail, *, prepared: _PreparedOriginal | None,
) -> None:
    digest = hashlib.sha256(put.body).hexdigest()
    on_disk = put.on_disk_mtime_ns is not None
    mtime_ns = time.time_ns() if put.on_disk_mtime_ns is None else put.on_disk_mtime_ns
    fields = frontmatter_json(put.body) if prepared is None else prepared.fields
    if prepared is not None:
        mtime_ns = prepared.mtime_ns
    tx.execute(
        "INSERT INTO store_pages(path, body, sha256, size, frontmatter_json, revision, mtime_ns) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET body = excluded.body, "
        "sha256 = excluded.sha256, size = excluded.size, "
        "frontmatter_json = excluded.frontmatter_json, "
        "revision = excluded.revision, mtime_ns = excluded.mtime_ns",
        (put.rel, put.body, digest, len(put.body), fields, revision, mtime_ns),
    )
    if on_disk:
        record_manifest(tx, put.rel, digest, len(put.body), mtime_ns, revision)
        _clear_queued(tx, put.rel)
    audit.wrote(put.rel, digest, queue=not on_disk,
                reason=MEDIA_PAYLOAD_WRITE_REASON if put.media_allocation else None)


def _clear_queued(tx: Connection, *paths: str) -> None:
    """Drop the projection intents queued for ``paths``: their files already match the store."""
    tx.executemany("DELETE FROM store_projection_queue WHERE path = ?", [(path,) for path in paths])


def _delete_page(tx: Connection, page: PageDelete, audit: _Trail) -> None:
    rel = page.rel
    row = tx.execute("SELECT sha256 FROM store_pages WHERE path = ?", (rel,)).fetchone()
    if row is None and not page.absent_ok:
        raise StoreConflict(rel, "the store holds no page to delete")
    tx.execute("DELETE FROM store_pages WHERE path = ?", (rel,))
    if page.on_disk:
        # The file is already gone, so nothing records it and nothing is left to project.
        tx.execute("DELETE FROM store_manifest WHERE path = ?", (rel,))
        _clear_queued(tx, rel)
    # A path with no page leaves a trail row without a hash: only its file's removal was queued.
    audit.removed(rel, None if row is None else row[0], queue=not page.on_disk)


def _move_page(
    tx: Connection, move: PageMove, revision: int, audit: _Trail, *,
    media_allocation: bool, prepared: _PreparedOriginal | None,
) -> None:
    page = tx.execute(
        "SELECT body, sha256, size, frontmatter_json, mtime_ns, revision FROM store_pages "
        "WHERE path = ?",
        (move.src,),
    ).fetchone()
    if page is None:
        raise StoreConflict(move.src, "the store holds no page to move")
    if _stored(tx, move.dst) is not None:
        raise StoreConflict(move.dst, "the store already holds it")
    *content, stored_revision = page
    if move.body is not None:
        content = [
            move.body,
            hashlib.sha256(move.body).hexdigest(),
            len(move.body),
            frontmatter_json(move.body) if prepared is None else prepared.fields,
            time.time_ns() if prepared is None else prepared.mtime_ns,
        ]
    if move.on_disk:
        # The file kept its bytes and mtime through the rename, so it must have been the stored page.
        if not _projected(tx, move.src, stored_revision, page[1]):
            raise StoreConflict(move.src, "its file on disk is not the stored page")
        if tx.execute("SELECT 1 FROM store_manifest WHERE path = ?", (move.dst,)).fetchone():
            raise StoreConflict(move.dst, "the manifest already records a file there")
    tx.execute("DELETE FROM store_pages WHERE path = ?", (move.src,))
    tx.execute(
        "INSERT INTO store_pages(path, body, sha256, size, frontmatter_json, mtime_ns, revision) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (move.dst, *content, revision),
    )
    if move.on_disk:
        tx.execute(
            "UPDATE store_manifest SET path = ?, revision = ? WHERE path = ?",
            (move.dst, revision, move.src),
        )
        # Nothing is left to project at either path; a queued intent at the destination named a file
        # that this rename replaced.
        _clear_queued(tx, move.src, move.dst)
    # The destination's move and its later write share this revision. Retain the
    # admission proved for the exact source, including a guarded lineage change.
    audit.moved(move, page[1], content[1], media_allocation=media_allocation)


def _record_binary(tx: Connection, record: BinaryRecord, revision: int, audit: _Trail) -> None:
    tx.execute(
        "INSERT INTO store_binaries(path, sha256, size, revision) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET sha256 = excluded.sha256, size = excluded.size, "
        "revision = excluded.revision",
        (record.rel, record.sha256, record.size, revision),
    )
    # The file is durable already, so a queued removal of an earlier record of it must not run.
    _clear_queued(tx, record.rel)
    audit.wrote(record.rel, record.sha256, queue=False)


def _delete_binary(tx: Connection, binary: BinaryDelete, audit: _Trail) -> None:
    row = tx.execute("SELECT sha256 FROM store_binaries WHERE path = ?", (binary.rel,)).fetchone()
    if row is None:
        raise StoreConflict(binary.rel, "the store records no binary to delete")
    tx.execute("DELETE FROM store_binaries WHERE path = ?", (binary.rel,))
    audit.removed(binary.rel, row[0], queue=not binary.on_disk)


def _move_binary(tx: Connection, move: BinaryMove, revision: int, audit: _Trail) -> None:
    if _stored(tx, move.dst) is not None:
        raise StoreConflict(move.dst, "the store already holds it")
    recorded = tx.execute(
        "SELECT sha256 FROM store_binaries WHERE path = ?", (move.src,)
    ).fetchone()
    tx.execute("DELETE FROM store_binaries WHERE path = ?", (move.src,))
    tx.execute(
        "INSERT INTO store_binaries(path, sha256, size, revision) VALUES (?, ?, ?, ?)",
        (move.dst, move.sha256, move.size, revision),
    )
    audit.moved_binary(move, None if recorded is None else recorded[0])


def _degrade_moves(tx: Connection, paths: tuple[str, ...]) -> None:
    """Turn each queued move that touches ``paths`` into the delete and the write it stands for.

    A move intent says that the old file may be renamed. A later change to the new path or to the
    old one must not leave the old file behind, and must not let the projector rename a page that
    now lives at the old path. So a touched move becomes a delete of its old path and a write of
    its new path, and the later change then replaces either row as it would any other.

    A binary's move cannot split that way: the store holds no bytes to write, and the delete would
    take away the only copy. Such a change refuses until the move reaches the disk, which the
    writer does right after its commit and the projector after a kill: it prevents the loss of a
    binary's bytes, and a wrong refusal costs the caller one retry.
    """
    marks = ",".join("?" * len(paths))
    touched = tx.execute(
        "SELECT path, from_path, revision, queued_at FROM store_projection_queue "
        f"WHERE operation = 'move' AND (path IN ({marks}) OR from_path IN ({marks}))",
        (*paths, *paths),
    ).fetchall()
    for path, from_path, revision, queued_at in touched:
        if ownership.classify(path) is ownership.Ownership.BINARY:
            raise StoreConflict(path, "its file has not moved yet; retry once it has")
        tx.execute(
            "UPDATE store_projection_queue SET operation = 'write', from_path = NULL WHERE path = ?",
            (path,),
        )
        tx.execute(
            "INSERT OR IGNORE INTO store_projection_queue(path, revision, operation, queued_at) "
            "VALUES (?, ?, 'delete', ?)",
            (from_path, revision, queued_at),
        )


def _enqueue(
    tx: Connection, intents: Mapping[str, tuple[str, str | None]], revision: int, now: str
) -> None:
    # A move vacates its old path, so whatever was queued there no longer has a file to act on.
    tx.executemany(
        "DELETE FROM store_projection_queue WHERE path = ?",
        [(from_path,) for operation, from_path in intents.values() if operation == "move"],
    )
    # queued_at is left out of the update, so the queue keeps the age of the oldest change.
    tx.executemany(
        "INSERT INTO store_projection_queue(path, revision, operation, from_path, queued_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
        "revision = excluded.revision, operation = excluded.operation, "
        "from_path = excluded.from_path",
        [
            (rel, revision, operation, from_path, now)
            for rel, (operation, from_path) in intents.items()
        ],
    )
