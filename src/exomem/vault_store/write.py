"""The write entry point of a store-mode vault (OpenSpec make-vault-store-canonical, 1.6, 2.2, 2.3).

``commit`` changes the store's pages, binary records, projection manifest, audit trail and
projection queue, and the derived rows of those pages (lexical, graph, identity; see ``derive``), in
one ``BEGIN IMMEDIATE`` transaction and advances ``store_mode.revision`` by exactly one. A reader
sees all of a commit or none of it.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from sqlite3 import Connection
from typing import TYPE_CHECKING

from .. import vault
from ..cli_ops import OpError
from ..collection_store import connection
from . import derive, mode, ownership, rederive
from .read import AFTER, NOT_ACTIVE

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

CONFLICT = "VAULT_STORE_CONFLICT"
NOT_OWNED = "VAULT_STORE_NOT_OWNED"


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
    expect: Mapping[str, Expect] = field(default_factory=dict)
    resolves_held: tuple[HeldRow, ...] = ()
    files_seen: tuple[FileSeen, ...] = ()
    refresh: tuple[str, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        """Every path the change names, a move naming both of its paths."""
        return (
            *(put.rel for put in self.puts),
            *(delete.rel for delete in self.deletes),
            *(path for move in self.moves for path in (move.src, move.dst)),
            *(record.rel for record in self.binaries),
            *(delete.rel for delete in self.binary_deletes),
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


def commit(writer: CollectionWriter, change: StoreChange) -> CommitResult:
    """Apply ``change`` in one transaction on the store thread's writer, and return its revision.

    The statements run in this order, so that a kill at any point leaves the old state whole: the
    expectations, the pages, the binary records and the manifest, one audit row per file, the
    projection intents, the held rows that the change resolves, the files that it saw and last the
    revision. One intent stands for each path, and the oldest queue time stays. A move queues one
    ``move`` intent under its destination path, with the old path in ``from_path``. A held row that
    changed or went meanwhile rolls the whole commit back.
    """
    _require_owned(change)
    with writer.handle.transaction() as tx:
        row = tx.execute("SELECT mode, revision FROM store_mode WHERE id = 1").fetchone()
        if row is None or row[0] != mode.STORE_MODE:
            raise connection.CollectionStoreError(NOT_ACTIVE, "this vault is not in store mode")
        revision = row[1] + 1
        check(tx, change.expect)
        for rel in change.refresh:
            if tx.execute(
                "SELECT 1 FROM store_pages WHERE path = ?", (rel,)
            ).fetchone():
                raise OpError(NOT_OWNED, f"the store holds {rel}: write it, do not refresh it")
        _degrade_moves(tx, change.paths)
        existing = _held_pages(tx, change)
        now = timestamp()
        audit = _Trail(tx, revision, change, now)
        for put in change.puts:
            _put_page(tx, put, revision, audit)
        for page in change.deletes:
            _delete_page(tx, page, audit)
        for move in change.moves:
            _move_page(tx, move, revision, audit)
        for record in change.binaries:
            _record_binary(tx, record, revision, audit)
        for binary in change.binary_deletes:
            _delete_binary(tx, binary, audit)
        derive.apply(tx, writer.root, change, existing)
        # The pages above derived with the overlay that the disk still holds; the job settles them.
        rederive.record_overlay_change(tx, writer.root, change.paths, revision)
        _enqueue(tx, audit.intents, revision, now)
        for held in change.resolves_held:
            _drop_held(tx, held)
        for seen in change.files_seen:
            _record_seen(tx, seen, revision)
        tx.execute("UPDATE store_mode SET revision = ? WHERE id = 1", (revision,))
    return CommitResult(revision, tuple(audit.written), tuple(audit.deleted))


def _held_pages(tx: Connection, change: StoreChange) -> frozenset[str]:
    """The paths of ``change``'s puts that the store held a page for before the commit."""
    return frozenset(
        put.rel
        for put in change.puts
        if tx.execute("SELECT 1 FROM store_pages WHERE path = ?", (put.rel,)).fetchone()
    )


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
    )
    for rels, kind in ((pages, ownership.Ownership.PAGE), (binaries, ownership.Ownership.BINARY)):
        for rel in rels:
            _require_safe(rel)
            if ownership.classify(rel) is not kind:
                raise OpError(NOT_OWNED, f"the store holds no {kind.value} at {rel}")


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


