"""A private stage for an upload's bytes, filled before the mutation boundary.

An upload commit used to copy, hash and fsync its bytes while it held the boundary, so a
large file kept every note save waiting. Each upload door now copies the bytes once, here,
and the commit takes only the stage's name: it renames the stage into its batch workspace
and then into place, and reads nothing.

A stage lives in one reserved directory on the vault's own filesystem, because a rename
install needs it there. It is never canonical. A stage is a `.part` file beside a `.lock`
file that its owner holds for as long as the stage exists. The kernel drops that lock when
its process dies, so a lock that can be taken proves the owner is gone; age proves nothing.
The sweep at serving activation removes a stage on that proof alone and leaves in place
what it cannot prove.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from filelock import FileLock, Timeout

from . import reserved_paths
from .kbdir import managed_root

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_PART_SUFFIX = ".part"
_LOCK_SUFFIX = ".lock"
#: The file-name format this module itself writes (a random id and one of two fixed suffixes), so a regex
#: over a closed format and not a guess at what a name means. Anything else in the directory is not ours.
_STAGE_NAME = re.compile(r"(?P<stem>[0-9a-f]{32})(?P<kind>\.part|\.lock)", re.ASCII)
#: How many entries one sweep reads. A directory past it is reported as truncated and the next start continues.
SWEEP_ENTRY_LIMIT = 4096
_LOCK_WAIT_SECONDS = 10.0


class StageChanged(OSError):
    """The file at a stage's path is no longer the file that was staged."""


@dataclass(eq=False)
class StagedUpload:
    """The bytes of one upload, hashed and fsynced in a private stage that the caller owns."""

    vault_root: Path
    path: Path
    size: int
    sha256: str
    device: int
    inode: int
    #: (size, mtime_ns, ctime_ns) read after the fsync; the commit rebinds it once it has renamed the file.
    generation: tuple[int, int, int]
    #: The shared-pool stage of the same bytes (`original_pool.PreparedOriginal`), when the filing pools them.
    pool: object | None = None

    def open(self) -> BinaryIO:
        """A new read handle on the staged file, refused when that file is not the one staged."""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (self.device, self.inode):
                raise StageChanged(f"staged upload {self.path.name} is not the file that was staged")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise


@dataclass(frozen=True)
class SweepReport:
    """What one sweep did, so the start-up log can say it.

    `live` counts stages whose owner still holds the lock. `unproven` counts stages the sweep could not
    prove orphaned, such as a `.part` with no lock file, and it left their bytes where they are.
    """

    removed: int = 0
    live: int = 0
    unproven: int = 0
    truncated: bool = False


def stage_root(vault_root: Path) -> Path:
    return managed_root(Path(vault_root)) / reserved_paths.UPLOAD_STAGE_DIRNAME


def _ensure_root(vault_root: Path) -> Path:
    root = stage_root(vault_root)
    # A fresh vault has no managed root yet; the batch that follows would create it, so the stage may too.
    os.makedirs(root, mode=0o700, exist_ok=True)
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"{root.name} is not a directory")
    return root


def _remove(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _copy(
    vault_root: Path, part: Path, stream: BinaryIO, *, limit: int, too_large: Callable[[], BaseException]
) -> StagedUpload:
    """Copy `stream` into `part` once, hashing as it goes, and fsync it."""
    try:
        stream.seek(0)
    except (OSError, AttributeError, ValueError):
        pass  # a stream that cannot seek is copied from where it stands
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(part, flags, 0o600)
    try:
        digest = hashlib.sha256()
        written = 0
        while chunk := stream.read(_CHUNK):
            written += len(chunk)
            if written > limit:
                raise too_large()
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
        info = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        _remove(part)
        raise
    os.close(descriptor)
    return StagedUpload(
        vault_root=Path(vault_root), path=part, size=written, sha256=digest.hexdigest(),
        device=info.st_dev, inode=info.st_ino,
        generation=(info.st_size, info.st_mtime_ns, info.st_ctime_ns),
    )


@contextlib.contextmanager
def stage(
    vault_root: Path,
    stream: BinaryIO,
    *,
    limit: int,
    too_large: Callable[[], BaseException],
    pooled: bool = False,
) -> Iterator[StagedUpload]:
    """Copy `stream` into a private stage and yield it; whatever is left of the stage goes when the block ends.

    `too_large` builds the refusal that the calling door already speaks, raised when the bytes pass `limit`.
    With `pooled` the same bytes are also staged into the shared original pool (`original_pool.prepare`),
    which holds its media pin until the block ends: the filing that follows publishes by rename.
    """
    from . import held_fs, original_pool

    root = _ensure_root(vault_root)
    stem = secrets.token_hex(16)
    lock_path = root / f"{stem}{_LOCK_SUFFIX}"
    part = root / f"{stem}{_PART_SUFFIX}"
    # The lock is taken before the `.part` exists, so a `.part` with a free lock is always an orphan. A sweep
    # that probes this lock holds it for an instant, so the owner waits that instant out.
    lock = FileLock(lock_path, timeout=_LOCK_WAIT_SECONDS, thread_local=False)
    try:
        lock.acquire()
        staged = _copy(vault_root, part, stream, limit=limit, too_large=too_large)
        with contextlib.ExitStack() as held:
            if pooled:
                def chunks() -> Iterator[bytes]:
                    with part.open("rb") as source:
                        while chunk := source.read(_CHUNK):
                            yield chunk

                staged.pool = held.enter_context(
                    original_pool.prepare(vault_root, staged.sha256, staged.size, held_fs.Chunks(chunks))
                )
            yield staged
    finally:
        try:
            _remove(part)
        finally:
            lock.release()
            _remove(lock_path)


def sweep(vault_root: Path) -> SweepReport:
    """Remove each stage whose owner is proven gone; leave every other entry where it is.

    Run once at serving activation. A stage is removed only when its lock can be taken, which means the
    process that held it is dead. A held lock is a live stage and stays; a `.part` whose lock file is missing
    cannot be proven either way and stays too, reported as `unproven`.
    """
    root = stage_root(vault_root)
    try:
        scanner = os.scandir(root)
    except FileNotFoundError:
        return SweepReport()
    parts: set[str] = set()
    locks: set[str] = set()
    truncated = False
    with scanner:
        for index, entry in enumerate(scanner):
            if index >= SWEEP_ENTRY_LIMIT:
                truncated = True
                break
            named = _STAGE_NAME.fullmatch(entry.name)
            if named is not None:
                (parts if named["kind"] == _PART_SUFFIX else locks).add(named["stem"])
    removed = live = unproven = 0
    for stem in sorted(parts | locks):
        if stem in parts and stem not in locks:
            unproven += 1
            continue
        lock_path = root / f"{stem}{_LOCK_SUFFIX}"
        probe = FileLock(lock_path, timeout=0, thread_local=False)
        try:
            probe.acquire()
        except Timeout:
            live += 1
            continue
        except OSError:
            unproven += 1
            continue
        try:
            _remove(root / f"{stem}{_PART_SUFFIX}")
        finally:
            probe.release()
        _remove(lock_path)
        removed += 1
    report = SweepReport(removed=removed, live=live, unproven=unproven, truncated=truncated)
    if removed or unproven or truncated:
        log.info(
            "upload stage sweep removed=%d live=%d unproven=%d truncated=%s",
            removed, live, unproven, truncated,
        )
    return report
