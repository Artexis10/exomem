"""Held items of a store-mode vault: list them, and resolve each with keep-mine or keep-store
(OpenSpec make-vault-store-canonical, design D5, tasks 4.1 to 4.4).

A held item is a person's outside edit that raced an Exomem change, or an outside delete of a page
the store owns. The projector, and later the importer, records it in ``store_held`` and stops
projecting that path. Keep-mine makes the person's file the stored page. Keep-store lets the
store's page win, and first saves the person's bytes to ``held-exports/<date>/<path>`` in the
vault's machine-local state, outside the vault: a resolution never loses a person's bytes, and it
never relies on ``store_held.body``, which a very large edit leaves empty.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from pathlib import Path, PurePosixPath
from sqlite3 import Connection
from typing import TYPE_CHECKING

from .. import held_fs, state_paths
from ..cli_ops import OpError
from . import mode, ownership, read, write

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

KEEP_MINE = "keep-mine"
KEEP_STORE = "keep-store"
# The two resolutions that the store contract fixes: the person's file or the store's page wins.
CHOICES = (KEEP_MINE, KEEP_STORE)  # nosemgrep: ep-word-set -- closed set that design D5 fixes

BAD_CHOICE = "VAULT_STORE_HELD_CHOICE"
UNREADABLE = "VAULT_STORE_HELD_UNREADABLE"
NO_WRITER = "VAULT_STORE_WRITER_UNAVAILABLE"
#: The folder of the vault's state directory that keeps the person's bytes that keep-store replaced.
EXPORT_FOLDER = "held-exports"

_LIST_AGAIN = (
    "List the held items again and read the item as it stands now. If the store's page changed "
    f"after the edit was held, {KEEP_STORE} takes it and saves the person's file first. "
    "Then resolve with the new item_version."
)
_ITEM_COLUMNS = "path, kind, store_revision, sha256, detected_at"


@dataclass(frozen=True, slots=True)
class PersonFile:
    """The person's file at a held path, read now.

    ``exists`` is None when the file could not be read: ``unreadable`` then names why, and the
    other fields stay empty.
    """

    exists: bool | None
    sha256: str | None = None
    size: int | None = None
    unreadable: str | None = None


@dataclass(frozen=True, slots=True)
class StoreFacts:
    """The store's page or binary record at the held path, now."""

    revision: int
    sha256: str


@dataclass(frozen=True, slots=True)
class HeldItem:
    """One held item.

    ``item_version`` names everything below, so a resolution refuses when any of it changed.
    """

    path: str
    kind: str
    detected_at: str
    #: What the row recorded when it was held: the store's revision then, and the held bytes' hash.
    held_store_revision: int | None
    held_sha256: str | None
    file: PersonFile
    store: StoreFacts | None
    item_version: str


@dataclass(frozen=True, slots=True)
class Resolution:
    """What a resolution did.

    ``exported_to`` is where keep-store saved the person's bytes, or None when no file was there.
    """

    path: str
    choice: str
    revision: int
    #: Whether the person's file was there when resolved: it says what keep-mine made of the page.
    person_file_existed: bool
    exported_to: Path | None = None
    exported_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _Disk:
    file: PersonFile
    mtime_ns: int | None = None
    body: bytes | None = None


def list_held(vault_root: Path) -> list[HeldItem]:
    """The held items of a store-mode vault, oldest first; a file-mode vault holds none."""
    root = Path(vault_root)
    if not mode.store_mode_active(root):
        return []
    with read.snapshot(root) as view:
        rows = view.connection.execute(
            f"SELECT {_ITEM_COLUMNS} FROM store_held ORDER BY detected_at, path"
        ).fetchall()
        stored = {row[0]: _store_facts(view, row[0]) for row in rows}
    disks = _observe(root, [row[0] for row in rows], keep_body=False)
    return [_item(row, disks[row[0]].file, stored[row[0]]) for row in rows]


