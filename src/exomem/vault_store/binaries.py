"""Record a lifecycle write's file moves in its store commit, then move the files (design D2, D4).

A binary's bytes stay on disk, so the store records the move and the file follows it: the commit
moves each binary's record to its new path and queues a ``move`` intent there, and the writer
renames the file right after the commit (``relocated``). The commit is the only decision, and
nothing that the store owns changes on disk before it:

- A kill before the commit changes nothing that the store owns. A new file that the commit
  records (a trash entry's sidecar, ``Placement``) waits in a temporary that the store does not own
  until the commit moves it into place; a kill before the commit leaves only that temporary.
- A kill after the commit leaves each unfinished binary move queued, and the projector renames the
  file when its old path still holds the recorded bytes and the new path is free. It never unlinks
  a binary. The importer leaves the old path of a queued move alone, and reads a missing file at
  its new path as the queued intent's, so it reads none of this as a person's change, whichever
  runs first after the restart.
- A page's dot folder of parts moves with the page's own queued move (``projector._move_parts``).
  Any other file or folder that the store does not own (a dot file, a sync conflict copy) moves
  right after the commit; a kill before its rename leaves it at the source, with no byte lost.

A commit that refuses leaves every file where it was. A file that the store does not own and that
holds Markdown has its rows derived again at its new path and dropped at its old one, after its
rename, in a second commit. A folder that the renames left empty goes, by the projector's rule.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import ownership, read
from .write import BinaryMove, BinaryRecord, Expect

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

log = logging.getLogger(__name__)

#: The sentence a writer adds for a file that did not move right after the commit.
LEFT = "did not move yet; the projector moves a binary, and a file the store does not own stays"
#: Each placement's temporary: a dot name beside its path, which the store does not own.
_PENDING = ".exomem-pending-"


class Unreadable(Exception):
    """A binary could not be read before the commit, so the write cannot record its move."""

    def __init__(self, path: str) -> None:
        super().__init__(f"{path}: the file could not be read")
        self.path = path


@dataclass(frozen=True, slots=True)
class Entry:
    """One rename: a file, or a folder that holds nothing the store owns (``folder``)."""

    src: str
    dst: str
    folder: bool = False


@dataclass(frozen=True, slots=True)
class Placement:
    """A new file that the commit records at ``dst``, such as a trash entry's sidecar."""

    dst: str
    data: bytes


@dataclass(frozen=True, slots=True)
class Relocation:
    """The renames and placements of one commit, and the records that the caller's snapshot holds
    for the binaries among them."""

    entries: tuple[Entry, ...]
    recorded: Mapping[str, read.BinaryMeta] = field(default_factory=dict)
    placements: tuple[Placement, ...] = ()

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
        for placement in self.placements:
            expect[placement.dst] = Expect(absent=True)
        return expect


def plan(
    view: read.StoreSnapshot, entries: Iterable[Entry], placements: Iterable[Placement] = ()
) -> Relocation:
    """The relocation of ``entries``, with the record of each binary source read in ``view``.

    A binary that the store records nothing for yet (bytes that nothing imported) moves as a
    recorded one does: the commit records it at its new path.
    """
    entries = tuple(entries)
    sources = [entry.src for entry in entries if not entry.folder and _binary(entry.src)]
    return Relocation(entries, view.binary_records(sources), tuple(placements))


@dataclass(slots=True)
class Moved:
    """What the commit records, and after it, what did not move."""

    binary_moves: tuple[BinaryMove, ...]
    #: The entries left at their source after the commit, which the projector or a person moves.
    left: list[str] = field(default_factory=list)

    @property
    def records(self) -> tuple[BinaryRecord, ...]:
        """The record that the commit makes at each new path: the bytes read before the commit."""
        return tuple(BinaryRecord(move.dst, move.sha256, move.size) for move in self.binary_moves)


