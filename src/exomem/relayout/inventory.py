"""What the planner reads of a vault: its files, and the bytes of its pages.

A file-mode vault is read from its files. A store-mode vault is read from the store's
snapshot for every file that the store owns, and from the disk for the files that it does
not own (collection layers, reserved families, hidden part folders) and for everything
outside the managed root. Both modes list the same files for the same vault, so a plan does
not depend on the mode. Nothing here writes.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .. import extraction_parts, kbdir
from ..find_corpus import EntryKind, entry_kind, is_sync_conflict_copy
from ..vault_store import mode, ownership, read

SKIPPED_CONFLICT_COPY = "conflict_copy"
SKIPPED_UNREADABLE = "unreadable"

#: A read batch holds at most this many pages and about this many bytes (a larger page is a batch
#: of its own), so a plan keeps a bounded few bodies in memory while it reads the store in few queries.
BATCH_PAGES = 200
BATCH_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ManagedFile:
    """A regular file below the managed root. `owner` says who serves its bytes in store mode."""

    path: str
    markdown: bool
    owner: ownership.Ownership

    @property
    def owned(self) -> bool:
        return self.owner is not ownership.Ownership.NONE


@dataclass(slots=True)
class OutsideEntry:
    """A top-level entry of the vault root that sits outside the managed root, with what it holds.

    `markdown` lists the paths of its Markdown files that Obsidian shows, and `attachments`
    those of its other files, which a link names by their full file name. A hidden file or
    folder below it is not counted.
    """

    name: str
    is_directory: bool
    files: int = 0
    markdown: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Inventory:
    """Every file the planner reads, and the way to read a page's bytes.

    `skipped` counts the entries that a plan leaves out, by `<where>:<reason>`: a conflict
    copy, or a link or special file that no Exomem walk keeps. `hidden` lists the dot-named
    entries below the managed root that the plan does not read (it never enters them), except
    a part folder, whose files are listed in `managed`. `revision` is the store revision that
    every read sees, or None in file mode.
    """

    root: Path
    managed: list[ManagedFile]
    outside: dict[str, OutsideEntry]
    skipped: Counter[str]
    hidden: list[str]
    revision: int | None = None
    _view: read.StoreSnapshot | None = None
    #: The size in bytes of each page that the store holds, by path.
    _stored: Mapping[str, int] = field(default_factory=dict)

    def read(self, rel: str) -> bytes | None:
        """The bytes of the file at `rel`, from the store when it holds the page, else from the disk.

        None when the file cannot be read, which the caller counts.
        """
        if rel in self._stored and self._view is not None:
            page = self._view.page(rel)
            return None if page is None else page.body
        return self._read_disk(rel)

    def _read_disk(self, rel: str) -> bytes | None:
        try:
            return (self.root / rel).read_bytes()
        except OSError:
            return None

    def read_all(self, rels: Iterable[str]) -> Iterator[tuple[str, bytes | None]]:
        """Each file of `rels` with its bytes, in order, read a batch at a time.

        A batch of stored pages costs a single store query where `read` costs a query per page.
        Only the batch being yielded is in memory; the caller consumes each body and drops it.
        """
        batch: list[str] = []
        held = 0
        for rel in rels:
            size = self._stored.get(rel, 0)
            if batch and (len(batch) >= BATCH_PAGES or held + size > BATCH_BYTES):
                yield from self._read_batch(batch)
                batch, held = [], 0
            batch.append(rel)
            held += size
        if batch:
            yield from self._read_batch(batch)

    def _read_batch(self, batch: list[str]) -> Iterator[tuple[str, bytes | None]]:
        view = self._view
        stored = [rel for rel in batch if rel in self._stored] if view is not None else []
        bodies = view.bodies(stored) if view is not None and stored else {}
        for rel in batch:
            if view is not None and rel in self._stored:
                yield rel, bodies.get(rel)
            else:
                yield rel, self._read_disk(rel)


def _scan(directory: Path) -> list[os.DirEntry[str]] | None:
    try:
        with os.scandir(directory) as scanned:
            return sorted(scanned, key=lambda entry: entry.name)
    except OSError:
        return None


# The reason an entry kind is left out of a walk; a regular file never reaches this map.
_KIND_REASON = {
    EntryKind.SYMLINK: "symlink",
    EntryKind.JUNCTION: "junction",
    EntryKind.HARD_LINKED: "hard_linked",
    EntryKind.OTHER: "not_regular",
}


def _is_part_folder(name: str) -> bool:
    """Whether a hidden folder name is an extraction part folder, whose pages move with their source page."""
    return name.endswith(extraction_parts.FOLDER_SUFFIX)


def _walk_managed(
    directory: Path,
    rel_dir: str,
    skipped: Counter[str],
    hidden: list[str],
    *,
    in_part_folder: bool = False,
) -> Iterator[ManagedFile]:
    """The regular files below the managed root, owned or not.

    A dot-named entry is not entered, and is named in `hidden`, except a part folder: its
    files are listed, because they belong to a page that the plan moves.
    """
    entries = _scan(directory)
    if entries is None:
        skipped[f"managed:{SKIPPED_UNREADABLE}"] += 1
        return
    for entry in entries:
        rel = f"{rel_dir}/{entry.name}"
        try:
            info = os.lstat(entry.path)
        except OSError:
            skipped[f"managed:{SKIPPED_UNREADABLE}"] += 1
            continue
        kind = entry_kind(entry.path, info)
        if entry.name.startswith(".") and not in_part_folder:
            if kind is EntryKind.DIRECTORY and _is_part_folder(entry.name):
                yield from _walk_managed(
                    Path(entry.path), rel, skipped, hidden, in_part_folder=True
                )
            else:
                hidden.append(rel)
            continue
        if kind is EntryKind.DIRECTORY:
            yield from _walk_managed(
                Path(entry.path), rel, skipped, hidden, in_part_folder=in_part_folder
            )
        elif is_sync_conflict_copy(entry.name):
            skipped[f"managed:{SKIPPED_CONFLICT_COPY}"] += 1
        elif kind is EntryKind.REGULAR:
            yield ManagedFile(rel, ownership.is_markdown(rel), ownership.classify(rel))
        else:
            skipped[f"managed:{_KIND_REASON[kind]}"] += 1


def _walk_outside(
    directory: Path, rel_dir: str, entry: OutsideEntry, skipped: Counter[str]
) -> None:
    """Count the files below an outside folder and list its Markdown and other files, never following a link."""
    entries = _scan(directory)
    if entries is None:
        skipped[f"outside:{SKIPPED_UNREADABLE}"] += 1
        return
    for child in entries:
        if child.name.startswith("."):
            continue
        rel = f"{rel_dir}/{child.name}"
        try:
            kind = entry_kind(child.path, os.lstat(child.path))
        except OSError:
            skipped[f"outside:{SKIPPED_UNREADABLE}"] += 1
            continue
        if kind is EntryKind.DIRECTORY:
            _walk_outside(Path(child.path), rel, entry, skipped)
        elif kind in (EntryKind.REGULAR, EntryKind.HARD_LINKED):
            entry.files += 1
            if not is_sync_conflict_copy(child.name):
                listed = entry.markdown if ownership.is_markdown(rel) else entry.attachments
                listed.append(rel)
        else:
            skipped[f"outside:{_KIND_REASON[kind]}"] += 1


def _list_vault(root: Path, view: read.StoreSnapshot | None) -> Inventory:
    skipped: Counter[str] = Counter()
    hidden: list[str] = []
    managed: dict[str, ManagedFile] = {}
    outside: dict[str, OutsideEntry] = {}
    top = _scan(root)
    if top is None:
        raise OSError(f"the vault root {root} cannot be listed")
    for entry in top:
        if entry.name.startswith("."):
            continue
        if kbdir.is_managed_root_name(entry.name):
            for item in _walk_managed(Path(entry.path), entry.name, skipped, hidden):
                # A store serves the files it owns, so the disk's copy of them is not the listing.
                if view is None or not item.owned:
                    managed[item.path] = item
            continue
        try:
            kind = entry_kind(entry.path, os.lstat(entry.path))
        except OSError:
            skipped[f"outside:{SKIPPED_UNREADABLE}"] += 1
            continue
        record = OutsideEntry(entry.name, kind is EntryKind.DIRECTORY)
        if kind is EntryKind.DIRECTORY:
            _walk_outside(Path(entry.path), entry.name, record, skipped)
        elif kind in (EntryKind.REGULAR, EntryKind.HARD_LINKED):
            record.files = 1
            if not is_sync_conflict_copy(entry.name):
                listed = (
                    record.markdown if ownership.is_markdown(entry.name) else record.attachments
                )
                listed.append(entry.name)
        else:
            skipped[f"outside:{_KIND_REASON[kind]}"] += 1
            continue
        outside[entry.name] = record
    stored: dict[str, int] = {}
    if view is not None:
        prefix = kbdir.managed_join("")
        pages = {meta.path: meta.size for meta in view.pages(prefix)}
        for rel in sorted(pages):
            managed[rel] = ManagedFile(rel, ownership.is_markdown(rel), ownership.classify(rel))
        for binary in view.binaries(prefix):
            managed[binary.path] = ManagedFile(
                binary.path, ownership.is_markdown(binary.path), ownership.classify(binary.path)
            )
        stored = pages
    return Inventory(
        root,
        [managed[path] for path in sorted(managed)],
        {name: outside[name] for name in sorted(outside)},
        skipped,
        sorted(hidden),
        view.revision if view is not None else None,
        view,
        stored,
    )


@contextmanager
def open_inventory(vault_root: Path) -> Iterator[Inventory]:
    """List the vault and keep a single consistent read view until the caller is done.

    In store mode the view is a single store snapshot, so every page read belongs to the
    revision that the listing saw.
    """
    root = Path(vault_root)
    if mode.store_mode_for_reads(root):
        with read.snapshot(root) as view:
            yield _list_vault(root, view)
    else:
        yield _list_vault(root, None)