def resolve_held(
    vault_root: Path,
    rel: str,
    choice: str,
    *,
    why: str,
    seen: str,
    writer: CollectionWriter | None = None,
) -> Resolution:
    """Resolve the held item at ``rel`` as ``choice`` says, in one commit of the store.

    ``seen`` is the ``item_version`` that the agent read from the list: the item as listed must
    still be the item as it stands, or the resolution refuses with a ``StoreConflict`` and
    changes nothing.
    ``writer`` is the store's writer; a served request binds it, so only a test passes one.
    """
    root = Path(vault_root)
    if choice not in CHOICES:
        raise OpError(
            BAD_CHOICE, f"{choice!r} is not a resolution", f"Choose {KEEP_MINE} or {KEEP_STORE}."
        )
    why = why.strip()
    if not why:
        raise OpError(
            "INVALID_INPUT",
            "a resolution needs a reason",
            "Say in `why` what the person or owner asked for.",
        )
    if not mode.store_mode_active(root):
        raise OpError(
            read.NOT_ACTIVE,
            "this vault is not in store mode, so it holds no held items",
            "Resolve held items only in a vault that `exomem store-mode` has migrated.",
        )
    if ownership.classify(rel) is not ownership.Ownership.PAGE:
        raise OpError(
            write.NOT_OWNED,
            f"{rel}: only a page's held item can be resolved here",
            "Restore a held binary from its source, then run "
            "`exomem store-mode rollback --export-held` if it stays held.",
        )
    writer = writer or _bound_writer(root)
    row, store, page = _read_store(root, rel, with_body=choice == KEEP_STORE)
    disk = _observe(root, [rel], keep_body=True)[rel]
    if disk.file.unreadable is not None:
        raise OpError(
            UNREADABLE,
            f"{rel}: the person's file cannot be read ({disk.file.unreadable})",
            "Make the file a readable regular file, not a symlink or a folder, "
            "then list the held items again.",
        )
    if _item(row, disk.file, store).item_version != seen:
        raise write.StoreConflict(rel, "this held item changed after it was listed", _LIST_AGAIN)
    held_revision = row[2]
    exported: tuple[Path | None, str | None] = (None, None)
    if choice == KEEP_MINE:
        change = _keep_mine(rel, disk, held_revision, why)
    else:
        exported = _export(root, rel, disk.body)
        change = _keep_store(rel, page, why)
    try:
        result = write.commit(writer, change, settle=_settle(rel, row, choice, disk))
    except write.StoreConflict as conflict:
        if conflict.remediation is None:
            raise write.StoreConflict(conflict.path, conflict.reason, _LIST_AGAIN) from conflict
        raise
    return Resolution(rel, choice, result.revision, disk.body is not None, *exported)


def _bound_writer(root: Path) -> CollectionWriter:
    from ..collection_store.preview import bound_writer

    writer = bound_writer(root)
    if writer is None:
        raise OpError(
            NO_WRITER,
            "a held item resolves inside the running Exomem service, which holds the writer",
            "Resolve it through the repair tool of a connected Exomem service.",
        )
    return writer


def _store_facts(view: read.StoreSnapshot, rel: str) -> StoreFacts | None:
    page = view.page_meta(rel)
    if page is not None:
        return StoreFacts(page.revision, page.sha256)
    binary = view.binary(rel)
    return None if binary is None else StoreFacts(binary.revision, binary.sha256)


def _read_store(
    root: Path, rel: str, *, with_body: bool
) -> tuple[tuple, StoreFacts | None, read.StoredPage | None]:
    """The held row at ``rel``, the store's facts for the path and, on request, its stored page."""
    with read.snapshot(root) as view:
        row = view.connection.execute(
            f"SELECT {_ITEM_COLUMNS} FROM store_held WHERE path = ?", (rel,)
        ).fetchone()
        if row is None:
            raise write.StoreConflict(
                rel,
                "nothing is held for this path: another agent may have resolved it",
                "List the held items to see what is held now.",
            )
        page = view.page(rel) if with_body else None
        store = _store_facts(view, rel)
    return row, store, page


def _item(row: tuple, file: PersonFile, store: StoreFacts | None) -> HeldItem:
    rel, kind, held_revision, held_sha256, detected_at = row
    witness = [
        rel,
        kind,
        held_revision,
        held_sha256,
        detected_at,
        file.exists,
        file.sha256,
        file.size,
        file.unreadable,
    ]
    if store is not None:
        witness += [store.revision, store.sha256]
    version = hashlib.sha256(json.dumps(witness).encode()).hexdigest()[:16]
    return HeldItem(rel, kind, detected_at, held_revision, held_sha256, file, store, version)


def _observe(root: Path, rels: Sequence[str], *, keep_body: bool) -> dict[str, _Disk]:
    """The person's file at each path, read through handles that refuse a symlink."""
    try:
        with held_fs.acquire(root).require() as filesystem:
            return {rel: _observe_file(filesystem, rel, keep_body) for rel in rels}
    except held_fs.HeldFsError as error:
        return {rel: _Disk(PersonFile(None, unreadable=error.code)) for rel in rels}


def _observe_file(filesystem: held_fs.HeldFilesystem, rel: str, keep_body: bool) -> _Disk:
    folder, _, leaf = rel.rpartition("/")
    try:
        opened = filesystem.parent(folder)
        if _missing(opened):
            return _Disk(PersonFile(False))
        with opened.require() as parent:
            found = filesystem.file(parent, leaf)
            if _missing(found):
                return _Disk(PersonFile(False))
            with found.require() as file:
                version = file.version()
                body = filesystem.read(file).require()
    except held_fs.HeldFsError as error:
        return _Disk(PersonFile(None, unreadable=error.code))
    facts = PersonFile(True, hashlib.sha256(body).hexdigest(), len(body))
    return _Disk(facts, version.mtime_ns, body if keep_body else None)


