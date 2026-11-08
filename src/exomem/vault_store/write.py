"""The write entry point of a store-mode vault: the page-table half of one commit (OpenSpec
make-vault-store-canonical, task 1.6, with 2.2 to build on it).

``commit`` changes the store's pages, binary records, projection manifest, audit trail and
projection queue in one ``BEGIN IMMEDIATE`` transaction and advances ``store_mode.revision`` by
exactly one. A reader sees all of a commit or none of it. The derived rows (lexical, graph,
references) are not written here: the derivation half joins this transaction later, and until
then nothing in production calls ``commit``.
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
from . import ownership
from .read import NOT_ACTIVE

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
    for the path and no outside change is held for it.
    """

    revision: int | None = None
    sha256: str | None = None
    absent: bool = False
    projected: bool = False

    def __post_init__(self) -> None:
        if self.absent and (self.revision, self.sha256, self.projected) != (None, None, False):
            raise ValueError("an expectation of absence names nothing else")


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
    rel: str


@dataclass(frozen=True, slots=True)
class PageMove:
    """A page's bytes moved to a path the store does not hold.

    ``on_disk`` says that the file already moved there.
    """

    src: str
    dst: str
    on_disk: bool = False


@dataclass(frozen=True, slots=True)
class BinaryRecord:
    """A binary that is already durable on disk: the store records its path, hash and size."""

    rel: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class BinaryDelete:
    """Drop a binary's record and queue the removal of its file."""

    rel: str


@dataclass(frozen=True, slots=True)
class StoreChange:
    """Everything one commit changes. A path appears in at most one of the operations."""

    origin: Origin
    reason: str
    puts: tuple[PagePut, ...] = ()
    deletes: tuple[PageDelete, ...] = ()
    moves: tuple[PageMove, ...] = ()
    binaries: tuple[BinaryRecord, ...] = ()
    binary_deletes: tuple[BinaryDelete, ...] = ()
    expect: Mapping[str, Expect] = field(default_factory=dict)

    def __post_init__(self) -> None:
        paths = [
            *(put.rel for put in self.puts),
            *(delete.rel for delete in self.deletes),
            *(path for move in self.moves for path in (move.src, move.dst)),
            *(record.rel for record in self.binaries),
            *(delete.rel for delete in self.binary_deletes),
        ]
        if not paths:
            raise ValueError("a store change names no file")
        if len(set(paths)) != len(paths):
            raise ValueError("a store change names a path more than once")


@dataclass(frozen=True, slots=True)
class CommitResult:
    """The revision that the commit made, and the paths it gave bytes or records to and removed."""

    revision: int
    written: tuple[str, ...]
    deleted: tuple[str, ...]


class StoreConflict(OpError):
    """A path did not hold what the change expected; nothing was committed."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(CONFLICT, f"{path}: {reason}")
        self.path = path


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
    projection intents, and last the revision. One intent stands for each path, and the oldest
    queue time stays.
    """
    _require_owned(change)
    with writer.handle.transaction() as tx:
        row = tx.execute("SELECT mode, revision FROM store_mode WHERE id = 1").fetchone()
        if row is None or row[0] != "store":
            raise connection.CollectionStoreError(NOT_ACTIVE, "this vault is not in store mode")
        revision = row[1] + 1
        _check(tx, change.expect)
        now = _timestamp()
        audit = _Trail(tx, revision, change, now)
        for put in change.puts:
            _put_page(tx, put, revision, audit)
        for page in change.deletes:
            _delete_page(tx, page.rel, audit)
        for move in change.moves:
            _move_page(tx, move, revision, audit)
        for record in change.binaries:
            _record_binary(tx, record, revision, audit)
        for binary in change.binary_deletes:
            _delete_binary(tx, binary.rel, audit)
        _enqueue(tx, audit.intents, revision, now)
        tx.execute("UPDATE store_mode SET revision = ? WHERE id = 1", (revision,))
    return CommitResult(revision, tuple(audit.written), tuple(audit.deleted))


def _require_owned(change: StoreChange) -> None:
    """Refuse, before any write, a path that the store cannot hold as its operation needs."""
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
            if ownership.classify(rel) is not kind:
                raise OpError(NOT_OWNED, f"the store holds no {kind.value} at {rel}")


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stored(tx: Connection, rel: str) -> tuple[int, str] | None:
    """The revision and hash of the page or binary record at ``rel``, or None.

    A path is a page or a binary, never both.
    """
    return tx.execute(
        "SELECT revision, sha256 FROM store_pages WHERE path = ? "
        "UNION ALL SELECT revision, sha256 FROM store_binaries WHERE path = ?",
        (rel, rel),
    ).fetchone()


def _check(tx: Connection, expect: Mapping[str, Expect]) -> None:
    for rel, want in expect.items():
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
        if want.projected and not _projected(tx, rel, have[0]):
            raise StoreConflict(rel, "its file on disk is not the stored page")


def _projected(tx: Connection, rel: str, revision: int) -> bool:
    manifest = tx.execute("SELECT revision FROM store_manifest WHERE path = ?", (rel,)).fetchone()
    queued = tx.execute("SELECT 1 FROM store_projection_queue WHERE path = ?", (rel,)).fetchone()
    held = tx.execute("SELECT 1 FROM store_held WHERE path = ?", (rel,)).fetchone()
    return manifest is not None and manifest[0] == revision and queued is None and held is None


