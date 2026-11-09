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
from typing import TYPE_CHECKING

from .. import held_fs, state_paths
from ..cli_ops import OpError
from ..curation import MAX_WHY_CHARS
from . import mode, ownership, projector, read, write

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

KEEP_MINE = "keep-mine"
KEEP_STORE = "keep-store"
# The two resolutions that the store contract fixes: the person's file or the store's page wins.
CHOICES = (KEEP_MINE, KEEP_STORE)  # nosemgrep: ep-word-set -- closed set that design D5 fixes

BAD_CHOICE = "VAULT_STORE_HELD_CHOICE"
UNREADABLE = "VAULT_STORE_HELD_UNREADABLE"
TOO_LARGE = "VAULT_STORE_HELD_TOO_LARGE"
NO_WRITER = "VAULT_STORE_WRITER_UNAVAILABLE"
#: The code of every refusal to write that the connector boundary gives, as a governed write has it.
WRITE_REFUSED = "WRITE_REFUSED"
#: The folder of the vault's state directory that keeps the person's bytes that keep-store replaced.
EXPORT_FOLDER = "held-exports"

#: The refusal for a path with no held item, whether it was resolved or the caller may not read it.
NOTHING_HELD = "nothing is held for this path: another agent may have resolved it"
_NOTHING_HELD_FIX = "List the held items to see what is held now."
_LIST_AGAIN = (
    "List the held items again and read the item as it stands now. If the store's page changed "
    f"after the edit was held, {KEEP_STORE} takes it and saves the person's file first. "
    "Then resolve with the new item_version."
)
#: Why an item cannot be resolved here, as a sentence an agent repeats: the list and the refusal
#: of a resolve both say it. A held binary waits for a later release; a file above the projector's
#: body limit cannot be read whole to save or adopt.
BINARY_BLOCK = "Binaries cannot be resolved here yet and stay held until a later release adds that."
LARGE_BLOCK = (
    f"A file above {projector.HELD_BODY_LIMIT // 2**20} MiB cannot be saved or adopted here, "
    "so it stays held."
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
    #: The sentence that says why no choice resolves this item here, or None when one does.
    blocked: str | None
    #: Whether keep-mine can still work: the store's page must be as it was when the edit was held.
    keep_mine_possible: bool

    @property
    def resolvable(self) -> bool:
        return self.blocked is None

    @property
    def choices(self) -> tuple[str, ...]:
        """The resolutions that can work for this item now."""
        if not self.resolvable:
            return ()
        return CHOICES if self.keep_mine_possible else (KEEP_STORE,)


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


def person_file_bytes(vault_root: Path, rel: str) -> bytes | None:
    """The person's file at ``rel`` as it is now, or None when it is absent, unreadable or above
    the projector's body limit. It is read through handles that refuse a symlink."""
    return _observe(Path(vault_root), [rel], keep_body=True)[rel].body


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
    why = _checked_why(why)
    if not mode.store_mode_active(root):
        raise OpError(
            read.NOT_ACTIVE,
            "this vault is not in store mode, so it holds no held items",
            "Resolve held items only in a vault that `exomem store-mode` has migrated.",
        )
    if ownership.classify(rel) is not ownership.Ownership.PAGE:
        raise OpError(
            write.NOT_OWNED,
            f"{rel}: a held binary cannot be resolved here yet",
            f"Leave it held. {BINARY_BLOCK}",
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
    item = _item(row, disk.file, store)
    if item.blocked is not None:
        raise OpError(
            TOO_LARGE,
            f"{rel}: its file is too large to resolve here",
            f"Leave it held. {item.blocked}",
        )
    if item.item_version != seen:
        raise write.StoreConflict(rel, "this held item changed after it was listed", _LIST_AGAIN)
    # The bytes that this resolution commits as the page, or None when the page goes away.
    if choice == KEEP_MINE:
        written = disk.body if disk.file.exists else None
    else:
        written = None if page is None else page.body
    _require_write(root, rel, written)
    exported: tuple[Path | None, str | None] = (None, None)
    if choice == KEEP_MINE:
        change = _keep_mine(rel, row, disk, why)
    else:
        exported = _export(root, rel, disk.body)
        change = _keep_store(root, rel, row, page, disk, exported, why)
    try:
        result = write.commit(writer, change)
    except write.StoreConflict as conflict:
        if conflict.remediation is None:
            raise write.StoreConflict(conflict.path, conflict.reason, _LIST_AGAIN) from conflict
        raise
    return Resolution(rel, choice, result.revision, bool(disk.file.exists), *exported)


def _require_write(root: Path, rel: str, written: bytes | None) -> None:
    """Refuse a caller whose write authority does not cover what this resolution commits.

    A governed write of a page passes the connector boundary's write check on the bytes it
    publishes, so a resolve passes the same check, before it saves or commits anything. A caller
    who may not read the item never gets here: the adapter answers it with ``nothing_held``.
    """
    from ..governance import connector_boundary

    try:
        connector_boundary.require_write(root, rel, content=written)
    except ValueError as refused:
        if str(refused).partition(":")[0] != WRITE_REFUSED:
            raise
        raise OpError(
            WRITE_REFUSED,
            f"{rel}: target is unavailable",
            "Leave the item held. Resolve it as the vault owner, or from a connection whose "
            "write access covers this page and the bytes that the choice keeps.",
        ) from None


def nothing_held(rel: str) -> write.StoreConflict:
    """The refusal for a path with no held item."""
    return write.StoreConflict(rel, NOTHING_HELD, _NOTHING_HELD_FIX)


def _checked_why(why: str) -> str:
    """The reason of a resolution: one non-empty line, as short as a curation reason."""
    why = why.strip()
    if not why or "\n" in why or "\r" in why:
        raise OpError(
            "INVALID_INPUT",
            "a resolution needs a reason of one line",
            "Say in `why`, on one line, what the person or owner asked for.",
        )
    if len(why) > MAX_WHY_CHARS:
        raise OpError(
            "INVALID_INPUT",
            f"the reason is {len(why)} characters; the limit is {MAX_WHY_CHARS}",
            f"Shorten `why` to {MAX_WHY_CHARS} characters or fewer.",
        )
    return why


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
            raise nothing_held(rel)
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
    if ownership.classify(rel) is not ownership.Ownership.PAGE:
        blocked = BINARY_BLOCK
    elif file.size is not None and file.size > projector.HELD_BODY_LIMIT:
        blocked = LARGE_BLOCK
    else:
        blocked = None
    # Keep-mine commits only against the revision that the edit was held on, or against no page.
    if held_revision is None:
        unchanged = store is None
    else:
        unchanged = store is not None and store.revision == held_revision
    return HeldItem(
        rel,
        kind,
        detected_at,
        held_revision,
        held_sha256,
        file,
        store,
        version,
        blocked,
        blocked is None and unchanged,
    )


def _observe(root: Path, rels: Sequence[str], *, keep_body: bool) -> dict[str, _Disk]:
    """The person's file at each path, read through handles that refuse a symlink."""
    try:
        with held_fs.acquire(projector.anchor(root)).require() as filesystem:
            return {rel: _observe_file(filesystem, rel, keep_body) for rel in rels}
    except held_fs.HeldFsError as error:
        return {rel: _Disk(PersonFile(None, unreadable=error.code)) for rel in rels}


def _observe_file(filesystem: held_fs.HeldFilesystem, rel: str, keep_body: bool) -> _Disk:
    """One file, read as the projector reads it: the hash is streamed, and the bytes come only on
    request and only up to the projector's limit, so a list of large files costs no memory."""
    try:
        found = projector.observe(filesystem, rel, keep_body=keep_body)
    except held_fs.HeldFsError as error:
        return _Disk(PersonFile(None, unreadable=error.code))
    if found is None:
        return _Disk(PersonFile(False))
    return _Disk(PersonFile(True, found.sha256, found.size), found.mtime_ns, found.body)


def _held_row(row: tuple) -> write.HeldRow:
    rel, kind, _, sha256, detected_at = row
    return write.HeldRow(rel, kind, detected_at, sha256)


def _keep_mine(rel: str, row: tuple, disk: _Disk, why: str) -> write.StoreChange:
    """The person's file as it is now becomes the stored page, or the page goes with the file.

    The page must still be at the revision it had when the edit was held, so a newer Exomem change
    is refused and never overwritten. The file already holds the bytes, so nothing is projected.
    """
    held_revision = row[2]
    expect = {
        rel: write.Expect(absent=True)
        if held_revision is None
        else write.Expect(revision=held_revision)
    }
    reason = f"held {KEEP_MINE}: {why}"
    resolves = (_held_row(row),)
    if not disk.file.exists:
        deletes = (write.PageDelete(rel, on_disk=True, absent_ok=True),)
        return write.StoreChange(
            write.Origin.OUTSIDE_EDITOR,
            reason,
            deletes=deletes,
            expect=expect,
            resolves_held=resolves,
        )
    assert disk.body is not None  # resolve_held refuses a file whose bytes were not read
    puts = (write.PagePut(rel, disk.body, on_disk_mtime_ns=disk.mtime_ns),)
    return write.StoreChange(
        write.Origin.OUTSIDE_EDITOR, reason, puts=puts, expect=expect, resolves_held=resolves
    )


def _keep_store(
    root: Path,
    rel: str,
    row: tuple,
    page: read.StoredPage | None,
    disk: _Disk,
    exported: tuple[Path | None, str | None],
    why: str,
) -> write.StoreChange:
    """The store's page, as read now, is queued for projection; no page queues a removal.

    The commit also records the person's file as what the projection manifest saw, so the
    projector replaces that file with the page and does not hold it again. The audit reason names
    the saved copy of the person's bytes.
    """
    reason = f"held {KEEP_STORE}: {why}"
    saved, saved_sha256 = exported
    if saved is not None:
        copy = saved.relative_to(state_paths.vault_state_dir(root)).as_posix()
        reason += f"; the person's file is saved as {copy} (sha256 {saved_sha256})"
    if not disk.file.exists:
        seen = write.FileSeen(rel, None)
    else:
        assert (
            disk.file.sha256 is not None
            and disk.file.size is not None
            and disk.mtime_ns is not None
        )
        seen = write.FileSeen(rel, disk.file.sha256, disk.file.size, disk.mtime_ns)
    resolves = (_held_row(row),)
    if page is None:
        return write.StoreChange(
            write.Origin.GOVERNED,
            reason,
            deletes=(write.PageDelete(rel, absent_ok=True),),
            expect={rel: write.Expect(absent=True)},
            resolves_held=resolves,
            files_seen=(seen,),
        )
    expect = {rel: write.Expect(revision=page.meta.revision, sha256=page.meta.sha256)}
    return write.StoreChange(
        write.Origin.GOVERNED,
        reason,
        puts=(write.PagePut(rel, page.body),),
        expect=expect,
        resolves_held=resolves,
        files_seen=(seen,),
    )


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
