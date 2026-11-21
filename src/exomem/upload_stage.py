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
import errno
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
#: Where the sweep sets a file aside before it deletes it; one per stem, since one sweep holds a stem's lock.
_CLAIM_SUFFIX = ".claim"
#: The file-name format this module itself writes (a random id and one of three fixed suffixes), so a regex
#: over a closed format and not a guess at what a name means. Anything else in the directory is not ours.
_STAGE_NAME = re.compile(r"(?P<stem>[0-9a-f]{32})(?P<kind>\.part|\.lock|\.claim)", re.ASCII)
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


#: Whether this platform can pin the stage directory by descriptor. Where it cannot (Windows), the sweep
#: keeps to paths and the root check below.
_PINNABLE = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and {os.open, os.stat, os.unlink, os.rename, os.link} <= os.supports_dir_fd
    and os.link in os.supports_follow_symlinks
    and os.scandir in os.supports_fd
)
_FileId = tuple[int, int]


@dataclass(frozen=True)
class _StageDirectory:
    """The stage directory as the sweep reaches it: one descriptor held for the whole sweep where the platform
    has one, otherwise its path. A name resolved through the descriptor cannot be redirected by renaming the
    directory or planting a link at its old name, which resolving the path again for each entry could not rule out.
    """

    path: Path
    fd: int | None

    def name(self, leaf: str) -> str:
        """The argument for a `dir_fd=` call: the bare name against a descriptor, the full path without one."""
        return leaf if self.fd is not None else os.fspath(self.path / leaf)

    def scan(self) -> contextlib.AbstractContextManager[Iterator[os.DirEntry[str]]]:
        return os.scandir(self.fd if self.fd is not None else self.path)

    def discard(self, leaf: str, expected: _FileId | None, *, empty_only: bool = False) -> bool:
        """Delete `leaf` once it is shown to be the regular file the sweep means to delete. True: it is gone.
        False: it stays, because it is something else, or the sweep could not tell.

        A name can be pointed at another file between a check and the unlink that follows it. Descriptor-relative
        calls cannot close that gap, so the sweep never unlinks a name it checked. It renames the entry to the
        stem's claim name, which nothing else uses while the sweep holds the stem's lock, judges the entry
        there and unlinks the claim. An entry that is not the expected one is linked back to its own name,
        which fails rather than replace a newer file; then the claim stays where it is, never deleted.
        A crash between the claim and the unlink leaves the claim beside its stem's lock, where the next sweep
        removes it under the same proof. `expected` is the scan's identity, None for an entry the scan never saw.
        """
        if self.fd is None:  # no descriptor-relative calls: the path route checks the name, then unlinks it
            claim = leaf
        else:
            claim = f"{leaf.partition('.')[0]}{_CLAIM_SUFFIX}"
            if leaf != claim:
                try:
                    os.rename(leaf, claim, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                except FileNotFoundError:
                    return True  # not there: nothing to delete
                except OSError:
                    return False
        try:
            info = os.stat(self.name(claim), dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if (
            stat.S_ISREG(info.st_mode)
            and (expected is None or (info.st_dev, info.st_ino) == expected)
            and not (empty_only and info.st_size)
        ):
            try:
                os.unlink(self.name(claim), dir_fd=self.fd)
            except FileNotFoundError:
                pass
            except OSError:
                return False
            return True
        if claim != leaf:
            with contextlib.suppress(OSError):  # a newer file at `leaf` makes the link fail and the claim stay
                os.link(claim, leaf, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
                os.unlink(claim, dir_fd=self.fd)
        return False


@contextlib.contextmanager
def _probe_lock(directory: _StageDirectory, leaf: str, expected: _FileId) -> Iterator[bool | None]:
    """Yield True while this process holds an existing lock nobody else holds, False when another process
    does, and None when this host cannot say or the file is no longer the one the scan saw.

    The probe never creates, truncates or follows a link: it opens the existing file as it is, so a planted
    link cannot make the sweep write outside the stage root. A host whose `flock` is unsupported (or a
    platform without `fcntl`) yields None, and the stage stays where it is.
    """
    try:
        import fcntl
    except ImportError:  # no fcntl on this platform: only the library's own lock can be probed
        probe = FileLock(directory.path / leaf, timeout=0, thread_local=False)
        try:
            probe.acquire()
        except Timeout:
            yield False
            return
        except OSError:
            yield None
            return
        try:
            yield True
        finally:
            probe.release()
        return
    try:
        descriptor = os.open(
            directory.name(leaf), os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory.fd
        )
    except OSError:
        yield None
        return
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != expected:
            yield None
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        except OSError:
            yield None
            return
        yield True
    finally:
        os.close(descriptor)


def _open_directory(root: Path) -> _StageDirectory | SweepReport:
    """Pin the stage root, or say why the sweep ends: no root is an empty sweep, a link or file is not ours."""
    if _PINNABLE:
        try:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
            return _StageDirectory(root, descriptor)
        except FileNotFoundError:
            return SweepReport()
        except OSError as error:
            if error.errno not in {errno.ELOOP, errno.ENOTDIR}:  # a link refused by O_NOFOLLOW, or a file
                raise
    else:
        try:
            if stat.S_ISDIR(os.lstat(root).st_mode):
                return _StageDirectory(root, None)
        except FileNotFoundError:
            return SweepReport()
    # A symlinked or replaced stage root is not ours to walk: nothing under it can be proven an orphan.
    log.warning("upload stage root is not a plain directory; the sweep left it alone")
    return SweepReport(unproven=1)


def sweep(vault_root: Path) -> SweepReport:
    """Remove each stage whose owner is proven gone; leave every other entry where it is.

    Run once at serving activation. A stage is removed only when its lock can be taken, which means the
    process that held it is dead. A held lock is a live stage and stays; a `.part` whose lock file is missing
    cannot be proven either way and stays too, reported as `unproven`. The directory is held open for the
    whole sweep, and a file is set aside and judged there before it is deleted (`_StageDirectory.discard`).
    """
    opened = _open_directory(stage_root(vault_root))
    if isinstance(opened, SweepReport):
        return opened
    try:
        return _sweep(opened)
    finally:
        if opened.fd is not None:
            os.close(opened.fd)


def _sweep(directory: _StageDirectory) -> SweepReport:
    found: dict[str, dict[str, _FileId]] = {_PART_SUFFIX: {}, _LOCK_SUFFIX: {}, _CLAIM_SUFFIX: {}}
    truncated = False
    with directory.scan() as scanner:
        for index, entry in enumerate(scanner):
            if index >= SWEEP_ENTRY_LIMIT:
                truncated = True
                break
            named = _STAGE_NAME.fullmatch(entry.name)
            if named is None:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue  # removed since it was listed
            if stat.S_ISREG(info.st_mode):  # a link or a directory under a stage name is not a stage
                found[named["kind"]][named["stem"]] = (info.st_dev, info.st_ino)
    parts, locks, claims = found[_PART_SUFFIX], found[_LOCK_SUFFIX], found[_CLAIM_SUFFIX]
    removed = live = unproven = 0
    for stem in sorted(parts.keys() | locks.keys() | claims.keys()):
        lock_leaf, part_leaf, claim_leaf = (f"{stem}{suffix}" for suffix in (_LOCK_SUFFIX, _PART_SUFFIX, _CLAIM_SUFFIX))
        if stem not in locks:
            # Nothing proves the owner gone. A claim alone is a lock that an earlier sweep set aside and never
            # deleted: it holds no bytes, so it goes. A part, with or without a claim, stays.
            if stem not in parts and directory.discard(claim_leaf, claims[stem], empty_only=True):
                removed += 1
            else:
                unproven += 1
            continue
        with _probe_lock(directory, lock_leaf, locks[stem]) as free:
            if free is None:
                unproven += 1
                continue
            if not free:
                live += 1
                continue
            # A free lock proves its owner gone, so the part and any claim left by a crashed sweep are orphans.
            # They are found by name, not by the scan: it may have stopped before them.
            if not (directory.discard(claim_leaf, claims.get(stem)) and directory.discard(part_leaf, parts.get(stem))):
                unproven += 1
                continue
        if directory.discard(lock_leaf, locks[stem]):
            removed += 1
        else:
            unproven += 1
    report = SweepReport(removed=removed, live=live, unproven=unproven, truncated=truncated)
    if removed or unproven or truncated:
        log.info(
            "upload stage sweep removed=%d live=%d unproven=%d truncated=%s",
            removed, live, unproven, truncated,
        )
    return report
