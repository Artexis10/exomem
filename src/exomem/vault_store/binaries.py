"""Move files on disk, then record the move in one store commit (store task 2.5, design D1).

A binary's bytes stay on disk, so the store has no binary move: a writer renames each file first,
then commits in one transaction a record at each new path and the removal of each old record. The
commit binds the old record as the writer read it (revision and hash) and the new path as empty.
It is the change that the importer commits for a rename it finds (``importer._commit_rename``),
made by a governed writer.

The window between the renames and the commit, for every writer that uses this module:

- A kill after a rename and before the commit leaves a record at the old path whose file is now
  at the new one. The next sweep judges the old path gone and the new file unrecorded, pairs the
  two by their bytes (``importer._pair_renames``) and commits the same change as an outside
  rename. No byte is lost, and no record stays dangling past one sweep. The writer's retry finds
  the file at its new path. A folder's renames that ran before the kill resolve the same way, one
  file each, and the pages that the commit would have moved have not moved.
- A commit that refuses renames each file back (``relocated``). When a rename back fails too, the
  file stays at its new path and ``Stranded`` says so; the next sweep records it there.
- A kill after the commit leaves nothing to do: the commit is the last step.

A file that the store does not own (a dot folder's part, a sync conflict copy) and a folder that
holds nothing the store owns move the same way with no record. A Markdown file among them has its
rows derived again at its new path and dropped at its old one (``Moved.refresh``).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import ownership, read
from .write import BinaryDelete, BinaryRecord, Expect

#: The sentence a writer adds when a refused commit could not put a moved file back.
STRANDED = "the file moved, and the next sweep records it"


class Stranded(Exception):
    """A commit refused and a file it had moved could not move back: it stays at its new path."""

    def __init__(self, path: str) -> None:
        super().__init__(f"{path}: {STRANDED}")
        self.path = path


@dataclass(frozen=True, slots=True)
class Entry:
    """One rename: a file, or a folder that holds nothing the store owns (``folder``)."""

    src: str
    dst: str
    folder: bool = False


@dataclass(frozen=True, slots=True)
class Relocation:
    """The renames of one commit and the binary records that the caller's snapshot holds for them."""

    entries: tuple[Entry, ...]
    recorded: Mapping[str, read.BinaryMeta] = field(default_factory=dict)

    @property
    def binary_deletes(self) -> tuple[BinaryDelete, ...]:
        """The removal of each old record: its file has moved, so nothing is left to unlink."""
        return tuple(BinaryDelete(rel, on_disk=True) for rel in self.recorded)

    def expect(self, linked_from: Mapping[str, frozenset[str]] | None = None) -> dict[str, Expect]:
        """Each old record as it was read, and each new binary path empty.

        ``linked_from`` binds the linking pages of a source, as a page's delete binds them.
        """
        bound = linked_from or {}
        expect = {
            rel: Expect(revision=meta.revision, sha256=meta.sha256, linked_from=bound.get(rel))
            for rel, meta in self.recorded.items()
        }
        for entry in self.entries:
            if not entry.folder and _binary(entry.dst):
                expect[entry.dst] = Expect(absent=True)
        return expect


def plan(view: read.StoreSnapshot, entries: Iterable[Entry]) -> Relocation:
    """The relocation of ``entries``, with the record of each binary source read in ``view``.

    A binary that the store records nothing for yet (bytes that nothing imported) gets a record at
    its new path and binds nothing at its old one, as file mode binds nothing there.
    """
    entries = tuple(entries)
    sources = [entry.src for entry in entries if not entry.folder and _binary(entry.src)]
    return Relocation(entries, view.binary_records(sources))


@dataclass(frozen=True, slots=True)
class Moved:
    """What the renames put on disk, for the commit to record and derive."""

    records: tuple[BinaryRecord, ...]
    refresh: tuple[str, ...]


@contextmanager
def relocated(vault_root: Path, relocation: Relocation) -> Iterator[Moved]:
    """Rename every entry, and rename them back when the body raises.

    The caller commits inside the body. A rename that fails undoes the ones before it and raises
    its ``ReservedPathLeafError`` (``CROSS_DEVICE`` for a move between filesystems). A rename back
    that fails raises ``Stranded`` from the body's error.
    """
    from .. import reserved_paths

    root = Path(vault_root)
    done: list[Entry] = []
    refresh: list[str] = []
    try:
        for entry in relocation.entries:
            # A folder's Markdown files are named before the rename, so both paths can refresh.
            names = _markdown_below(root, entry.src) if entry.folder else ()
            reserved_paths.move_generic_path(
                root,
                entry.src,
                entry.dst,
                source_kind="directory" if entry.folder else "file",
                physical=True,
            )
            done.append(entry)
            if entry.folder:
                refresh.extend(
                    path for name in names for path in (f"{entry.src}/{name}", f"{entry.dst}/{name}")
                )
            elif ownership.classify(entry.dst) is ownership.Ownership.NONE and (
                ownership.is_markdown(entry.dst)
            ):
                refresh.extend((entry.src, entry.dst))
        records = tuple(
            _record(root, entry.dst)
            for entry in relocation.entries
            if not entry.folder and _binary(entry.dst)
        )
        yield Moved(records, tuple(refresh))
    except BaseException:
        _undo(root, done)
        raise


def _undo(root: Path, done: list[Entry]) -> None:
    """Rename each moved entry back, the last first; raise ``Stranded`` for the first that stays."""
    from .. import reserved_paths
    from ..cli_ops import OpError

    stranded: list[tuple[Entry, Exception]] = []
    for entry in reversed(done):
        try:
            reserved_paths.move_generic_path(
                root,
                entry.dst,
                entry.src,
                source_kind="directory" if entry.folder else "file",
                physical=True,
                rollback=True,
            )
        except (OpError, reserved_paths.ReservedPathLeafError) as failed:
            stranded.append((entry, failed))
    if stranded:
        entry, failed = stranded[-1]
        raise Stranded(entry.dst) from failed


def _binary(rel: str) -> bool:
    return ownership.classify(rel) is ownership.Ownership.BINARY


def _record(root: Path, rel: str) -> BinaryRecord:
    """The record of the file that now sits at ``rel``: the bytes that moved, read once, as file
    mode's trash manifest reads them."""
    from .. import reserved_paths

    raw = reserved_paths.read_generic_bytes(root, rel).data
    return BinaryRecord(rel, hashlib.sha256(raw).hexdigest(), len(raw))


def _markdown_below(root: Path, folder: str) -> tuple[str, ...]:
    """The Markdown files below a folder, relative to it: one walk of a folder the store does not
    own (a dot folder's parts)."""
    base = root / folder
    found: list[str] = []
    for current, _dirs, files in os.walk(base):
        for name in files:
            if ownership.is_markdown(name):
                found.append((Path(current) / name).relative_to(base).as_posix())
    return tuple(sorted(found))


def prune_empty(folder: Path) -> None:
    """Remove the folders below and at ``folder`` that the renames emptied, the deepest first, as a
    rename of the whole folder would leave none. A folder that still holds a page's file, which the
    projector moves later, stays until then."""
    for current, _dirs, _files in os.walk(folder, topdown=False):
        try:
            os.rmdir(current)
        except OSError:
            continue