def _missing(result: held_fs.HeldResult) -> bool:
    return result.error is not None and result.error.code == "MISSING"


def _keep_mine(rel: str, disk: _Disk, held_revision: int | None, why: str) -> write.StoreChange:
    """The person's file as it is now becomes the stored page, or the page goes with the file.

    The page must still be at the revision it had when the edit was held, so a newer Exomem change
    is refused and never overwritten. The file already holds the bytes, so nothing is projected.
    """
    expect = {
        rel: write.Expect(absent=True)
        if held_revision is None
        else write.Expect(revision=held_revision)
    }
    reason = f"held {KEEP_MINE}: {why}"
    if disk.body is None:
        deletes = (write.PageDelete(rel, on_disk=True, absent_ok=True),)
        return write.StoreChange(
            write.Origin.OUTSIDE_EDITOR, reason, deletes=deletes, expect=expect
        )
    puts = (write.PagePut(rel, disk.body, on_disk_mtime_ns=disk.mtime_ns),)
    return write.StoreChange(write.Origin.OUTSIDE_EDITOR, reason, puts=puts, expect=expect)


def _keep_store(rel: str, page: read.StoredPage | None, why: str) -> write.StoreChange:
    """The store's page, as read now, is queued for projection; no page queues a removal."""
    reason = f"held {KEEP_STORE}: {why}"
    if page is None:
        return write.StoreChange(
            write.Origin.GOVERNED,
            reason,
            deletes=(write.PageDelete(rel, absent_ok=True),),
            expect={rel: write.Expect(absent=True)},
        )
    expect = {rel: write.Expect(revision=page.meta.revision, sha256=page.meta.sha256)}
    return write.StoreChange(
        write.Origin.GOVERNED, reason, puts=(write.PagePut(rel, page.body),), expect=expect
    )


def _settle(rel: str, row: tuple, choice: str, disk: _Disk):
    """The part of the commit that is the resolution itself, in the commit's own transaction.

    It drops the held row that the agent read, so a row that changed or went meanwhile rolls the
    whole commit back. Keep-store also records the person's file as what the projection manifest
    saw, so the projector replaces that file with the store's page and does not hold it again.
    """
    _, kind, _, held_sha256, detected_at = row

    def settle(tx: Connection, revision: int) -> None:
        dropped = tx.execute(
            "DELETE FROM store_held "
            "WHERE path = ? AND kind = ? AND detected_at = ? AND sha256 IS ?",
            (rel, kind, detected_at, held_sha256),
        ).rowcount
        if dropped != 1:
            raise write.StoreConflict(
                rel, "this held item changed while it was resolved", _LIST_AGAIN
            )
        if choice != KEEP_STORE:
            return
        if disk.body is None:
            tx.execute("DELETE FROM store_manifest WHERE path = ?", (rel,))
        else:
            assert (
                disk.mtime_ns is not None
                and disk.file.sha256 is not None
                and disk.file.size is not None
            )
            write.record_manifest(
                tx, rel, disk.file.sha256, disk.file.size, disk.mtime_ns, revision
            )

    return settle


def _export(root: Path, rel: str, body: bytes | None) -> tuple[Path | None, str | None]:
    """Save the person's bytes under ``held-exports/<date>/<path>``, durably.

    It returns where they went and their hash.

    A file that is already there is never replaced: the bytes go to the first free
    ``<name>.<n><suffix>``. The bytes are linked into place from a flushed temporary file, so a
    crash leaves them whole or absent.
    """
    if body is None:
        return None, None
    parts = PurePosixPath(rel).parts
    if ".." in parts or rel.startswith("/"):
        raise OpError(
            "INVALID_INPUT",
            f"{rel}: a held path stays inside the vault",
            "List the held items again.",
        )
    state = state_paths.vault_state_dir(root)
    target = state / EXPORT_FOLDER / datetime.now(UTC).strftime("%Y-%m-%d") / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".held-export-{uuid.uuid4().hex}"
    try:
        with open(temporary, "xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in count():
            candidate = (
                target
                if attempt == 0
                else target.with_name(f"{target.stem}.{attempt}{target.suffix}")
            )
            try:
                os.link(temporary, candidate)
            except FileExistsError:
                continue
            break
    finally:
        temporary.unlink(missing_ok=True)
    directory = candidate.parent
    while True:
        held_fs.flush_directory_path(directory).require()
        if directory == state:
            break
        directory = directory.parent
    return candidate, hashlib.sha256(body).hexdigest()