@contextmanager
def relocated(
    vault_root: Path, relocation: Relocation, writer: CollectionWriter
) -> Iterator[Moved]:
    """Read what moves, let the body commit it, then move the files.

    Before the body: each binary's bytes are read where they are, a placement's bytes go to a
    durable temporary beside its path, and a rename between two filesystems refuses with
    ``CROSS_DEVICE``. The body commits ``Moved.binary_moves`` with the rest of its change. After a
    commit, every entry and placement moves; what cannot is named in ``Moved.left`` and stays for
    the projector, or at its source when the store does not own it. When the body raises before its
    commit, the temporaries go and nothing else has changed. ``writer`` drops the intent of each
    binary move that the writer finished and derives the rows of moved Markdown that the store does
    not own.
    """
    from .. import reserved_paths

    root = Path(vault_root)
    for entry in relocation.entries:
        _require_one_filesystem(root, entry)
    read_at: dict[str, tuple[int, int]] = {}
    moves = [
        _planned(root, entry, read_at) for entry in relocation.entries if _moves_a_binary(entry)
    ]
    staged: list[tuple[str, str]] = []
    try:
        for placement in relocation.placements:
            folder = placement.dst.rpartition("/")[0]
            if (root / placement.dst).exists():
                from ..vault import CreateOnlyConflict

                raise CreateOnlyConflict(placement.dst)
            # The name keeps no part of the leaf, so no listing of sidecars or binaries reads it.
            temporary = f"{folder}/{_PENDING}{secrets.token_hex(16)}"
            (root / folder).mkdir(parents=True, exist_ok=True)
            reserved_paths.publish_generic_bytes(
                root, temporary, placement.data, expected_identity=None
            )
            staged.append((temporary, placement.dst))
            moves.append(
                BinaryMove(
                    temporary,
                    placement.dst,
                    hashlib.sha256(placement.data).hexdigest(),
                    len(placement.data),
                )
            )
        moved = Moved(tuple(moves))
        yield moved
    except BaseException:
        landed = _landed(root, moves, relocation.recorded)
        if landed:
            # The commit is durable and something after it failed: its files still move.
            _finish(root, relocation, staged, moves, read_at, writer)
        elif landed is False:
            for temporary, _dst in staged:
                _discard(root, temporary)
        raise
    moved.left.extend(_finish(root, relocation, staged, moves, read_at, writer))


def _finish(
    root: Path,
    relocation: Relocation,
    staged: list[tuple[str, str]],
    moves: list[BinaryMove],
    read_at: Mapping[str, tuple[int, int]],
    writer: CollectionWriter,
) -> list[str]:
    """Move every entry and placement after the commit; return the sources that stayed.

    The commit is durable, so nothing here refuses the write: a rename that fails, the drop of a
    finished move's intent and the rows of moved Markdown are left to the projector and the
    importer, which finish them.
    """
    from . import projector, write

    planned = {move.src: move for move in moves}
    finished: list[BinaryMove] = []
    refresh: list[str] = []
    left: list[str] = []
    for temporary, dst in staged:
        if _rename(root, Entry(temporary, dst), planned[temporary], None):
            finished.append(planned[temporary])
        else:
            left.append(dst)
    for entry in relocation.entries:
        move = planned.get(entry.src) if _moves_a_binary(entry) else None
        names = _markdown_below(root, entry.src) if entry.folder else ()
        if not _rename(root, entry, move, read_at.get(entry.src)):
            left.append(entry.src)
            continue
        if move is not None:
            finished.append(move)
        elif entry.folder:
            refresh.extend(
                path for name in names for path in (f"{entry.src}/{name}", f"{entry.dst}/{name}")
            )
        elif ownership.is_markdown(entry.dst):
            refresh.extend((entry.src, entry.dst))
    _prune_emptied(root, {entry.src.rpartition("/")[0] for entry in relocation.entries})
    try:
        if finished:
            with writer.handle.transaction() as tx:
                for move in finished:
                    projector.drop_binary_move(tx, move.dst, move.src)
        if refresh:
            reason = "moved files that the store does not own"
            write.commit(
                writer, write.StoreChange(write.Origin.GOVERNED, reason, refresh=tuple(refresh))
            )
    except Exception:  # noqa: BLE001 - durable already; the projector and the importer finish it
        log.warning("a lifecycle commit's bookkeeping is left to the next pass", exc_info=True)
    return left


def _rename(
    root: Path, entry: Entry, move: BinaryMove | None, seen: tuple[int, int] | None
) -> bool:
    """Rename one entry while its source is what the commit planned and its new path is free.

    A binary's source must still show the size and modification time that its planned read saw;
    otherwise the projector, which hashes it, decides. True when the file sits at its new path,
    the projector having moved it first included.
    """
    from .. import reserved_paths
    from ..cli_ops import OpError

    if move is not None and seen is not None and _stat(root, entry.src) != seen:
        return _sha256(root, entry.dst) == move.sha256
    try:
        reserved_paths.move_generic_path(
            root,
            entry.src,
            entry.dst,
            source_kind="directory" if entry.folder else "file",
            physical=True,
        )
    except (OpError, reserved_paths.ReservedPathLeafError, OSError):
        if move is not None:
            return _sha256(root, entry.dst) == move.sha256
        return not (root / entry.src).exists() and (root / entry.dst).exists()
    return True