def check(tx: Connection, expect: Mapping[str, Expect]) -> None:
    """Raise ``StoreConflict`` for the first path that does not hold what ``expect`` says."""
    # One read of the inbound rows binds the linking pages of every path: a folder's delete binds
    # hundreds.
    linked = vault.store_inbound_wikilinks(
        tx, [rel for rel, want in expect.items() if want.linked_from is not None]
    )
    for rel, want in expect.items():
        _check_below(tx, rel, want)
        if not want.binds_path:
            continue
        have = _stored(tx, rel)
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
#: Exomem put at a path in its current life from those of an earlier life, which a person may have
#: restored.
BEGINS_A_LIFE = (
    "(operation IN ('write', 'import') OR (operation = 'move' AND from_path IS NOT NULL))"
)
ENDS_A_LIFE = "(operation = 'delete' OR (operation = 'move' AND from_path IS NULL))"
#: A migration or a rollback writes one audit row for the whole vault, with no path. Every path's
#: life starts after the latest one: the vault ran in file mode in between, so no byte that the
#: audit trail names from before it is known to be Exomem's.
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
        self, rel: str, operation: str, sha256: str | None, from_path: str | None = None
    ) -> None:
        self._tx.execute(
            "INSERT INTO store_audit("
            "revision, path, operation, origin, sha256, reason, at, from_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self._revision, rel, operation, self._origin.value, sha256, self._reason, self._now,
             from_path),
        )

    def wrote(self, rel: str, sha256: str, *, queue: bool = True) -> None:
        self.record(rel, self._entry, sha256)
        self.written.append(rel)
        if queue:
            self.intents[rel] = ("write", None)

    def removed(self, rel: str, sha256: str | None, *, queue: bool = True) -> None:
        self.record(rel, "delete", sha256)
        self.deleted.append(rel)
        if queue:
            self.intents[rel] = ("delete", None)

    def moved(self, move: PageMove, old_sha256: str, new_sha256: str) -> None:
        # Each side has its own row, so a read of either path's trail meets the move; the new path's
        # row names the old path. Each row holds the bytes that its path held or now holds.
        self.record(move.src, "move", old_sha256)
        self.record(move.dst, "move", new_sha256, from_path=move.src)
        self.deleted.append(move.src)
        self.written.append(move.dst)
        if not move.on_disk:
            self.intents[move.dst] = ("move", move.src)


def _put_page(tx: Connection, put: PagePut, revision: int, audit: _Trail) -> None:
    digest = hashlib.sha256(put.body).hexdigest()
    on_disk = put.on_disk_mtime_ns is not None
    mtime_ns = time.time_ns() if put.on_disk_mtime_ns is None else put.on_disk_mtime_ns
    tx.execute(
        "INSERT INTO store_pages(path, body, sha256, size, frontmatter_json, revision, mtime_ns) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET body = excluded.body, "
        "sha256 = excluded.sha256, size = excluded.size, "
        "frontmatter_json = excluded.frontmatter_json, "
        "revision = excluded.revision, mtime_ns = excluded.mtime_ns",
        (put.rel, put.body, digest, len(put.body), frontmatter_json(put.body), revision, mtime_ns),
    )
    if on_disk:
        record_manifest(tx, put.rel, digest, len(put.body), mtime_ns, revision)
        _clear_queued(tx, put.rel)
    audit.wrote(put.rel, digest, queue=not on_disk)


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


def _move_page(tx: Connection, move: PageMove, revision: int, audit: _Trail) -> None:
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
            frontmatter_json(move.body),
            time.time_ns(),
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
    audit.moved(move, page[1], content[1])


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


def _degrade_moves(tx: Connection, paths: tuple[str, ...]) -> None:
    """Turn each queued move that touches ``paths`` into the delete and the write it stands for.

    A move intent says that the old file may be renamed. A later change to the new path or to the
    old one must not leave the old file behind, and must not let the projector rename a page that
    now lives at the old path. So a touched move becomes a delete of its old path and a write of
    its new path, and the later change then replaces either row as it would any other.
    """
    marks = ",".join("?" * len(paths))
    touched = tx.execute(
        "SELECT path, from_path, revision, queued_at FROM store_projection_queue "
        f"WHERE operation = 'move' AND (path IN ({marks}) OR from_path IN ({marks}))",
        (*paths, *paths),
    ).fetchall()
    for path, from_path, revision, queued_at in touched:
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