class _Trail:
    """Writes a commit's audit rows and collects the intents to queue and the paths it touched."""

    def __init__(self, tx: Connection, revision: int, change: StoreChange, now: str) -> None:
        self._tx, self._revision, self._origin, self._reason, self._now = (
            tx, revision, change.origin, change.reason, now,
        )
        # A person's edit that the importer adopted is an import; any other new content is a write.
        self._entry = "import" if change.origin is Origin.OUTSIDE_EDITOR else "write"
        self.intents: dict[str, str] = {}
        self.written: list[str] = []
        self.deleted: list[str] = []

    def record(
        self, rel: str, operation: str, sha256: str | None, reason: str | None = None
    ) -> None:
        self._tx.execute(
            "INSERT INTO store_audit(revision, path, operation, origin, sha256, reason, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self._revision, rel, operation, self._origin.value, sha256,
             reason or self._reason, self._now),
        )

    def wrote(self, rel: str, sha256: str, *, queue: bool = True) -> None:
        self.record(rel, self._entry, sha256)
        self.written.append(rel)
        if queue:
            self.intents[rel] = "write"

    def removed(self, rel: str, sha256: str, *, queue: bool = True) -> None:
        self.record(rel, "delete", sha256)
        self.deleted.append(rel)
        if queue:
            self.intents[rel] = "delete"

    def moved(self, move: PageMove, sha256: str) -> None:
        # The audit has no column for a move's side, so each row's reason names the other path.
        self.record(move.src, "move", sha256, f"{self._reason}; moved to {move.dst}")
        self.record(move.dst, "move", sha256, f"{self._reason}; moved from {move.src}")
        self.deleted.append(move.src)
        self.written.append(move.dst)
        if not move.on_disk:
            self.intents[move.src], self.intents[move.dst] = "delete", "write"


def _put_page(tx: Connection, put: PagePut, revision: int, audit: _Trail) -> None:
    digest = hashlib.sha256(put.body).hexdigest()
    on_disk = put.on_disk_mtime_ns is not None
    mtime_ns = put.on_disk_mtime_ns if on_disk else time.time_ns()
    tx.execute(
        "INSERT INTO store_pages(path, body, sha256, size, frontmatter_json, revision, mtime_ns) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET body = excluded.body, "
        "sha256 = excluded.sha256, size = excluded.size, "
        "frontmatter_json = excluded.frontmatter_json, "
        "revision = excluded.revision, mtime_ns = excluded.mtime_ns",
        (put.rel, put.body, digest, len(put.body), frontmatter_json(put.body), revision, mtime_ns),
    )
    if on_disk:
        tx.execute(
            "INSERT INTO store_manifest(path, sha256, size, mtime_ns, revision) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET sha256 = excluded.sha256, size = excluded.size, "
            "mtime_ns = excluded.mtime_ns, revision = excluded.revision",
            (put.rel, digest, len(put.body), mtime_ns, revision),
        )
        # The file already is the page, so a queued projection of an older state has nothing to do.
        tx.execute("DELETE FROM store_projection_queue WHERE path = ?", (put.rel,))
    audit.wrote(put.rel, digest, queue=not on_disk)


def _delete_page(tx: Connection, rel: str, audit: _Trail) -> None:
    row = tx.execute("SELECT sha256 FROM store_pages WHERE path = ?", (rel,)).fetchone()
    if row is None:
        raise StoreConflict(rel, "the store holds no page to delete")
    tx.execute("DELETE FROM store_pages WHERE path = ?", (rel,))
    audit.removed(rel, row[0])


def _move_page(tx: Connection, move: PageMove, revision: int, audit: _Trail) -> None:
    page = tx.execute(
        "SELECT body, sha256, size, frontmatter_json, mtime_ns FROM store_pages WHERE path = ?",
        (move.src,),
    ).fetchone()
    if page is None:
        raise StoreConflict(move.src, "the store holds no page to move")
    if _stored(tx, move.dst) is not None:
        raise StoreConflict(move.dst, "the store already holds it")
    tx.execute("DELETE FROM store_pages WHERE path = ?", (move.src,))
    tx.execute(
        "INSERT INTO store_pages(path, body, sha256, size, frontmatter_json, mtime_ns, revision) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (move.dst, *page, revision),
    )
    if move.on_disk:
        # The file kept its bytes and mtime through the rename, so the manifest row follows it.
        moved = tx.execute(
            "UPDATE store_manifest SET path = ?, revision = ? WHERE path = ?",
            (move.dst, revision, move.src),
        )
        if moved.rowcount != 1:
            raise StoreConflict(move.src, "the manifest records no projected file to move")
    audit.moved(move, page[1])


def _record_binary(tx: Connection, record: BinaryRecord, revision: int, audit: _Trail) -> None:
    tx.execute(
        "INSERT INTO store_binaries(path, sha256, size, revision) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET sha256 = excluded.sha256, size = excluded.size, "
        "revision = excluded.revision",
        (record.rel, record.sha256, record.size, revision),
    )
    audit.wrote(record.rel, record.sha256, queue=False)


def _delete_binary(tx: Connection, rel: str, audit: _Trail) -> None:
    row = tx.execute("SELECT sha256 FROM store_binaries WHERE path = ?", (rel,)).fetchone()
    if row is None:
        raise StoreConflict(rel, "the store records no binary to delete")
    tx.execute("DELETE FROM store_binaries WHERE path = ?", (rel,))
    audit.removed(rel, row[0])


def _enqueue(tx: Connection, intents: Mapping[str, str], revision: int, now: str) -> None:
    # queued_at is left out of the update, so the queue keeps the age of the oldest change.
    tx.executemany(
        "INSERT INTO store_projection_queue(path, revision, operation, queued_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
        "revision = excluded.revision, operation = excluded.operation",
        [(rel, revision, operation, now) for rel, operation in intents.items()],
    )