def _planned(root: Path, entry: Entry, read_at: dict[str, tuple[int, int]]) -> BinaryMove:
    """The move of a binary file, with the bytes that its source holds now."""
    from .. import reserved_paths

    before = _stat(root, entry.src)
    try:
        raw = reserved_paths.read_generic_bytes(root, entry.src, physical=True).data
    except reserved_paths.ReservedPathLeafError as error:
        raise Unreadable(entry.src) from error
    if before is not None and _stat(root, entry.src) == before:
        read_at[entry.src] = before
    return BinaryMove(entry.src, entry.dst, hashlib.sha256(raw).hexdigest(), len(raw))


def _stat(root: Path, rel: str) -> tuple[int, int] | None:
    try:
        found = os.stat(root / rel, follow_symlinks=False)
    except OSError:
        return None
    return found.st_size, found.st_mtime_ns


def _landed(
    root: Path, moves: list[BinaryMove], recorded: Mapping[str, read.BinaryMeta]
) -> bool | None:
    """Whether the commit that names ``moves`` is durable: the store records each new path with
    its bytes and none of the recorded old paths. One snapshot read of those paths.

    None when the store cannot say; the caller then leaves every file as it is.
    """
    if not moves:
        return False
    try:
        with read.snapshot(root) as view:
            found = view.binary_records([*(move.dst for move in moves), *recorded])
    except Exception:  # noqa: BLE001 - an unreadable store decides nothing either way
        return None
    return not any(rel in found for rel in recorded) and all(
        move.dst in found and found[move.dst].sha256 == move.sha256 for move in moves
    )


def _require_one_filesystem(root: Path, entry: Entry) -> None:
    """Refuse a rename between two filesystems before the commit, as the rename itself would."""
    from .. import reserved_paths

    target = root / entry.dst
    while not target.exists() and target != root:
        target = target.parent
    try:
        if os.stat(root / entry.src).st_dev != os.stat(target).st_dev:
            raise reserved_paths.ReservedPathLeafError("CROSS_DEVICE")
    except FileNotFoundError:
        return  # the planned read of the source names a missing file


def _sha256(root: Path, rel: str) -> str | None:
    from .. import reserved_paths

    try:
        raw = reserved_paths.read_generic_bytes(root, rel, physical=True).data
    except (reserved_paths.ReservedPathLeafError, OSError):
        return None
    return hashlib.sha256(raw).hexdigest()


def _discard(root: Path, rel: str) -> None:
    from .. import reserved_paths

    try:
        reserved_paths.unlink_generic_file(root, rel)
    except (reserved_paths.ReservedPathLeafError, OSError):
        return  # a temporary that stays is a dot file that the store does not own


def _moves_a_binary(entry: Entry) -> bool:
    return not entry.folder and _binary(entry.src)


def refusal(error: BaseException, failed_code: str, failed: str) -> tuple[str, str]:
    """The code and reason that a writer raises when its relocation did not commit.

    ``failed_code`` and ``failed`` are the writer's own. A rename across filesystems has its own
    code, as file mode names it.
    """
    from .. import reserved_paths

    if isinstance(error, reserved_paths.ReservedPathLeafError) and error.code == "CROSS_DEVICE":
        return "CROSS_DEVICE_MOVE", "the destination is on another filesystem than the source"
    return failed_code, failed


def _binary(rel: str) -> bool:
    return ownership.classify(rel) is ownership.Ownership.BINARY


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


def _prune_emptied(root: Path, folders: Iterable[str]) -> None:
    """Remove each folder that the renames left empty, by the projector's own rule."""
    from .. import held_fs
    from . import projector

    acquired = held_fs.acquire(projector.anchor(root))
    if not acquired.ok:
        return  # an emptied folder then stays, as file mode leaves one; no bytes are lost
    with acquired.require() as filesystem:
        for folder in sorted(folders, reverse=True):
            projector.prune_emptied(filesystem, folder)


def prune_empty(folder: Path) -> None:
    """Remove the folders below and at ``folder`` that the renames emptied, the deepest first, as a
    rename of the whole folder would leave none. A folder that still holds a page's file, which the
    projector moves later, stays until then, and the projector removes it once it is empty."""
    for current, _dirs, _files in os.walk(folder, topdown=False):
        try:
            os.rmdir(current)
        except OSError:
            continue
